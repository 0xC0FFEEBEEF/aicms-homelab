from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import AppConfig
from app.db_migrations import migrate_database
from app.models import Base


def create_engine_for_config(config: AppConfig):
    connect_args = {}
    if config.resolved_database_url.startswith("sqlite:///"):
        connect_args["check_same_thread"] = False

    return create_engine(
        config.resolved_database_url,
        connect_args=connect_args,
        future=True,
    )


def init_database(config: AppConfig) -> sessionmaker[Session]:
    config.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    config.resolved_logs_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine_for_config(config)
    migrate_database(engine)
    Base.metadata.create_all(engine)

    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
