"""POST /api/tasks 的 Idempotency-Key 幂等测试。"""
import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest

from app import db
from tests.conftest import make_word


def _payload(words=None, page_width=1000, page_height=1400):
    return {
        "page_width": page_width,
        "page_height": page_height,
        "words": words if words is not None else [
            make_word(0, "hello", 100, 100, 80, 30),
            make_word(1, "world", 210, 100, 80, 30),
        ],
    }


def _create(client, payload, key=..., headers=None):
    if key is not ...:
        headers = {"Idempotency-Key": key} if key is not None else None
    return client.post("/api/tasks", json=payload, headers=headers)


def _wait_done(client, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError("task did not finish in time")


def test_without_header_each_request_creates_new_task(client):
    r1 = _create(client, _payload(), key=None)
    r2 = _create(client, _payload(), key=None)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_same_key_same_body_replays_original_task(client):
    payload = _payload()
    r1 = _create(client, payload, key="k-replay-1")
    assert r1.status_code == 201
    first = r1.json()
    assert first["status"] == "pending"

    r2 = _create(client, payload, key="k-replay-1")
    assert r2.status_code == 200  # 重试返回 200 而非 201
    second = r2.json()
    assert second["task_id"] == first["task_id"]

    body = _wait_done(client, first["task_id"])
    assert body["status"] == "done"

    # 任务完成后再重试：返回原 task_id 与最新 status，仍不重复入队
    r3 = _create(client, payload, key="k-replay-1")
    assert r3.status_code == 200
    assert r3.json() == {"task_id": first["task_id"], "status": "done"}

    rec = db.get_idempotency_record("k-replay-1")
    assert rec is not None
    assert rec["task_id"] == first["task_id"]
    # 摘要是规范化后请求体的 SHA-256（64 位十六进制）
    from app.schemas import SortRequest
    canonical = SortRequest.model_validate(payload).model_dump_json()
    assert rec["request_hash"] == db.request_digest(canonical)


def test_same_key_different_body_conflicts(client):
    r1 = _create(client, _payload(page_width=1000), key="k-conflict-1")
    assert r1.status_code == 201

    r2 = _create(client, _payload(page_width=1001), key="k-conflict-1")
    assert r2.status_code == 409
    assert "Idempotency-Key" in r2.json()["detail"]

    # 冲突不影响原任务，且原 key 仍然可用（同体重试仍返回原任务）
    r3 = _create(client, _payload(page_width=1000), key="k-conflict-1")
    assert r3.status_code == 200
    assert r3.json()["task_id"] == r1.json()["task_id"]


def test_distinct_keys_create_distinct_tasks(client):
    payload = _payload()
    r1 = _create(client, payload, key="k-a")
    r2 = _create(client, payload, key="k-b")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_blank_header_is_treated_as_absent(client):
    payload = _payload()
    r1 = client.post(
        "/api/tasks", json=payload, headers={"Idempotency-Key": "   "}
    )
    r2 = _create(client, payload, key="   ")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_superficial_json_differences_share_the_digest(client):
    """字段顺序/空白不同但语义相同的请求体视为相同请求。"""
    key = "k-canonical-1"
    r1 = client.post(
        "/api/tasks",
        content=json.dumps(_payload(page_width=1000)),
        headers={"Idempotency-Key": key, "Content-Type": "application/json"},
    )
    payload = _payload(page_width=1000)
    r2 = client.post(
        "/api/tasks",
        content=json.dumps(
            {k: payload[k] for k in reversed(list(payload.keys()))},
            indent=2,
        ),
        headers={"Idempotency-Key": key, "Content-Type": "application/json"},
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.json()["task_id"] == r1.json()["task_id"]


def test_concurrent_same_key_creates_single_task(client):
    payload = _payload()
    results = []
    errors = []

    def submit():
        try:
            results.append(_create(client, payload, key="k-concurrent-1"))
        except Exception as exc:  # pragma: no cover - 仅用于暴露线程内失败
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 8
    # 恰好一个 201（实际创建），其余全部 200（幂等重试）
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200] * 7 + [201], statuses
    task_ids = {r.json()["task_id"] for r in results}
    assert len(task_ids) == 1
    task_id = task_ids.pop()

    body = _wait_done(client, task_id)
    assert body["status"] == "done"

    # 数据库中同样只落了一个任务记录
    assert db.get_idempotency_record("k-concurrent-1")["task_id"] == task_id


def test_idempotency_table_created_on_legacy_database():
    """旧版本数据库（只有 tasks 表）首次启动后自动补齐幂等表并可用。"""
    tmpdir = tempfile.mkdtemp()
    legacy_path = os.path.join(tmpdir, "legacy.db")

    conn = sqlite3.connect(legacy_path)
    try:
        # 按旧版 schema 手工建表，模拟升级前已存在的数据库
        conn.execute(
            "CREATE TABLE tasks ("
            "task_id VARCHAR NOT NULL PRIMARY KEY, "
            "status VARCHAR NOT NULL, "
            "request_json TEXT NOT NULL, "
            "result_json TEXT, error TEXT, "
            "created_at DATETIME, started_at DATETIME, finished_at DATETIME)"
        )
        conn.execute(
            "INSERT INTO tasks (task_id, status, request_json, created_at) "
            "VALUES ('legacy-task', 'done', '{}', '2026-01-01 00:00:00')"
        )
        conn.commit()
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {"tasks"}
    finally:
        conn.close()

    old_path = os.environ.get("OCR_DB_PATH")
    old_engine = db._engine
    os.environ["OCR_DB_PATH"] = legacy_path
    db._engine = None
    try:
        created_id = db.create_idempotent_task(
            "legacy-key", "new-task",
            json.dumps(_payload(), ensure_ascii=False),
        )
        assert created_id == "new-task"
        replayed = db.create_idempotent_task(
            "legacy-key", "another-task",
            json.dumps(_payload(), ensure_ascii=False),
        )
        assert replayed == "new-task"  # 不重复创建
        # 旧数据完好
        assert db.get_task("legacy-task")["status"] == "done"
        assert db.get_task("new-task") is not None

        with pytest.raises(db.IdempotencyConflict):
            db.create_idempotent_task(
                "legacy-key", "third-task",
                json.dumps(_payload(page_width=999), ensure_ascii=False),
            )
    finally:
        if old_path is None:
            os.environ.pop("OCR_DB_PATH", None)
        else:
            os.environ["OCR_DB_PATH"] = old_path
        if old_engine is not None:
            db._engine = old_engine
        else:
            db.reset_engine()
