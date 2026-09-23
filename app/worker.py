"""后台排序任务执行。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import db
from .layout import analyze_layout

logger = logging.getLogger("ocr_sort")


def process_task(task_id: str) -> None:
    """读取任务输入 -> 执行版面排序 -> 回写结果。供线程池调用。"""
    task = db.get_task(task_id)
    if task is None:
        return
    try:
        db.mark_status(task_id, "processing",
                       started_at=datetime.now(timezone.utc))
        payload = json.loads(task["request_json"])
        result = analyze_layout(
            page_width=float(payload["page_width"]),
            page_height=float(payload["page_height"]),
            raw_words=[
                {
                    "id": w.get("id"),
                    "text": w.get("text", ""),
                    "confidence": w.get("confidence"),
                    "quad": [(p["x"], p["y"]) for p in w["quad"]],
                }
                for w in payload.get("words", [])
            ],
        )
        db.mark_status(
            task_id, "done",
            result_json=json.dumps(result, ensure_ascii=False),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as exc:  # 任务失败不应拖垮服务
        logger.exception("task %s failed", task_id)
        db.mark_status(
            task_id, "failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(timezone.utc),
        )
