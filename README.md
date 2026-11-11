# dropship

Daily research and risk-check agents for an Amazon dropshipping workflow. Amazon data comes only
from the Canopy API (default, free tier) or the Keepa API (no scraping). **Nothing is ever purchased automatically**: you review
proposals and place orders yourself.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
copy config.example.toml config.toml
```

Edit `config.toml` and set `categories` (required): Canopy category id strings such as
`"bestsellers_kitchen"`, or Keepa integer ids when `data_source = "keepa"`. Mixing them up is a
config error. Relative paths in the config (database, reports, supplier quotes) are resolved
against the config file's folder.

Environment variables (secrets are never stored in the DB, reports or logs):

- `CANOPY_API_KEY` - required for `python -m dropship run` when `data_source = "canopy"` (the default).
- `KEEPA_API_KEY` - required instead when `data_source = "keepa"`.
- `ANTHROPIC_API_KEY` - needed unless you have run `ant auth login`. Without any credentials the
  run degrades to deterministic scoring only.
- `ALERT_WEBHOOK_URL` - optional; notified when a run is degraded or failed.

## Canopy demo setup (US, $0)

Sign up at Canopy API for a free key (the free tier allows 100 requests per month; the pipeline
makes one request per category per run) and export `CANOPY_API_KEY`. The defaults target a US
demo: `data_source = "canopy"`, `canopy_domain = "US"`, prices 15-60 USD, order cap 300 USD.

Demo limits: Canopy best-sellers gives no price or rank history and no seller (offer) count, so
`price_avg90` and `sales_rank_avg90` are empty and `offer_count` is unknown (the offer filter is
skipped). The best-seller list position is used as the sales rank (`max_sales_rank = 50`). Set
`data_source = "keepa"` for history and offer counts.

Because of this, the risk score excludes price volatility, rank volatility and competition: they
count as 0 and are marked unknown (the proposal narrative ends with
`[risk excludes: ... - no data]`). With the default thresholds almost every promising product
will pass the risk gate, so the score is not a safety check here. Judge each proposal's narrative
and cost source (quoted or estimated) yourself before approving.

If a category returns no products (for example a wrong category id), the run is marked degraded
and the report names the category under Errors and Dropped products.

## What the agents do

**Research** pulls candidate products in your categories from the data source, filters them by price, rank,
rating, reviews, offer count and your `blocked_brands` list (big brands can't be sourced from dropship
suppliers), and estimates margin from your supplier quotes (or the
`est_cost_ratio` fallback). A cheap LLM pass then judges the top candidates.

**Risk** takes the candidates the LLM rated promising (or, if research ran without the LLM,
the top `llm_top_n` by score, skipping ASINs that already have a pending or recently decided
proposal) and scores them for risk (price volatility, competition, demand). An LLM adds a short narrative and a suggested quantity, which is capped by
`max_units_per_order` and `max_order_value`. Candidates under `max_risk_score` become proposals.

**Monitor** runs both stages with retries, records each stage's status, writes the daily Markdown
report to `reports/`, and posts a webhook alert if the run was degraded or failed.

**Buyer** handles approval. `approve` checks the order and daily spend caps, then records the
approval only.

## Daily routine

1. Read the latest report in `reports/` (or `python -m dropship report`).
2. `.venv\Scripts\python -m dropship proposals` lists pending proposals (`--status pending|approved_manual|rejected`).
3. `.venv\Scripts\python -m dropship approve <id>` or `... reject <id>`.

The examples use `python -m dropship` because Windows Application Control can block
`dropship.exe`; plain `dropship <cmd>` also works where the exe isn't blocked.

**Approving only records the order** and prints a reference such as `manual-3`. You then place
the order with your supplier yourself.

Other commands: `python -m dropship run` (run the pipeline now), `python -m dropship report --run <id>`.
Global option: `--config PATH` (default `config.toml`).
Exit codes: 0 success, 1 failed run or rejected approval, 2 configuration or missing-key error.

## Scheduling (Windows Task Scheduler)

Replace `C:\path\to\dropship-agents` with the folder you cloned the repo into:

```
schtasks /Create /SC DAILY /ST 08:00 /TN "dropship-run" /TR "\"C:\path\to\dropship-agents\.venv\Scripts\python.exe\" -m dropship --config \"C:\path\to\dropship-agents\config.toml\" run"
```

The task needs `CANOPY_API_KEY` (or `KEEPA_API_KEY` when `data_source = "keepa"`) and
`ANTHROPIC_API_KEY` available to it, for example as user environment variables set via `setx`.
Check it with `schtasks /Run /TN "dropship-run"`.

## Tests

```powershell
.venv\Scripts\pytest
```
