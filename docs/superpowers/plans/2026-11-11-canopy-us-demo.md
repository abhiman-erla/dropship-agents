# Canopy US Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the pipeline at demo scale on the US market for $0 data cost using Canopy API's best-sellers list (100 free requests/month), and fix the two gaps the sample trial found.

**Architecture:** Add `CanopyClient` with the same two-method interface as `KeepaClient` (`bestsellers`, `products`). One best-sellers request returns everything, so `products` serves from a per-run cache. A shared `DataSourceError` base lets the monitor treat both sources alike. Config gains `data_source`. The defaults switch to US demo values. Proposals get a cooldown, and every run records why products were dropped.

**Tech Stack:** unchanged (Python ≥ 3.12, httpx, pydantic v2, anthropic, sqlite3, pytest).

**Spec:** `docs/superpowers/specs/2026-11-10-dropship-agents-design.md` (update its defaults block and Agents section in Task 1/2 as noted)

## Global Constraints

- All the constraints of the original plan still hold: no scraping, nothing bought without `approve`, model IDs/effort/fallbacks unchanged, secrets only from env and never logged, no real network in tests.
- Canopy REST facts (from canopyapi.co docs, 2026-11-11):
  - `GET https://rest.canopyapi.co/api/amazon/bestsellers` with header `API-KEY: <key>`
  - query: `categoryId` (slug, e.g. `bestsellers_electronics`), `domain` (`US` default), `page` (1)
  - response: `data.amazonBestSellers.productResults.results[]`, each with `asin`, `title`, `bestSellersRank` (list position, int), `rating` (float), `ratingsTotal` (int), `price` object `{value: float, currency, symbol, display}` (it may be missing or null)
  - Error codes are undocumented: treat 429 and ≥500 as transient, and anything else non-2xx as a non-transient error.
- Env var for the key: `CANOPY_API_KEY`.
- Demo scale: exactly ONE Canopy request per category per run (`page=1` only).

## Review Focus

1. **A best-sellers row with no `price` or a null price** → `Product.price=None`, so the filter drops it with "missing price" and nothing crashes. *(Task 1: `test_canopy_missing_price`)*
2. **A product approved or rejected yesterday reappears in today's list** → no new proposal within `proposal_cooldown_days`. *(Task 2: `test_cooldown_blocks_reproposal`)*
3. **The Canopy key in errors** → the key never appears in exception text. *(Task 1: `test_canopy_key_not_in_errors`)*
4. **The old Keepa path still works** → `data_source="keepa"` builds a KeepaClient, and every existing Keepa test passes. *(Task 1: `test_cli_builds_keepa_client`)*
5. **A run where every product is filtered** → the report says why for each, instead of an empty table with no explanation. *(Task 2: `test_report_lists_dropped_products`)*

---

### Task 1: Canopy client, data-source switch, US demo defaults

**Files:**
- Create: `src/dropship/errors.py`, `src/dropship/canopy_client.py`, `tests/test_canopy_client.py`, `tests/fixtures/canopy_bestsellers.json`
- Modify: `src/dropship/keepa_client.py` (KeepaError subclasses DataSourceError), `src/dropship/config.py`, `src/dropship/monitor.py` (transient check), `src/dropship/cli.py` (build the client by source), `src/dropship/buyer.py` + `src/dropship/llm.py`/`research.py` wherever the em dash appears in user-facing strings, `config.example.toml`, `README.md`, spec defaults block
- Test: `tests/test_canopy_client.py`, plus updates to `tests/test_config.py`, `tests/test_monitor.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `errors.DataSourceError(Exception)` with `status_code: int | None`. `KeepaError` now subclasses it (keep the `KeepaError` name and behaviour).
  - `canopy_client.CanopyError(DataSourceError)`
  - `canopy_client.CanopyClient(api_key: str, domain: str = "US", http: httpx.Client | None = None)`:
    - `bestsellers(category: str) -> list[str]`: one GET (`categoryId=<category>`, `domain`, `page=1`). It maps each row to `Product(asin, title or "", price=price.value or None, price_avg90=None, sales_rank=bestSellersRank, sales_rank_avg90=None, rating, review_count=ratingsTotal, offer_count=None)`, caches it by ASIN, and returns the ASINs in list order.
    - `products(asins: list[str]) -> list[Product]`: returns the cached Products for those ASINs (in the given order, skipping unknown ones) with NO network call.
    - Non-2xx → `CanopyError(status_code=..., message "GET /api/amazon/bestsellers -> HTTP <code>")`, with no key and no chained URL. A non-dict body or missing `data.amazonBestSellers` → `CanopyError(status_code=None, "unexpected response shape")`.
  - `config.Settings` changes:
    - `data_source: Literal["keepa", "canopy"] = "canopy"`
    - `canopy_domain: str = "US"`
    - `categories: list[int | str]`
    - US demo defaults: `keepa_domain=1`, `price_min=15.0`, `price_max=60.0`, `max_sales_rank=50` (the list position is the rank for Canopy), `shipping_cost=5.0`, `max_order_value=300.0`, `daily_spend_cap=1000.0`, `max_asins_per_category=50`
    - All other defaults unchanged
  - `monitor._is_transient`: a `DataSourceError` with `status_code == 429 or >= 500` is transient (KeepaOutOfTokens keeps its special handling first).
  - `cli`: `run` picks the source from `s.data_source`. canopy → needs `CANOPY_API_KEY` (missing → exit 2 with a clear message) and builds `CanopyClient(key, s.canopy_domain)`. keepa → the existing `KEEPA_API_KEY` path. Plain-ASCII hyphens replace every em dash in CLI/report/narrative output.

- [ ] **Step 1: Write failing tests**
  - `test_canopy_bestsellers_maps_rows`: a fixture with 3 rows → ASINs in order. Row 1 maps `price 29.99, rating 4.6, review_count 1000, sales_rank 1, offer_count None`.
  - `test_canopy_products_from_cache_no_request`: after `bestsellers`, `products([...])` makes 0 further transport calls.
  - `test_canopy_missing_price` (RF1): a row without `price` and a row with `"price": null` → `price is None`.
  - `test_canopy_sends_key_header_and_params`: the transport sees header `API-KEY`, `categoryId`, `domain=US`, `page=1`.
  - `test_canopy_http_500_raises` → `CanopyError.status_code == 500`. `test_canopy_bad_shape_raises`.
  - `test_canopy_key_not_in_errors` (RF3).
  - `test_us_demo_defaults`: `data_source == "canopy"`, `price_max == 60.0`, `max_order_value == 300.0`, `daily_spend_cap == 1000.0`, `keepa_domain == 1`. String categories are accepted.
  - `test_canopy_429_is_transient` (monitor). `test_cli_missing_canopy_key_exits_2`. `test_cli_builds_keepa_client` (RF4, data_source keepa + KEEPA_API_KEY → KeepaClient passed to run_pipeline).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full suite → PASS (update existing tests that relied on the old India defaults by setting those values explicitly, never by weakening assertions).
- [ ] **Step 5:** Update `config.example.toml` (`data_source = "canopy"`, `categories = ["bestsellers_kitchen"]`, the US values, and a commented Keepa alternative), the README (Canopy setup, the free 100/month, the demo limits: no history and no seller count), and the spec defaults block plus a one-line note on the data sources.
- [ ] **Step 6: Commit** `feat: Canopy best-sellers data source and US demo defaults`

---

### Task 2: Proposal cooldown and drop reasons

**Files:**
- Modify: `src/dropship/db.py`, `src/dropship/margins.py`, `src/dropship/research.py`, `src/dropship/buyer.py`, `src/dropship/monitor.py`, `src/dropship/config.py`
- Test: `tests/test_db.py`, `tests/test_margins.py`, `tests/test_research.py`, `tests/test_buyer.py`, `tests/test_monitor.py`

**Interfaces:**
- Consumes: Task 1's Settings.
- Produces:
  - `Settings.proposal_cooldown_days: int = Field(30, ge=0)`
  - `db`: new table `dropped(id, run_id, asin, title, stage, reason)`. `record_dropped(conn, run_id, rows: list[tuple[str, str, str, str]]) -> None` (asin, title, stage, reason). `get_dropped(conn, run_id) -> list[dict]`. `recently_decided(conn, asin: str, days: int) -> bool` (any proposal for the ASIN with status approved_manual/rejected and `decided_at` within the last `days` days, using local time).
  - `margins.explain_drops(products, quotes, s) -> list[tuple[str, str, str]]` → (asin, title, reason) for every product NOT in `build_candidates`: the filter reason, or `f"margin {pct:.0%} below min {min:.0%}"`.
  - `research.run_research` records `explain_drops` rows with stage "research".
  - `buyer.run_risk` skips an ASIN when `has_pending_proposal(...)` or `recently_decided(conn, asin, s.proposal_cooldown_days)`, recording the reason `"cooldown: decided within N days"` (stage "risk"). It also records `f"risk {score} > max {max}"` for over-risk candidates and `f"verdict {v}"` for candidates excluded by the best-candidates rule (stage "risk"). Pending duplicates are skipped silently, as today.
  - `monitor.write_report` adds a `## Dropped products` section after `## Proposals awaiting approval`: a table `| ASIN | Title | Stage | Reason |`, or "None.".

- [ ] **Step 1: Write failing tests**
  - `test_recently_decided`: approved today → True for days=30. Decided 31 days ago (backdate `decided_at`) → False. A pending one → False.
  - `test_cooldown_blocks_reproposal` (RF2): approve a proposal, run `run_risk` again for a new run with the same ASIN as a promising candidate → no new proposal and no LLM call. The dropped row has the reason starting with "cooldown".
  - `test_explain_drops_reasons`: a missing-price product → "missing price". A thin-margin product → its reason starts with "margin".
  - `test_research_records_drops`. `test_risk_records_over_risk_and_verdict_drops`.
  - `test_report_lists_dropped_products` (RF5): all products filtered → the report has a `## Dropped products` table with a reason for each.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full suite → PASS.
- [ ] **Step 5: Commit** `feat: proposal cooldown and dropped-product reasons in reports`
