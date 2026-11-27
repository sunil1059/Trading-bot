from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.security import hash_password, verify_password
from backend.core.config import AppSettings, TradingMode
from backend.services.runtime import LiveModeUnavailable, Runtime

PW = "correct horse battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = AppSettings(_env_file=None, database_url=f"sqlite:///{tmp_path}/api.db",
                    dashboard_secret_key="k" * 40, dashboard_password_hash=hash_password(PW, n=2**10))
    from backend.database.models import Base
    from backend.database.session import make_engine
    Base.metadata.create_all(make_engine(s.database_url))
    app = create_app(s, frontend_dir=tmp_path / "none")
    return TestClient(app)


def _login(c):
    r = c.post("/api/login", json={"password": PW})
    assert r.status_code == 200
    return r.json()["csrf"]


def test_password_hash():
    h = hash_password("abc", n=2**10)
    assert verify_password("abc", h) and not verify_password("abd", h)


def test_requires_login(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/controls/emergency-stop").status_code == 401


def test_wrong_password_throttled(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": PW}).status_code == 429


def test_csrf_required_for_controls(client):
    csrf = _login(client)
    assert client.post("/api/controls/pause").status_code == 403
    r = client.post("/api/controls/pause", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409  # engine not running until Kite login


def test_status_before_kite_login(client):
    _login(client)
    body = client.get("/api/status").json()
    assert body["state"] == "awaiting_login" and body["mode"] == "paper" and body["live_enabled"] is False
    assert client.get("/api/performance").json()["metrics"]["total_trades"] == 0


def test_confirmation_phrases(client):
    csrf = _login(client)
    client.app.state.runtime.engine = SimpleNamespace(close_all=lambda u: 1, reset_emergency=lambda u: None)
    h = {"X-CSRF-Token": csrf}
    assert client.post("/api/controls/close-all", json={"confirm": "yes"}, headers=h).status_code == 400
    assert client.post("/api/controls/close-all", json={"confirm": "CLOSE ALL"}, headers=h).json() == {"closed": 1}


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Cache-Control"] == "no-store"


def test_live_mode_refused(tmp_path):
    s = AppSettings(_env_file=None, mode=TradingMode.LIVE, live_trading_enabled=True,
                    database_url=f"sqlite:///{tmp_path}/x.db")
    with pytest.raises(LiveModeUnavailable):
        Runtime(s)


def test_api_refuses_to_start_without_auth_config(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(AppSettings(_env_file=None, database_url=f"sqlite:///{tmp_path}/y.db"))



def test_kite_login_flow_requires_matching_state(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, unquote, urlparse

    import backend.brokers.zerodha as z

    monkeypatch.chdir(tmp_path)
    s = AppSettings(_env_file=None, database_url=f"sqlite:///{tmp_path}/api.db", kite_api_key="x",
                    kite_api_secret="y", dashboard_secret_key="k" * 40,
                    dashboard_password_hash=hash_password(PW, n=2**10))
    from backend.database.models import Base
    from backend.database.session import make_engine
    Base.metadata.create_all(make_engine(s.database_url))
    c = TestClient(create_app(s, frontend_dir=tmp_path / "none"))
    monkeypatch.setattr(z, "login_url", lambda key: "https://kite.zerodha.com/connect/login?v=3&api_key=x")
    exchanged = []
    monkeypatch.setattr(z, "exchange_request_token", lambda k, sec, t: exchanged.append(t) or "tok")
    started = []
    monkeypatch.setattr(Runtime, "start_with_token", lambda self, tok: started.append(tok))
    _login(c)

    def new_state():
        r = c.get("/auth/kite/login", follow_redirects=False)
        assert r.status_code in (302, 307)
        q = parse_qs(urlparse(r.headers["location"]).query)
        return parse_qs(unquote(q["redirect_params"][0]))["state"][0]

    state = new_state()
    assert c.get("/auth/kite/callback?status=success&request_token=abc&state=bad",
                 follow_redirects=False).status_code == 400
    # single use: the failed attempt consumed it
    assert c.get(f"/auth/kite/callback?status=success&request_token=abc&state={state}",
                 follow_redirects=False).status_code == 400
    assert exchanged == [] and started == []
    state = new_state()
    r = c.get(f"/auth/kite/callback?status=success&request_token=abc&state={state}", follow_redirects=False)
    assert r.status_code in (302, 307) and exchanged == ["abc"] and started == ["tok"]
