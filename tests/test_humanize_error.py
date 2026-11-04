"""错误提示不能被"再人话化"降级。

背景（2026-10-31 实测）：小红书那条可操作提示里含"笔记已删除/受限"，
被 `_humanize_error` 末尾的"已删除 / 地区限制"规则再次改写，用户看到的成了
通用文案，真正的建议（去 App 重新复制链接）只剩日志里有。
"""
from app.extractor import _humanize_error

XHS_GUIDE = ("笔记无法访问：链接可能缺少 xsec_token 或笔记已删除/受限。"
             "请在小红书 App 里重新「分享 → 复制链接」后再试。")


def test_xsec_token_guidance_survives():
    assert _humanize_error(RuntimeError(XHS_GUIDE), "小红书") == XHS_GUIDE


def test_guidance_survives_even_without_platform():
    assert _humanize_error(RuntimeError(XHS_GUIDE), "") == XHS_GUIDE


def test_raw_deleted_still_gets_generic():
    out = _humanize_error(RuntimeError("ERROR: this video has been deleted"), "抖音")
    assert out == "该内容已删除或存在地区限制，无法提取。"


def test_raw_url_echo_with_token_not_treated_as_guidance():
    """yt-dlp 原样回显的 URL 里也含 xsec_token，不能当成"已是人话"放过去。"""
    raw = ("ERROR: Unsupported URL: https://www.xiaohongshu.com/404?redirectPath="
           "https%3A%2F%2Fwww.xiaohongshu.com%2Fexplore%2Fabc%3Fxsec_token%3DABN0000"
           "&error_code=300031")
    out = _humanize_error(RuntimeError(raw), "小红书")
    assert out != raw
    assert "登录" in out


def test_300031_still_login_hint():
    out = _humanize_error(RuntimeError("error_code 300031"), "小红书")
    assert "登录" in out


def test_no_video_formats_xhs_still_image_hint():
    out = _humanize_error(RuntimeError("ERROR: [XiaoHongShu] abc: No video formats found!"), "小红书")
    assert "图文笔记" in out


def test_cooldown_message_passes_through():
    msg = "平台冷却中：还需 900 秒"
    assert _humanize_error(RuntimeError(msg), "抖音") == msg