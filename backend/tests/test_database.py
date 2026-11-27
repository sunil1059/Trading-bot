import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, StatementError

from backend.core.clock import IST
from backend.database.models import Base, Order, Position
from backend.database.session import make_engine

NOW = datetime(2030, 1, 1, 10, 0, tzinfo=IST)


def _order(**kw):
    base = dict(client_order_id="c1", mode="paper", exchange="NFO", tradingsymbol="NIFTYTEST",
                instrument_token=1, side="BUY", purpose="ENTRY", order_type="LIMIT", quantity=65,
                limit_price=Decimal("100.50"), status="PENDING_SUBMIT", filled_quantity=0,
                created_at=NOW, updated_at=NOW)
    base.update(kw)
    return Order(**base)


def test_entry_must_be_buy(db_session):
    db_session.add(_order(side="SELL"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_exit_must_be_sell(db_session):
    db_session.add(_order(purpose="EXIT", side="BUY"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_client_order_id_unique(db_session):
    db_session.add(_order())
    db_session.commit()
    db_session.add(_order())
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_filled_cannot_exceed_quantity(db_session):
    db_session.add(_order(filled_quantity=130))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_position_cannot_be_short(db_session):
    db_session.add(Position(mode="paper", exchange="NFO", tradingsymbol="X", instrument_token=1,
                            underlying="NIFTY", option_type="CE", strike=Decimal("24000"),
                            expiry=date(2030, 1, 7), lot_size=65, quantity=-65,
                            avg_entry_price=Decimal("100"), status="OPEN", opened_at=NOW))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_roundtrip_keeps_timezone_and_decimal(db_session):
    db_session.add(_order())
    db_session.commit()
    o = db_session.query(Order).one()
    assert o.limit_price == Decimal("100.50")


def test_alembic_migration_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path}/mig.db"
    env = {"TB_DATABASE_URL": url, "PATH": ""}
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    engine = make_engine(url)
    migrated = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert migrated == set(Base.metadata.tables)
    r = subprocess.run([sys.executable, "-m", "alembic", "check"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_datetimes_come_back_timezone_aware(db_session):
    db_session.add(_order())
    db_session.commit()
    db_session.expire_all()
    o = db_session.query(Order).one()
    assert o.created_at.tzinfo is not None and o.created_at == NOW


def test_naive_datetime_rejected(db_session):
    db_session.add(_order(created_at=datetime(2030, 1, 1, 10, 0)))
    with pytest.raises(StatementError):
        db_session.commit()
