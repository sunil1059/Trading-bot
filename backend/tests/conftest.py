import pytest
from sqlalchemy.orm import Session

from backend.database.models import Base
from backend.database.session import make_engine, make_session_factory


@pytest.fixture
def db_session(tmp_path) -> Session:
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s
    engine.dispose()
