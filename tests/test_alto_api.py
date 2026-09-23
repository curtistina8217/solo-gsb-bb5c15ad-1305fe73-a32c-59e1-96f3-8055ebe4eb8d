"""ALTO XML 导出接口端到端测试。"""
import json
import xml.etree.ElementTree as ET

from tests.conftest import make_word
from tests.test_api import _submit, _wait

ALTO_NS = "http://www.loc.gov/standards/alto/ns-v4#"
NS = {"a": ALTO_NS}


def _alto(client, task_id):
    return client.get(f"/api/tasks/{task_id}/alto.xml")


def _parse(text):
    root = ET.fromstring(text)
    assert root.tag == f"{{{ALTO_NS}}}alto"
    return root


def test_alto_export_basic_structure_and_reading_order(client):
    words = [
        make_word(0, "hello", 100, 100, 80, 30, conf=0.99),
        make_word(1, "world", 190, 100, 80, 30, conf=0.8),
        make_word(2, "second line", 100, 170, 140, 30, conf=0.9),
    ]
    task_id = _submit(client, words)
    _wait(client, task_id)

    resp = _alto(client, task_id)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/xml"
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="{task_id}.alto.xml"'
    )
    assert resp.content.startswith(
        b'<?xml version="1.0" encoding="UTF-8"?>')

    root = _parse(resp.text)
    # xsd schemaLocation 指向 ALTO 4.3
    assert "alto-4-3.xsd" in root.attrib[
        "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"]

    page = root.find("a:Layout/a:Page", NS)
    assert page.get("WIDTH") == "1000"
    assert page.get("HEIGHT") == "1400"
    ps = page.find("a:PrintSpace", NS)
    assert ps.get("HPOS") == "0" and ps.get("VPOS") == "0"
    assert ps.get("WIDTH") == "1000" and ps.get("HEIGHT") == "1400"

    blocks = page.findall(".//a:TextBlock", NS)
    assert len(blocks) >= 1

    lines = page.findall(".//a:TextLine", NS)
    line_text = ["".join(s.get("CONTENT") for s in ln.findall("a:String", NS))
                 for ln in lines]
    # 行顺序与结果阅读顺序一致（hello world 在前，second line 在后）
    assert line_text == ["helloworld", "second line"]

    strings = page.findall(".//a:String", NS)
    assert [s.get("CONTENT") for s in strings] == [
        "hello", "world", "second line"]
    assert [s.get("WC") for s in strings] == ["0.99", "0.8", "0.9"]


def test_alto_word_keeps_original_quad_bounding_box(client):
    # 整体倾斜 2°：词外接框必须取原始（倾斜）四角，而不是校正后坐标
    words = [make_word(7, "tilted", 100, 100, 100, 40, conf=0.91, skew=2.0)]
    task_id = _submit(client, words)
    result = _wait(client, task_id)["result"]

    resp = _alto(client, task_id)
    root = _parse(resp.text)
    s = root.find(".//a:String", NS)

    raw_quad = result["lines"][0]["words"][0]["quad"]
    xs = [p[0] for p in raw_quad]
    ys = [p[1] for p in raw_quad]
    assert s.get("HPOS") == str(round(min(xs), 3))
    assert s.get("VPOS") == str(round(min(ys), 3))
    assert s.get("WIDTH") == str(round(max(xs) - min(xs), 3))
    assert s.get("HEIGHT") == str(round(max(ys) - min(ys), 3))
    assert s.get("ID") == "word_1"


def test_alto_block_and_line_order_matches_result(client):
    words = []
    for i in range(3):
        words.append(make_word(100 + i, f"L{i}", 80, 100 + i * 80, 80, 30))
    for i in range(3):
        words.append(make_word(200 + i, f"R{i}", 560, 100 + i * 80, 80, 30))
    task_id = _submit(client, words)
    result = _wait(client, task_id)["result"]

    root = _parse(_alto(client, task_id).text)
    block_els = root.findall(".//a:TextBlock", NS)

    # 块顺序、块内行顺序与结果 line_ids 完全一致
    expected_line_ids = [lid for b in result["blocks"] for lid in b["line_ids"]]
    got_line_ids = [
        int(ln.get("ID").removeprefix("line_"))
        for b in block_els
        for ln in b.findall("a:TextLine", NS)
    ]
    assert got_line_ids == expected_line_ids

    # ALTO TextBlock 无 TYPE 属性：块类型经 Tags/LayoutTag + TAGREFS 表达
    tags = {t.get("ID"): t.get("TYPE")
            for t in root.findall("a:Tags/a:LayoutTag", NS)}
    assert set(tags.values()) == {b["type"] for b in result["blocks"]}
    for block_el, block in zip(block_els, result["blocks"]):
        assert "TYPE" not in block_el.attrib
        assert tags[block_el.get("TAGREFS")] == block["type"]


def test_alto_space_elements_between_words(client):
    words = [
        make_word(0, "a", 100, 100, 30, 30),
        make_word(1, "b", 140, 100, 30, 30),  # 与 a 之间 10px 间隙
    ]
    task_id = _submit(client, words)
    _wait(client, task_id)

    root = _parse(_alto(client, task_id).text)
    line = root.find(".//a:TextLine", NS)
    children = [c.tag.split("}")[1] for c in line]
    assert children == ["String", "SP", "String"]
    sp = line.find("a:SP", NS)
    assert sp.get("HPOS") == "130"
    assert sp.get("WIDTH") == "10"


def test_alto_escapes_xml_special_characters(client):
    words = [
        make_word(0, "a & b < c > d \"e\" 'f'", 100, 100, 400, 30),
    ]
    task_id = _submit(client, words)
    _wait(client, task_id)

    resp = _alto(client, task_id)
    # 原始字节中是实体转义，不出现裸露的 & 字符（除实体外）
    assert b"a &amp; b &lt; c &gt; d &quot;e&quot;" in resp.content
    # 解析回滚后仍是原文
    root = _parse(resp.text)
    assert root.find(".//a:String", NS).get("CONTENT") == 'a & b < c > d "e" \'f\''


def test_alto_repeated_is_stable(client):
    words = [make_word(0, "hello", 100, 100, 80, 30, conf=0.99)]
    task_id = _submit(client, words)
    _wait(client, task_id)
    r1 = _alto(client, task_id).content
    r2 = _alto(client, task_id).content
    assert r1 == r2


def test_alto_processing_time_uses_task_finished_at(client):
    words = [make_word(0, "hello", 100, 100, 80, 30)]
    task_id = _submit(client, words)
    body = _wait(client, task_id)

    root = _parse(_alto(client, task_id).text)
    dt = root.find(".//a:processingDateTime", NS).text
    assert dt == body["finished_at"]


def test_alto_empty_page(client):
    task_id = _submit(client, [])
    _wait(client, task_id)

    resp = _alto(client, task_id)
    assert resp.status_code == 200
    root = _parse(resp.text)
    assert root.findall(".//a:String", NS) == []
    assert root.find("a:Tags", NS) is None
    page = root.find("a:Layout/a:Page", NS)
    assert page.get("WIDTH") == "1000"


def test_alto_does_not_modify_task(client):
    words = [make_word(0, "hello", 100, 100, 80, 30)]
    task_id = _submit(client, words)
    before = _wait(client, task_id)
    _alto(client, task_id)
    after = client.get(f"/api/tasks/{task_id}").json()
    assert after["status"] == "done"
    assert after["result"] == before["result"]
    assert after["finished_at"] == before["finished_at"]


def test_alto_unknown_task_404(client):
    resp = _alto(client, "no-such-task")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "task not found"}


def test_alto_pending_task_409(client):
    from app import db
    db.create_task("pending-alto-1",
                   json.dumps({"page_width": 1, "page_height": 1, "words": []}))
    resp = _alto(client, "pending-alto-1")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "task not finished: status=pending"
    assert db.get_task("pending-alto-1")["status"] == "pending"


def test_alto_failed_task_409(client):
    from app import db
    db.create_task("failed-alto-1",
                   json.dumps({"page_width": 1, "page_height": 1, "words": []}))
    db.mark_status("failed-alto-1", "failed", error="boom")
    resp = _alto(client, "failed-alto-1")
    assert resp.status_code == 409
    assert "status=failed" in resp.json()["detail"]
