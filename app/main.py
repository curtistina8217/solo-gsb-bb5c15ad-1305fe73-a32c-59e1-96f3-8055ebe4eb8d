"""FastAPI 入口：任务创建 / 结果查询。"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response

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
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tasks", response_model=TaskCreated, status_code=201)
def create_task(
    req: SortRequest,
    background: BackgroundTasks,
    response: Response,
    idempotency_key: Optional[str] = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "可选幂等键：相同 key + 相同请求体重试不会重复入队，"
            "直接返回原 task_id/status；相同 key + 不同请求体返回 409。"
        ),
    ),
) -> TaskCreated:
    """创建排序任务，立即返回任务 ID，排序在线程池后台执行。

    带 ``Idempotency-Key`` 请求头时以该键做幂等：
    键与请求体摘要（规范化 JSON 的 sha256）落库持久化；重试返回原任务，
    并发相同请求也只创建一个任务（由数据库唯一约束保证）。
    """
    # Pydantic 规范化序列化：字段顺序/省略可选字段不影响摘要
    request_json = req.model_dump_json()

    key: Optional[str] = None
    if idempotency_key is not None:
        candidate = idempotency_key.strip()
        if candidate:
            if len(candidate) > db.IDEMPOTENCY_KEY_MAX_LEN:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Idempotency-Key too long: max "
                        f"{db.IDEMPOTENCY_KEY_MAX_LEN} characters"
                    ),
                )
            key = candidate
        # 仅空白的头视为缺省，不做幂等处理

    if key is not None:
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        task_id = uuid.uuid4().hex
        result = db.reserve_idempotent_task(
            key=key,
            task_id=task_id,
            request_json=request_json,
            request_hash=request_hash,
        )
        if result["outcome"] == "created":
            # 仅事务提交成功的胜出者入队后台任务
            background.add_task(process_task, task_id)
            return TaskCreated(task_id=task_id, status="pending")

        existing = db.get_task(result["task_id"])
        if existing is None:  # 不可达：登记与任务在同一事务写入
            raise HTTPException(status_code=500, detail="task record missing")
        if result["outcome"] == "conflict":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Idempotency-Key was already used with a different request "
                    f"body: existing_task_id={result['task_id']}"
                ),
            )
        # 同键同体重试：返回原任务的当前状态，不重复入队
        response.status_code = 200
        return TaskCreated(
            task_id=existing["task_id"], status=existing["status"]
        )

    # 未携带幂等键：保持原有行为
    task_id = uuid.uuid4().hex
    db.create_task(task_id, request_json)
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
