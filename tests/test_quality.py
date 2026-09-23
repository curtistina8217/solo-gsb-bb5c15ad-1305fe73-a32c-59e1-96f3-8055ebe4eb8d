"""词框质量报告的规则测试（确定性、不改结果）。"""
import copy

from app.layout import analyze_layout
from app.quality import build_quality_report
from tests.conftest import make_word


def _report(words, page_width=1000, page_height=1400):
    payload = {"page_width": page_width, "page_height": page_height, "words": words}
    result = analyze_layout(page_width, page_height, words)
    return payload, result, build_quality_report(payload, result)


def _kinds(report, word_id):
    return [(i["kind"], i["severity"]) for i in report["issues"]
            if i["word_id"] == word_id]


ISSUE_FIELDS = {"word_id", "line_id", "kind", "severity", "reason", "suggestion"}


def test_clean_page_has_no_issues():
    words = []
    for li in range(3):
        x = 100
        for tok in ("alpha", "beta", "gamma"):
            words.append(make_word(len(words), tok, x, 100 + li * 60, 70, 30))
            x += 90

    _, _, report = _report(words)
    assert report["issues"] == []
    s = report["summary"]
    assert s["total"] == 0
    assert s["lines_checked"] == 3
    assert s["words_checked"] == 9
    assert s["unclassified_count"] == 0
    assert set(s["by_kind"]) == {"low_confidence", "size_outlier",
                                 "overlap", "gap_anomaly"}
    assert set(s["by_severity"]) == {"high", "medium", "low"}


def test_low_confidence_thresholds():
    words = [
        make_word(0, "a", 100, 100, 60, 30, conf=0.95),
        make_word(1, "b", 175, 100, 60, 30, conf=0.60),   # medium
        make_word(2, "c", 250, 100, 60, 30, conf=0.40),   # high
        make_word(3, "d", 325, 100, 60, 30),              # 缺省 0.99
    ]
    _, _, report = _report(words)
    assert _kinds(report, 1) == [("low_confidence", "medium")]
    assert _kinds(report, 2) == [("low_confidence", "high")]
    assert _kinds(report, 0) == _kinds(report, 3) == []
    assert report["summary"]["by_kind"]["low_confidence"] == 2
    issue = next(i for i in report["issues"] if i["word_id"] == 2)
    assert set(issue) == ISSUE_FIELDS
    assert "0.400" in issue["reason"]


def test_size_outlier_high():
    words = [
        make_word(0, "a", 100, 100, 80, 30),
        make_word(1, "b", 190, 100, 80, 30),
        make_word(2, "c", 280, 100, 80, 30),
        make_word(3, "d", 370, 100, 80, 30),
        make_word(4, "BIG", 460, 60, 80, 110),  # 字高显著离群
        make_word(5, "e", 550, 100, 80, 30),
    ]
    _, result, report = _report(words)
    assert len(result["lines"]) == 1  # 几何上仍在同一行
    assert _kinds(report, 4) == [("size_outlier", "high")]
    issue = next(i for i in report["issues"] if i["kind"] == "size_outlier")
    assert "字号中位数" in issue["reason"]
    assert issue["line_id"] == result["lines"][0]["order"]


def test_size_outlier_medium():
    words = [
        make_word(0, "a", 100, 100, 80, 30),
        make_word(1, "b", 190, 100, 80, 30),
        make_word(2, "c", 280, 100, 80, 30),
        make_word(3, "d", 370, 100, 80, 30),
        make_word(4, "odd", 460, 85, 80, 60),  # z≈5.6 -> medium
    ]
    _, _, report = _report(words)
    assert _kinds(report, 4) == [("size_outlier", "medium")]


def test_overlap_flags_both_words():
    words = [
        make_word(0, "a", 100, 100, 100, 30),
        make_word(1, "b", 160, 100, 100, 30),  # 水平侵入 40%
        make_word(2, "c", 300, 100, 100, 30),
    ]
    _, _, report = _report(words)
    assert _kinds(report, 0) == [("overlap", "medium")]
    assert _kinds(report, 1) == [("overlap", "medium")]
    assert _kinds(report, 2) == []
    reason = report["issues"][0]["reason"]
    assert "重叠" in reason


def test_gap_anomaly_flags_left_word_once():
    words = [
        make_word(0, "a", 100, 100, 80, 30),
        make_word(1, "b", 190, 100, 80, 30),
        make_word(2, "c", 280, 100, 80, 30),
        make_word(3, "d", 370, 100, 80, 30),
        make_word(4, "e", 495, 100, 80, 30),  # 前间隙 45，典型 10
        make_word(5, "f", 585, 100, 80, 30),
    ]
    _, result, report = _report(words)
    assert len(result["lines"]) == 1  # 仍聚成一行才谈得上行内间隙
    assert _kinds(report, 3) == [("gap_anomaly", "medium")]
    assert _kinds(report, 4) == []  # 间隙只挂左侧词
    assert report["summary"]["by_kind"]["gap_anomaly"] == 1


def test_issues_sorted_by_original_id():
    # 输入顺序与 id 顺序故意不一致
    words = [
        make_word(30, "a", 100, 100, 60, 30, conf=0.3),
        make_word(2, "b", 175, 100, 60, 30, conf=0.3),
        make_word(17, "c", 250, 100, 60, 30, conf=0.3),
        make_word(1, "d", 325, 100, 60, 30, conf=0.3),
    ]
    _, _, report = _report(words)
    ids = [i["word_id"] for i in report["issues"]]
    assert ids == sorted(ids) == [1, 2, 17, 30]


def test_report_is_deterministic_and_does_not_mutate_result():
    words = [
        make_word(0, "a", 100, 100, 80, 30, conf=0.4),
        make_word(1, "b", 190, 100, 80, 30),
        make_word(2, "c", 280, 100, 80, 30),
    ]
    payload, result, report1 = _report(words)
    snapshot = copy.deepcopy(result)
    report2 = build_quality_report(payload, result)
    assert report1 == report2
    assert result == snapshot  # 结果对象未被改写


def test_unclassified_words_counted_not_checked():
    words = [
        make_word(0, "ok", 100, 100, 80, 30),
        # 空文本（合法四角）-> empty_text，不进任何行
        {
            "id": 1, "text": "   ", "confidence": 0.9,
            "quad": [
                {"x": 300, "y": 100}, {"x": 380, "y": 100},
                {"x": 380, "y": 130}, {"x": 300, "y": 130},
            ],
        },
    ]
    _, result, report = _report(words)
    assert [u["reason"] for u in result["unclassified"]] == ["empty_text"]
    s = report["summary"]
    assert s["unclassified_count"] == 1
    assert s["words_checked"] == 1
    assert report["issues"] == []


def test_report_works_on_skewed_page():
    words = [
        make_word(0, "alpha", 100, 100, 80, 30, skew=1.5),
        make_word(1, "beta", 190, 100, 80, 30, conf=0.2, skew=1.5),
        make_word(2, "gamma", 280, 100, 80, 30, skew=1.5),
    ]
    _, _, report = _report(words)
    assert _kinds(report, 1) == [("low_confidence", "high")]
