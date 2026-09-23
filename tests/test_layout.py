"""版面排序算法测试。"""
from app.layout import analyze_layout
from tests.conftest import make_word


def _line_texts(result):
    return [ln["text"] for ln in result["lines"]]


def test_single_column_reading_order():
    """单栏：严格自上而下。"""
    words = []
    lines = ["alpha beta", "gamma delta", "epsilon zeta", "eta theta"]
    for li, line in enumerate(lines):
        x = 100
        for tok in line.split():
            words.append(make_word(len(words), tok, x, 100 + li * 60, 70, 30))
            x += 90

    result = analyze_layout(1000, 1400, words)
    assert _line_texts(result) == lines
    assert result["ordered_text"] == "\n".join(lines)
    assert result["block_count"] == 1
    assert result["unclassified"] == []


def test_two_columns_column_major_order():
    """双栏：左栏全部行读完，再读右栏；行间顺序保持自上而下。"""
    words = []
    left = [f"L{i} word" for i in range(4)]
    right = [f"R{i} word" for i in range(4)]
    for li, tok in enumerate(left):
        x = 80
        for part in tok.split():
            words.append(make_word(len(words), part, x, 100 + li * 80, 60, 30))
            x += 75
    for li, tok in enumerate(right):
        x = 540
        for part in tok.split():
            words.append(make_word(len(words), part, x, 100 + li * 80, 60, 30))
            x += 75

    result = analyze_layout(1000, 1400, words)
    texts = _line_texts(result)
    assert texts == left + right
    assert result["block_count"] == 2
    assert {b["type"] for b in result["blocks"]} == {"column"}


def test_spanning_title_before_columns():
    """跨栏标题排在最前，之后左栏、右栏。"""
    words = [make_word(0, "Document Title", 300, 60, 380, 40)]
    for i in range(3):
        words.append(make_word(len(words), f"left{i}", 80, 200 + i * 80, 120, 30))
        words.append(make_word(len(words), f"right{i}", 560, 200 + i * 80, 120, 30))

    result = analyze_layout(1000, 1400, words)
    texts = _line_texts(result)
    assert texts[0] == "Document Title"
    body = texts[1:]
    assert body == [f"left{i}" for i in range(3)] + [f"right{i}" for i in range(3)]


def test_skewed_page_still_ordered():
    """整页倾斜 5 度：估计倾斜角，行聚类与阅读顺序保持稳定。"""
    words = []
    for i in range(3):
        x = 80
        for tok in (f"A{i}", f"B{i}"):
            words.append(make_word(len(words), tok, x, 120 + i * 90, 60, 30, skew=5))
            x += 90
    for i in range(3):
        x = 540
        for tok in (f"C{i}", f"D{i}"):
            words.append(make_word(len(words), tok, x, 120 + i * 90, 60, 30, skew=5))
            x += 90

    result = analyze_layout(1000, 1400, words)
    assert abs(result["deskew_angle"] - 5.0) < 1.0
    texts = _line_texts(result)
    assert texts == [f"A{i} B{i}" for i in range(3)] + [f"C{i} D{i}" for i in range(3)]


def test_uneven_line_spacing():
    """行距不均（同段落内行距波动）不影响行的先后顺序，也不切碎版面。"""
    words = []
    ys = [100, 155, 225, 285, 350, 410]
    for i, y in enumerate(ys):
        words.append(make_word(i, f"line{i}", 100, y, 90, 30))

    result = analyze_layout(1000, 1400, words)
    assert _line_texts(result) == [f"line{i}" for i in range(len(ys))]
    assert result["block_count"] == 1


def test_paragraph_and_page_zones():
    """标题/正文/页脚式的大间隔切为横向区域，顺序自上而下。"""
    words = [make_word(0, "HEADER TITLE", 300, 80, 300, 40)]
    for i in range(3):
        words.append(make_word(1 + i, f"body{i}", 100, 300 + i * 55, 90, 30))
    words.append(make_word(4, "99", 480, 1200, 40, 30))
    result = analyze_layout(1000, 1400, words)
    texts = _line_texts(result)
    assert texts[0] == "HEADER TITLE"
    assert texts[1:4] == ["body0", "body1", "body2"]
    assert texts[-1] == "99"


def test_unclassified_words():
    """空文本 / 退化词框进入 unclassified，不参与排序。"""
    words = [
        make_word(0, "good", 100, 100, 80, 30),
        {  # 空文本
            "id": 1, "text": "  ", "confidence": 0.5,
            "quad": [{"x": 100, "y": 200}, {"x": 180, "y": 200},
                     {"x": 180, "y": 230}, {"x": 100, "y": 230}],
        },
        {  # 退化（四点共线）
            "id": 2, "text": "degenerate", "confidence": 0.9,
            "quad": [{"x": 0, "y": 0}, {"x": 10, "y": 0},
                     {"x": 20, "y": 0}, {"x": 30, "y": 0}],
        },
    ]
    result = analyze_layout(1000, 1400, words)
    reasons = {u["id"]: u["reason"] for u in result["unclassified"]}
    assert reasons == {1: "empty_text", 2: "invalid_quad"}
    assert _line_texts(result) == ["good"]


def test_words_sorted_along_baseline_within_line():
    """输入顺序打乱时，行内词仍按基线投影从左到右排列。"""
    w1 = make_word(0, "first", 100, 100, 60, 30)
    w2 = make_word(1, "second", 190, 100, 70, 30)
    w3 = make_word(2, "third", 290, 100, 60, 30)
    result = analyze_layout(1000, 1400, [w3, w1, w2])
    assert _line_texts(result) == ["first second third"]
    assert [w["id"] for w in result["lines"][0]["words"]] == [0, 1, 2]


def test_empty_request():
    result = analyze_layout(1000, 1400, [])
    assert result["lines"] == []
    assert result["ordered_text"] == ""
    assert result["line_count"] == 0


def test_three_columns_order():
    """三栏：左、中、右依次，每栏自上而下。"""
    words = []
    col_x = [60, 380, 700]
    for ci, x0 in enumerate(col_x):
        for ri in range(3):
            words.append(make_word(len(words), f"c{ci}r{ri}", x0,
                                   100 + ri * 70, 100, 30))
    result = analyze_layout(1000, 1400, words)
    expected = [f"c{ci}r{ri}" for ci in range(3) for ri in range(3)]
    assert _line_texts(result) == expected


def test_realistic_two_column_words():
    """宽词框、错位行高的双栏仍按列阅读。"""
    words = []
    for i, y in enumerate([100, 175, 260]):
        words.append(make_word(len(words), f"Left{i}", 90, y, 200, 34))
    for i, y in enumerate([105, 182, 258, 340]):
        words.append(make_word(len(words), f"Right{i}", 560, y, 210, 34))
    result = analyze_layout(1000, 1400, words)
    texts = _line_texts(result)
    assert texts[:3] == ["Left0", "Left1", "Left2"]
    assert texts[3:] == ["Right0", "Right1", "Right2", "Right3"]


def test_cjk_text_no_spurious_spaces():
    """中日韩文字词之间不插入空格，拉丁文之间插入空格。"""
    words = [
        make_word(0, "你好", 100, 100, 80, 30),
        make_word(1, "世界", 190, 100, 80, 30),
        make_word(2, "Hello", 100, 200, 80, 30),
        make_word(3, "World", 195, 200, 80, 30),
    ]
    result = analyze_layout(1000, 1400, words)
    texts = _line_texts(result)
    assert "你好世界" in texts
    assert "Hello World" in texts


def test_baseline_angles_reported():
    """行基线角度字段存在且数值合理。"""
    words = [
        make_word(0, "a", 100, 100, 50, 30, skew=3),
        make_word(1, "b", 180, 104, 50, 30, skew=3),
    ]
    result = analyze_layout(1000, 1400, words)
    assert len(result["lines"]) == 1
    # 倾斜角应被准确估计并校正
    assert abs(result["deskew_angle"] - 3.0) < 0.5
    # 两个词仍聚成同一行（倾斜没有破坏行聚类）
    assert result["lines"][0]["text"] == "a b"
