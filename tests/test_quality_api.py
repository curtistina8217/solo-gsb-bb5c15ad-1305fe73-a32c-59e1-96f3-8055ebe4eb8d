"""质量报告接口端到端测试。"""
import copy
import json

from tests.conftest import make_word
from tests.test_api import _submit, _wait


def _report(client, task_id):
    return client.post(f"/api/tasks/{task_id}/quality-report")


def test_quality_report_endpoint(client):
    words = [
        make_word(0, "hello", 100, 100, 80, 30, conf=0.99),
        make_word(1, "world", 190, 100, 80, 30, conf=0.30),
        make_word(2, "second", 100, 170, 80, 30, conf=0.99),
        make_word(3, "line", 190, 170, 80, 30, conf=0.99),
    ]
    task_id = _submit(client, words)
    _wait(client, task_id)

    resp = _report(client, task_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == task_id
    assert body["summary"]["lines_checked"] == 2
    assert body["summary"]["words_checked"] == 4
    low = [i for i in body["issues"] if i["kind"] == "low_confidence"]
    assert len(low) == 1
    assert low[0]["word_id"] == 1
    assert low[0]["severity"] == "high"
    assert set(low[0]) == {
        "word_id", "line_id", "kind", "severity", "reason", "suggestion",
    }
    # issues 按原始 id 排序
    ids = [i["word_id"] for i in body["issues"]]
    assert ids == sorted(ids)


def test_quality_report_repeated_is_stable(client):
    words = [make_word(0, "hello", 100, 100, 80, 30)]
    task_id = _submit(client, words)
    _wait(client, task_id)
    r1 = _report(client, task_id).json()
    r2 = _report(client, task_id).json()
    assert r1 == r2


def test_quality_report_does_not_modify_task_result(client):
    words = [make_word(0, "hello", 100, 100, 80, 30, conf=0.2)]
    task_id = _submit(client, words)
    before = _wait(client, task_id)
    _report(client, task_id)
    after = client.get(f"/api/tasks/{task_id}").json()
    assert after["status"] == "done"
    assert after["result"] == before["result"]
    assert after["finished_at"] == before["finished_at"]


def test_quality_report_unknown_task_404(client):
    resp = _report(client, "no-such-task")
    assert resp.status_code == 404
    # 与 GET 任务接口沿用统一错误结构
    assert resp.json() == {"detail": "task not found"}


def test_quality_report_pending_task_409(client):
    from app import db
    db.create_task("pending-quality-1",
                   json.dumps({"page_width": 1, "page_height": 1, "words": []}))
    resp = _report(client, "pending-quality-1")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "task not finished: status=pending"
    # 任务本身未被改动
    assert db.get_task("pending-quality-1")["status"] == "pending"


def test_quality_report_failed_task_409(client):
    from app import db
    db.create_task("failed-quality-1",
                   json.dumps({"page_width": 1, "page_height": 1, "words": []}))
    db.mark_status("failed-quality-1", "failed", error="boom")
    resp = _report(client, "failed-quality-1")
    assert resp.status_code == 409
    assert "status=failed" in resp.json()["detail"]
