import logging
import sys
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    Base,
    PayrollImportColumnMap,
    PayrollImportRawRow,
    PayrollImportRun,
    PayrollImportSnapshot,
    PayrollImportSynonym,
)

logger = logging.getLogger(__name__)
_engine = None
_SessionLocal = None

# Tables owned by this app (aligned with Laravel `2026_06_04_120000_create_payroll_import_tables`).
_PAYROLL_IMPORT_TABLES = (
    PayrollImportRun.__table__,
    PayrollImportSynonym.__table__,
    PayrollImportRawRow.__table__,
    PayrollImportColumnMap.__table__,
    PayrollImportSnapshot.__table__,
)


def ensure_payroll_import_schema(engine) -> None:
    """Create payroll_import_* tables if missing (no Laravel migration required)."""
    insp = inspect(engine)
    names = [t.name for t in _PAYROLL_IMPORT_TABLES]
    if all(insp.has_table(n) for n in names):
        return
    missing = [n for n in names if not insp.has_table(n)]
    logger.warning(
        "Creating missing CSV import tables (Python auto-DDL): %s",
        ", ".join(missing),
    )
    Base.metadata.create_all(bind=engine, tables=list(_PAYROLL_IMPORT_TABLES))


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        if not s.mysql_database and not s.database_url:
            if getattr(sys, "frozen", False):
                raise RuntimeError(
                    "Set MYSQL_DATABASE or DATABASE_URL in a `.env` file next to this program's .exe "
                    "(copy `.env.example` to `.env` and edit). "
                    "Remove conflicting empty MYSQL_* / DATABASE_URL from Windows environment variables if needed."
                )
            raise RuntimeError(
                "Set MYSQL_DATABASE or DATABASE_URL in csv-payslip-import/.env. "
                "If the file is correct, remove a conflicting empty MYSQL_DATABASE "
                "from Windows user or system environment variables."
            )
        _engine = create_engine(
            s.sqlalchemy_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        ensure_payroll_import_schema(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_session() -> Generator[Session, None, None]:
    """SELECT-only: always rollback (no commit). Avoids rare commit failures on read-only pages."""
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def get_db() -> Generator[Session, None, None]:
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Database session failed (during request or commit)")
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
