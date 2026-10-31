"""快手分享页解析加固：正常页面不被 JS 常量误判、风控只在"什么都没解析到"时触发。"""
import pytest

from app import kuaishou, utils

URL = "https://www.kuaishou.com/short-video/3x5gu3rc5sqd9ni"


class _Resp:
    def __init__(self, body: str):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, body: str):
        self._body = body

    def open(self, req, timeout=None):
        return _Resp(self._body)


@pytest.fixture(autouse=True)
def _clean_cooldown():
    utils.clear_platform_cooldown("kuaishou")
    yield
    utils.clear_platform_cooldown("kuaishou")


def _patch(monkeypatch, html: str) -> None:
    monkeypatch.setattr(kuaishou, "http_opener", lambda *a, **k: _Opener(html))


def _page(caption="标题", author="作者", mp4="https://x.ndcimgs.com/a.mp4", extra=""):
    return ('<html><script>window.__X={"caption":"%s","userName":"%s","duration":54000}'
            '</script><video src="%s"></video>%s</html>'
            % (caption, author, mp4, extra))


def test_normal_page_parses(monkeypatch):
    _patch(monkeypatch, _page())
    d = kuaishou.fetch_kuaishou(URL)
    assert d["title"] == "标题"
    assert d["author"] == "作者"
    assert d["duration"] == 54.0
    assert d["video_url"].endswith("a.mp4")


def test_risk_words_in_page_js_do_not_false_positive(monkeypatch):
    """页面 JS 里带"稍后再试"这类常量，不能把正常页面判成风控。"""
    _patch(monkeypatch, _page(extra="<script>var t='稍后再试';</script>"))
    d = kuaishou.fetch_kuaishou(URL)
    assert d["title"] == "标题"
    assert utils.platform_cooldown_info("kuaishou")["remaining"] == 0


def test_risk_marker_triggers_only_when_nothing_parsed(monkeypatch):
    _patch(monkeypatch, "<html><body>访问过于频繁，请完成验证</body></html>" + "x" * 5000)
    with pytest.raises(RuntimeError) as ei:
        kuaishou.fetch_kuaishou(URL)
    assert "冷却" in str(ei.value)
    assert utils.platform_cooldown_info("kuaishou")["remaining"] > 0


def test_result2_json_is_risk(monkeypatch):
    _patch(monkeypatch, '{"result":2}')
    with pytest.raises(RuntimeError) as ei:
        kuaishou.fetch_kuaishou(URL)
    assert "冷却" in str(ei.value)
    assert utils.platform_cooldown_info("kuaishou")["remaining"] > 0


def test_short_page_is_not_cooled(monkeypatch):
    _patch(monkeypatch, "<html>too short</html>")
    with pytest.raises(RuntimeError):
        kuaishou.fetch_kuaishou(URL)
    assert utils.platform_cooldown_info("kuaishou")["remaining"] == 0


def test_atlas_hint(monkeypatch):
    html = '<html><script>window.__X={"photoType":2,"atlas":{}}</script></html>' + "x" * 5000
    _patch(monkeypatch, html)
    with pytest.raises(RuntimeError) as ei:
        kuaishou.fetch_kuaishou(URL)
    assert "图集" in str(ei.value)


def test_page_fingerprint_shape():
    fp = kuaishou._page_fingerprint('{"caption":"a","userName":"b"}', "a", "b", 1.0, ["u"])
    assert "bytes=" in fp and "caption=1" in fp and "mp4=1" in fp and "keys=[" in fp
