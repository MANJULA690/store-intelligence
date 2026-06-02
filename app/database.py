"""
database.py — SQLite schema via SQLAlchemy.
All events, sessions, and POS transactions stored here.
"""

import json
import os
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import (
    Boolean, Column, Float, Integer, String, Text,
    UniqueConstraint, create_engine, event as sa_event,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DB_PATH = os.getenv("DB_PATH", "store_intelligence.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

# Enable WAL mode for better concurrency
@sa_event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class EventRecord(Base):
    """Stores one row per detection event emitted by the pipeline."""
    __tablename__ = "events"

    id            = Column(Integer, primary_key=True, index=True)
    event_id      = Column(String, unique=True, nullable=False, index=True)
    store_id      = Column(String, nullable=False, index=True)
    camera_id     = Column(String, nullable=False)
    visitor_id    = Column(String, nullable=False, index=True)
    event_type    = Column(String, nullable=False, index=True)
    timestamp     = Column(String, nullable=False, index=True)
    zone_id       = Column(String, nullable=True)
    dwell_ms      = Column(Integer, default=0)
    is_staff      = Column(Boolean, default=False, index=True)
    confidence    = Column(Float, nullable=False)
    metadata_json = Column(Text, nullable=True)
    created_at    = Column(String, default=lambda: _now())

    def metadata_dict(self) -> dict:
        if self.metadata_json:
            try:
                return json.loads(self.metadata_json)
            except Exception:
                return {}
        return {}

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_id"),
    )


class POSTransaction(Base):
    """One row per unique POS order."""
    __tablename__ = "pos_transactions"

    id               = Column(Integer, primary_key=True, index=True)
    store_id         = Column(String, nullable=False, index=True)
    order_id         = Column(String, unique=True, nullable=False)
    timestamp        = Column(String, nullable=False, index=True)   # UTC ISO-8601
    basket_value_inr = Column(Float, nullable=False)
    created_at       = Column(String, default=lambda: _now())

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_order_id"),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a DB session, closes on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
