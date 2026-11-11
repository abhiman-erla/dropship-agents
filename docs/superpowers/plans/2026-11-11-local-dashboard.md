# Local Dashboard (Option A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A view-only website (`dropship web` → `http://localhost:8000`) that shows runs, draft orders, products, drop reasons and usage from the existing SQLite DB.

**Architecture:** `src/dropship/web/queries.py` owns every read (view models in, SQL out) over a read-only SQLite connection. `src/dropship/web/app.py` is a FastAPI app with GET routes only, rendering Jinja2 templates. The CLI gains a `web` subcommand that serves it on `127.0.0.1` with uvicorn. One small pipeline change records data-source request counts per run.

**Tech Stack:** Python ≥ 3.12, FastAPI, uvicorn, Jinja2, sqlite3, pytest (FastAPI `TestClient`)

**Spec:** `docs/superpowers/specs/2026-11-11-local-dashboard-design.md`

## Global Constraints

- **Display only:** GET routes only, no `<form>` in any template. The DB is opened with `sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)`, so it can't be written.
- **Local only:** serve on host `127.0.0.1` only. The CLI has no host option. The default port is `8000`.
- **Auto-refresh:** every page's `<head>` has `<meta http-equiv="refresh" content="30">`. The busy page uses `content="3"`.
- **Amazon link:** `https://www.amazon.com/dp/<ASIN>`.
- **Copy commands shown on pages, exactly:** `.venv\Scripts\python -m dropship approve <id>` and `.venv\Scripts\python -m dropship reject <id>`.
- **Empty-state text, exactly:** `No runs yet. Run .venv\Scripts\python -m dropship run in PowerShell.`
- **Secrets:** the site never reads environment variables and never renders API keys.
- **Jinja2:** autoescape is on for every template, via `Jinja2Templates`' default `.html` autoescape.
- **Free-tier tile:** `N of 100 free` when `data_source == "canopy"`. Otherwise just `N`.
- Existing behaviour and all 213 existing tests stay green. Tests never touch the real network.
- Commit trailer: `Co-Authored-By:` naming the model that wrote the commit. Repo identity is already set (abhiman-erla no-reply). Don't change it.

## Review Focus

1. **A run is writing while a page loads** (locked DB) → HTTP 503 page saying "Database busy — retrying", not a traceback. *(Task 3: `test_locked_db_returns_busy_page`)*
2. **`data/dropship.db` doesn't exist yet** (fresh clone) → every page shows the empty-state text with HTTP 200, and the site does **not** create the file. *(Task 3: `test_missing_db_shows_empty_state_and_creates_nothing`)*
3. **An Amazon title containing HTML** (`<script>`, `&`) → shown as text. *(Task 3: `test_titles_are_escaped`)*
4. **The month boundary for the request tile** → requests from last month aren't counted. *(Task 2: `test_requests_this_month_ignores_last_month`)*
5. **Port 8000 already in use** → clear message and exit 1, not a uvicorn traceback. *(Task 4: `test_web_port_in_use_exits_1`)*

---

## File Structure

```
src/dropship/canopy_client.py   # + requests_made counter
src/dropship/keepa_client.py    # + requests_made counter
src/dropship/db.py              # + source_requests table, record/sum helpers
src/dropship/monitor.py         # run_pipeline records the request count
src/dropship/web/__init__.py
src/dropship/web/queries.py     # read-only connection + view models + queries
src/dropship/web/app.py         # create_app(settings) -> FastAPI, GET routes
src/dropship/web/templates/     # base, dashboard, orders, runs, run_detail, products, settings, empty, busy, not_found
src/dropship/web/static/style.css
src/dropship/cli.py             # + `web` subcommand
pyproject.toml                  # + fastapi, uvicorn, jinja2; package data for templates/static
tests/test_source_requests.py, tests/test_web_queries.py, tests/test_web_app.py, tests/test_cli.py (+)
```

---

### Task 1: Count data-source requests per run

**Files:**
- Modify: `src/dropship/canopy_client.py`, `src/dropship/keepa_client.py`, `src/dropship/db.py`, `src/dropship/monitor.py`
- Test: `tests/test_source_requests.py`

**Interfaces:**
- Produces:
  - `CanopyClient.requests_made: int` and `KeepaClient.requests_made: int`. Each starts at 0 and is incremented **before** each `self._http.get(...)`, so failed and raising requests count. A cached Canopy list makes no request and adds nothing.
  - The `db` schema gets `source_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs(id), source TEXT NOT NULL, count INTEGER NOT NULL, created_at TEXT NOT NULL)`, using `CREATE TABLE IF NOT EXISTS`.
  - `db.record_source_requests(conn, run_id: int, source: str, count: int) -> None`
  - `db.source_requests_in_month(conn, source: str, year: int, month: int) -> int`: the sum of `count` where `created_at` starts with `f"{year:04d}-{month:02d}"`, or 0.
  - `run_pipeline` calls `record_source_requests(conn, run_id, s.data_source, getattr(keepa, "requests_made", 0))` once, right after the risk stage is recorded and before `finish_run`.

- [ ] **Step 1: Write the failing tests** in `tests/test_source_requests.py`:
  - `test_canopy_counts_every_request_including_failures`: transport returns 503 and then 200. `bestsellers("a")` raises, then succeeds, then a cached repeat → `requests_made == 2`.
  - `test_keepa_counts_chunked_requests`: 250 ASINs → `requests_made == 3`.
  - `test_record_and_sum_source_requests`: record 2 and 3 for canopy, and 5 for keepa, this month → `source_requests_in_month(conn, "canopy", y, m) == 5`.
  - `test_requests_this_month_ignores_last_month` (RF4): a row whose `created_at` is backdated to last month is excluded.
  - `test_run_pipeline_records_request_count`: a fake source with `requests_made = 2` → one `source_requests` row `(run_id, "canopy", 2)`. A fake without the attribute → a row with count 0.
- [ ] **Step 2:** `.venv\Scripts\python -m pytest -q tests/test_source_requests.py` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite → all pass.
- [ ] **Step 5: Commit:** `feat: record data-source requests per run`

---

### Task 2: Read-only queries and view models

**Files:**
- Create: `src/dropship/web/__init__.py`, `src/dropship/web/queries.py`
- Test: `tests/test_web_queries.py`

**Interfaces:**
- Consumes: the `db` functions (`get_run`, `get_stage_runs`, `latest_run_id`, `list_proposals`, `get_candidates`, `get_dropped`, `approved_total_today`, `llm_tokens_today`, `source_requests_in_month`), `monitor.proposal_details`, `margins.load_supplier_quotes`, and `Settings`.
- Produces (all dataclasses with `frozen=True`):
  - `amazon_url(asin: str) -> str`
  - `open_readonly(db_path: Path) -> sqlite3.Connection | None`: None if the file doesn't exist. Otherwise a `mode=ro` URI connection with `timeout=5` and `row_factory = sqlite3.Row`.
  - `RunSummary(id: int, started_at: str, finished_at: str | None, status: str, duration_s: float | None, fetched: int, dropped: int, proposed: int)`, where:
    - `fetched` = distinct ASINs in this run's candidates ∪ research-stage drops (excluding asin `"-"`)
    - `dropped` = drop rows for the run (excluding asin `"-"`)
    - `proposed` = proposals with this run_id
  - `OrderView(id, asin, title, qty, unit_cost, total, risk_score, cost_label, narrative, status, created_at, decided_at, amazon_url)`. `title`/`cost_label` come from `proposal_details`.
  - `StageView(stage, status, attempts, error_type, message, started_at, finished_at)`
  - `DashboardData(last_run: RunSummary | None, pending_count: int, approved_today: float, daily_cap: float, requests_this_month: int, free_quota: int | None, tokens_today: int, pending: list[OrderView], last_errors: list[StageView])`. `free_quota` is 100 for canopy, otherwise None. `last_errors` = the last run's stages with a non-empty `error_type`.
  - `dashboard(conn, s: Settings, today: date) -> DashboardData`
  - `orders(conn, status: str) -> list[OrderView]`: newest first. An unknown status → `ValueError`.
  - `runs(conn) -> list[RunSummary]`: newest first.
  - `RunDetail(summary: RunSummary, stages: list[StageView], candidates: list[Candidate], dropped: list[dict], tokens: int)`
  - `run_detail(conn, run_id: int, reason: str | None) -> RunDetail | None`: `reason` is a case-insensitive substring filter on drops. `tokens` = the sum of `llm_usage` input+output for the run.
  - `ProductRow(asin, title, price: float | None, margin_pct: float | None, verdict: str | None, outcome: str, last_run_id: int, amazon_url)`
  - `products(conn, q: str | None) -> list[ProductRow]`: one row per ASIN from candidates ∪ drops (asin ≠ `"-"`) ∪ proposals, ordered by `last_run_id` desc then ASIN. `q` is a case-insensitive substring match on title or ASIN. Price, margin and verdict come from the ASIN's latest candidate row (None if never a candidate). For `outcome`, look at the latest run that mentions the ASIN, in priority order:
    - a proposal in that run → `"pending"` / `"approved"` / `"rejected"`
    - otherwise a drop in that run → `f"dropped: {reason}"`
    - otherwise → `"candidate"`
  - `settings_lines(s: Settings) -> list[str]`, exactly these lines in order:
    - `f"Data source: {s.data_source} ({', '.join(map(str, s.categories))})"`
    - `f"Products must cost ${s.price_min:g}–{s.price_max:g}"`
    - `f"Minimum rating {s.min_rating:g}, at least {s.min_reviews} reviews"`
    - `f"Minimum margin {s.min_margin_pct:.0%} (fee {s.fee_pct:.0%}, shipping ${s.shipping_cost:g}, estimated cost {s.est_cost_ratio:.0%} of price)"`
    - `f"Max ${s.max_order_value:g} per order, ${s.daily_spend_cap:g} per day, {s.max_units_per_order} units"`
    - `f"Risk score must be at most {s.max_risk_score}"`
    - `f"Re-proposal cooldown: {s.proposal_cooldown_days} days"`
    - `f"Blocked brands: {', '.join(s.blocked_brands) or 'none'}"`
  - `quotes(s: Settings) -> list[tuple[str, float]]`: from `load_supplier_quotes(s.supplier_quotes_path)`, sorted by ASIN.

- [ ] **Step 1: Write the failing tests** (seed a temp DB with the `db` functions, then reopen it with `open_readonly`):
  - `test_open_readonly_missing_returns_none`, and `test_open_readonly_rejects_writes` (an INSERT raises `sqlite3.OperationalError`).
  - `test_dashboard_tiles`: 2 pending, 1 approved today for 24.49, 3 canopy requests this month, 900 tokens → `pending_count == 2`, `approved_today == 24.49`, `requests_this_month == 3`, `free_quota == 100`, `tokens_today == 900`, and `last_errors` contains the degraded stage.
  - `test_dashboard_empty_db`: `last_run is None`, all counts 0.
  - `test_orders_filtered_and_labelled`: the pending tab has only pending orders, and an estimated-cost order has `cost_label == "estimated cost"`. `orders(conn, "bogus")` raises `ValueError`.
  - `test_run_summary_counts`: 3 candidates + 4 research drops + a `"-"` empty-category row + 1 risk drop + 1 proposal → `fetched == 7`, `dropped == 5`, `proposed == 1`.
  - `test_run_detail_reason_filter`: `reason="BRAND"` keeps only the `blocked brand: X` rows. An unknown run → `None`.
  - `test_products_outcome_priority`: an ASIN proposed in run 1 then cooldown-dropped in run 2 → outcome `"dropped: cooldown: decided within 30 days"`. An ASIN only a candidate → `"candidate"`. A pending one → `"pending"`. Search `q="sourdough"` is case-insensitive.
  - `test_settings_lines_and_quotes`: the second line equals `"Products must cost $15–60"` for the default Settings, and the quotes come back sorted.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full suite → pass.
- [ ] **Step 5: Commit:** `feat: read-only dashboard queries`

---

### Task 3: FastAPI app and pages

**Files:**
- Create: `src/dropship/web/app.py`, `src/dropship/web/templates/{base,dashboard,orders,runs,run_detail,products,settings,empty,busy,not_found}.html`, `src/dropship/web/static/style.css`
- Modify: `pyproject.toml` (add `fastapi`, `uvicorn`, `jinja2` to `dependencies`, with lower bounds equal to the installed versions after `.venv\Scripts\pip install fastapi uvicorn jinja2`; include `web/templates/*.html` and `web/static/*.css` as package data)
- Test: `tests/test_web_app.py`

**Interfaces:**
- Consumes: everything in Task 2's Interfaces.
- Produces: `create_app(settings: Settings) -> fastapi.FastAPI` with these GET routes:
  - `/` → dashboard
  - `/orders?status=pending|approved_manual|rejected` (default pending; an invalid status → 404 page)
  - `/runs`, `/runs/{run_id}?reason=`
  - `/products?q=`
  - `/settings`
  - `/static/...` (StaticFiles)
- Each request opens `open_readonly(settings.db_path)` and closes it in `finally`. If it returns None → render `empty.html` with the empty-state text (200); `/settings` still renders, because it doesn't need the DB.
- An `sqlite3.OperationalError` whose message contains `"locked"` → `busy.html`, status 503.
- Unknown path or run → `not_found.html`, 404.
- `base.html`:
  - a nav bar with links Dashboard (`/`) · Orders (`/orders`) · Runs (`/runs`) · Products (`/products`) · Settings (`/settings`)
  - `last updated <HH:MM:SS>`
  - the 30 s refresh meta
  - the `style.css` link
- Pages render the content listed in the spec's Pages section.
  - The dashboard says `Run #<id> in progress…` when the last run's status is `running`.
  - Cost labels get the CSS classes `cost-quoted` / `cost-estimated`.

- [ ] **Step 1: Write the failing tests** with `fastapi.testclient.TestClient(create_app(settings))` over a seeded temp DB:
  - `test_every_page_renders`: `/`, `/orders`, `/orders?status=rejected`, `/runs`, `/runs/1`, `/products`, `/settings` → 200. The dashboard contains `.venv\Scripts\python -m dropship approve 1` and `estimated cost`.
  - `test_no_forms_and_only_get_routes`: no page contains `<form`, and every route in `app.routes` (except static) has methods ⊆ `{"GET", "HEAD"}`.
  - `test_missing_db_shows_empty_state_and_creates_nothing` (RF2): settings point to a nonexistent path → `/` shows the empty-state text, and the file still doesn't exist afterwards.
  - `test_locked_db_returns_busy_page` (RF1): monkeypatch `queries.dashboard` to raise `sqlite3.OperationalError("database is locked")` → 503, containing `Database busy`.
  - `test_unknown_run_is_404` and `test_unknown_path_is_404`.
  - `test_titles_are_escaped` (RF3): a candidate titled `<script>alert(1)</script> & Co` → the `/products` page contains `&lt;script&gt;` and not `<script>alert`.
  - `test_no_secrets_rendered`: with `CANOPY_API_KEY=SECRET-XYZ` set via monkeypatch, no page contains `SECRET-XYZ`.
  - `test_run_in_progress_banner`: the latest run's status is `running` → `/` contains `in progress`.
  - `test_refresh_meta_present`: `/` contains `http-equiv="refresh" content="30"`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (clean, readable CSS; plain HTML; no JS framework). **Step 4:** Full suite → pass, with pristine output.
- [ ] **Step 5: Commit:** `feat: view-only dashboard pages`

---

### Task 4: `dropship web` command and docs

**Files:**
- Modify: `src/dropship/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `create_app` (Task 3).
- Produces:
  - A subcommand `web [--port PORT]` (int, default 8000), handled in `main` **after** config loading and **before** `db.connect` (the web command must not create or open a write connection).
  - `_cmd_web(s: Settings, port: int, serve=None) -> int`:
    - Checks the port by binding a socket to `("127.0.0.1", port)`. On `OSError` it prints `port {port} is busy, try --port {port+1}` to stderr and returns 1.
    - Otherwise it prints `Dashboard: http://localhost:{port}  (Ctrl+C to stop)` and calls `serve(create_app(s), "127.0.0.1", port)`, where the default `serve` is `uvicorn.run(app, host=host, port=port, log_level="warning")`. It returns 0.
- README: a "Dashboard" section with the start command `.venv\Scripts\python -m dropship web`, the URL, the note that it's view-only and local-only, and that actions stay in PowerShell.

- [ ] **Step 1: Write the failing tests:**
  - `test_web_bad_config_exits_2`
  - `test_web_port_in_use_exits_1` (RF5): hold a socket bound to a free port, then call `main([... "web", "--port", str(port)])` → 1, with stderr containing `is busy`.
  - `test_web_serves_on_localhost_only`: call `_cmd_web(s, port, serve=fake)`. The fake records `(host, port)` → `("127.0.0.1", port)`, and the app is a FastAPI instance.
  - `test_web_does_not_create_db`: config `db_path` points to a nonexistent file, and `main` runs `web` with `serve` patched to a no-op (monkeypatch `cli._serve`) → the file still doesn't exist.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (expose the default `serve` as a module-level `_serve` so tests can monkeypatch it). **Step 4:** Full suite → pass.
- [ ] **Step 5: Commit:** `feat: dropship web command`
- [ ] **Step 6: Manual check (controller):** start `dropship web` against the owner's real `data/dropship.db` and view every page in the browser pane.
