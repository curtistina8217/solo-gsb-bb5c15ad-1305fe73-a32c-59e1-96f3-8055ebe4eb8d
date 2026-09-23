"""已完成任务的 ALTO 4.3 XML 导出（只读、确定性序列化）。

输入：任务原始请求（页面尺寸）+ 已落库的排序结果（块 / 行 / 词的阅读顺序）。
输出：单页 ALTO 4.3 文档，结构为

    Description（像素单位、确定性的处理时间戳与软件标识）
    Tags（LayoutTag：column/zone 文本块分类）
    Layout/Page（页面宽高）
      PrintSpace（整页）
        TextBlock（结果 blocks，按结果顺序，TAGREFS 指向块类型标签）
          TextLine（按 block.line_ids 引用的结果行顺序）
            String（行内词，按结果行内顺序；词间正间距补 SP）

几何约定：
- 词节点保留**原始四角坐标的轴对齐外接框**（HPOS/VPOS/WIDTH/HEIGHT），
  不做去倾斜旋转，坐标与提交时的四角点同一坐标系；
- 行 / 块框同样取结果中原始坐标系多边形的轴对齐外接框；
- ALTO 坐标不允许负值，外算坐标做 0 下限保护。

文本经 XML 解析器写属性值，``& < > " '`` 等特殊字符自动转义；
XML 1.0 不允许的控制字符会被剔除。序列化只依赖标准库、字典插入序与
固定换行缩进，同样输入逐字节一致；本模块不写库、不改写任务数据。

生成结果通过 ALTO 4.3 官方 XSD（alto-4-3.xsd）校验。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v4#"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOCATION = (
    "http://www.loc.gov/standards/alto/ns-v4# "
    "http://www.loc.gov/standards/alto/v4/alto-4-3.xsd"
)
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'

SOFTWARE_NAME = "OCR 版面排序服务"
SOFTWARE_VERSION = "1.0.0"

# 序列化时让 ALTO 成为默认命名空间（<alto xmlns="...">），xsi 保留前缀
ET.register_namespace("", ALTO_NS)
ET.register_namespace("xsi", XSI_NS)


def _qname(tag: str) -> str:
    return f"{{{ALTO_NS}}}{tag}"


def _num(value: Any) -> float:
    """四舍五入到 3 位小数；非法 / NaN / 无穷退化为 0。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f in (float("inf"), float("-inf")):
        return 0.0
    return round(f, 3)


def _format(value: float) -> str:
    """float -> 稳定字符串：整值不带小数点，其余去掉尾随 0。"""
    if value == 0:
        return "0"
    return ("%f" % value).rstrip("0").rstrip(".")


def _points(polygon: Any) -> Optional[List[Tuple[float, float]]]:
    """把结果中的多边形（dict 点或 [x, y] 点）转成数值点对。"""
    if not isinstance(polygon, (list, tuple)) or len(polygon) < 2:
        return None
    pts: List[Tuple[float, float]] = []
    for p in polygon:
        try:
            if isinstance(p, dict):
                x, y = float(p["x"]), float(p["y"])
            else:
                x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, KeyError, IndexError):
            return None
        if x != x or y != y:  # NaN
            return None
        pts.append((x, y))
    return pts


def _bbox_attrs(polygon: Any) -> Optional[Dict[str, float]]:
    """四角多边形 -> ALTO 轴对齐外接框属性（坐标非负）。"""
    pts = _points(polygon)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return {
        "HPOS": round(max(0.0, left), 3),
        "VPOS": round(max(0.0, top), 3),
        "WIDTH": round(max(0.0, right - left), 3),
        "HEIGHT": round(max(0.0, bottom - top), 3),
    }


def _set_geo(parent: ET.Element, geo: Optional[Dict[str, float]]) -> None:
    if geo:
        for key in ("HPOS", "VPOS", "WIDTH", "HEIGHT"):
            parent.set(key, _format(geo[key]))


def _xml_safe(text: str) -> str:
    """剔除 XML 1.0 不允许的控制字符（保留制表/换行/回车）。

    特殊字符（``& < >`` 等）不在此处理——写入属性时由 ElementTree
    完成实体转义，这里只保证整份文档始终是合法 XML。
    """
    def allowed(ch: str) -> bool:
        code = ord(ch)
        return (
            code in (0x09, 0x0A, 0x0D)
            or 0x20 <= code <= 0xD7FF
            or 0xE000 <= code <= 0xFFFD
            or 0x10000 <= code <= 0x10FFFF
        )

    return "".join(ch for ch in text if allowed(ch))


def _append_word(line_el: ET.Element, seq: int, word: Dict[str, Any],
                 geo: Dict[str, float],
                 prev_geo: Optional[Dict[str, float]]) -> None:
    """在行节点上追加一个词（String），与前词有正水平间距时先补 SP。"""
    # ALTO 用 SP 表达词间隙：只在两词外接框之间确有正间距时输出
    if prev_geo is not None:
        gap = geo["HPOS"] - (prev_geo["HPOS"] + prev_geo["WIDTH"])
        if gap > 0:
            sp = ET.SubElement(line_el, _qname("SP"))
            sp.set("HPOS", _format(round(prev_geo["HPOS"] + prev_geo["WIDTH"], 3)))
            sp.set("VPOS", _format(prev_geo["VPOS"]))
            sp.set("WIDTH", _format(round(gap, 3)))

    string_el = ET.SubElement(line_el, _qname("String"))
    string_el.set("ID", f"word_{seq}")
    for key in ("HPOS", "VPOS", "WIDTH", "HEIGHT"):
        string_el.set(key, _format(geo[key]))
    string_el.set("CONTENT", _xml_safe(str(word.get("text") or "")))
    conf = word.get("confidence")
    if conf is not None:
        try:
            wc = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            pass
        else:
            string_el.set("WC", _format(round(wc, 4)))


def _processed_at(raw: Any) -> str:
    """处理时间戳：优先任务完成时刻，保证重复导出逐字节一致。"""
    if isinstance(raw, datetime):
        return raw.isoformat()
    if isinstance(raw, str) and raw:
        return raw
    return datetime.min.isoformat()


def build_alto_xml(request_payload: Dict[str, Any], result: Dict[str, Any],
                   *, task_id: str,
                   processed_at: Optional[Any] = None) -> bytes:
    """把已完成任务结果序列化为 ALTO 4.3 文档（UTF-8 字节）。

    只读：不修改入参，不访问数据库；块/行/词严格按结果中的顺序展开。
    """
    page_info = result.get("page") or {}
    page_width = _num(page_info.get("width", request_payload.get("page_width")))
    page_height = _num(page_info.get("height", request_payload.get("page_height")))

    lines_by_order: Dict[Any, Dict[str, Any]] = {}
    for line in result.get("lines", []):
        lines_by_order[line.get("order", line.get("id"))] = line

    blocks = result.get("blocks", [])

    # 结果块类型 -> 去重后的 LayoutTag 定义（块类型标签只出现一次）
    block_types: List[str] = []
    for block in blocks:
        kind = str(block.get("type") or "text")
        if kind not in block_types:
            block_types.append(kind)
    tag_id_by_type: Dict[str, str] = {
        kind: f"tag_block_type_{i + 1}" for i, kind in enumerate(block_types)
    }

    # ---- 根节点 + 固定的命名空间 / Schema 位置 ----
    root = ET.Element(_qname("alto"))
    root.set(f"{{{XSI_NS}}}schemaLocation", SCHEMA_LOCATION)

    # ---- Description：单位与处理元数据（时间戳取任务完成时刻） ----
    desc = ET.SubElement(root, _qname("Description"))
    ET.SubElement(desc, _qname("MeasurementUnit")).text = "pixel"
    ocr = ET.SubElement(desc, _qname("OCRProcessing"))
    ocr.set("ID", "ocr_processing_1")
    step = ET.SubElement(ocr, _qname("ocrProcessingStep"))
    ET.SubElement(step, _qname("processingCategory")).text = "contentGeneration"
    ET.SubElement(step, _qname("processingDateTime")).text = _processed_at(processed_at)
    software = ET.SubElement(step, _qname("processingSoftware"))
    ET.SubElement(software, _qname("softwareName")).text = SOFTWARE_NAME
    ET.SubElement(software, _qname("softwareVersion")).text = SOFTWARE_VERSION

    # ---- Tags：文本块分类（ALTO TextBlock 无 TYPE 属性，类型经 LayoutTag 表达） ----
    if block_types:
        tags = ET.SubElement(root, _qname("Tags"))
        for kind in block_types:
            tag_el = ET.SubElement(tags, _qname("LayoutTag"))
            tag_el.set("ID", tag_id_by_type[kind])
            tag_el.set("TYPE", kind)
            tag_el.set("LABEL", kind)

    # ---- Layout / Page / PrintSpace ----
    layout = ET.SubElement(root, _qname("Layout"))
    page = ET.SubElement(layout, _qname("Page"))
    page.set("ID", "page_1")
    page.set("PHYSICAL_IMG_NR", "1")
    page.set("WIDTH", _format(page_width))
    page.set("HEIGHT", _format(page_height))

    print_space = ET.SubElement(page, _qname("PrintSpace"))
    print_space.set("ID", "printspace_1")
    print_space.set("HPOS", "0")
    print_space.set("VPOS", "0")
    print_space.set("WIDTH", _format(page_width))
    print_space.set("HEIGHT", _format(page_height))

    # ---- 文本块（结果顺序即阅读顺序） ----
    word_seq = 0  # String 编号全文递增，保证 ID 唯一
    for block in blocks:
        block_el = ET.SubElement(print_space, _qname("TextBlock"))
        block_el.set("ID", f"block_{block.get('id')}")
        kind = str(block.get("type") or "text")
        block_el.set("TAGREFS", tag_id_by_type[kind])
        _set_geo(block_el, _bbox_attrs(block.get("bbox")))

        # line_ids 严格按结果中记录的顺序展开，保证阅读顺序稳定
        for line_id in block.get("line_ids", []):
            line = lines_by_order.get(line_id)
            if line is None:
                continue
            line_el = ET.SubElement(block_el, _qname("TextLine"))
            line_el.set("ID", f"line_{line.get('order', line.get('id'))}")
            _set_geo(line_el, _bbox_attrs(line.get("bbox")))

            prev_geo: Optional[Dict[str, float]] = None
            for word in line.get("words", []):
                geo = _bbox_attrs(word.get("quad"))
                if geo is None:
                    # 结果词框必然带合法四角；缺几何时无法生成 ALTO 坐标，跳过
                    continue
                word_seq += 1
                _append_word(line_el, word_seq, word, geo, prev_geo)
                prev_geo = geo

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return (XML_DECLARATION + body).encode("utf-8")
