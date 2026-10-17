# Kildespor static deployment

All company data is read at runtime from `/data/profiles.json`. No API, database, credentials or authentication is used.

- Run `pnpm export` to generate the static site in `out/`.
- Deploy the contents of `out/` to a static host that serves directory `index.html` files and `404.html`.
- Replace `out/data/profiles.json` with your Python agent's JSON to refresh data without rebuilding. Use an atomic file replacement and appropriate cache headers.
- Company links use `/company/?org=...`; comparisons use `/compare/?orgs=...` and accept up to four identifiers. These routes do not require per-company HTML generation.
- `pnpm build` produces the standalone version required by the JBOX managed preview. The same pages and same client-side data loader are used in both modes. No server data logic is required.
- The included eight companies and all their evidence are fictional. A `runId` starting with `demo` displays the demo banner. Set a real run ID when replacing the file. The banner is not a validation of data truth.
- Keep organisation numbers unique; optional arrays can be empty or omitted. Use explicit numeric zero only for a genuine reported zero, and null for missing values. Supply coverage state and reason for all five areas. Missing evidence never receives invented attribution.
- Financial metric matching supports revenue, operating_result, annual_result, equity_ratio and common aliases documented in `lib/profiles.ts`. Unmapped metrics remain visible in Accounts. General manager and chair match role names or matching generic facts. Company summary values only inherit matching provenance.
- Evidence URLs must be absolute HTTP(S). Links navigate to the original source; the app never fetches external evidence. Hashes are displayed as provided, not cryptographically verified against external content.
- The symbol is a registered cloud SVG used as static presentation media; company data remains solely in the JSON. System fonts require no font requests. There are no external API calls.
- This implementation is hosted at a domain root. If deploying under a subpath, adjust root-relative routes and data loading accordingly.
