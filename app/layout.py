"""版面几何排序核心算法。

输入：页面宽高 + OCR 词框（四角点、文本、置信度）。
输出：文本行聚类、按阅读顺序排好的文本、文本块（栏/跨栏区域）以及无法归类的词框。

处理流程：
1. 预处理与校验（归一化角点、估计页面倾斜角并整体旋转校正）；
2. 行聚类：按左边缘扫描 + 垂直重叠 + 自适应水平间距阈值做并查集合并；
3. 行内排序：PCA 拟合行方向（基线），按中心在该方向上的投影排序；
4. 阅读顺序：XY-cut（版面切分），先尝试纵向切出分栏，再横向切出行区，
   保证双栏“先左栏自上而下、再右栏”，跨栏标题/页码排在最前/最后。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Quad = Sequence[Sequence[float]]

_SMOOTH_ANGLE = 20.0  # 认为是“水平文本行”的最大倾斜角（度）
_MAX_DESKEW_ANGLE = 15.0  # 整体去倾斜允许的最大角度（度）


# ---------------------------------------------------------------------------
# 文本拼接
# ---------------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x30FF  # 假名
        or 0xAC00 <= code <= 0xD7AF  # 谚文
        or 0xF900 <= code <= 0xFAFF
    )


# 出现在前一个词末尾时不需要额外空格的标点（右/闭引号类 + 中文句读）
_TRAILING_NO_SPACE = set(")]}’”》〉）】」』、。，；：！？·…—–")
# 出现在后一个词开头时不需要额外空格的标点（左/开引号类）
_LEADING_NO_SPACE = set("([{‘“《〈（【「『")


def _join_words(words: List["Word"]) -> str:
    """按语言习惯拼接词：拉丁文字词间加空格，中日韩文字之间不加。"""
    parts: List[str] = []
    for w in words:
        text = " ".join(w.text.split())
        if not text:
            continue
        if not parts:
            parts.append(text)
            continue
        prev = parts[-1]
        if (prev and _is_cjk(prev[-1])) or (text and _is_cjk(text[0])):
            parts.append(text)
        elif prev[-1:] in _TRAILING_NO_SPACE or text[:1] in _LEADING_NO_SPACE:
            parts.append(text)
        else:
            parts.append(" " + text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# 数据结构与几何工具
# ---------------------------------------------------------------------------

@dataclass
class Word:
    """单个 OCR 词框在校正坐标系下的几何信息。"""

    index: Any                 # 客户端提供的词 ID（缺省为输入序号）
    text: str
    confidence: Optional[float]
    quad: List[Point]          # 原始坐标的四角点（保持输入顺序）
    cx: float = 0.0
    cy: float = 0.0
    left: float = 0.0
    right: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    width: float = 0.0
    height: float = 0.0
    angle: float = 0.0         # 该词框自身方向角（度，绕正 x 轴）


@dataclass
class Line:
    words: List[Word]
    cx: float = 0.0
    cy: float = 0.0
    left: float = 0.0
    right: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    baseline_angle: float = 0.0  # 行方向角（度，校正坐标系）


def _wrap_angle(a: float) -> float:
    """归一化到 (-90, 90]。"""
    while a > 90.0:
        a -= 180.0
    while a <= -90.0:
        a += 180.0
    return a


def _polygon_metrics(quad: Quad) -> Optional[Dict[str, Any]]:
    """从四角多边形提取中心、外接矩形、宽高与方向角。

    角点顺序任意：先用相对质心的极角排序，再取相邻边，
    以最长两条对边作为“书写方向”（宽度方向）。
    面积过小或点重合返回 None。
    """
    pts: List[Point] = []
    for p in quad:
        if p is None:
            return None
        try:
            if isinstance(p, dict):
                x, y = float(p["x"]), float(p["y"])
            else:
                if len(p) < 2:
                    return None
                x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, KeyError, IndexError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        pts.append((x, y))
    if len(pts) < 4:
        return None

    cx = sum(p[0] for p in pts) / 4.0
    cy = sum(p[1] for p in pts) / 4.0

    # 确定角点的环绕顺序。轴对齐矩形直接按 (上->下, 左->右) 排，
    # 避免极角排序把规整输入的边顺序打乱；倾斜框再用极角排序。
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    xspan = max(xs) - min(xs)
    yspan = max(ys) - min(ys)
    aligned = True
    for x, y in pts:
        near_v = abs(x - min(xs)) < 1e-6 * max(xspan, 1.0) or \
                 abs(x - max(xs)) < 1e-6 * max(xspan, 1.0)
        near_h = abs(y - min(ys)) < 1e-6 * max(yspan, 1.0) or \
                 abs(y - max(ys)) < 1e-6 * max(yspan, 1.0)
        if not (near_v and near_h):
            aligned = False
            break
    if aligned:
        pts.sort(key=lambda p: (round(p[1], 6), round(p[0], 6)))
        top_left, top_right = pts[0], pts[1]
        bottom_left, bottom_right = pts[2], pts[3]
        if top_left[0] > top_right[0]:
            top_left, top_right = top_right, top_left
        if bottom_left[0] > bottom_right[0]:
            bottom_left, bottom_right = bottom_right, bottom_left
        pts = [top_left, top_right, bottom_right, bottom_left]
    else:
        pts.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

    # 鞋带面积
    area2 = 0.0
    for i in range(4):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % 4]
        area2 += x1 * y2 - x2 * y1
    if abs(area2) < 1e-9:
        return None

    # 4 条相邻边，长的一对是水平方向，短的一对是字高方向
    edges = []
    for i in range(4):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % 4]
        edges.append((math.hypot(x2 - x1, y2 - y1), (x2 - x1, y2 - y1)))
    edges.sort(key=lambda e: e[0], reverse=True)
    w1, v1 = edges[0]
    w2, v2 = edges[1]
    if w1 < 1e-9 or w2 < 1e-9:
        return None
    width = (w1 + w2) / 2.0
    # 高度：两条较短边
    h3 = edges[2][0]
    h4 = edges[3][0]
    height = (h3 + h4) / 2.0
    if height < 1e-9:
        return None

    vx, vy = v1
    # 与另一条长边方向对齐（可能反向）
    dot = vx * v2[0] + vy * v2[1]
    if dot < 0:
        vx, vy = -vx, -vy
    # 方向统一为从左到右（x 分量为正），避免水平行得到 ±180°
    if vx < 0:
        vx, vy = -vx, -vy
    angle = math.degrees(math.atan2(vy, vx))

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {
        "center": (cx, cy),
        "left": min(xs),
        "right": max(xs),
        "top": min(ys),
        "bottom": max(ys),
        "width": width,
        "height": height,
        "angle": angle,
    }


def _estimate_skew(metrices: List[Dict[str, Any]]) -> float:
    """估计页面整体倾斜角：仅取接近水平的词框方向角，取中位数。"""
    angles = [
        m["angle"]
        for m in metrices
        if abs(_wrap_angle(m["angle"])) <= _SMOOTH_ANGLE and m["width"] >= m["height"]
    ]
    if not angles:
        return 0.0
    skew = statistics.median(angles)
    skew = _wrap_angle(skew)
    if abs(skew) > _MAX_DESKEW_ANGLE:
        return 0.0
    return skew


def _rotate(x: float, y: float, theta: float) -> Point:
    c, s = math.cos(theta), math.sin(theta)
    return (x * c - y * s, x * s + y * c)


# ---------------------------------------------------------------------------
# 行聚类
# ---------------------------------------------------------------------------

def _overlap_ratio(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    inter = max(0.0, hi - lo)
    shorter = min(a[1] - a[0], b[1] - b[0])
    if shorter <= 0:
        return 0.0
    return inter / shorter


class _DSU:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != x:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _deskew_word(word_id: Any, text: str, confidence: Optional[float],
                 raw_pts: List[Point], m: Dict[str, Any], skew: float) -> Word:
    """由原始角点度量与整页倾斜角构建校正坐标系下的 :class:`Word`。

    analyze_layout 与质量报告共用此函数，保证两边看到的词框几何完全一致。
    """
    theta = math.radians(-skew)  # 反向旋转校正
    cx, cy = _rotate(m["center"][0], m["center"][1], theta)
    rot_pts = [_rotate(x, y, theta) for x, y in raw_pts]
    xs = [p[0] for p in rot_pts]
    ys = [p[1] for p in rot_pts]
    # 旋转后词宽 = 长边在水平方向的投影，近似用 |(w*cos, w*sin)|
    w = m["width"]
    h = m["height"]
    a = math.radians(m["angle"] - skew)
    width = abs(w * math.cos(a)) + abs(h * math.sin(a))
    height = abs(w * math.sin(a)) + abs(h * math.cos(a))
    return Word(
        index=word_id,
        text=text,
        confidence=confidence,
        quad=raw_pts,
        cx=cx,
        cy=cy,
        left=min(xs),
        right=max(xs),
        top=min(ys),
        bottom=max(ys),
        width=max(width, 1e-6),
        height=max(height, 1e-6),
        angle=_wrap_angle(math.degrees(a)),
    )


def _cluster_lines(words: List[Word], page_width: float) -> List[Line]:
    """把词框聚成文本行。

    规则（校正坐标系下）：
    - 垂直重叠 >= 0.5（按较矮词归一化）；
    - 仅连接“水平邻居”：a 右边没有别的词隔开 b，避免跨栏误连；
    - 水平间距 gap <= 自适应阈值：
      相邻词间距中位数 + 1.2 倍中位字高，并以 5% 页宽封顶。
    """
    n = len(words)
    if n == 0:
        return []
    if n == 1:
        return [_build_line([words[0]], page_width)]

    order = sorted(range(n), key=lambda i: words[i].left)
    median_h = max(statistics.median(w.height for w in words), 1e-6)

    # 第一遍：枚举所有“垂直重叠、且水平间距不超过硬上限”的词对。
    # 同一行的相邻词必然满足；下一行词（垂直不重叠）、对栏词
    # （间距为整栏宽度）都会被排除，因此不会跨行/跨栏误连。
    scan_limit = 0.5 * page_width + 2.0 * median_h
    gap_cap = max(2.5 * median_h, 0.03 * page_width)
    candidates: List[Tuple[float, int, int]] = []
    for a in range(n):
        ia = order[a]
        wa = words[ia]
        for b in range(a + 1, n):
            ib = order[b]
            wb = words[ib]
            if wb.left - wa.left > scan_limit:
                break  # 按左边缘排序，后面的词只会更远
            gap = max(0.0, wb.left - wa.right)
            if gap > gap_cap:
                continue
            if _overlap_ratio((wa.top, wa.bottom), (wb.top, wb.bottom)) < 0.5:
                continue
            candidates.append((gap, ia, ib))

    if not candidates:
        return [_build_line([w], page_width) for w in words]

    # 典型词间距 = 候选间距中位数；最终阈值取“典型间距 + 1.2 倍字高”，
    # 并以硬上限封顶，容忍行距不均、词间距波动。
    gaps = sorted(g for g, _, _ in candidates)
    median_gap = gaps[len(gaps) // 2]
    threshold = min(median_gap + 1.2 * median_h, gap_cap)

    dsu = _DSU(n)
    for gap, ia, ib in candidates:
        if gap <= threshold:
            dsu.union(ia, ib)

    groups: Dict[int, List[Word]] = {}
    for pos, w in enumerate(words):
        groups.setdefault(dsu.find(pos), []).append(w)

    lines = [_build_line(sorted(g, key=lambda w: w.left), page_width)
             for g in groups.values()]
    lines.sort(key=lambda ln: (ln.cy, ln.left))
    return lines


def _build_line(words: List[Word], page_width: float) -> Line:
    """对一行词做 PCA 拟合行方向，并按基线投影重排词序。"""
    cx = statistics.mean(w.cx for w in words)
    cy = statistics.mean(w.cy for w in words)

    sxx = sum((w.cx - cx) ** 2 for w in words)
    syy = sum((w.cy - cy) ** 2 for w in words)
    sxy = sum((w.cx - cx) * (w.cy - cy) for w in words)

    if sxx <= 1e-9 and syy <= 1e-9:
        angle = 0.0
    else:
        # PCA 主方向
        theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
        angle = math.degrees(theta)

    # 方向统一为“阅读从左到右”：与平均水平方向一致。
    # 近水平行直接按 left 排序，避免 PCA 在共线点上出现 ±180° 抖动。
    if abs(_wrap_angle(angle)) < 1.0:
        ordered = sorted(words, key=lambda w: w.left)
    else:
        vx = math.cos(math.radians(angle))
        if vx < 0:
            angle += 180.0
            vx = math.cos(math.radians(angle))
        vy = math.sin(math.radians(angle))
        ordered = sorted(words,
                         key=lambda w: (w.cx - cx) * vx + (w.cy - cy) * vy)

    left = min(w.left for w in words)
    right = max(w.right for w in words)
    top = min(w.top for w in words)
    bottom = max(w.bottom for w in words)

    line = Line(
        words=ordered,
        cx=cx,
        cy=cy,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        baseline_angle=_wrap_angle(angle),
    )
    return line


# ---------------------------------------------------------------------------
# XY-cut 阅读顺序
# ---------------------------------------------------------------------------

def _merge_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """合并一维重叠/相接区间。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: List[Tuple[float, float]] = [ordered[0]]
    for lo, hi in ordered[1:]:
        mlo, mhi = merged[-1]
        if lo <= mhi:
            merged[-1] = (mlo, max(mhi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _projection_gaps(intervals: List[Tuple[float, float]]
                     ) -> List[Tuple[float, float]]:
    """返回投影中的空白段（区间并集之间），按宽度降序。"""
    merged = _merge_intervals(intervals)
    gaps = [
        (merged[i][1], merged[i + 1][0])
        for i in range(len(merged) - 1)
        if merged[i + 1][0] > merged[i][1]
    ]
    gaps.sort(key=lambda g: g[1] - g[0], reverse=True)
    return gaps


def _gutter_spans(lines: List[Line], gx0: float, gx1: float,
                  node_top: float, node_bottom: float,
                  median_h: float) -> bool:
    """判断竖直栏缝 (gx0, gx1) 是否贯穿节点高度。

    取条带两侧所有行的 y 区间并集（允许 <=2 倍字高的行间断口，以容忍
    双栏行间错位），要求并集从节点上沿附近连续延伸到下沿附近。
    跨栏标题/页脚会在条带处留下大于容差的空洞，使该条带落选。
    """
    tol = 2.0 * median_h
    ys = sorted(
        (ln.top, ln.bottom) for ln in lines
        if ln.right <= gx0 + 1e-9 or ln.left >= gx1 - 1e-9
    )
    if not ys:
        return False
    union: List[List[float]] = []
    for t, b in ys:
        if union and t <= union[-1][1] + tol:
            union[-1][1] = max(union[-1][1], b)
        else:
            union.append([t, b])
    if len(union) != 1:
        return False
    return (union[0][0] <= node_top + tol
            and union[0][1] >= node_bottom - tol)


def _xy_cut(lines: List[Line], median_h: float, page_width: float,
            page_height: float, depth: int = 0, from_x: bool = False
            ) -> List[Tuple[List[Line], str]]:
    """递归 XY-cut，返回叶节点 ``(行列表, 块类型)``，顺序即阅读顺序。

    每个节点：
      1. 先找“贯穿节点高度”的纵向空白（栏缝），存在则纵切 -> column；
      2. 否则找“显著大于行高”的横向空白（标题/正文/页脚），存在则横切；
      3. 都没有则为叶节点，节点内行按 (top, left) 排序。

    纵切优先保证双栏“左栏自上而下、再右栏”；跨栏标题不贯穿节点高度，
    因此根节点先横切分离标题，正文节点内再纵切分栏。
    """
    if len(lines) <= 1 or depth > 16:
        kind = "column" if from_x else "zone"
        return [(lines, kind)] if lines else []

    node_left = min(ln.left for ln in lines)
    node_right = max(ln.right for ln in lines)
    node_top = min(ln.top for ln in lines)
    node_bottom = max(ln.bottom for ln in lines)

    # ---- 1) 纵向：x 投影空白 ----
    x_gaps = _projection_gaps([(ln.left, ln.right) for ln in lines])
    gutter_min = max(1.5 * median_h, 0.05 * page_width)
    for gx0, gx1 in x_gaps:
        if gx1 - gx0 < gutter_min:
            break  # 已按宽度降序
        cut = (gx0 + gx1) / 2.0
        # 严格纵切：没有任何行跨越切线
        crossing = [ln for ln in lines if ln.left < cut < ln.right]
        if crossing:
            continue
        left_lines = [ln for ln in lines if ln.right <= cut]
        right_lines = [ln for ln in lines if ln.left >= cut]
        if len(left_lines) < 2 or len(right_lines) < 2:
            continue
        # 宽度保护：两侧都不能过窄
        left_w = max(ln.right for ln in left_lines) - node_left
        right_w = node_right - min(ln.left for ln in right_lines)
        if left_w < 0.06 * page_width or right_w < 0.06 * page_width:
            continue
        # 栏缝必须贯穿节点高度（容忍行间错位，拒绝跨栏标题/页脚）
        if not _gutter_spans(lines, gx0, gx1, node_top, node_bottom, median_h):
            continue
        left_lines.sort(key=lambda ln: (ln.top, ln.left))
        right_lines.sort(key=lambda ln: (ln.top, ln.left))
        return (
            _xy_cut(left_lines, median_h, page_width, page_height,
                    depth + 1, from_x=True)
            + _xy_cut(right_lines, median_h, page_width, page_height,
                      depth + 1, from_x=True)
        )

    # ---- 2) 横向：y 投影空白 ----
    y_gaps = _projection_gaps([(ln.top, ln.bottom) for ln in lines])
    # 仅在“区域级”空白处横切：明显大于一个字高（行距通常与字高相当，
    # 标题/正文/页脚间的空白明显更大）。
    y_cut_min = 2.0 * median_h
    for gy0, gy1 in y_gaps:
        if gy1 - gy0 < y_cut_min:
            break
        cut = (gy0 + gy1) / 2.0
        crossing = [ln for ln in lines if ln.top < cut < ln.bottom]
        if crossing:
            continue
        top_lines = [ln for ln in lines if ln.bottom <= cut]
        bottom_lines = [ln for ln in lines if ln.top >= cut]
        if not top_lines or not bottom_lines:
            continue
        top_lines.sort(key=lambda ln: (ln.top, ln.left))
        bottom_lines.sort(key=lambda ln: (ln.top, ln.left))
        return (
            _xy_cut(top_lines, median_h, page_width, page_height,
                    depth + 1, from_x=from_x)
            + _xy_cut(bottom_lines, median_h, page_width, page_height,
                      depth + 1, from_x=from_x)
        )

    # ---- 3) 叶节点 ----
    kind = "column" if from_x else "zone"
    return [(sorted(lines, key=lambda ln: (ln.top, ln.left)), kind)]


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------

def _word_payload(w: Word) -> Dict[str, Any]:
    return {
        "id": w.index,
        "text": w.text,
        "confidence": w.confidence,
        "quad": [[round(x, 3), round(y, 3)] for x, y in w.quad],
    }


def analyze_layout(page_width: float, page_height: float,
                   raw_words: List[Dict[str, Any]]) -> Dict[str, Any]:
    """版面排序主入口。

    raw_words: [{"id"/"index"?, "text", "confidence"?, "quad": [[x,y]*4]}]
    返回行分组、有序文本、文本块与无法归类词框。
    """
    unclassified: List[Dict[str, Any]] = []
    valid: List[Word] = []

    # 估算倾斜需要先在原坐标系测量各词方向
    metrices: List[Dict[str, Any]] = []
    measured: List[Tuple[int, str, Optional[float], List[Point], Dict[str, Any]]] = []

    for idx, rw in enumerate(raw_words):
        word_id = rw.get("id", rw.get("index", idx))
        text = rw.get("text") or ""
        conf = rw.get("confidence")
        quad = rw.get("quad") or []

        def _as_xy(p: Any) -> List[float]:
            if isinstance(p, dict):
                return [float(p["x"]), float(p["y"])]
            return [float(p[0]), float(p[1])]

        def _reject(reason: str) -> None:
            try:
                quad_out = [_as_xy(p) for p in list(quad)[:4]]
            except (TypeError, ValueError, KeyError, IndexError):
                quad_out = None
            unclassified.append({
                "id": word_id,
                "text": text,
                "confidence": conf,
                "quad": quad_out if quad_out and len(quad_out) == 4 else quad,
                "reason": reason,
            })

        if len(quad) < 4:
            _reject("invalid_quad")
            continue
        m = _polygon_metrics(quad)
        if m is None:
            _reject("invalid_quad")
            continue
        if not text.strip():
            _reject("empty_text")
            continue
        raw_pts: List[Point] = [
            (float(p["x"]), float(p["y"])) if isinstance(p, dict)
            else (float(p[0]), float(p[1]))
            for p in quad[:4]
        ]
        measured.append((idx, word_id, text, conf, raw_pts, m))
        metrices.append(m)

    skew = _estimate_skew(metrices)

    for idx, word_id, text, conf, raw_pts, m in measured:
        valid.append(_deskew_word(word_id, text, conf, raw_pts, m, skew))

    lines = _cluster_lines(valid, max(page_width, 1e-6))
    median_h = statistics.median([w.height for w in valid]) if valid else 0.0

    leaves = _xy_cut(lines, max(median_h, 1.0),
                     max(page_width, 1.0), max(page_height, 1.0))

    # ---- 组装行 / 块 / 有序文本 ----
    line_results: List[Dict[str, Any]] = []
    blocks_out: List[Dict[str, Any]] = []
    text_blocks: List[str] = []
    order = 0

    for block_idx, (leaf, block_type) in enumerate(leaves):
        leaf = sorted(leaf, key=lambda ln: (ln.top, ln.left))
        line_ids: List[int] = []
        block_lines_text: List[str] = []

        b_left = min(ln.left for ln in leaf)
        b_right = max(ln.right for ln in leaf)
        b_top = min(ln.top for ln in leaf)
        b_bottom = max(ln.bottom for ln in leaf)

        for ln in leaf:
            # 行外接框映射回原始坐标系（四角 AABB 旋转回去）
            corners = [
                (ln.left, ln.top), (ln.right, ln.top),
                (ln.right, ln.bottom), (ln.left, ln.bottom),
            ]
            inv = math.radians(skew)
            orig = [_rotate(x, y, inv) for x, y in corners]

            text = _join_words(ln.words)
            line_results.append({
                "id": order,
                "block_id": block_idx,
                "order": order,
                "text": text,
                "baseline_angle": round(ln.baseline_angle + skew, 3),
                "confidence": round(
                    sum(w.confidence for w in ln.words if w.confidence is not None)
                    / max(1, sum(1 for w in ln.words if w.confidence is not None)),
                    4,
                ) if any(w.confidence is not None for w in ln.words) else None,
                "bbox": [[round(x, 3), round(y, 3)] for x, y in orig],
                "words": [_word_payload(w) for w in ln.words],
            })
            line_ids.append(order)
            block_lines_text.append(text)
            order += 1

        # 块框也映射回原始坐标
        b_corners = [
            (b_left, b_top), (b_right, b_top),
            (b_right, b_bottom), (b_left, b_bottom),
        ]
        inv = math.radians(skew)
        b_orig = [_rotate(x, y, inv) for x, y in b_corners]
        blocks_out.append({
            "id": block_idx,
            "type": block_type,
            "line_ids": line_ids,
            "bbox": [[round(x, 3), round(y, 3)] for x, y in b_orig],
        })
        text_blocks.append("\n".join(block_lines_text))

    return {
        "page": {"width": page_width, "height": page_height},
        "deskew_angle": round(skew, 3),
        "line_count": len(line_results),
        "block_count": len(blocks_out),
        "blocks": blocks_out,
        "lines": line_results,
        "ordered_text": "\n\n".join(text_blocks),
        "unclassified": unclassified,
    }
