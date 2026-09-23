"""SQLite 持久化（SQLAlchemy Core）。"""
from __future__ import annotations

import os

from sqlalchemy import (
    Column, DateTime, MetaData, String, Table, Text, create_engine, select,
)
from sqlalchemy.engine import Engine

from datetime import datetime, timezone

DB_PATH = os.environ.get("OCR_DB_PATH", "/data/ocr_sort.db")

metadata = MetaData()

tasks = Table(
    "tasks",
    metadata,
    Column("task_id", String, primary_key=True),
    Column("status", String, nullable=False, default="pending", index=True),
    Column("request_json", Text, nullable=False),
    Column("result_json", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc)),
    Column("started_at", DateTime, nullable=True),
    Column("finished_at", DateTime, nullable=True),
)

_engine: Engine | None = None


def get_engine() -> Engine:
    """惰性创建 engine；首次调用时确保目录存在并建表。"""
    global _engine
    if _engine is None:
        directory = os.path.dirname(os.path.abspath(DB_PATH))
        os.makedirs(directory, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        metadata.create_all(_engine)
    return _engine


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_task(task_id: str, request_json: str) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            tasks.insert().values(
                task_id=task_id,
                status="pending",
                request_json=request_json,
                created_at=datetime.now(timezone.utc),
            )
        )


def get_task(task_id: str) -> dict | None:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(tasks).where(tasks.c.task_id == task_id)
        ).mappings().first()
    return dict(row) if row else None


def mark_status(task_id: str, status: str, **fields) -> None:
    engine = get_engine()
    values = {"status": status}
    values.update(fields)
    with engine.begin() as conn:
        conn.execute(
            tasks.update().where(tasks.c.task_id == task_id).values(**values)
        )
