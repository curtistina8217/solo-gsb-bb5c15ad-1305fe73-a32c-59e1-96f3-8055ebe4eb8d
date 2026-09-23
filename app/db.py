"""SQLite 持久化（SQLAlchemy Core）。"""
from __future__ import annotations

import os

from sqlalchemy import (
    Column, DateTime, MetaData, String, Table, Text, create_engine, select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from datetime import datetime, timezone

DB_PATH = os.environ.get("OCR_DB_PATH", "/data/ocr_sort.db")

# Idempotency-Key 的最大长度；与持久化列宽保持一致
IDEMPOTENCY_KEY_MAX_LEN = 255

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

# 幂等键登记表：key 唯一主键保证同 key 并发请求只胜出一个
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("key", String(IDEMPOTENCY_KEY_MAX_LEN), primary_key=True),
    Column("task_id", String, nullable=False),
    # 规范化请求体的 sha256 摘要（hex，64 字符），不保存额外副本
    Column("request_hash", String(64), nullable=False),
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc)),
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


def get_idempotency(key: str) -> dict | None:
    """按幂等键回读登记记录；不存在返回 None。"""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(idempotency_keys).where(idempotency_keys.c.key == key)
        ).mappings().first()
    return dict(row) if row else None


class IdempotencyMismatch(Exception):
    """同 key 已登记，但请求体摘要不一致（HTTP 409）。"""

    def __init__(self, existing_task_id: str):
        self.existing_task_id = existing_task_id
        super().__init__(f"request body mismatch for existing task {existing_task_id}")


def reserve_idempotent_task(key: str, task_id: str, request_json: str,
                            request_hash: str) -> dict:
    """原子地把幂等键绑定到新任务（同事务写 idempotency_keys 与 tasks）。

    返回：
      {"outcome": "created"}  本次调用成功登记，调用方负责入队后台任务；
      {"outcome": "replay", "task_id": ..., "request_hash": ...}
        键已存在且摘要一致，返回原任务；
      {"outcome": "conflict", "task_id": ...}
        键已存在但摘要不一致，调用方应返回 409。

    依赖 key 的唯一主键：并发相同请求中只有一个事务能插入成功，
    其余在 IntegrityError 后回读胜者记录，因此只会创建一个任务。
    """
    engine = get_engine()
    created_at = datetime.now(timezone.utc)
    try:
        with engine.begin() as conn:
            existing = conn.execute(
                select(idempotency_keys).where(idempotency_keys.c.key == key)
            ).mappings().first()
            if existing is not None:
                # 事务在 begin() 内回滚，无任何写入
                if existing["request_hash"] == request_hash:
                    return {"outcome": "replay",
                            "task_id": existing["task_id"],
                            "request_hash": existing["request_hash"]}
                return {"outcome": "conflict",
                        "task_id": existing["task_id"]}
            conn.execute(
                idempotency_keys.insert().values(
                    key=key,
                    task_id=task_id,
                    request_hash=request_hash,
                    created_at=created_at,
                )
            )
            conn.execute(
                tasks.insert().values(
                    task_id=task_id,
                    status="pending",
                    request_json=request_json,
                    created_at=created_at,
                )
            )
        return {"outcome": "created"}
    except IntegrityError:
        # 并发场景：另一事务刚抢先登记了同 key，回读胜者记录即可
        row = get_idempotency(key)
        if row is None:  # 理论不可达：冲突键必有记录
            raise
        if row["request_hash"] == request_hash:
            return {"outcome": "replay",
                    "task_id": row["task_id"],
                    "request_hash": row["request_hash"]}
        return {"outcome": "conflict", "task_id": row["task_id"]}
