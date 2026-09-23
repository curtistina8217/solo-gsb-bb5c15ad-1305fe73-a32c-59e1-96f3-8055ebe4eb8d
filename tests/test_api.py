"""任务接口端到端测试。"""
import time

from tests.conftest import make_word


def _submit(client, words, page_width=1000, page_height=1400):
    resp = client.post("/api/tasks", json={
        "page_width": page_width,
        "page_height": page_height,
        "words": words,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


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


def test_task_lifecycle(client):
    words = [
        make_word(0, "hello", 100, 100, 80, 30),
        make_word(1, "world", 210, 100, 80, 30),
        make_word(2, "second line", 100, 170, 140, 30),
    ]
    task_id = _submit(client, words)
    body = _wait(client, task_id)
    assert body["status"] == "done", body.get("error")
    result = body["result"]
    assert [ln["text"] for ln in result["lines"]] == ["hello world", "second line"]
    assert result["ordered_text"] == "hello world\nsecond line"
    assert result["unclassified"] == []
    assert result["line_count"] == 2


def test_two_columns_api(client):
    words = []
    for i in range(3):
        words.append(make_word(len(words), f"L{i}", 80, 100 + i * 80, 80, 30))
    for i in range(3):
        words.append(make_word(len(words), f"R{i}", 560, 100 + i * 80, 80, 30))

    task_id = _submit(client, words)
    body = _wait(client, task_id)
    assert body["status"] == "done"
    assert [ln["text"] for ln in body["result"]["lines"]] == [
        "L0", "L1", "L2", "R0", "R1", "R2",
    ]


def test_task_not_found(client):
    resp = client.get("/api/tasks/nonexistent")
    assert resp.status_code == 404


def test_invalid_payload_rejected(client):
    resp = client.post("/api/tasks", json={
        "page_width": -1, "page_height": 1400, "words": [],
    })
    assert resp.status_code == 422

    resp = client.post("/api/tasks", json={
        "page_width": 1000, "page_height": 1400,
        "words": [{"text": "x", "confidence": 2.0,
                   "quad": [{"x": 0, "y": 0}] * 4}],
    })
    assert resp.status_code == 422


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
