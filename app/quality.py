"""已完成任务的词框质量报告（只读、确定性计算）。

输入：任务原始请求（原始词框）+ 已落库的排序结果（行分组与去倾斜角）。
输出：按词框原始 id 排序的异常 issues 与 summary 计数。

异常类别：
- ``low_confidence``  置信度过低；
- ``size_outlier``    词高相对所在行字号中位数/MAD 离群；
- ``overlap``         相邻词框水平重叠（疑似重框/坐标错误）；
- ``gap_anomaly``     行内相邻间距相对行内间距中位数/MAD 离群（疑似漏词/误分行）。

所有几何均复用 :mod:`app.layout` 的去倾斜流程重建，与任务结果看到的词框完全一致；
本模块不写库、不改写任务结果；同样输入必然得到同样输出。
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

from .layout import Word, _deskew_word, _overlap_ratio, _polygon_metrics

# ---- 阈值（确定性常量，不依赖随机量） ---------------------------------------

CONF_MEDIUM = 0.75   # 置信度 < 0.75 记 medium
CONF_HIGH = 0.50     # 置信度 < 0.50 记 high

Z_MEDIUM = 3.5       # 稳健 z 分数 >= 3.5 记 medium
Z_HIGH = 6.0         # 稳健 z 分数 >= 6.0 记 high
MAD_K = 1.4826       # MAD -> 标准差的一致估计系数
MIN_SIZE_SAMPLES = 3  # 行内词数 >= 3 才做字号离群检测
MIN_GAP_SAMPLES = 2   # 行内间距数 >= 2 才做间隙离群检测

OVERLAP_MEDIUM = 0.15  # 水平侵入较窄词宽度比例 >= 0.15 记 medium
OVERLAP_HIGH = 0.50    # >= 0.50 记 high
V_OVERLAP_MIN = 0.5    # 垂直重叠（按较矮词归一化）至少 0.5 才算重叠

KIND_ORDER = ("low_confidence", "size_outlier", "overlap", "gap_anomaly")
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# 词框重建：原始请求词框 + 结果行分组 -> 校正坐标系下的 Word
# ---------------------------------------------------------------------------

def _as_points(quad: Any) -> Optional[List[Tuple[float, float]]]:
    """把存储的四角点（dict 或 list）转为 (x, y) 列表；非法返回 None。"""
    if not isinstance(quad, list) or len(quad) < 4:
        return None
    pts: List[Tuple[float, float]] = []
    for p in quad[:4]:
        try:
            if isinstance(p, dict):
                x, y = float(p["x"]), float(p["y"])
            else:
                x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, KeyError, IndexError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        pts.append((x, y))
    return pts


def _round_sig(pts: List[Tuple[float, float]]) -> Tuple[Tuple[float, float], ...]:
    """与结果落库一致的 3 位小数签名，用于原始词框与结果词框对齐。"""
    return tuple((round(x, 3), round(y, 3)) for x, y in pts)


def _index_request(raw_words: List[Dict[str, Any]]
                   ) -> Tuple[Dict[Any, Dict[str, Any]],
                              Dict[Tuple[Any, ...], List[Dict[str, Any]]]]:
    """按 id 与 (文本, 角点签名) 建索引；同一键多个时保留出现次序。"""
    by_id: Dict[Any, Dict[str, Any]] = {}
    by_sig: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for rw in raw_words:
        wid = rw.get("id")
        if wid is not None and wid not in by_id:
            by_id[wid] = rw
        pts = _as_points(rw.get("quad"))
        if pts is not None:
            key = (rw.get("text") or "", _round_sig(pts))
            by_sig.setdefault(key, []).append(rw)
    return by_id, by_sig


def _match_raw(word_id: Any, text: str,
               stored_pts: List[Tuple[float, float]],
               by_id: Dict[Any, Dict[str, Any]],
               by_sig: Dict[Tuple[Any, ...], List[Dict[str, Any]]]
               ) -> Optional[Dict[str, Any]]:
    """先用原始 id 精确匹配，找不到再按 (文本, 角点签名) 匹配（均一次性消费）。"""
    if word_id is not None:
        rw = by_id.pop(word_id, None)
        if rw is not None:
            return rw
    bucket = by_sig.get((text, tuple(stored_pts)))
    if bucket:
        # 优先消耗 id 相同的原始词框，否则取最先出现者
        for i, rw in enumerate(bucket):
            if rw.get("id") == word_id:
                return bucket.pop(i)
        return bucket.pop(0)
    return None


def _build_grouped_words(request_payload: Dict[str, Any], result: Dict[str, Any]
                         ) -> List[List[Word]]:
    """按结果行分组重建校正坐标系词框，返回与结果行一一对应的二维列表。

    几何（外接框、宽高、去倾斜旋转）全部复用 layout 的同一套流程，
    保证质量报告与排序结果基于完全相同的词框几何。
    """
    skew = float(result.get("deskew_angle") or 0.0)
    by_id, by_sig = _index_request(request_payload.get("words", []))

    grouped: List[List[Word]] = []
    for line in result.get("lines", []):
        row: List[Word] = []
        for wp in line.get("words", []):
            word_id = wp.get("id")
            text = wp.get("text") or ""
            stored_pts = _as_points(wp.get("quad")) or []

            rw = _match_raw(word_id, text, stored_pts, by_id, by_sig)
            if rw is not None:
                pts = _as_points(rw.get("quad"))
                confidence = rw.get("confidence")
                raw_id = rw.get("id")
            else:
                # 理论上不会发生（结果词框必然来自请求）；退化为用存储角点
                pts = stored_pts or None
                confidence = wp.get("confidence")
                raw_id = word_id

            m = _polygon_metrics(pts) if pts is not None else None
            if m is None:
                continue
            row.append(_deskew_word(raw_id, text, confidence, pts, m, skew))
        grouped.append(row)
    return grouped


# ---------------------------------------------------------------------------
# 稳健统计
# ---------------------------------------------------------------------------

def _robust_z(value: float, samples: List[float],
              fallback_scale: float) -> Tuple[float, float, float]:
    """返回 (|z|, 中位数, 离散尺度)。

    优先用中位数 + MAD（1.4826 缩放）；MAD 退化（字号/间距高度一致）时
    使用调用方给定的 fallback_scale，避免除零与个别重复值造成的虚高 z。
    """
    med = statistics.median(samples)
    mad = statistics.median(abs(v - med) for v in samples)
    scale = MAD_K * mad
    if scale < fallback_scale:
        scale = fallback_scale
    return abs(value - med) / max(scale, 1e-12), med, scale


def _severity_from_z(z: float) -> Optional[str]:
    if z >= Z_HIGH:
        return "high"
    if z >= Z_MEDIUM:
        return "medium"
    return None


# ---------------------------------------------------------------------------
# 各类检查
# ---------------------------------------------------------------------------

def _word_label(w: Word, pos: int) -> str:
    return f"id={w.index}" if w.index is not None else f"行内第 {pos + 1} 个词"


def _check_confidence(words: List[Word], line_id: Any,
                      issues: List[Dict[str, Any]]) -> None:
    for pos, w in enumerate(words):
        c = w.confidence
        if c is None:
            continue
        if c < CONF_HIGH:
            severity = "high"
        elif c < CONF_MEDIUM:
            severity = "medium"
        else:
            continue
        issues.append({
            "word_id": w.index,
            "line_id": line_id,
            "kind": "low_confidence",
            "severity": severity,
            "reason": f"OCR 置信度 {c:.3f} 低于阈值 {CONF_MEDIUM:.2f}",
            "suggestion": "建议人工复核该词的识别文本与四角坐标",
            "_pos": pos,
        })


def _check_size(words: List[Word], line_id: Any,
                issues: List[Dict[str, Any]]) -> None:
    if len(words) < MIN_SIZE_SAMPLES:
        return
    heights = [w.height for w in words]
    med_h = statistics.median(heights)
    # 行内字号高度一致（MAD≈0）时，以典型字高的 18% 作为离散尺度
    fallback = 0.18 * med_h
    for pos, w in enumerate(words):
        z, med, _scale = _robust_z(w.height, heights, fallback)
        severity = _severity_from_z(z)
        if severity is None:
            continue
        direction = "偏大" if w.height > med else "偏小"
        issues.append({
            "word_id": w.index,
            "line_id": line_id,
            "kind": "size_outlier",
            "severity": severity,
            "reason": (
                f"词高 {w.height:.1f} 相对行内字号中位数 {med:.1f} "
                f"{direction}（稳健 z={z:.2f}），疑似字号/框高异常"
            ),
            "suggestion": "核对该词四角坐标是否按真实文字边缘框选",
            "_pos": pos,
        })


def _check_overlap(words: List[Word], line_id: Any,
                   issues: List[Dict[str, Any]]) -> None:
    for i in range(1, len(words)):
        a, b = words[i - 1], words[i]
        if _overlap_ratio((a.top, a.bottom), (b.top, b.bottom)) < V_OVERLAP_MIN:
            continue
        depth = min(a.right, b.right) - max(a.left, b.left)
        if depth <= 0:
            continue
        ratio = depth / min(max(a.width, 1e-9), max(b.width, 1e-9))
        if ratio < OVERLAP_MEDIUM:
            continue
        severity = "high" if ratio >= OVERLAP_HIGH else "medium"
        for w, other, pos in ((a, b, i - 1), (b, a, i)):
            issues.append({
                "word_id": w.index,
                "line_id": line_id,
                "kind": "overlap",
                "severity": severity,
                "reason": (
                    f"与相邻词「{_word_label(other, i if w is a else i - 1)}」"
                    f"水平重叠 {ratio * 100:.0f}%，疑似重复框或坐标错位"
                ),
                "suggestion": "检查是否为同一文本的重复词框，或修正四角横坐标",
                "_pos": pos,
            })


def _check_gaps(words: List[Word], line_id: Any,
                issues: List[Dict[str, Any]]) -> None:
    if len(words) < MIN_GAP_SAMPLES + 1:
        return
    gaps = [max(0.0, words[i].left - words[i - 1].right)
            for i in range(1, len(words))]
    if not gaps:
        return
    med_h = max(statistics.median(w.height for w in words), 1e-9)
    med_gap = statistics.median(gaps)
    # 间距高度一致（MAD≈0）时，以四分之一字高为最小尺度：
    # 比典型间距多出约一个字宽的间隙即可判离群（疑似漏字/误分行）。
    # 尺度刻意小于行聚类的连接阈值（1.2 字高），保证两者衔接而不互相吞没。
    fallback = 0.25 * med_h
    for i, gap in enumerate(gaps):
        if gap <= med_gap:
            continue  # 偏小/侵入由 overlap 规则负责
        z, med, _scale = _robust_z(gap, gaps, fallback)
        severity = _severity_from_z(z)
        if severity is None:
            continue
        w = words[i]  # 间隙挂在其左侧词上，保证一个间隙只有一条 issue
        nxt = words[i + 1]
        issues.append({
            "word_id": w.index,
            "line_id": line_id,
            "kind": "gap_anomaly",
            "severity": severity,
            "reason": (
                f"与后邻词「{_word_label(nxt, i + 1)}」间距 {gap:.1f}，"
                f"行内典型间距 {med:.1f}（稳健 z={z:.2f}），疑似漏词或误分行"
            ),
            "suggestion": "检查间隙处是否有漏识别词框，或该行分组是否应拆分",
            "_pos": i,
        })


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------

def _issue_sort_key(issue: Dict[str, Any]) -> Tuple[Any, ...]:
    """按词框原始 id 排序（id 缺省置末尾），同类再按行/位置/类别确定性展开。"""
    wid = issue["word_id"]
    id_key = (0, wid) if isinstance(wid, int) and not isinstance(wid, bool) else (1, 0)
    return (
        id_key,
        issue["line_id"],
        issue["_pos"],
        KIND_ORDER.index(issue["kind"]),
        SEVERITY_ORDER[issue["severity"]],
    )


def build_quality_report(request_payload: Dict[str, Any],
                         result: Dict[str, Any]) -> Dict[str, Any]:
    """根据原始请求与已完成结果计算质量报告（只读、确定性）。"""
    grouped = _build_grouped_words(request_payload, result)

    issues: List[Dict[str, Any]] = []
    words_checked = 0
    for line, words in zip(result.get("lines", []), grouped):
        line_id = line.get("order", line.get("id"))
        words_checked += len(words)
        _check_confidence(words, line_id, issues)
        _check_size(words, line_id, issues)
        _check_overlap(words, line_id, issues)
        _check_gaps(words, line_id, issues)

    issues.sort(key=_issue_sort_key)
    for issue in issues:
        issue.pop("_pos", None)

    by_kind = {kind: 0 for kind in KIND_ORDER}
    by_severity = {"high": 0, "medium": 0, "low": 0}
    for issue in issues:
        by_kind[issue["kind"]] += 1
        by_severity[issue["severity"]] += 1

    return {
        "issues": issues,
        "summary": {
            "total": len(issues),
            "by_kind": by_kind,
            "by_severity": by_severity,
            "lines_checked": len(grouped),
            "words_checked": words_checked,
            "unclassified_count": len(result.get("unclassified", [])),
        },
    }
