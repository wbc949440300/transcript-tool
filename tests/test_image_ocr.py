"""图文按需 OCR：先认前 N 张，之后"图片不再带字"就停。"""
import pytest

from app import extractor as ex
from app import ocr as ocr_mod
from app import utils as utils_mod


class _Resp:
    # 2026-11-03 起取图走 http_get_bytes（带退避重试），它会 dict(resp.headers)，
    # 所以假响应必须给 headers，否则 AttributeError 被吞掉 → OCR 一次都没被调用。
    headers: dict = {}

    def read(self):
        return b"fake-image-bytes"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def open(self, req, timeout=None):
        return _Resp()


@pytest.fixture
def patched(monkeypatch):
    """把网络与 OCR 都换掉，并屏蔽 0.5s 节流。"""
    monkeypatch.setattr(utils_mod, "http_opener", lambda *a, **k: _Opener())
    monkeypatch.setattr(utils_mod, "throttle", lambda *a, **k: 0.0)
    calls = []

    def install(pages):
        def fake_ocr(data):
            calls.append(len(calls))
            return pages[len(calls) - 1] if len(calls) <= len(pages) else ""
        monkeypatch.setattr(ocr_mod, "ocr_image", fake_ocr)

    return calls, install


def test_stops_when_images_stop_carrying_text(patched):
    calls, install = patched
    pages = ["正文甲" * 30, "正文乙" * 30, "", "", ""]
    install(pages)
    urls = [f"https://img.example/{i}.jpg" for i in range(5)]
    text = ex._ocr_images(urls)

    assert "正文甲" in text and "正文乙" in text
    # 前两张有字；第 3、4、5 张都没字 → 连续三张"静默"才停（第 5 张后）
    assert len(calls) == 5


def test_short_verse_images_are_not_cut_off(patched):
    """短句图文：每张只有十几个字，也不能被"静默"规则提前掐掉。"""
    calls, install = patched
    pages = ["心乱则愚起" * 2] * 12          # 每张约 10 字
    install(pages)
    urls = [f"https://img.example/{i}.jpg" for i in range(12)]
    text = ex._ocr_images(urls)
    assert len(calls) == 12, "每张都有字 → 应扫到 MAX/全部"
    assert len(text) > 100


def test_always_scans_at_least_first_images(patched):
    calls, install = patched
    install(["", "", "", "", ""])          # 全都没字
    urls = [f"https://img.example/{i}.jpg" for i in range(5)]
    assert ex._ocr_images(urls) == ""
    # 至少要认到 FIRST（默认 3）张才允许停；第 3 张时"最近两张都没字" → 停
    assert len(calls) == 3


def test_quiet_zero_never_stops_early(patched, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_IMAGE_OCR_QUIET", "0")
    calls, install = patched
    install(["", "", "", "", ""])
    urls = [f"https://img.example/{i}.jpg" for i in range(5)]
    ex._ocr_images(urls)
    assert len(calls) == 5


def test_max_caps_image_count(patched, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_IMAGE_OCR_MAX", "2")
    monkeypatch.setenv("TRANSCRIPT_IMAGE_OCR_FIRST", "1")
    calls, install = patched
    install(["字" * 50] * 3)
    urls = [f"https://img.example/{i}.jpg" for i in range(6)]
    ex._ocr_images(urls)
    assert len(calls) == 2


def test_empty_urls_returns_empty(patched):
    calls, install = patched
    install([])
    assert ex._ocr_images([]) == ""
    assert calls == []