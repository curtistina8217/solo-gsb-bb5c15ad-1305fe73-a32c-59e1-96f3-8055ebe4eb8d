"""SQLite 持久化（SQLAlchemy Core）。"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, MetaData, String, Table, Text, create_engine, select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

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

# Idempotency-Key -> 已创建任务；key 上的唯一约束保证并发同 key 只入队一次
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("key", String(255), primary_key=True),
    Column("task_id", String, nullable=False, index=True),
    Column("request_hash", String(64), nullable=False),
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc)),
)

_engine: Engine | None = None


def get_engine() -> Engine:
    """惰性创建 engine；首次调用时确保目录存在并建表。

    metadata.create_all 只补建缺失表，不改动已有表结构，因此对旧版本
    留下的 SQLite 数据库完全兼容。
    """
    global _engine
    if _engine is None:
        # 动态读取环境变量，便于测试切换数据库；DB_PATH 仅作默认值参考
        db_path = os.environ.get("OCR_DB_PATH", DB_PATH)
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )
        metadata.create_all(_engine)
    return _engine


def reset_engine() -> None:
    """丢弃缓存 engine（主要供测试切换数据库路径后重建）。"""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_digest(request_json: str) -> str:
    """规范化请求体（Pydantic 重序列化后的 JSON）的 SHA-256 摘要。"""
    return hashlib.sha256(request_json.encode("utf-8")).hexdigest()


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


class IdempotencyConflict(Exception):
    """同一 Idempotency-Key 被用于不同的请求体。"""

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"idempotency key reused with a different request: {key!r}")


def create_idempotent_task(key: str, task_id: str, request_json: str) -> str:
    """按 Idempotency-Key 幂等创建任务。

    在同一事务内插入 tasks 与 idempotency_keys 两行：
    - key 首次出现：两行插入成功，返回新 task_id（调用方负责入队）；
    - key 已存在且请求摘要一致：插入触发唯一约束冲突，回滚后返回
      原 task_id（重试，不重复入队）；
    - key 已存在但请求摘要不同：抛出 IdempotencyConflict（409）。

    依赖 key 主键的唯一约束串行化并发请求，无需先 SELECT 再 INSERT，
    因而并发相同请求也只会有一个事务创建任务。
    """
    digest = request_digest(request_json)
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                tasks.insert().values(
                    task_id=task_id,
                    status="pending",
                    request_json=request_json,
                    created_at=datetime.now(timezone.utc),
                )
            )
            conn.execute(
                idempotency_keys.insert().values(
                    key=key,
                    task_id=task_id,
                    request_hash=digest,
                    created_at=datetime.now(timezone.utc),
                )
            )
    except IntegrityError:
        existing = get_idempotency_record(key)
        if existing is None:
            # 冲突不来自 idempotency_keys（例如极小概率的 task_id 碰撞），原样抛出
            raise
        if existing["request_hash"] != digest:
            raise IdempotencyConflict(key)
        return existing["task_id"]
    return task_id


def get_idempotency_record(key: str) -> dict | None:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(idempotency_keys).where(idempotency_keys.c.key == key)
        ).mappings().first()
    return dict(row) if row else None
