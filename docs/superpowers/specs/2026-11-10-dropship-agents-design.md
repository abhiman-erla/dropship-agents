# Dropship Agents — Design Spec

**Date:** 2026-11-10
**Status:** Draft for review

## Goal

A scheduled Python pipeline with three agents that (1) find Amazon best-sellers with healthy estimated margins while spending as few LLM tokens as possible, (2) risk-score the best candidates and draft purchase proposals that a human must approve before any money moves, and (3) watch every run, retry/fix known failure types automatically, and report errors.

## Decisions (from the user)

| Topic | Decision |
|---|---|
| Product data | **Canopy API** best-sellers (default, US demo, `CANOPY_API_KEY`) or **Keepa API** (`KEEPA_API_KEY`) — no scraping of Amazon pages |
| Purchasing | **Human approves every order.** The agent drafts; nothing is bought without `dropship approve <id>` |
| Marketplace / sales channel | **Not decided.** Marketplace is a config value (Keepa domain id); the selling side and the supplier integration are pluggable and out of scope here |
| Stack | **Python + Claude API.** Plain code does data fetching and math; Claude is called only where judgment is needed |

## Agents

### 1. Research agent (token-frugal)
- Pulls best-seller ASIN lists for configured categories from Keepa, then product stats (price, sales rank, rating, review count, offer count, 90-day averages).
- **Deterministic filter first** (no LLM): price band, max sales rank, min rating, min reviews, max offer count.
- **Margin estimate** per product: `margin = sell_price − supplier_cost − sell_price × fee_pct − shipping_cost`; `margin_pct = margin / sell_price`. Supplier cost comes from `supplier_quotes.csv` when an ASIN has a quote, otherwise `sell_price × est_cost_ratio` and the row is marked `cost_source="estimated"`.
- Keeps products with `margin_pct ≥ min_margin_pct`, ranks by a deterministic score, takes the top `llm_top_n`.
- **One** Claude Haiku 5.5 call (`claude-haiku-5-5`, effort `low`, structured output) over compact JSON of those top N returns a short verdict and one-line reason per ASIN. Static system prompt is cached.
- A daily LLM token budget is enforced; if it is exhausted, the LLM step is skipped and the deterministic ranking is used.
- Output: `candidates` rows in SQLite + a section in the run report.

### 2. Buyer / risk agent
- Deterministic risk score 0–100 (higher = riskier) from: price volatility (current vs 90-day average), sales-rank volatility, offer-count competition, low rating, thin margin, estimated (not quoted) cost.
- Candidates with `risk_score ≤ max_risk_score` get one Claude Opus 5.5 call (`claude-opus-5-5`, effort `medium`, structured output, server-side refusal fallbacks enabled) that writes a short risk narrative and a suggested quantity (bounded by config).
- Creates `purchase_proposals` (status `pending`). Nothing is purchased automatically.
- `dropship approve <id>` re-checks the per-order cap and the daily spend cap, then hands the proposal to a `Purchaser`. The only purchaser in scope is `RecordOnlyPurchaser`, which records the order as `approved_manual` so the user places it with their supplier. A real supplier integration is a later plan.
- `dropship reject <id>` marks it `rejected`.

### 3. Monitor agent
- Runs the pipeline stages in order (`research` → `risk`), recording a `stage_runs` row per stage (start, end, status, error type, message, attempts).
- **Auto-fix playbook — only these, nothing else:**
  - Transient network / HTTP 429 / 5xx / Anthropic rate-limit or connection errors → retry with exponential backoff (max 3 attempts).
  - Keepa out of tokens (`tokensLeft ≤ 0`) → wait `refillIn` ms (capped at 15 min) once, then retry.
  - LLM budget exhausted, LLM refusal, or LLM output that fails validation → continue without the LLM step (deterministic fallback), mark run `degraded`.
- Anything else → no fix; stage marked `failed`, later stages skipped.
- It never edits code or config.
- After each run, writes `reports/<date>-run-<id>.md` (candidates, proposals awaiting approval, stage results, errors, token usage) and, if `ALERT_WEBHOOK_URL` is set, POSTs a JSON summary when the run is `failed` or `degraded`.
- Staleness check: if no `research` stage succeeded in the last 26 h, the report and alert say so.

## Interfaces

- CLI: `dropship run`, `dropship proposals`, `dropship approve <id>`, `dropship reject <id>`, `dropship report [--run <id>]`.
- Config: `config.toml` (thresholds, categories, domain) + env vars `CANOPY_API_KEY` (when `data_source = "canopy"`, the default) or `KEEPA_API_KEY` (when `data_source = "keepa"`), `ANTHROPIC_API_KEY` (or an `ant auth login` profile), `ALERT_WEBHOOK_URL` (optional).
- Storage: one SQLite file, `data/dropship.db`.
- Schedule: Windows Task Scheduler runs `dropship run` daily.

## Default config values

```
data_source = "canopy"       # "canopy" or "keepa"
canopy_domain = "US"
keepa_domain = 1             # 1 = amazon.com, 10 = amazon.in
categories = []              # Canopy category ids (str) or Keepa ids (int); required
price_min = 15.0
price_max = 60.0
max_sales_rank = 50          # Canopy: best-seller list position
min_rating = 4.0
min_reviews = 50
max_offer_count = 15
fee_pct = 0.15
shipping_cost = 5.0
est_cost_ratio = 0.35
min_margin_pct = 0.25
llm_top_n = 20
daily_llm_token_budget = 200000
max_risk_score = 60
max_order_value = 300.0
daily_spend_cap = 1000.0
max_units_per_order = 20
max_asins_per_category = 50   # best-seller lists are rank-ordered; keeps data-source use bounded
```
Money is in the marketplace currency (USD for the US demo; ₹ for Keepa domain 10).
Data sources: Canopy (default, $0 tier, best-sellers only, no history or offer counts) or Keepa.

## Out of scope

Scraping; automatic purchasing; supplier and storefront integrations; listing products for sale; a web UI.
