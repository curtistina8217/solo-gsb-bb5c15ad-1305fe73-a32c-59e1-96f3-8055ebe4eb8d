# OCR 版面排序服务

基于 **FastAPI + SQLite** 的 OCR 版面阅读顺序排序服务。客户端提交页面宽高以及一组带
**四角坐标、文本、置信度**的词框，服务按几何位置自动完成：

1. **去倾斜**：用所有词框长边方向的中位数估计整页倾斜角并旋转校正；
2. **行聚类**：按垂直重叠 + 自适应水平间距，用并查集把词框聚成文本行，
   词间距阈值随字号/页宽自适应，不会把双栏间隙误连成一行；
3. **行内排序**：PCA 拟合每行基线方向，词按基线投影从左到右排列；
4. **阅读顺序（XY-cut）**：递归做投影切分——
   - 先沿**贯穿整页高度的垂直空白**切出分栏；
   - 再沿**显著横向空白**切出标题 / 正文 / 页脚等区域；
   - 因此 **双栏版面按“左栏自上而下 → 右栏自上而下”**输出，
     跨栏标题排在正文之前，页码排在最后；
   - 行距不均、左右栏行错位、整页轻度倾斜时仍保持稳定顺序。
5. 同一行的拉丁文词之间自动加空格、中日韩文字之间不加空格。

## 一键启动（Docker）

需要 Docker（含 Compose 插件）。

```bash
docker compose up --build -d
```

服务监听 **端口 `8000`**，SQLite 数据库保存在命名卷 `ocr-data`（容器内 `/data/ocr_sort.db`），
容器重建后任务历史仍在。

不用 Compose 也可以：

```bash
docker build -t ocr-layout-sort .
docker run -d -p 8000:8000 -v ocr-data:/data --name ocr-layout-sort ocr-layout-sort
```

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

镜像内置词框质量报告接口（`POST /api/tasks/{task_id}/quality-report`），
报告按请求实时计算，无需额外环境变量、数据库迁移或卷；历史任务只要在
`/data/ocr_sort.db` 中处于 `done` 状态即可直接生成报告。

停止：

```bash
docker compose down            # 保留数据卷
docker compose down -v         # 同时删除任务数据
```

## 本地开发运行（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OCR_DB_PATH=./ocr_sort.db   # 缺省为 /data/ocr_sort.db
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

交互式 API 文档：<http://localhost:8000/docs>（Swagger UI）、<http://localhost:8000/redoc>。

## API

任务为**异步**模式：创建任务后立即返回 `task_id`，排序在线程池后台执行，
通过查询接口轮询结果。

### `POST /api/tasks` — 创建排序任务

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `page_width` | number, >0 | 页面宽度（像素/点，与词框坐标同坐标系） |
| `page_height` | number, >0 | 页面高度 |
| `words` | array | 词框列表 |
| `words[].id` | integer, 可选 | 词框自定义 ID；缺省使用提交序号（从 0 开始） |
| `words[].text` | string | OCR 文本 |
| `words[].confidence` | number 0~1, 可选 | 置信度 |
| `words[].quad` | 4 个点 | 四角坐标，建议按左上→右上→右下→左下顺序（顺序不强制，服务自动归一化） |
| `words[].quad[].x/y` | number | 角点坐标 |

`201` 响应：

```json
{ "task_id": "5f1c…", "status": "pending" }
```

### `GET /api/tasks/{task_id}` — 查询状态与结果

`status` 为 `pending` / `processing` / `done` / `failed`。`done` 时 `result` 包含：

- `deskew_angle`：估计的整页倾斜角（度）；
- `blocks[]`：文本块，`type` 为 `column`（栏）或 `zone`（跨栏区域，如标题/页脚），
  含 `line_ids` 与原始坐标系 `bbox`；
- `lines[]`：按阅读顺序编号的文本行，含 `order`、`block_id`、`text`、
  `baseline_angle`、平均 `confidence`、原始坐标系 `bbox`，以及行内每个词 `words[]`；
- `ordered_text`：最终有序文本，行内换行、块之间空一行；
- `unclassified[]`：无法归类的词框（空文本 `empty_text`、退化/非法四角 `invalid_quad`），
  附带原始 `id`、`text`、`confidence`、`quad` 与 `reason`。

### `POST /api/tasks/{task_id}/quality-report` — 词框质量报告

对 **`status=done`** 的任务做只读的确定性质检：读取任务的**原始词框**与已落库的
**行分组**，在与排序完全相同的去倾斜坐标系下逐行检查异常词框。报告**实时计算、不写库、
不改写任务结果**，同样的任务多次请求结果逐字节一致。

检测规则（阈值为服务端固定常量，不依赖随机量）：

| `kind` | 判定规则 | 触发位置 |
| --- | --- | --- |
| `low_confidence` | 置信度 `<0.50` 记 `high`，`<0.75` 记 `medium`；未提供置信度不检查 | 单个词框 |
| `size_outlier` | 词高相对**行内字号中位数**的稳健 z 分数（**MAD×1.4826**；MAD 退化时取行中位字高的 18%）`≥3.5` 记 `medium`、`≥6` 记 `high`；行内词数 ≥3 才检查 | 单个词框 |
| `overlap` | 行内相邻词**垂直重叠 ≥50%** 且水平侵入较窄词宽度 `≥15%` 记 `medium`、`≥50%` 记 `high`；词对双方各一条 issue | 相邻词对 |
| `gap_anomaly` | 行内相邻水平间距相对**行内间距中位数**的稳健 z 分数（MAD 退化时尺度取 0.25 倍中位字高）`≥3.5`/`≥6`；仅检查偏大间隙（侵入由 overlap 负责），issue 挂在间隙左侧词，行内间距数 ≥2 才检查 | 相邻词对 |

`200` 响应：

```json
{
  "task_id": "5f1c…",
  "issues": [
    {
      "word_id": 1,
      "line_id": 0,
      "kind": "low_confidence",
      "severity": "high",
      "reason": "OCR 置信度 0.400 低于阈值 0.75",
      "suggestion": "建议人工复核该词的识别文本与四角坐标"
    }
  ],
  "summary": {
    "total": 1,
    "by_kind": {"low_confidence": 1, "size_outlier": 0, "overlap": 0, "gap_anomaly": 0},
    "by_severity": {"high": 1, "medium": 0, "low": 0},
    "lines_checked": 1,
    "words_checked": 3,
    "unclassified_count": 0
  }
}
```

- `issues[]` 按词框**原始 id 升序**排列（未提供 id 的词框排在最后，其 `word_id` 为
  `null`）；每个 issue 含 `word_id`、`line_id`（结果行 `order`）、`kind`、`severity`、
  `reason`（含量化指标）、`suggestion`；
- 行内正常的排版波动不会产生 issue；未归行词框（`unclassified`）不参与行内检查，
  仅在 `summary.unclassified_count` 中计数。

错误沿用统一结构：任务不存在返回 `404 {"detail": "task not found"}`；
任务未完成（`pending` / `processing` / `failed`）返回
`409 {"detail": "task not finished: status=<status>"}`。

```bash
curl -s -X POST http://localhost:8000/api/tasks/<TASK_ID>/quality-report
```

### `GET /api/tasks/{task_id}/alto.xml` — ALTO 4.3 XML 导出

对 **`status=done`** 的任务按阅读顺序导出 [ALTO 4.3](http://www.loc.gov/standards/alto/)
XML（`Content-Type: application/xml`，输出通过官方 `alto-4-3.xsd` 校验）。
文档严格按结果顺序稳定序列化：`TextBlock` 按 `blocks` 顺序、`TextLine` 按各块
`line_ids` 引用的结果行顺序、行内 `String` 按结果词顺序展开。

- `Page` 写入页面尺寸（`WIDTH` / `HEIGHT`），`PrintSpace` 覆盖整页；
- `TextBlock` 的块类型（`column` / `zone`）通过 `Tags/LayoutTag` +
  `TAGREFS` 表达（ALTO 的 `TextBlock` 没有 `TYPE` 属性）；
- 每个词输出一个 `String`：`HPOS/VPOS/WIDTH/HEIGHT` 取**原始四角坐标的轴对齐
  外接框**（不做去倾斜旋转），`CONTENT` 为词文本，有置信度时写 `WC`（0~1）；
  相邻词之间存在正水平间距时补 `SP`；
- 文本中的 `& < > " '` 等 XML 特殊字符均做实体转义，非法控制字符被剔除；
- 导出**实时计算、不写库、不改写任务数据**，处理时间戳取任务完成时刻，
  同一任务多次下载逐字节一致；无法归类的词框（`unclassified`）不进入版面树。

错误沿用统一结构：任务不存在返回 `404`，任务未完成返回 `409`（同上）。

```bash
# 直接查看
curl -s http://localhost:8000/api/tasks/<TASK_ID>/alto.xml

# 下载为文件（响应带 Content-Disposition: attachment）
curl -s -OJ http://localhost:8000/api/tasks/<TASK_ID>/alto.xml
#   或显式指定文件名
curl -s -o result.alto.xml \
  http://localhost:8000/api/tasks/<TASK_ID>/alto.xml
```

## 示例请求

仓库内置一个带跨栏标题、左右双栏（含中文）、页码并整体倾斜 1.5° 的示例：
[`examples/request.json`](examples/request.json)。

```bash
# 1) 创建任务
curl -s -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  --data @examples/request.json
# {"task_id":"<TASK_ID>","status":"pending"}

# 2) 查询结果（把 <TASK_ID> 换成上一步返回的 ID）
curl -s http://localhost:8000/api/tasks/<TASK_ID>

# 3) 任务完成后下载 ALTO 4.3 XML
curl -s -OJ http://localhost:8000/api/tasks/<TASK_ID>/alto.xml
```

最小内联示例（双栏各两行）：

```bash
curl -s -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "page_width": 1000,
    "page_height": 1400,
    "words": [
      {"id": 0, "text": "Left0",  "confidence": 0.98,
       "quad": [{"x": 80,  "y": 100}, {"x": 200, "y": 100},
                {"x": 200, "y": 130}, {"x": 80,  "y": 130}]},
      {"id": 1, "text": "Left1",  "confidence": 0.97,
       "quad": [{"x": 80,  "y": 170}, {"x": 200, "y": 170},
                {"x": 200, "y": 200}, {"x": 80,  "y": 200}]},
      {"id": 2, "text": "Right0", "confidence": 0.96,
       "quad": [{"x": 560, "y": 100}, {"x": 680, "y": 100},
                {"x": 680, "y": 130}, {"x": 560, "y": 130}]},
      {"id": 3, "text": "Right1", "confidence": 0.95,
       "quad": [{"x": 560, "y": 170}, {"x": 680, "y": 170},
                {"x": 680, "y": 200}, {"x": 560, "y": 200}]}
    ]
  }'
```

返回结果中 `ordered_text` 为：

```
Left0
Left1

Right0
Right1
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

测试覆盖：单栏顺序、双栏/三栏列优先顺序、跨栏标题、倾斜页面、行距不均、
标题/页脚区域切分、行内基线排序、无法归类词框、CJK 文本拼接、
质量报告四类异常规则（低置信/字号离群/重叠/间隙异常）及其确定性与只读性、
ALTO 4.3 导出（页面尺寸/块/行/词的阅读顺序、原始四角外接框、置信度、
特殊字符转义、确定性、404/409 错误结构）、
HTTP 全流程（含 404/409 统一错误结构）。

## 项目结构

```
app/
  main.py      FastAPI 入口（POST /api/tasks、GET /api/tasks/{id}、
               POST /api/tasks/{id}/quality-report、
               GET /api/tasks/{id}/alto.xml、/health）
  schemas.py   Pydantic 请求/响应模型与入参校验
  layout.py    版面几何算法（去倾斜 / 行聚类 / 基线排序 / XY-cut / 结果组装）
  quality.py   词框质量报告（中位数/MAD 稳健离群、重叠与间隙检测，确定性只读）
  alto.py      ALTO 4.3 XML 导出（块/行/词按结果顺序序列化，确定性只读）
  worker.py    后台任务执行（线程池）
  db.py        SQLite 持久化（SQLAlchemy Core）
examples/
  request.json 示例请求
tests/         单元测试与接口测试
Dockerfile
docker-compose.yml
```
