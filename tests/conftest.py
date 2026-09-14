import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("AC_ENV_FILE", "/nonexistent")
os.environ.setdefault("AC_SECRET_KEY", "test-secret-key-for-tests-only")
os.environ.setdefault("AC_DATABASE_URL", "sqlite://")
os.environ.setdefault("AC_RUN_ENGINE_IN_WEB", "false")
os.environ.setdefault("AC_BOOTSTRAP_PASSWORD", "")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with maker() as s:
        yield s
    Base.metadata.drop_all(engine)
