"""测试夹具：使用临时 SQLite 数据库。"""
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["OCR_DB_PATH"] = os.path.join(_tmp, "test_ocr.db")


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_word(word_id, text, x, y, w, h, conf=0.99, skew=0.0):
    """生成轴对齐（或整体旋转 skew 度）的词框四角点。"""
    import math
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    if skew:
        t = math.radians(skew)
        c, s = math.cos(t), math.sin(t)
        pts = [(px * c - py * s, px * s + py * c) for px, py in pts]
    return {
        "id": word_id,
        "text": text,
        "confidence": conf,
        "quad": [{"x": px, "y": py} for px, py in pts],
    }
