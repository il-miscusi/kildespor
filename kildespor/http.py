"""Outbound HTTP with a safe URL policy, robots.txt, per-host limits and snapshots.

Every network read in the agent goes through :class:`Fetcher`. It refuses
non-HTTP(S) schemes, hosts that resolve to private, loopback, link-local or
reserved addresses (checked again on every redirect hop), oversized bodies,
and paths a site's robots.txt disallows. Each successful read becomes a
:class:`Snapshot` carrying the requested and final URL, the redirect chain,
the status, the retrieval time and a SHA-256 of the body, which is what the
evidence records point at.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import urllib.request

import requests
from urllib3.util import connection as urllib3_connection

from . import __version__

USER_AGENT = f"KildesporBot/{__version__} (+https://github.com/il-miscusi/kildespor; company-research agent)"
ROBOTS_AGENT = "KildesporBot"
MAX_BODY_BYTES = 3_000_000
MAX_REDIRECTS = 5
ALLOWED_PORTS = {None, 80, 443}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UnsafeURL(ValueError):
    pass


class RobotsDisallowed(RuntimeError):
    pass


class RequestBudgetExhausted(RuntimeError):
    pass


@dataclass
class Snapshot:
    requested_url: str
    final_url: str
    status: int
    retrieved_at: str
    sha256: str
    content_type: str
    body: bytes = field(repr=False)
    redirect_chain: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        if not self.body:
            return ""
        charset = None
        ctype = self.content_type.lower()
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"')
        for encoding in (charset, "utf-8", "cp1252"):
            if not encoding:
                continue
            try:
                return self.body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.body.decode("utf-8"))

    def meta(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status": self.status,
            "retrieved_at": self.retrieved_at,
            "content_sha256": self.sha256,
            "content_type": self.content_type,
            "redirect_chain": self.redirect_chain,
        }


def _ip_is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 6 and ip.ipv4_mapped and not _ip_is_public(str(ip.ipv4_mapped)))
    )


_dns_cache: dict[str, tuple[bool, str]] = {}
DNS_STATS = {"timeouts": 0}
_dns_lock = threading.Lock()


def resolve_public(host: str, timeout: float = 6.0) -> bool:
    """True when every address the host resolves to is a public address.

    Definitive answers (addresses, or a name-resolution error) are cached; a
    lookup that merely timed out is retried once and never cached.
    """
    host = host.lower().rstrip(".")
    with _dns_lock:
        if host in _dns_cache:
            return _dns_cache[host][0]
    for _attempt in range(2):
        result: list[Any] = []
        failed: list[BaseException] = []

        def lookup() -> None:
            try:
                result.extend(socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP))
            except OSError as exc:
                failed.append(exc)

        worker = threading.Thread(target=lookup, daemon=True)
        worker.start()
        worker.join(timeout)
        if result or failed:
            addresses = {item[4][0] for item in result}
            ok = bool(addresses) and all(_ip_is_public(address) for address in addresses)
            with _dns_lock:
                _dns_cache[host] = (ok, ",".join(sorted(addresses)) if addresses else "nxdomain")
            return ok
    with _dns_lock:
        DNS_STATS["timeouts"] += 1
    return False


_PROXIES = {k.lower(): v for k, v in urllib.request.getproxies().items()}
_PROXY_HOSTS = {(urlsplit(v if "://" in v else "http://" + v).hostname or "").lower() for k, v in _PROXIES.items() if k != "no" and v}


def _proxied(scheme: str, host: str) -> bool:
    if not (_PROXIES.get(scheme) or _PROXIES.get("all")):
        return False
    try:
        return not urllib.request.proxy_bypass(host)
    except Exception:
        return True


def _public_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):  # type: ignore[no-untyped-def]
    """urllib3's create_connection, but it only ever connects to a public address.

    The address is resolved once and the socket connects to that exact address, so a
    name that resolves publicly at check time and privately at connect time (DNS
    rebinding) cannot reach an internal service. Configured proxy hosts are exempt.
    """
    host, port = address
    host = host.strip("[]") if isinstance(host, str) else host
    exempt = isinstance(host, str) and host.lower() in _PROXY_HOSTS
    err: BaseException | None = None
    for family, socktype, proto, _canon, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        if not exempt and not _ip_is_public(sockaddr[0]):
            err = UnsafeURL(f"refusing to connect to non-public address {sockaddr[0]} for {host}")
            continue
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            for option in socket_options or []:
                sock.setsockopt(*option)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError(f"getaddrinfo returned no addresses for {host}")


urllib3_connection.create_connection = _public_create_connection


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme not allowed: {parts.scheme!r}")
    if not parts.hostname:
        raise UnsafeURL("missing host")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in URL")
    if parts.port not in ALLOWED_PORTS:
        raise UnsafeURL(f"port not allowed: {parts.port}")
    host = parts.hostname.lower()
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        raise UnsafeURL("literal IP hosts are not fetched")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise UnsafeURL(f"internal host name: {host}")
    if _proxied(parts.scheme, host):
        # Egress goes through a configured proxy, which resolves the name; the connection-level
        # guard below still refuses any direct connection to a non-public address.
        return host
    if not resolve_public(host):
        cached = _dns_cache.get(host)
        why = "DNS lookup timed out" if cached is None else ("no such host" if cached[1] == "nxdomain" else f"non-public address {cached[1]}")
        raise UnsafeURL(f"{why}: {host}")
    return host.lower()


class Fetcher:
    """Thread-safe fetcher shared by every worker in a run."""

    def __init__(
        self,
        *,
        per_host_limit: int = 2,
        host_limits: dict[str, int] | None = None,
        timeout: tuple[float, float] = (5.0, 12.0),
        respect_robots: bool = True,
    ) -> None:
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.per_host_limit = per_host_limit
        self.host_limits = host_limits or {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self.requests = 0
        self.bytes = 0
        self.latencies: list[int] = []
        self.max_requests: int | None = None
        self.budget_refusals = 0
        self._cooldown: dict[str, float] = {}
        self.rate_limited = 0

    # -- plumbing -----------------------------------------------------------------
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6",
                }
            )
            session.max_redirects = MAX_REDIRECTS
            self._local.session = session
        return session

    def _semaphore(self, host: str) -> threading.BoundedSemaphore:
        key = host
        for suffix, limit in self.host_limits.items():
            if host == suffix or host.endswith("." + suffix):
                key = suffix
                with self._lock:
                    if key not in self._semaphores:
                        self._semaphores[key] = threading.BoundedSemaphore(limit)
                    return self._semaphores[key]
        with self._lock:
            if key not in self._semaphores:
                self._semaphores[key] = threading.BoundedSemaphore(self.per_host_limit)
            return self._semaphores[key]

    def robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            known = origin in self._robots
            parser = self._robots.get(origin)
        if not known:
            parser = None
            try:
                snap = self._raw_get(origin + "/robots.txt", check_robots=False, max_bytes=500_000)
                if snap.status in (401, 403):
                    parser = RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])
                elif snap.ok and "html" not in snap.content_type.lower():
                    parser = RobotFileParser()
                    parser.parse(snap.text().splitlines())
            except Exception:
                parser = None
            with self._lock:
                self._robots[origin] = parser
        if parser is None:
            return True
        return parser.can_fetch(ROBOTS_AGENT, url)

    def _raw_get(
        self,
        url: str,
        *,
        check_robots: bool = True,
        max_bytes: int = MAX_BODY_BYTES,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> Snapshot:
        session = self._session()
        chain: list[str] = []
        current = url
        started = time.monotonic()
        for _hop in range(MAX_REDIRECTS + 1):
            self._charge()
            host = check_url(current)
            if check_robots and not self.robots_allows(current):
                raise RobotsDisallowed(current)
            pause = self._cooldown.get(host, 0.0) - time.monotonic()
            if pause > 0:
                time.sleep(min(pause, 20.0))
            with self._semaphore(host):
                response = session.get(
                    current,
                    timeout=timeout or self.timeout,
                    allow_redirects=False,
                    stream=True,
                    headers=headers,
                )
                try:
                    if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            break
                        chain.append(current)
                        current = urljoin(current, location)
                        continue
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            break
                    status = response.status_code
                    ctype = response.headers.get("Content-Type", "")
                    retry_after = None
                    if status in (429, 503):
                        try:
                            retry_after = float(response.headers.get("Retry-After") or "")
                        except ValueError:
                            retry_after = None
                        # Every worker backs off this host, not just the one that was refused.
                        with self._lock:
                            self.rate_limited += 1
                            self._cooldown[host] = max(self._cooldown.get(host, 0.0), time.monotonic() + min(retry_after or 5.0, 20.0))
                finally:
                    response.close()
            elapsed = int((time.monotonic() - started) * 1000)
            data = bytes(body[:max_bytes])
            self._local.count = getattr(self._local, "count", 0) + 1 + len(chain)
            with self._lock:
                self.requests += 1 + len(chain)
                self.bytes += len(data)
                self.latencies.append(elapsed)
            return Snapshot(
                requested_url=url,
                final_url=current,
                status=status,
                retrieved_at=utc_now(),
                sha256=hashlib.sha256(data).hexdigest(),
                content_type=ctype,
                body=data,
                redirect_chain=chain,
                elapsed_ms=elapsed,
                retry_after=retry_after,
            )
        raise requests.TooManyRedirects(url)

    # -- public API ---------------------------------------------------------------
    def get(self, url: str, *, retries: int = 1, **kwargs: Any) -> Snapshot:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                snap = self._raw_get(url, **kwargs)
                if snap.status in (429, 502, 503, 504) and attempt < retries:
                    wait = snap.retry_after if snap.retry_after is not None else 2.0 * (2 ** attempt)
                    time.sleep(min(max(wait, 1.0), 20.0))
                    continue
                return snap
            except (UnsafeURL, RobotsDisallowed, RequestBudgetExhausted):
                raise
            except requests.RequestException as exc:
                last = exc
                if attempt < retries:
                    time.sleep(0.8 * (attempt + 1))
        assert last is not None
        raise last

    def _charge(self) -> None:
        """Refuse a request that would exceed the run's or the company's request budget."""
        count = getattr(self._local, "count", 0)
        cap = getattr(self._local, "cap", None)
        if cap is not None and count - getattr(self._local, "cap_start", 0) >= cap:
            with self._lock:
                self.budget_refusals += 1
            raise RequestBudgetExhausted("per-company request cap reached")
        if self.max_requests is not None and self.requests >= self.max_requests:
            with self._lock:
                self.budget_refusals += 1
            raise RequestBudgetExhausted("run request budget reached")

    def begin_company(self, cap: int | None) -> None:
        self._local.cap = cap
        self._local.cap_start = getattr(self._local, "count", 0)

    def thread_requests(self) -> int:
        return getattr(self._local, "count", 0)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            latencies = sorted(self.latencies)
        def pct(p: float) -> int | None:
            if not latencies:
                return None
            return latencies[min(len(latencies) - 1, int(len(latencies) * p))]
        return {"requests": self.requests, "bytes": self.bytes, "p50_ms": pct(0.5), "p95_ms": pct(0.95), "max_requests": self.max_requests, "budget_refusals": self.budget_refusals, "rate_limited_responses": self.rate_limited, "dns_timeouts": DNS_STATS["timeouts"]}
