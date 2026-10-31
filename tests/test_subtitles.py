"""字幕选择与解码：绝不允许把 URL / 二进制 / 弹幕当成文案。"""
import gzip
import zlib

from app import utils
from app.extractor import _pick_subtitle, _subtitle_keys


VTT = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n这是正文第一句\n\n"
       "00:00:04.000 --> 00:00:06.000\n这是一句\n这是一句\n")
SRT = "1\n00:00:01,000 --> 00:00:02,000\n第一句\n\n2\n00:00:03,000 --> 00:00:04,000\n第二句\n"


def test_vtt_data():
    assert _pick_subtitle({"requested_subtitles": {"zh-CN": {"data": VTT}}}) == \
        "这是正文第一句\n这是一句"


def test_srt_data():
    assert _pick_subtitle({"subtitles": {"zh": [{"data": SRT}]}}) == "第一句\n第二句"


def test_json_subtitle_youtube_json3():
    info = {"automatic_captions": {"en": [{"data":
            '{"events":[{"segs":[{"utf8":"hello"},{"utf8":"world"}]}]}'}]}}
    assert _pick_subtitle(info) == "hello\nworld"


def test_json_subtitle_bilibili_content():
    info = {"subtitles": {"zh-CN": [{"data":
            '{"body":[{"from":0,"to":1,"content":"第一句"},'
            '{"from":1,"to":2,"content":"第二句"}]}'}]}}
    assert _pick_subtitle(info) == "第一句\n第二句"


def test_danmaku_is_not_subtitle():
    assert _subtitle_keys({"danmaku": [{"url": "http://x"}]}) == []
    assert _pick_subtitle({"subtitles": {"danmaku": [{"url": "http://127.0.0.1:9/x.xml"}]}}) == ""


def test_url_is_never_returned_as_text():
    text = _pick_subtitle({"requested_subtitles": {"zh-CN": {"url": "http://127.0.0.1:9/x.srt"}}})
    assert "http" not in text


def test_list_shape_does_not_crash():
    assert _pick_subtitle({"subtitles": {"zh-CN": [{"url": "http://127.0.0.1:9/x.srt"}]}}) == ""


def test_binary_data_rejected():
    assert _pick_subtitle({"subtitles": {"zh": [{"data": "\x00\x01\x02" * 80}]}}) == ""


def test_prefers_chinese():
    info = {"subtitles": {"en": [{"data": "english text here"}],
                          "zh-CN": [{"data": "中文正文"}]}}
    assert _pick_subtitle(info) == "中文正文"


def test_no_subtitle_returns_empty():
    assert _pick_subtitle({"title": "x"}) == ""
    assert _pick_subtitle({}) == ""


def test_decode_gzip_text():
    assert utils._decode_subtitle_bytes(gzip.compress("第一句\n第二句".encode())) == "第一句\n第二句"


def test_decode_utf8_bom():
    assert utils._decode_subtitle_bytes(b"\xef\xbb\xbf" + "第一句".encode()) == "第一句"


def test_decode_utf16_bom():
    assert utils._decode_subtitle_bytes("第一句".encode("utf-16")) == "第一句"


def test_decode_binary_is_rejected():
    # zlib 压缩后的二进制：既不能当文本，也不能被 gb18030 之类"解"成汉字
    assert utils._decode_subtitle_bytes(zlib.compress(b"\x89PNG" + bytes(range(200)))) == ""
    assert utils._decode_subtitle_bytes(bytes(range(256)) * 3) == ""


def test_looks_like_text():
    assert utils.looks_like_text("正常中文文本")
    assert not utils.looks_like_text("tVR\x1bI\x16\x15˙7\x11j|2")
