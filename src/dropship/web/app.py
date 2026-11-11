"""View-only FastAPI site over the pipeline's SQLite DB. GET routes only, no forms."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from dropship.config import Settings
from dropship.web import queries

_HERE = Path(__file__).parent
_ORDER_TABS = [("pending", "Pending"), ("approved_manual", "Approved"),
               ("rejected", "Rejected")]


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="dropship dashboard", docs_url=None, redoc_url=None,
                  openapi_url=None)
    templates = Jinja2Templates(directory=_HERE / "templates")
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx["updated"] = datetime.now().strftime("%H:%M:%S")
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def not_found(request: Request) -> HTMLResponse:
        return render(request, "not_found.html", 404, active="")

    def with_db(request: Request, template: str, active: str, build) -> HTMLResponse:
        """Open the DB read-only, build the context, always close the connection."""
        conn = queries.open_readonly(settings.db_path)
        if conn is None:
            return render(request, "empty.html", active=active)
        try:
            ctx = build(conn)
            if ctx is None:
                return not_found(request)
            return render(request, template, active=active, **ctx)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            return render(request, "busy.html", 503, active=active)
        finally:
            conn.close()

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return not_found(request)
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)

    @app.get("/", response_class=HTMLResponse)
    def dashboard_page(request: Request):
        return with_db(request, "dashboard.html", "dashboard",
                       lambda c: {"d": queries.dashboard(c, settings, date.today())})

    @app.get("/orders", response_class=HTMLResponse)
    def orders_page(request: Request, status: str = "pending"):
        if status not in {k for k, _ in _ORDER_TABS}:
            return not_found(request)
        return with_db(request, "orders.html", "orders", lambda c: {
            "orders": queries.orders(c, status), "tab": status, "tabs": _ORDER_TABS})

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        return with_db(request, "runs.html", "runs", lambda c: {"runs": queries.runs(c)})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str, reason: str = ""):
        if not run_id.isdecimal():
            return not_found(request)

        def build(c):
            detail = queries.run_detail(c, int(run_id), reason or None)
            return None if detail is None else {"r": detail, "reason": reason}

        return with_db(request, "run_detail.html", "runs", build)

    @app.get("/products", response_class=HTMLResponse)
    def products_page(request: Request, q: str = ""):
        return with_db(request, "products.html", "products", lambda c: {
            "rows": queries.products(c, q or None), "q": q})

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return render(request, "settings.html", active="settings",
                      lines=queries.settings_lines(settings), quotes=queries.quotes(settings))

    return app
