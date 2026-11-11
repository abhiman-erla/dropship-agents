# dropship

Daily research and risk-check agents for an Amazon dropshipping workflow. Amazon data comes only
from the Keepa API (no scraping). **Nothing is ever purchased automatically**: you review
proposals and place orders yourself.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
copy config.example.toml config.toml
```

Edit `config.toml` and set `categories` (Keepa category ids; required). Relative paths in the
config (database, reports, supplier quotes) are resolved against the config file's folder.

Environment variables (secrets are never stored in the DB, reports or logs):

- `KEEPA_API_KEY` - required for `python -m dropship run`.
- `ANTHROPIC_API_KEY` - needed unless you have run `ant auth login`. Without any credentials the
  run degrades to deterministic scoring only.
- `ALERT_WEBHOOK_URL` - optional; notified when a run is degraded or failed.

## What the agents do

**Research** pulls candidate products in your categories from Keepa, filters them by price, rank,
rating, reviews and offer count, and estimates margin from your supplier quotes (or the
`est_cost_ratio` fallback). A cheap LLM pass then judges the top candidates.

**Risk** takes the promising candidates and scores them for risk (price volatility, competition,
demand). An LLM adds a short narrative and a suggested quantity, which is capped by
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

```
schtasks /Create /SC DAILY /ST 08:00 /TN "dropship-run" /TR "\"G:\vscode\codes\dropship-agents\.venv\Scripts\python.exe\" -m dropship --config \"G:\vscode\codes\dropship-agents\config.toml\" run"
```

The task needs `KEEPA_API_KEY` (and `ANTHROPIC_API_KEY`) available to it, for example as user
environment variables set via `setx`. Check it with `schtasks /Run /TN "dropship-run"`.

## Tests

```powershell
.venv\Scripts\pytest
```
