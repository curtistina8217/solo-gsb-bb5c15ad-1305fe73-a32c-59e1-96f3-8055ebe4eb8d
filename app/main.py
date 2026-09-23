"""FastAPI 入口：任务创建 / 结果查询。"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import (
    BackgroundTasks, FastAPI, Header, HTTPException, Response,
)

from . import db
from .alto import build_alto_xml
from .quality import build_quality_report
from .schemas import QualityReport, SortRequest, TaskCreated, TaskStatus
from .worker import process_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建库建表，避免首个请求延迟
    db.get_engine()
    yield


app = FastAPI(
    title="OCR 版面排序服务",
    description=(
        "提交页面宽高与带四角坐标的 OCR 词框，服务按几何位置自动聚类文本行，"
        "在倾斜、行距不均、双栏版面下输出稳定的阅读顺序。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tasks", response_model=TaskCreated, status_code=201)
def create_task(
    req: SortRequest,
    background: BackgroundTasks,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=255,
        description="可选幂等键：同 key + 同请求体重试返回原任务，不重复入队；"
                    "同 key + 不同请求体返回 409。",
    ),
):
    """创建排序任务，立即返回任务 ID，排序在线程池后台执行。

    携带 ``Idempotency-Key`` 请求头时启用幂等：
    - 该 key 首次出现：正常创建任务，返回 ``201``；
    - 同 key 搭配相同请求体重试：不重复入队，返回原任务 ID 与当前
      状态，状态码 ``200``；
    - 同 key 搭配不同请求体：返回 ``409``。
    不携带该头时行为与历史版本一致。
    """
    # 规范化后的请求体作为摘要输入：字段顺序/空白等表层差异不影响判定
    request_json = req.model_dump_json()

    if not idempotency_key or not idempotency_key.strip():
        task_id = uuid.uuid4().hex
        db.create_task(task_id, request_json)
        background.add_task(process_task, task_id)
        return TaskCreated(task_id=task_id, status="pending")

    key = idempotency_key.strip()
    task_id = uuid.uuid4().hex
    try:
        existing_id = db.create_idempotent_task(key, task_id, request_json)
    except db.IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key already used with a different request body",
        )

    if existing_id != task_id:
        # 同 key 同请求体重试：返回原任务的当前状态，不再入队
        row = db.get_task(existing_id)
        status = row["status"] if row else "pending"
        return Response(
            content=json.dumps({"task_id": existing_id, "status": status}),
            media_type="application/json",
            status_code=200,
        )

    background.add_task(process_task, task_id)
    return TaskCreated(task_id=task_id, status="pending")


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    result = None
    if row.get("result_json"):
        result = json.loads(row["result_json"])

    def iso(v):
        return v.isoformat() if v is not None else None

    return {
        "task_id": row["task_id"],
        "status": row["status"],
        "created_at": iso(row.get("created_at")),
        "started_at": iso(row.get("started_at")),
        "finished_at": iso(row.get("finished_at")),
        "error": row.get("error"),
        "result": result,
    }


@app.get("/api/tasks/{task_id}", response_model=TaskStatus)
def get_task(task_id: str) -> Dict[str, Any]:
    """查询任务状态；done 时 result 内含行分组、有序文本与无法归类词框。"""
    row = db.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _serialize(row)


@app.post("/api/tasks/{task_id}/quality-report", response_model=QualityReport)
def quality_report(task_id: str) -> Dict[str, Any]:
    """对已完成任务生成词框质量报告。

    读取任务原始词框与已落库的行分组，按行内字号中位数/MAD、相邻间距与
    置信度做确定性异常检测；不写库、不改写任务结果。任务不存在返回 404，
    任务尚未完成（pending/processing/failed）返回 409。
    """
    row = db.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if row["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"task not finished: status={row['status']}",
        )
    request_payload = json.loads(row["request_json"])
    result = json.loads(row["result_json"])
    report = build_quality_report(request_payload, result)
    return {"task_id": task_id, **report}


@app.get("/api/tasks/{task_id}/alto.xml")
def export_alto(task_id: str) -> Response:
    """对已完成任务按阅读顺序导出 ALTO 4.3 XML。

    以任务结果中的 blocks -> line_ids -> 行内 words 顺序序列化页面尺寸、
    文本块、行和词；词节点保留原始四角坐标的外接框、文本与置信度。
    实时生成、不写库、不改写任务数据，同样的任务多次请求逐字节一致。
    任务不存在返回 404，任务尚未完成（pending/processing/failed）返回 409。
    """
    row = db.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if row["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"task not finished: status={row['status']}",
        )
    request_payload = json.loads(row["request_json"])
    result = json.loads(row["result_json"])
    xml_bytes = build_alto_xml(
        request_payload, result,
        task_id=task_id, processed_at=row.get("finished_at"),
    )
    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{task_id}.alto.xml"',
        },
    )
