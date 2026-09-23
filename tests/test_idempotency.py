"""POST /api/tasks 的 Idempotency-Key 幂等行为测试。"""
import concurrent.futures
import time

from sqlalchemy import select

from app import db
from tests.conftest import make_word


def _body(marker="idem", page_width=1000, page_height=1400):
    return {
        "page_width": page_width,
        "page_height": page_height,
        "words": [make_word(0, marker, 100, 100, 120, 30)],
    }


def _wait(client, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("task did not finish in time")


def test_retry_same_key_same_body_returns_original_task(client):
    key = "retry-key-1"
    r1 = client.post("/api/tasks", json=_body("A"),
                     headers={"Idempotency-Key": key})
    assert r1.status_code == 201, r1.text
    t1 = r1.json()
    assert t1["status"] == "pending"

    # 等到任务完成后重试，应返回完成态而不是 pending
    done = _wait(client, t1["task_id"])
    assert done["status"] == "done"

    r2 = client.post("/api/tasks", json=_body("A"),
                     headers={"Idempotency-Key": key})
    assert r2.status_code == 200, r2.text
    t2 = r2.json()
    assert t2["task_id"] == t1["task_id"]
    assert t2["status"] == "done"

    # 再来一次，仍然是同一个任务
    r3 = client.post("/api/tasks", json=_body("A"),
                     headers={"Idempotency-Key": key})
    assert r3.status_code == 200
    assert r3.json()["task_id"] == t1["task_id"]


def test_same_key_different_body_conflicts(client):
    key = "conflict-key-1"
    r1 = client.post("/api/tasks", json=_body("B"),
                     headers={"Idempotency-Key": key})
    assert r1.status_code == 201
    task_id = r1.json()["task_id"]

    r2 = client.post("/api/tasks", json=_body("DIFFERENT"),
                     headers={"Idempotency-Key": key})
    assert r2.status_code == 409
    assert task_id in r2.json()["detail"]

    # 页面尺寸不同也算不同请求体
    r3 = client.post("/api/tasks", json=_body("B", page_width=999),
                     headers={"Idempotency-Key": key})
    assert r3.status_code == 409

    # 冲突后用原请求体仍可正常重放
    r4 = client.post("/api/tasks", json=_body("B"),
                     headers={"Idempotency-Key": key})
    assert r4.status_code == 200
    assert r4.json()["task_id"] == task_id


def test_canonical_body_equivalence(client):
    """省略可选字段 / 字段顺序变化不影响摘要（服务端规范化后等价）。"""
    key = "canonical-key-1"
    r1 = client.post(
        "/api/tasks",
        json={"words": [
            {"quad": [{"x": 0, "y": 0}, {"x": 10, "y": 0},
                      {"x": 10, "y": 10}, {"x": 0, "y": 10}],
             "text": "C"}],
              "page_height": 1400, "page_width": 1000},
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201, r1.text
    task_id = r1.json()["task_id"]

    r2 = client.post(
        "/api/tasks",
        json={  # 字段顺序不同，且显式给出 confidence: null
            "page_width": 1000,
            "page_height": 1400,
            "words": [
                {"text": "C", "confidence": None, "id": None,
                 "quad": [{"x": 0, "y": 0}, {"x": 10, "y": 0},
                          {"x": 10, "y": 10}, {"x": 0, "y": 10}]}
            ],
        },
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["task_id"] == task_id


def test_different_keys_create_separate_tasks(client):
    r1 = client.post("/api/tasks", json=_body("D"),
                     headers={"Idempotency-Key": "key-a"})
    r2 = client.post("/api/tasks", json=_body("D"),
                     headers={"Idempotency-Key": "key-b"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_without_key_keeps_legacy_behavior(client):
    r1 = client.post("/api/tasks", json=_body("E"))
    r2 = client.post("/api/tasks", json=_body("E"))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_blank_key_is_ignored(client):
    r1 = client.post("/api/tasks", json=_body("F"),
                     headers={"Idempotency-Key": "   "})
    r2 = client.post("/api/tasks", json=_body("F"),
                     headers={"Idempotency-Key": "   "})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_overlong_key_rejected(client):
    r = client.post("/api/tasks", json=_body("G"),
                    headers={"Idempotency-Key": "k" * 256})
    assert r.status_code == 400
    assert "Idempotency-Key" in r.json()["detail"]


def test_concurrent_same_key_creates_single_task(client):
    key = "concurrent-key-1"
    body = _body("concurrent-marker")
    n = 8

    def submit():
        return client.post("/api/tasks", json=body,
                           headers={"Idempotency-Key": key})

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(lambda _: submit(), range(n)))

    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == n - 1, statuses
    task_ids = {r.json()["task_id"] for r in responses}
    assert len(task_ids) == 1

    # 库里也只有一个任务承载该请求体
    engine = db.get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.tasks.c.task_id)
            .where(db.tasks.c.request_json.like("%concurrent-marker%"))
        ).all()
    assert len(rows) == 1

    # 唯一任务可以正常完成，说明只入队了一次后台处理
    done = _wait(client, task_ids.pop())
    assert done["status"] == "done"


def test_concurrent_same_key_different_bodies(client):
    """并发同 key 不同体：一个创建，其余均 409，绝不出现第二个任务。"""
    key = "concurrent-conflict-key-1"
    bodies = [_body(f"race-body-{i}") for i in range(6)]

    def submit(body):
        return client.post("/api/tasks", json=body,
                           headers={"Idempotency-Key": key})

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, bodies))

    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 5, statuses
    created = next(r for r in responses if r.status_code == 201)
    winner_id = created.json()["task_id"]
    for r in responses:
        if r.status_code == 409:
            assert winner_id in r.json()["detail"]

    # 幂等登记表与任务表一致：键只指向胜出任务
    rec = db.get_idempotency(key)
    assert rec is not None and rec["task_id"] == winner_id
