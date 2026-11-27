"""FastAPI dashboard API.

Security: every /api route except /api/health and /api/login needs a logged-in session;
every state-changing route also needs the CSRF token (X-CSRF-Token header) issued at
login. Destructive controls need a typed confirmation phrase. The server binds to
localhost by default. Emergency controls do not depend on any AI service.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, select
from starlette.middleware.sessions import SessionMiddleware

from backend.api.security import LoginThrottle, new_csrf_token, verify_password
from backend.core.config import PROJECT_ROOT, AppSettings
from backend.database.models import BrokerEvent, Order, RiskEvent, Signal, Trade
from backend.services.engine import EngineError
from backend.services.metrics import TradeResult, compute_metrics
from backend.services.runtime import Runtime

FRONTEND_DIR = PROJECT_ROOT / "frontend" / "out"


class LoginBody(BaseModel):
    password: str


class ConfirmBody(BaseModel):
    confirm: str = ""


def _j(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def create_app(settings: AppSettings, runtime_factory: Callable[[AppSettings], Runtime] = Runtime,
               frontend_dir: Path = FRONTEND_DIR) -> FastAPI:
    if settings.dashboard_secret_key is None or settings.dashboard_password_hash is None:
        raise RuntimeError("Set TB_DASHBOARD_SECRET_KEY and TB_DASHBOARD_PASSWORD_HASH before starting the API")
    runtime = runtime_factory(settings)
    throttle = LoginThrottle()
    app = FastAPI(title="Trading Bot", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.dashboard_secret_key.get_secret_value(),
                       session_cookie="tb_session", max_age=12 * 3600, same_site="lax",
                       https_only=settings.dashboard_cookie_secure)
    app.state.runtime = runtime

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def user(request: Request) -> str:
        u = request.session.get("user")
        if not u:
            raise HTTPException(401, "login required")
        return u

    def csrf(request: Request, u: str = Depends(user)) -> str:
        token = request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token, request.session.get("csrf", "")):
            raise HTTPException(403, "bad CSRF token")
        return u

    # --- auth -------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request) -> dict[str, str]:
        key = request.client.host if request.client else "unknown"
        if not throttle.allow(key):
            raise HTTPException(429, "too many attempts, wait 5 minutes")
        assert settings.dashboard_password_hash is not None
        if not verify_password(body.password, settings.dashboard_password_hash.get_secret_value()):
            throttle.fail(key)
            raise HTTPException(401, "invalid password")
        throttle.reset(key)
        request.session.clear()
        request.session["user"] = "owner"
        request.session["csrf"] = new_csrf_token()
        return {"csrf": request.session["csrf"]}

    @app.post("/api/logout")
    def logout(request: Request, u: str = Depends(csrf)) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/session")
    def session(request: Request, u: str = Depends(user)) -> dict[str, str]:
        return {"user": u, "csrf": request.session.get("csrf", "")}

    # --- Kite daily login (OAuth-style redirect) -----------------------------------
    @app.get("/auth/kite/login")
    def kite_login(request: Request, u: str = Depends(user)) -> RedirectResponse:
        from urllib.parse import quote

        from backend.brokers.zerodha import login_url

        if settings.kite_api_key is None:
            raise HTTPException(400, "TB_KITE_API_KEY not configured")
        # One-time state, returned by Kite via redirect_params, ties the callback to this login.
        state = secrets.token_urlsafe(16)
        request.session["kite_state"] = state
        url = login_url(settings.kite_api_key.get_secret_value())
        return RedirectResponse(f"{url}&redirect_params={quote(f'state={state}')}")

    @app.get("/auth/kite/callback")
    def kite_callback(request: Request, request_token: str = "", status: str = "", state: str = "",
                      u: str = Depends(user)) -> RedirectResponse:
        from backend.brokers.zerodha import exchange_request_token

        expected = request.session.pop("kite_state", None)
        if not expected or not secrets.compare_digest(state, expected):
            raise HTTPException(400, "login state mismatch: start the Kite login from the dashboard")
        if status != "success" or not request_token.isalnum():
            raise HTTPException(400, "login was not successful")
        if settings.kite_api_key is None or settings.kite_api_secret is None:
            raise HTTPException(400, "Kite credentials not configured")
        token = exchange_request_token(settings.kite_api_key.get_secret_value(),
                                       settings.kite_api_secret.get_secret_value(), request_token)
        try:
            runtime.start_with_token(token)
        except Exception as e:  # noqa: BLE001
            runtime.last_error = str(e)
            raise HTTPException(500, f"engine failed to start: {e}") from e
        return RedirectResponse("/")

    # --- read views -----------------------------------------------------------------
    @app.get("/api/status")
    def status(u: str = Depends(user)) -> dict[str, Any]:
        base = {"state": runtime.state, "mode": settings.mode.value, "live_enabled": False,
                "last_error": runtime.last_error}
        if runtime.engine is not None:
            base.update(runtime.engine.snapshot())
        else:
            base["alerts"] = [{"ts": a.ts.isoformat(), "event": a.event, "severity": a.severity,
                               "message": a.message} for a in list(runtime.notifier.alerts)[:50]]
        return base

    @app.get("/api/signals")
    def signals(limit: int = 50, u: str = Depends(user)) -> list[dict[str, Any]]:
        with runtime.sessions() as s:
            rows = s.scalars(select(Signal).where(Signal.mode == settings.mode.value)
                             .order_by(desc(Signal.ts)).limit(min(limit, 500)))
            return [{"id": r.id, "ts": _j(r.ts), "direction": r.direction, "score": r.score, "status": r.status,
                     "reason": r.reason, "details": r.details} for r in rows]

    @app.get("/api/trades")
    def trades(limit: int = 200, u: str = Depends(user)) -> list[dict[str, Any]]:
        with runtime.sessions() as s:
            rows = s.scalars(select(Trade).where(Trade.mode == settings.mode.value)
                             .order_by(desc(Trade.exit_time)).limit(min(limit, 2000)))
            return [{k: _j(getattr(r, k)) for k in ("id", "entry_time", "exit_time", "quantity", "avg_entry_price",
                                                    "avg_exit_price", "gross_pnl", "charges", "net_pnl",
                                                    "exit_reason")} for r in rows]

    @app.get("/api/performance")
    def performance(u: str = Depends(user)) -> dict[str, Any]:
        with runtime.sessions() as s:
            rows = list(s.scalars(select(Trade).where(Trade.mode == settings.mode.value).order_by(Trade.exit_time)))
        results = [TradeResult(r.entry_time, r.exit_time, float(r.net_pnl), r.exit_reason) for r in rows]
        m = compute_metrics(results, float(runtime.risk_cfg.capital.total_capital))
        daily: dict[str, float] = {}
        for t in results:
            daily[t.exit_time.date().isoformat()] = daily.get(t.exit_time.date().isoformat(), 0.0) + t.net_pnl
        return {"metrics": m, "daily_pnl": [{"date": k, "pnl": v} for k, v in sorted(daily.items())],
                "disclaimer": "Past paper results do not predict future returns."}

    @app.get("/api/activity")
    def activity(limit: int = 100, u: str = Depends(user)) -> list[dict[str, Any]]:
        n = min(limit, 500)
        out: list[dict[str, Any]] = []
        with runtime.sessions() as s:
            for r in s.scalars(select(Signal).order_by(desc(Signal.ts)).limit(n)):
                out.append({"ts": _j(r.ts), "kind": f"SIGNAL_{r.status}", "message": f"{r.direction} score "
                            f"{r.score}: {r.reason}"})
            for r in s.scalars(select(Order).order_by(desc(Order.updated_at)).limit(n)):
                out.append({"ts": _j(r.updated_at), "kind": f"ORDER_{r.status}", "message":
                            f"{r.purpose} {r.side} {r.quantity} {r.tradingsymbol} @ {r.limit_price or 'MKT'} "
                            f"filled {r.filled_quantity} {r.status_message or ''}".strip()})
            for r in s.scalars(select(RiskEvent).order_by(desc(RiskEvent.ts)).limit(n)):
                out.append({"ts": _j(r.ts), "kind": f"RISK_{r.kind}", "message": r.message})
            for r in s.scalars(select(BrokerEvent).order_by(desc(BrokerEvent.ts)).limit(n)):
                out.append({"ts": _j(r.ts), "kind": f"BROKER_{r.kind}", "message": r.message})
        return sorted(out, key=lambda x: x["ts"], reverse=True)[:n]

    # --- controls (work without AI; need CSRF) ----------------------------------------
    def eng():  # type: ignore[no-untyped-def]
        if runtime.engine is None:
            raise HTTPException(409, "engine not running (log in to Kite first)")
        return runtime.engine

    @app.post("/api/controls/pause")
    def pause(u: str = Depends(csrf)) -> dict[str, bool]:
        eng().pause(u)
        return {"ok": True}

    @app.post("/api/controls/resume")
    def resume(body: ConfirmBody, u: str = Depends(csrf)) -> dict[str, bool]:
        if body.confirm != "RESUME":
            raise HTTPException(400, 'type "RESUME" to confirm')
        try:
            eng().resume(u)
        except EngineError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.post("/api/controls/emergency-stop")
    def emergency(u: str = Depends(csrf)) -> dict[str, Any]:
        return eng().emergency_stop(u)

    @app.post("/api/controls/close-all")
    def close_all(body: ConfirmBody, u: str = Depends(csrf)) -> dict[str, int]:
        if body.confirm != "CLOSE ALL":
            raise HTTPException(400, 'type "CLOSE ALL" to confirm')
        return {"closed": eng().close_all(u)}

    @app.post("/api/controls/cancel-all")
    def cancel_all(u: str = Depends(csrf)) -> dict[str, int]:
        return {"cancelled": eng().cancel_all(u)}

    @app.post("/api/controls/reset-emergency")
    def reset(body: ConfirmBody, u: str = Depends(csrf)) -> dict[str, bool]:
        if body.confirm != "RESET":
            raise HTTPException(400, 'type "RESET" to confirm')
        eng().reset_emergency(u)
        return {"ok": True}

    @app.post("/api/controls/disable-live")
    def disable_live(u: str = Depends(csrf)) -> dict[str, str]:
        # LIVE cannot be enabled in this build; endpoint exists so the control is always available.
        return {"mode": settings.mode.value, "live_enabled": "false"}

    @app.exception_handler(HTTPException)
    async def http_err(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    if frontend_dir.exists():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="dashboard")
    return app
