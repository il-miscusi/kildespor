"""Fit the news gate (kildespor/newsgate.py) from labelled news candidates.

    python3 tools/news_train.py --items teacher_items.jsonl.gz --labels teacher_labels.jsonl.gz \
        [--audit audit_labels.tsv] [--holdout-orgs a.jsonl,b.jsonl] [--bits 16] [--target 0.96] \
        [--out kildespor/models/newsgate.json]

Labels: the teacher's letter per item (A company_event, B product_or_service, C evergreen guide or
marketing, D commentary or case, E recruitment, F other entity, G listing or navigation, H
uncertain), overridden by a hand audit where one exists. The target is A; H is left out.

Split: every host of a held-out organisation (``--holdout-orgs``) is test-only, and the rest is
cross-validated by host (GroupKFold), so no site is on both sides. The threshold is the lowest at
which the out-of-fold precision, on items the rules let through, reaches ``--target``. Weights
are quantised to int8 before the threshold is chosen, exactly as they ship.

Needs numpy, scipy and scikit-learn (development only; the agent itself reads the JSON in pure
Python).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import sparse
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kildespor import newsgate  # noqa: E402


def read_jsonl(path: str) -> list[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_orgs(paths: str) -> set[str]:
    out: set[str] = set()
    for p in [x for x in (paths or "").split(",") if x]:
        for line in Path(p).read_text().splitlines():
            if line.strip():
                out.add(str(json.loads(line)["organisation_number"]) if line.startswith("{") else line.strip())
    return out


def matrix(rows: list[dict], bits: int) -> sparse.csr_matrix:
    indptr, indices, data = [0], [], []
    for r in rows:
        counts = Counter(newsgate.hashed(newsgate.features(r), bits))
        for k in sorted(counts):
            indices.append(k)
            data.append(float(counts[k]))
        indptr.append(len(indices))
    return sparse.csr_matrix((np.array(data), np.array(indices), np.array(indptr)), shape=(len(rows), 1 << bits))


def quantise(coef: np.ndarray) -> tuple[list[int], float]:
    scale = float(np.max(np.abs(coef))) / 127.0 or 1.0
    q = np.clip(np.round(coef / scale), -127, 127).astype(int)
    return q.tolist(), scale


def qscore(X: sparse.csr_matrix, q: list[int], scale: float, bias: float) -> np.ndarray:
    z = bias + scale * (X @ np.array(q, dtype=float))
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit(X, y, C: float) -> LogisticRegression:
    model = LogisticRegression(C=C, solver="liblinear", max_iter=2000, random_state=0)
    model.fit(X, y)
    return model


def pr_at(p: np.ndarray, y: np.ndarray, thr: float) -> tuple[float, float, int]:
    sel = p >= thr
    tp = int((sel & (y == 1)).sum())
    n = int(sel.sum())
    return (tp / n if n else 1.0), (tp / int((y == 1).sum()) if (y == 1).any() else 0.0), n


def choose_threshold(p: np.ndarray, y: np.ndarray, target: float) -> float:
    for thr in np.round(np.arange(0.05, 0.96, 0.01), 2):
        prec, _rec, n = pr_at(p, y, thr)
        if n >= 20 and prec >= target:
            return float(thr)
    return 0.95


def company_cover(p: np.ndarray, y: np.ndarray, orgs: list[str], thr: float) -> tuple[int, int, int]:
    """(companies with >= 1 correct item kept, companies with any wrong item kept, companies with >= 1 correct item at all)."""
    good, bad, any_pos = set(), set(), set()
    for pi, yi, o in zip(p, y, orgs):
        if yi == 1:
            any_pos.add(o)
        if pi >= thr:
            (good if yi == 1 else bad).add(o)
    return len(good), len(bad), len(any_pos)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--audit", default="")
    ap.add_argument("--holdout-orgs", default="")
    ap.add_argument("--bits", type=int, default=16)
    ap.add_argument("--target", type=float, default=0.96)
    ap.add_argument("--C", default="0.1,0.3,1,3")
    ap.add_argument("--out", default=str(ROOT / "kildespor" / "models" / "newsgate.json"))
    ap.add_argument("--oof-out", default="")
    args = ap.parse_args()

    items = {r["id"]: r for r in read_jsonl(args.items)}
    labels = {r["id"]: r["label"] for r in read_jsonl(args.labels)}
    source = {k: "teacher" for k in labels}
    if args.audit:
        with open(args.audit, encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("id") and row.get("label"):
                    labels[row["id"]] = row["label"].strip().upper()[:1]
                    source[row["id"]] = "audit"
    rows = [dict(items[k], y_label=labels[k], y_source=source[k]) for k in labels if k in items and labels[k] in "ABCDEFG"]
    held = read_orgs(args.holdout_orgs)
    held_hosts = {r.get("host") for r in rows if r.get("org") in held}
    train = [r for r in rows if r.get("host") not in held_hosts]
    test = [r for r in rows if r.get("host") in held_hosts]
    print(f"labelled {len(rows)} (audit {sum(1 for r in rows if r['y_source'] == 'audit')}), train {len(train)} items / {len({r.get('host') for r in train})} hosts, "
          f"test {len(test)} / {len(held_hosts)} hosts; labels {dict(Counter(r['y_label'] for r in rows))}")

    Xtr = matrix(train, args.bits)
    ytr = np.array([1 if r["y_label"] == "A" else 0 for r in train])
    groups = [r.get("host") or "" for r in train]
    # Items the gate actually judges in production: rule-passed and inside the 24-month window.
    judged = np.array([not r.get("rule") for r in train])
    folds = list(GroupKFold(n_splits=5).split(Xtr, ytr, groups))

    best = None
    for C in [float(c) for c in args.C.split(",")]:
        oof = np.zeros(len(train))
        for tr, va in folds:
            m = fit(Xtr[tr], ytr[tr], C)
            q, scale = quantise(m.coef_[0])
            oof[va] = qscore(Xtr[va], q, scale, float(m.intercept_[0]))
        thr = choose_threshold(oof[judged], ytr[judged], args.target)
        prec, rec, n = pr_at(oof[judged], ytr[judged], thr)
        ll = float(-np.mean(ytr * np.log(np.clip(oof, 1e-6, 1)) + (1 - ytr) * np.log(np.clip(1 - oof, 1e-6, 1))))
        print(f"C={C}: OOF logloss {ll:.4f}; judged items {int(judged.sum())}: threshold {thr:.2f} precision {prec:.3f} recall {rec:.3f} kept {n}")
        key = (rec if prec >= args.target else -1, -ll)
        if best is None or key > best[0]:
            best = (key, C, thr, oof)
    _key, C, thr, oof = best
    orgs_tr = [r.get("org") or "" for r in train]
    j = judged
    good, bad, pos = company_cover(oof[j], ytr[j], [o for o, k in zip(orgs_tr, j) if k], thr)
    print(f"chosen C={C} threshold {thr:.2f}; OOF companies with a correct item kept {good}/{pos}, companies with a wrong item kept {bad}")
    rules_only = pr_at(np.ones(int(j.sum())), ytr[j], 0.5)
    print(f"rules alone on the same items: precision {rules_only[0]:.3f} ({rules_only[2]} items)")
    by_label = defaultdict(lambda: [0, 0])
    for r, pi, k in zip(train, oof, j):
        if k:
            by_label[r["y_label"]][0] += 1
            by_label[r["y_label"]][1] += int(pi >= thr)
    print("OOF kept by label: " + ", ".join(f"{k} {v[1]}/{v[0]}" for k, v in sorted(by_label.items())))

    model = fit(Xtr, ytr, C)
    q, scale = quantise(model.coef_[0])
    bias = float(model.intercept_[0])
    if test:
        Xte = matrix(test, args.bits)
        yte = np.array([1 if r["y_label"] == "A" else 0 for r in test])
        jt = np.array([not r.get("rule") for r in test])
        pte = qscore(Xte, q, scale, bias)
        prec, rec, n = pr_at(pte[jt], yte[jt], thr)
        g, b, ps = company_cover(pte[jt], yte[jt], [r.get("org") or "" for r, k in zip(test, jt) if k], thr)
        base = pr_at(np.ones(int(jt.sum())), yte[jt], 0.5)
        print(f"held-out hosts: precision {prec:.3f} recall {rec:.3f} kept {n} of {int(jt.sum())} rule-passed (rules alone {base[0]:.3f}); companies {g}/{ps} kept a correct item, {b} kept a wrong one")
    spec = {
        "feature_version": newsgate.FEATURE_VERSION, "bits": args.bits, "bias": round(bias, 6), "scale": scale, "threshold": thr,
        "weights": newsgate.encode_weights(q),
        "trained": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "C": C, "items": len(train), "hosts": len(set(groups)),
        "positives": int(ytr.sum()), "target_precision": args.target,
        "labels": "teacher LLM (campaign/news) with hand-audit overrides; target = company_event",
    }
    spec["sha256"] = newsgate.spec_sha256(spec)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({Path(args.out).stat().st_size} bytes), sha256 {spec['sha256'][:16]}")
    if args.oof_out:
        with open(args.oof_out, "w", encoding="utf-8") as fh:
            for r, pi in zip(train, oof):
                fh.write(json.dumps({"id": r["id"], "host": r.get("host"), "y": r["y_label"], "p": round(float(pi), 4), "rule": r.get("rule") or ""}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
