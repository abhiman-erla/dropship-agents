# Local Dashboard (Option A) — Design Spec

**Date:** 2026-11-11
**Status:** Approved in conversation; pending written-spec review
**Builds on:** `2026-11-10-dropship-agents-design.md` (pipeline), Canopy US demo plan

## Goal

A **view-only** website on the owner's PC that shows everything the dropship pipeline has produced — runs, draft orders, products, drop reasons, usage — so the owner can follow it in a browser instead of reading markdown reports. All actions (`run`, `approve`, `reject`) stay as PowerShell commands.

Later (Option B, separate spec): a hosted version for the owner and friends with logins and an online database. This design must not block that.

## Decisions

| Topic | Decision |
|---|---|
| Purpose | Display only. No control in the site changes data. |
| Who | The owner, on their own PC |
| Approach | Small server-rendered website started from the CLI (FastAPI + Jinja2 templates + one CSS file, no JS framework) |
| Network | Binds to `127.0.0.1` only; no option to expose it in this version |
| Data | Reads the existing SQLite DB in read-only mode on every request |
| Freshness | Page reflects the DB at load time; auto-refresh every 30 s |

## Success criteria

1. `.venv\Scripts\python -m dropship web` starts the site; the owner opens `http://localhost:8000`.
2. After a `run`, `approve` or `reject` in PowerShell, a refresh (or the 30 s auto-refresh) shows the new state.
3. The owner can read every draft order's notes and cost source and copy the `approve`/`reject` command, without opening a markdown file.
4. Nothing the site does can modify the database, the config or the quotes file. Exception: dropship web adds missing tables (CREATE TABLE IF NOT EXISTS) to an existing database at startup; it never changes rows and never creates the file.

## Pages

Every page shares a top bar — **Dashboard · Orders · Runs · Products · Settings** — with "last updated <time>" and an "auto-refresh 30s" note.

1. **Dashboard** (`/`)
   - Last run: id, start time, status (ok / degraded / failed / running), duration.
   - Four tiles: draft orders pending · approved today (`$X of $<daily_spend_cap>`) · data-source requests this month (`N of 100 free` when `data_source = "canopy"`; just `N` for Keepa) · Claude tokens today.
   - Pending draft orders (short list) with the PowerShell command to copy: `.venv\Scripts\python -m dropship approve <id>` / `reject <id>`.
   - Errors from the last run (stage, type, message), if any.
2. **Orders** (`/orders?status=pending|approved_manual|rejected`, default pending)
   - Tabs for the three statuses. One card per order: title, Amazon link, `qty × unit = total`, risk score, cost source (**quoted** green / **estimated** amber), narrative, created / decided time. Pending cards show the approve/reject commands.
3. **Runs** (`/runs`) and **Run detail** (`/runs/{id}`)
   - List: id, date, status, products fetched, dropped count, proposals created.
   - Detail: stages table, top candidates table, dropped products table with a reason filter (`?reason=<text>` substring, case-insensitive), errors, LLM tokens for that run.
4. **Products** (`/products?q=<text>`)
   - One row per ASIN ever seen (candidates ∪ dropped ∪ proposals), searchable by title or ASIN substring.
   - Columns: ASIN (Amazon link), title, latest known price, latest margin %, latest AI verdict, latest outcome, last seen (run id/date).
   - Latest outcome, from the most recent run that mentions the ASIN, in priority: proposal status (`pending` / `approved` / `rejected`) › `dropped: <reason>` › `candidate`.
5. **Settings** (`/settings`)
   - Current settings in plain words (e.g. "Products must cost $15–60", "Minimum margin 25%", "Max $300 per order, $1,000 per day"), the data source and categories, blocked brands, and the supplier quotes table.
   - Note: "Edit `config.toml` / `supplier_quotes.csv` to change these."

Amazon link: `https://www.amazon.com/dp/<ASIN>` (US marketplace; the demo is US-only).

## Architecture

```
src/dropship/web/
  __init__.py
  app.py          # create_app(settings) -> FastAPI; one route per page; no write routes
  queries.py      # all read queries -> plain dataclasses/pydantic view models
  templates/      # base.html + dashboard, orders, runs, run_detail, products, settings, not_found
  static/style.css
```

- **`create_app(settings: Settings) -> FastAPI`** stores the resolved settings; every request opens the DB with `sqlite3.connect("file:<db_path>?mode=ro", uri=True, timeout=5)` and closes it at the end of the request.
- **Only GET routes.** No `<form>` and no POST/PUT/DELETE handlers exist.
- **`queries.py`** owns all SQL the site uses; templates receive view models only.
- **CLI:** new subcommand `web [--port 8000]` in `dropship.cli`. It loads and resolves the config exactly like `run` (bad config → exit 2), checks the port is free, and serves on `127.0.0.1:<port>` via uvicorn.
- **Dependencies:** add `fastapi`, `uvicorn`, `jinja2` to the main `dependencies` (lower bounds = installed versions). Tests use FastAPI's `TestClient` (needs `httpx`, already present).

### Pipeline change: count data-source requests

- `CanopyClient` and `KeepaClient` each expose `requests_made: int`, incremented once per HTTP request attempted (including failed ones).
- New table `source_requests(id, run_id, source, count, created_at)` (created with `CREATE TABLE IF NOT EXISTS`).
- `run_pipeline` records `(run_id, settings.data_source, client.requests_made)` once at the end of every run, success or failure (a client without the attribute records 0).
- Dashboard tile = sum of `count` for the current data source in the current calendar month (local time).

## Error handling

| Situation | Behaviour |
|---|---|
| DB file missing | Dashboard shows "No runs yet. Run `.venv\Scripts\python -m dropship run` in PowerShell."; other pages show the same empty state |
| DB locked > 5 s | Page shows "Database busy — retrying" with a 3 s refresh; HTTP 503 |
| Latest run status `running` | Dashboard shows "Run #N in progress…" |
| Invalid config | `dropship web` prints the config error and exits 2 (same as `run`) |
| Port in use | Prints "port <p> is busy, try --port <p+1>" and exits 1 |
| Unknown run id / path | Friendly not-found page (HTTP 404) with a link to the dashboard |
| Titles/reasons with `<`, `&`, quotes | Rendered as text: Jinja2 autoescape on for all templates |

**Secrets:** the site never reads environment variables and never renders API keys. Settings shows only `Settings` fields and quotes.

## Testing

- **Queries** (`tests/test_web_queries.py`): a temp DB seeded via the existing `db` functions; assert tile numbers, order tab filtering, run summaries (fetched/dropped/proposed counts), product "latest outcome" priority, request counting for the current month only.
- **Pages** (`tests/test_web_app.py`, FastAPI `TestClient`):
  - every page returns 200 with its key content (e.g. pending card shows "estimated cost" and `dropship approve <id>`);
  - no page contains `<form`; the app has no non-GET routes;
  - empty/missing DB, unknown run id (404), reason filter, product search;
  - a title containing `<script>` is escaped;
  - no page contains the value of a dummy `CANOPY_API_KEY` set in the environment.
- **Read-only:** a write through the site's connection raises `sqlite3.OperationalError`.
- **Request counting:** client counters (incl. failed request), `run_pipeline` records one row per run.
- **CLI:** `web` with bad config exits 2; port-in-use exits 1 (server start is mocked in tests).
- **Manual:** start the site against the owner's real `data/dropship.db` and view every page in a browser.

## Out of scope (A)

Buttons or forms that change data; running the pipeline from the site; logins; exposing beyond `127.0.0.1`; charts; editing settings or quotes; hosting. These belong to Option B or later work.
