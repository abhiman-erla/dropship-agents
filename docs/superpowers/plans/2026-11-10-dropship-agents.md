# Dropship Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled Python CLI with a token-frugal research agent, a risk/buyer agent that only drafts orders for human approval, and a monitor that retries known failures and reports errors.

**Architecture:** One package `dropship` with plain functions per stage, SQLite for state, Keepa over `httpx`, and Claude through the official `anthropic` SDK behind a small wrapper that enforces a token budget. The monitor is the orchestrator: it runs each stage inside a retry/playbook wrapper and writes a markdown report. All external clients are injected so tests never hit the network.

**Tech Stack:** Python ≥ 3.12, `httpx`, `pydantic` v2, `anthropic` (official SDK), `sqlite3` (stdlib), `tomllib` (stdlib), `argparse` (stdlib), `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-10-dropship-agents-design.md`

## Global Constraints

- Project root: `G:\vscode\codes\dropship-agents\` (all paths below are relative to it). `git init` happens in Task 1.
- No scraping. Amazon data comes only from the Keepa API (`https://api.keepa.com`).
- Nothing is purchased without `dropship approve <id>`. The only purchaser is `RecordOnlyPurchaser`.
- Research LLM: `claude-haiku-5-5`, `output_config={"effort": "low"}`. Risk LLM: `claude-opus-5-5`, `output_config={"effort": "medium"}`, `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`. Never send `temperature`, `top_p`, `top_k`, `budget_tokens`, or `thinking={"type": "disabled"}`.
- Structured output via `client.messages.parse(..., output_format=<PydanticModel>)` → `response.parsed_output`. Always check `response.stop_reason == "refusal"` before reading it.
- Cache the static system prompt with `"cache_control": {"type": "ephemeral"}` on the system text block.
- Default config values are exactly those in the spec's "Default config values" block.
- Secrets come only from env: `KEEPA_API_KEY`, `ANTHROPIC_API_KEY` (optional when an `ant auth login` profile exists), `ALERT_WEBHOOK_URL` (optional). Never write them to the DB, the reports or the logs.
- Tests never call Keepa, Anthropic, or a webhook for real: use `httpx.MockTransport` and fake LLM clients.

## Review Focus

1. **Keepa "no data" values (`-1`) or a price of 0** → that product is skipped with a reason. It must not crash the run or produce a divide-by-zero margin. *(Task 4: `test_filter_skips_missing_price`)*
2. **Running `dropship run` twice in one day** → the second run must not create a second `pending` proposal for an ASIN that already has one. *(Task 6: `test_no_duplicate_pending_proposal`)*
3. **Approving a proposal that is already approved or rejected** → refused with a clear message and no second order row. *(Task 6: `test_approve_twice_refused`)*
4. **Several approvals on the same day** → the daily spend cap counts every order approved today, not just the current one. *(Task 6: `test_daily_cap_counts_prior_approvals`)*
5. **The LLM returns ASINs that weren't sent or leaves some out** → unknown ASINs are ignored, and missing ones keep the deterministic verdict `"unreviewed"`. *(Task 5: `test_llm_extra_and_missing_asins`)*

---

## File Structure

```
pyproject.toml
config.example.toml
supplier_quotes.example.csv
src/dropship/__init__.py
src/dropship/config.py        # Settings model + load_config
src/dropship/models.py        # Product, Candidate, RiskAssessment, Proposal, StageResult
src/dropship/db.py            # SQLite schema + repository functions
src/dropship/keepa_client.py  # Keepa HTTP client → Product
src/dropship/margins.py       # filter, margin, deterministic score (pure)
src/dropship/llm.py           # Claude wrapper with token budget
src/dropship/research.py      # research agent stage
src/dropship/risk.py          # deterministic risk score (pure)
src/dropship/buyer.py         # risk stage, proposals, approve/reject, purchaser
src/dropship/monitor.py       # retry/playbook, orchestrator, reports, alerts
src/dropship/cli.py           # argparse entry point
tests/fixtures/keepa_bestsellers.json
tests/fixtures/keepa_products.json
tests/test_*.py               # one per module
```

---

### Task 1: Project scaffold, config, and models

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `config.example.toml`, `supplier_quotes.example.csv`, `src/dropship/__init__.py`, `src/dropship/config.py`, `src/dropship/models.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `class Settings(pydantic.BaseModel)` with every key in the spec's default block as a typed field with that default, plus `db_path: Path = Path("data/dropship.db")`, `reports_dir: Path = Path("reports")`, `supplier_quotes_path: Path = Path("supplier_quotes.csv")`.
  - `load_config(path: Path) -> Settings`: reads TOML. Raises `ConfigError` if `categories` is empty.
  - `class ConfigError(Exception)`
  - `models.py` (pydantic models):
    - `Product(asin: str, title: str, price: float | None, price_avg90: float | None, sales_rank: int | None, sales_rank_avg90: int | None, rating: float | None, review_count: int | None, offer_count: int | None)`
    - `Candidate(asin, title, sell_price: float, supplier_cost: float, cost_source: Literal["quoted","estimated"], margin: float, margin_pct: float, score: float, llm_verdict: Literal["promising","weak","avoid","unreviewed"] = "unreviewed", llm_reason: str = "")`
    - `RiskAssessment(asin, risk_score: int, factors: dict[str, int], narrative: str = "", suggested_qty: int = 0)`
    - `Proposal(id: int | None, run_id: int, asin, qty: int, unit_cost: float, total: float, risk_score: int, narrative: str, status: Literal["pending","approved_manual","rejected"])`
    - `StageResult(stage: str, status: Literal["ok","degraded","failed","skipped"], attempts: int, error_type: str = "", message: str = "")`

- [ ] **Step 1:** Create the venv and install: `python -m venv .venv`, then `.venv\Scripts\pip install httpx pydantic anthropic pytest`. Write `pyproject.toml` (src layout, `[project.scripts] dropship = "dropship.cli:main"`) with lower-bound pins equal to the installed versions (`pip freeze`). Run `git init`. `.gitignore` covers `.venv/`, `data/`, `reports/`, `supplier_quotes.csv`, `config.toml`, `.env`.
- [ ] **Step 2: Write failing tests** in `tests/test_config.py`:
  - `test_defaults_match_spec`: `load_config` on a TOML with only `categories = [1350380031]` → `s.keepa_domain == 10`, `s.min_margin_pct == 0.25`, `s.llm_top_n == 20`, `s.daily_spend_cap == 15000.0`, `s.max_risk_score == 60`.
  - `test_empty_categories_raises`: `categories = []` → `pytest.raises(ConfigError)`.
  - `test_override`: `min_rating = 4.5` → `s.min_rating == 4.5`.
- [ ] **Step 3:** Run `.venv\Scripts\pytest tests/test_config.py -v` → FAIL (import error).
- [ ] **Step 4:** Implement `config.py` and `models.py`. `config.example.toml` lists every key with its default. `supplier_quotes.example.csv` has the header `asin,unit_cost` and one sample row.
- [ ] **Step 5:** Run the tests → PASS.
- [ ] **Step 6: Commit:** `git add -A && git commit -m "feat: project scaffold, settings and domain models"`

---

### Task 2: SQLite storage

**Files:**
- Create: `src/dropship/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: models from Task 1.
- Produces (every function takes a `sqlite3.Connection` first):
  - `connect(path: Path) -> sqlite3.Connection`: creates parent dirs, sets `row_factory = sqlite3.Row`, calls `init_schema`.
  - `init_schema(conn)`: tables `runs(id, started_at, finished_at, status)`, `stage_runs(id, run_id, stage, status, attempts, error_type, message, started_at, finished_at)`, `candidates(id, run_id, asin, title, sell_price, supplier_cost, cost_source, margin, margin_pct, score, llm_verdict, llm_reason)`, `proposals(id, run_id, asin, qty, unit_cost, total, risk_score, narrative, status, created_at, decided_at)`, `llm_usage(id, run_id, model, input_tokens, output_tokens, created_at)`.
  - `start_run(conn) -> int`, `finish_run(conn, run_id: int, status: str) -> None`
  - `record_stage(conn, run_id: int, result: StageResult, started_at: str, finished_at: str) -> None`
  - `save_candidates(conn, run_id: int, cands: list[Candidate]) -> None`, `get_candidates(conn, run_id: int) -> list[Candidate]`
  - `insert_proposal(conn, p: Proposal) -> int`, `get_proposal(conn, pid: int) -> Proposal | None`, `list_proposals(conn, status: str | None = None) -> list[Proposal]`, `has_pending_proposal(conn, asin: str) -> bool`, `set_proposal_status(conn, pid: int, status: str) -> None` (sets `decided_at`)
  - `approved_total_today(conn) -> float`: sum of `total` for proposals that turned `approved_manual` today (local date).
  - `record_llm_usage(conn, run_id: int, model: str, input_tokens: int, output_tokens: int) -> None`, `llm_tokens_today(conn) -> int`
  - `last_success(conn, stage: str) -> datetime | None`

- [ ] **Step 1: Write failing tests** using `connect(tmp_path / "t.db")`:
  - `test_candidates_roundtrip`: save 2 candidates → `get_candidates` returns equal models.
  - `test_pending_flag`: insert a pending proposal for `"B0X"` → `has_pending_proposal(conn, "B0X") is True`. After `set_proposal_status(..., "rejected")` → `False`.
  - `test_approved_total_today`: approve two proposals with totals 1000 and 2500 → `approved_total_today(conn) == 3500.0`.
  - `test_llm_tokens_today`: record (100 in, 50 out) twice → `300`.
- [ ] **Step 2:** Run `pytest tests/test_db.py -v` → FAIL.
- [ ] **Step 3:** Implement `db.py`. Use ISO-8601 timestamps from `datetime.now().isoformat()`. Store `factors`-like dicts as JSON text if needed.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit:** `feat: sqlite storage layer`

---

### Task 3: Keepa client

**Files:**
- Create: `src/dropship/keepa_client.py`, `tests/fixtures/keepa_bestsellers.json`, `tests/fixtures/keepa_products.json`
- Test: `tests/test_keepa_client.py`

**Interfaces:**
- Consumes: `Product`.
- Produces:
  - `class KeepaError(Exception)` with `status_code: int | None`
  - `class KeepaOutOfTokens(KeepaError)` with `refill_in_ms: int`
  - `class KeepaClient(api_key: str, domain: int, http: httpx.Client | None = None)`
    - `bestsellers(category: int) -> list[str]`: `GET /bestsellers?key&domain&category`, returns `bestSellersList.asinList`.
    - `products(asins: list[str]) -> list[Product]`: `GET /product?key&domain&asin=<comma list>&stats=90`, in chunks of 100 ASINs.
  - Mapping from `products[i].stats` (confirm the indexes against Keepa's docs while building the fixture, and record them as constants at the top of the module): `current[1]` NEW price, `avg90[1]` NEW 90-day price, `current[3]` sales rank, `avg90[3]` 90-day rank, `current[16]` rating (÷10), `current[17]` review count, `current[11]` new-offer count. Prices are integers in hundredths (÷100). **Any value `-1` or missing → `None`.**
  - Any response with `tokensLeft <= 0` and no data → raise `KeepaOutOfTokens(refill_in_ms=body["refillIn"])`. HTTP 429 also raises it. Other non-2xx → `KeepaError(status_code=...)`.

- [ ] **Step 1:** Build the two fixtures. Base their shape on Keepa's documented response; one product has `-1` for its NEW price.
- [ ] **Step 2: Write failing tests** with `httpx.MockTransport`:
  - `test_bestsellers_parses_asins`: fixture → `["B0A", "B0B", "B0C"]`.
  - `test_products_maps_fields`: first product → `price == 499.0`, `rating == 4.3`, `review_count == 812`, `offer_count == 6`.
  - `test_minus_one_becomes_none`: product with `-1` NEW → `price is None`.
  - `test_chunking`: 250 ASINs → the transport sees 3 requests.
  - `test_out_of_tokens`: body `{"tokensLeft": -5, "refillIn": 42000}` → `KeepaOutOfTokens` with `refill_in_ms == 42000`.
  - `test_http_500`: → `KeepaError` with `status_code == 500`.
  - `test_api_key_not_in_exception_text`: on a 500, `"SECRETKEY" not in str(exc)`.
- [ ] **Step 3:** Run → FAIL.
- [ ] **Step 4:** Implement it. Use `httpx.Client(base_url="https://api.keepa.com", timeout=30)` by default.
- [ ] **Step 5:** Run → PASS.
- [ ] **Step 6: Commit:** `feat: keepa client`

---

### Task 4: Filter, margin, and deterministic score

**Files:**
- Create: `src/dropship/margins.py`
- Test: `tests/test_margins.py`

**Interfaces:**
- Consumes: `Product`, `Candidate`, `Settings`.
- Produces:
  - `load_supplier_quotes(path: Path) -> dict[str, float]`: returns `{}` if the file is missing.
  - `passes_filter(p: Product, s: Settings) -> tuple[bool, str]`: the reason is `""` on a pass, otherwise e.g. `"missing price"` or `"rank too high"`. A price that is `None` or `<= 0` fails first.
  - `compute_margin(sell_price: float, supplier_cost: float, s: Settings) -> tuple[float, float]`: returns `(margin, margin_pct)` using the spec formula.
  - `score(p: Product, margin_pct: float, s: Settings) -> float`: `0.5*margin_pct_norm + 0.3*rank_norm + 0.2*rating_norm`, each in [0, 1]. `margin_pct_norm = min(margin_pct/0.6, 1)`, `rank_norm = 1 - sales_rank/max_sales_rank`, `rating_norm = (rating-3)/2` clipped to [0, 1].
  - `build_candidates(products: list[Product], quotes: dict[str, float], s: Settings) -> list[Candidate]`: filters, prices, keeps rows with `margin_pct >= min_margin_pct`, and sorts by score descending.

- [ ] **Step 1: Write failing tests:**
  - `test_margin_formula`: price 1000, cost 350, fee 0.15, ship 60 → `(440.0, 0.44)`.
  - `test_estimated_cost_used_without_quote`: no quote → `cost_source == "estimated"`, `supplier_cost == 350.0`.
  - `test_quote_overrides`: quote 200 → `cost_source == "quoted"`, `supplier_cost == 200.0`.
  - `test_filter_skips_missing_price` (Review Focus 1): price `None` → `(False, "missing price")`. Price `0` → `(False, "missing price")`. `build_candidates` with such products returns `[]` and raises nothing.
  - `test_below_min_margin_dropped`: margin_pct 0.2 → not in the output.
  - `test_sorted_by_score`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit:** `feat: margin and candidate scoring`

---

### Task 5: LLM wrapper and research agent

**Files:**
- Create: `src/dropship/llm.py`, `src/dropship/research.py`
- Test: `tests/test_llm.py`, `tests/test_research.py`

**Interfaces:**
- Consumes: `KeepaClient`, `build_candidates`, `load_supplier_quotes`, db functions, `Settings`.
- Produces:
  - `class LLMBudgetExceeded(Exception)`, `class LLMUnusable(Exception)` (refusal or validation failure)
  - `class LLM(client: anthropic.Anthropic, conn, run_id: int, daily_budget: int)`
    - `parse(model: str, system: str, user: str, schema: type[BaseModel], effort: str, max_tokens: int = 4000, fallbacks: bool = False) -> BaseModel`: raises `LLMBudgetExceeded` if `llm_tokens_today(conn) >= daily_budget` **before** calling. Sends `system=[{"type":"text","text":system,"cache_control":{"type":"ephemeral"}}]` and `output_config={"effort": effort}`. With `fallbacks=True` it goes through `client.beta.messages.parse` with the fallback betas/param from Global Constraints. Records `usage.input_tokens + output_tokens` through `record_llm_usage`. Raises `LLMUnusable` on `stop_reason == "refusal"`, a `None` `parsed_output`, or a pydantic `ValidationError`.
  - `research.py`:
    - `class Verdict(BaseModel): asin: str; verdict: Literal["promising","weak","avoid"]; reason: str` (the prompt asks for at most 15 words) and `class Verdicts(BaseModel): items: list[Verdict]`
    - `RESEARCH_SYSTEM: str`: a fixed prompt with no dates or IDs, so it caches.
    - `run_research(keepa: KeepaClient, llm: LLM | None, conn, run_id: int, s: Settings) -> tuple[StageResult, dict[str, Product]]` (the dict maps ASIN → Product for every fetched product and feeds `run_risk`): bestsellers for every category → de-duplicated ASINs → `products` → `build_candidates` → top `llm_top_n` → one `llm.parse(model="claude-haiku-5-5", effort="low", schema=Verdicts, user=<compact JSON: asin, title[:60], price, margin_pct (2dp), rank, rating, reviews, offers>)` → merge verdicts → `save_candidates` (all candidates, with verdicts on the top N). If `llm` is `None`, or it raises `LLMBudgetExceeded`/`LLMUnusable`, save without verdicts and return `status="degraded"`, `error_type` = the exception class name. Keepa errors propagate (the monitor handles them).

- [ ] **Step 1: Write failing tests** with a fake Anthropic client (an object whose `messages.parse` / `beta.messages.parse` return `SimpleNamespace(stop_reason=..., parsed_output=..., usage=SimpleNamespace(input_tokens=..., output_tokens=...))`) and a fake Keepa (a class with `bestsellers`/`products`):
  - `test_budget_blocks_call`: tokens today ≥ budget → `LLMBudgetExceeded`, and the fake records 0 calls.
  - `test_refusal_raises_unusable`.
  - `test_usage_recorded`: after one call, `llm_tokens_today == in+out`.
  - `test_system_prompt_cached`: the captured `system[0]["cache_control"] == {"type": "ephemeral"}`.
  - `test_research_happy_path`: 3 products, 2 pass → 2 candidates saved with verdicts, status `"ok"`, exactly 1 LLM call.
  - `test_research_degraded_on_budget`: → status `"degraded"`, `error_type == "LLMBudgetExceeded"`, candidates still saved with `"unreviewed"`.
  - `test_llm_extra_and_missing_asins` (Review Focus 5): the LLM returns verdicts for `"B0A"` and the unknown `"ZZZ"` only → B0A has the verdict, B0B stays `"unreviewed"`, and no `"ZZZ"` row exists.
  - `test_only_top_n_sent`: `llm_top_n=1` → the user JSON contains exactly 1 ASIN.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit:** `feat: token-budgeted LLM wrapper and research agent`

---

### Task 6: Risk scoring, proposals, approve/reject

**Files:**
- Create: `src/dropship/risk.py`, `src/dropship/buyer.py`
- Test: `tests/test_risk.py`, `tests/test_buyer.py`

**Interfaces:**
- Consumes: `Candidate`, `Product`, `LLM`, db functions, `Settings`.
- Produces:
  - `risk.py`: `risk_score(c: Candidate, p: Product, s: Settings) -> RiskAssessment`. Sum of capped factors (total capped at 100):
    - `price_volatility`: `|price − price_avg90| / price_avg90` × 100, cap 25. 0 if avg missing.
    - `rank_volatility`: `max(0, (rank − rank_avg90) / rank_avg90)` × 50, cap 20.
    - `competition`: `offer_count / max_offer_count` × 20, cap 20.
    - `low_rating`: `(4.5 − rating) × 20`, clamped to [0, 15].
    - `thin_margin`: `(0.40 − margin_pct) × 50`, clamped to [0, 10].
    - `estimated_cost`: 10 if `cost_source == "estimated"`, otherwise 0.
  - `buyer.py`:
    - `class RiskNote(BaseModel): narrative: str; suggested_qty: int`
    - `RISK_SYSTEM: str` (fixed)
    - `run_risk(llm: LLM | None, conn, run_id: int, products: dict[str, Product], s: Settings) -> StageResult`: for each candidate of this run whose verdict is not `"avoid"`: compute the risk. If `risk_score <= max_risk_score` **and** `not has_pending_proposal(asin)` → one `llm.parse(model="claude-opus-5-5", effort="medium", fallbacks=True, schema=RiskNote)` → `qty = clamp(suggested_qty, 1, max_units_per_order)`, then shrink `qty` until `qty*unit_cost <= max_order_value` (skip if even 1 unit doesn't fit) → `insert_proposal(status="pending")`. On an LLM failure, use `narrative="LLM unavailable — deterministic risk only"`, `qty=1`, and status `"degraded"`.
    - `class Purchaser(Protocol): def place(self, p: Proposal) -> str` (returns a reference)
    - `class RecordOnlyPurchaser`: returns `f"manual-{p.id}"` and does nothing else.
    - `class ApprovalError(Exception)`
    - `approve(conn, pid: int, s: Settings, purchaser: Purchaser) -> str`: raises `ApprovalError` if the proposal is not found or not `pending`, if `total > max_order_value`, or if `approved_total_today + total > daily_spend_cap`. Otherwise it calls `purchaser.place`, sets `approved_manual`, and returns the reference.
    - `reject(conn, pid: int) -> None`: only from `pending`, otherwise `ApprovalError`.

- [ ] **Step 1: Write failing tests:**
  - `test_risk_factors_example`: price 1100 vs avg 1000 (→10), rank 12000 vs 10000 (→10), offers 6/15 (→8), rating 4.0 (→10), margin 0.30 (→5), estimated (→10) → `risk_score == 53`.
  - `test_risk_capped_100`.
  - `test_low_risk_creates_pending_proposal`: one Opus call, the proposal is `pending`, `qty == min(suggested, 20)`.
  - `test_high_risk_no_proposal`: score 70 → no proposal, no LLM call.
  - `test_avoid_verdict_skipped`.
  - `test_no_duplicate_pending_proposal` (Review Focus 2): run `run_risk` twice → 1 pending proposal and 1 LLM call.
  - `test_qty_shrinks_to_order_cap`: unit 1200, suggested 10, cap 5000 → qty 4.
  - `test_approve_happy`: returns `"manual-<id>"`, status `approved_manual`.
  - `test_approve_twice_refused` (Review Focus 3): the second approve → `ApprovalError`, and the purchaser was called once. Approving after a reject → `ApprovalError`.
  - `test_daily_cap_counts_prior_approvals` (Review Focus 4): settings `max_order_value=10000`, `daily_spend_cap=15000`; approve 9000, then 7000 → the second raises `ApprovalError` mentioning "daily spend cap".
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit:** `feat: risk scoring and approval-gated purchase proposals`

---

### Task 7: Monitor — retry playbook, orchestrator, report, alerts

**Files:**
- Create: `src/dropship/monitor.py`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: `run_research`, `run_risk`, `KeepaOutOfTokens`, `KeepaError`, `LLM`, db functions, `Settings`.
- Produces:
  - `TRANSIENT = (httpx.TimeoutException, httpx.ConnectError, anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)`. A `KeepaError` with `status_code` ≥ 500 also counts as transient.
  - `run_stage(name: str, fn: Callable[[], StageResult], sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3) -> StageResult`: the playbook. Transient → backoff `2**attempt` seconds, up to `max_attempts`. `KeepaOutOfTokens` → `sleep(min(refill_in_ms/1000, 900))` once, then retry. Any other exception → `StageResult(status="failed", error_type=<class name>, message=str(exc)[:500])`. Stamps `attempts`.
  - `run_pipeline(conn, s: Settings, keepa: KeepaClient, llm_factory: Callable[[int], LLM | None], sleep=time.sleep) -> int`: `start_run` → research stage → risk stage (marked `skipped` if research `failed`) → `record_stage` for each → `finish_run` with the worst status (`failed` > `degraded` > `ok`) → `write_report` → `maybe_alert`. Returns `run_id`. The research stage is passed to `run_stage` as a closure that calls `run_research`, stores the returned products dict in a local variable, and returns the `StageResult`. The risk closure passes that dict to `run_risk`.
  - `write_report(conn, run_id: int, s: Settings) -> Path`: writes `reports/<YYYY-MM-DD>-run-<id>.md` with the sections `Summary`, `Stages`, `Top candidates` (table, top 20), `Proposals awaiting approval` (with the `dropship approve <id>` hint), `Errors`, `LLM tokens today`, and a `STALE` warning when `last_success("research")` is older than 26 h or `None`.
  - `maybe_alert(run_status: str, report_path: Path, webhook_url: str | None, http: httpx.Client | None = None) -> bool`: POSTs `{"status": ..., "report": str(report_path), "summary": <first 1500 chars of the report>}` only when the status is `failed` or `degraded` and the URL is set. Webhook errors are swallowed (logged) and return `False`.

- [ ] **Step 1: Write failing tests** (a fake `sleep` records its calls):
  - `test_retry_then_success`: fails with `httpx.ConnectError` twice, then ok → `status == "ok"`, `attempts == 3`, sleeps `[2, 4]`.
  - `test_retry_exhausted_fails`: always `ConnectError` → `failed`, `attempts == 3`.
  - `test_keepa_out_of_tokens_waits_once`: raises `KeepaOutOfTokens(refill_in_ms=2_000_000)`, then ok → one sleep of `900`, status `ok`.
  - `test_unknown_error_not_retried`: `ValueError` → `failed` after 1 attempt, `error_type == "ValueError"`.
  - `test_pipeline_skips_risk_after_research_failure`: stage rows `research=failed`, `risk=skipped`, run status `failed`.
  - `test_report_contains_sections_and_no_secrets`: the report has every section header. With `KEEPA_API_KEY=SECRETKEY` in env, `"SECRETKEY" not in report text`.
  - `test_stale_warning`: no prior successful research → the report contains `STALE`.
  - `test_alert_only_on_bad_status`: `ok` → no POST. `degraded` → 1 POST. A webhook 500 → returns `False` and doesn't raise.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit:** `feat: monitor with retry playbook, reports and alerts`

---

### Task 8: CLI and scheduling

**Files:**
- Create: `src/dropship/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv: list[str] | None = None) -> int` with the subcommands `run`, `proposals [--status pending|approved_manual|rejected]`, `approve <id>`, `reject <id>`, `report [--run <id>]`, plus a global `--config PATH` (default `config.toml`). `run` builds `KeepaClient(os.environ["KEEPA_API_KEY"], s.keepa_domain)` and an `llm_factory` that returns `LLM(anthropic.Anthropic(), conn, run_id, s.daily_llm_token_budget)`. It exits 2 with a clear message if `KEEPA_API_KEY` is missing. `approve` prints the reference and exits 0, or prints the `ApprovalError` message and exits 1.

- [ ] **Step 1: Write failing tests:**
  - `test_missing_keepa_key_exits_2` (monkeypatch env).
  - `test_approve_flow`: seed a pending proposal in a tmp DB via a config pointing to `tmp_path` → `main(["--config", cfg, "approve", "1"]) == 0` and the output contains `manual-1`. Repeating it → `1`.
  - `test_proposals_lists_pending`: the output contains the ASIN and the total.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Write `README.md`: setup (venv, copy `config.example.toml` → `config.toml`, set env vars), the daily approval routine, and the Task Scheduler command:
  `schtasks /Create /SC DAILY /ST 08:00 /TN "dropship-run" /TR "G:\vscode\codes\dropship-agents\.venv\Scripts\dropship.exe --config G:\vscode\codes\dropship-agents\config.toml run"`
- [ ] **Step 6:** Full suite: `.venv\Scripts\pytest -v` → all PASS.
- [ ] **Step 7: Commit:** `feat: CLI, README and scheduling`

---

## After this plan (not in scope)

- A real supplier `Purchaser` (once the supplier is chosen).
- The storefront/listing integration (once the sales channel is decided).
- Tuning `est_cost_ratio` and thresholds from the first few weeks of reports.
