"""视频号（微信）元数据：用 finder-preview 的 get_feed_info 拿标题/作者/封面/正文文案。

2026-11-04「链接模式」：实测证明匿名 get_feed_info 能返回**正文文案、作者、头像、
封面、互动数据、发布时间、话题标签**（见 F:\新版提取工具 的探针与交接文档），
唯独**不返回视频播放地址**（h264VideoInfo/h265VideoInfo/videoUrl 都不给，流是加密的）。
所以贴视频号链接可以直接建成一条 SUCCESS 任务展示这些信息，口播语音转文字仍走
「微信内下载 → 落盘 → 自动入队」。

背景：视频号没有可读的网页接口；元数据有一个公开预览接口可用（交接文档实测），
视频流则靠元宝登录态换带凭证的直链（2026-11-05 起不再使用外部下载器）：

    POST https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info
    body {"baseReq":{"generalToken":""},"shortUri":"<id>"}

拿到后（2026-11-05 起只做一件事）：用户在页面贴视频号链接时，把标题/作者/封面
**带回去给他看**，而不是只丢一句"不能解析"；配了元宝登录态则直接换直链下载转写。

注意：解析响应用的是容错遍历（接口字段名在不同版本里变过），所以即使字段改名也大概率能取到；
取不到就安静放弃，不影响主流程。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Optional

from .utils import http_opener, throttle

logger = logging.getLogger("video_channel")

INFO_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"

_SHORT_URI_PATTERNS = (
    re.compile(r"/sph/([A-Za-z0-9_-]{6,})"),
    re.compile(r"[?&]shortUri=([A-Za-z0-9_-]{6,})"),
    re.compile(r"/finder-preview/[^/]*?([A-Za-z0-9_-]{8,})"),
)


def short_uri_from(url: str) -> str:
    """从视频号链接里取 shortUri（取不到返回空串）。"""
    raw = (url or "").strip()
    for pat in _SHORT_URI_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1)
    return ""


def _walk_find(obj, keys: tuple[str, ...], depth: int = 0):
    """在嵌套 JSON 里找第一个命中的 key（容错：接口字段名变过）。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            got = _walk_find(v, keys, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj[:8]:
            got = _walk_find(v, keys, depth + 1)
            if got:
                return got
    return None


def _fmt_time(ts) -> str:
    """createtime（秒级时间戳）→ 'YYYY-MM-DD HH:MM'；异常返回空串。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:  # noqa: BLE001
        return ""


def tags_from_text(text: str) -> list[str]:
    """从正文里提话题标签：视频号写法是 '#DeepSeek #Super-agent'（无收尾 #）。"""
    out: list[str] = []
    for m in re.findall(r"#([^#\s\n]{1,24})", text or ""):
        t = m.strip(" #")
        if t and t not in out:
            out.append(t)
    return out[:12]


def parse_feed_info(payload: dict) -> dict:
    """从 get_feed_info 的响应里取元数据（取不到就留空）。

    2026-11-04 扩成「链接模式」：除了标题/作者/封面，还取**正文文案**、作者头像、
    发布信息、互动数据、话题标签、作品 ID（dynamicExportId）—— 贴链接就能直接
    给用户看（视频流拿不到，见模块 docstring 与交接文档实测）。
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    feed = data.get("feedInfo") if isinstance(data.get("feedInfo"), dict) else {}
    author_obj = data.get("authorInfo") if isinstance(data.get("authorInfo"), dict) else {}
    scene = data.get("sceneInfo") if isinstance(data.get("sceneInfo"), dict) else {}
    jump = feed.get("jumpInfo") if isinstance(feed.get("jumpInfo"), dict) else {}

    desc = (feed.get("description") or "").strip()
    title = (_walk_find(payload, ("desc", "objectDesc", "title", "feedDesc")) or "").strip()
    if not title and desc:
        # 视频号的"标题"就是正文第一句（接口不给独立标题字段）
        title = re.split(r"[。\n！!？?；;]", desc)[0][:40].strip() or desc[:40]
    author = (author_obj.get("nickname") or "").strip() or (
        _walk_find(payload, ("nickname", "authorName", "username", "nickName")) or "")
    cover = (feed.get("coverUrl") or "").strip() or (
        _walk_find(payload, ("coverUrl", "cover_url", "coverImgUrl", "thumbUrl", "cover")) or "")
    head = (author_obj.get("headImgUrl") or "").strip()

    def _cnt(*keys) -> str:
        for k in keys:
            v = feed.get(k)
            if v not in (None, ""):
                return str(v).strip()
        # 老响应形状（data.object.*，2026-10-31 那版）里数值在别处：兜底再走一遍全树
        for k in keys:
            v = _walk_find(payload, (k,))
            if v:
                return str(v).strip()
        return ""

    out = {
        "title": title, "author": author, "cover": cover,
        "likes": _cnt("likeCountFmt", "likeCount"),
        "favCount": _cnt("favCountFmt", "favCount"),
        "forwardCount": _cnt("forwardCountFmt", "forwardCount"),
        "commentCount": _cnt("commentCountFmt", "commentCount"),
        "description": desc,
        "headImg": head,
        "location": (jump.get("wording") or "").strip(),
        "publishAt": _fmt_time(feed.get("createtime")),
        "dynamicExportId": (scene.get("dynamicExportId") or "").strip(),
        "isVideo": not bool(feed.get("picInfo")),
        "tags": tags_from_text(desc),
    }
    return out if (out["title"] or out["author"] or out["cover"] or out["description"]) else {}


def fetch_feed_info(short_uri: str, timeout: float = 12.0) -> dict:
    """取视频号元数据；失败返回 {}（调用方按"没有元数据"处理）。

    timeout 12 秒（2026-11-05 收紧）：正常响应 1~2 秒，之前给 20 秒时
    一次 SSL 握手卡死就白等 20+ 秒；收紧后有重试兜底，等待可控。
    """
    uri = (short_uri or "").strip()
    if not uri:
        return {}
    body = json.dumps({"baseReq": {"generalToken": ""}, "shortUri": uri}).encode("utf-8")
    import urllib.request

    req = urllib.request.Request(INFO_API, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://channels.weixin.qq.com/",
        # 实测：缺 Origin 时接口返回 401 Unauthorized，补上才 201
        "Origin": "https://channels.weixin.qq.com",
    })
    throttle("channels.weixin.qq.com")
    try:
        with http_opener("").open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:  # noqa: BLE001
        logger.info("视频号元数据获取失败: %s", exc)
        return {}
    meta = parse_feed_info(data)
    if meta:
        logger.info("视频号元数据: 标题=%s 作者=%s 封面=%s 正文=%d字",
                    meta["title"][:20], meta["author"][:12],
                    "有" if meta["cover"] else "无", len(meta.get("description") or ""))
    return meta


# ---------- 元宝（yuanbao）换凭证 → 视频号直链 ----------
#
# 2026-11-04 实测结论：finder-preview 的 get_feed_info 用 shortUri 只能拿到
# 文字/封面/互动数据，**永远不给 videoUrl**；要拿 h264/h265 直链必须同时带
# generalToken + exportId 这对凭证。这对凭证由腾讯元宝的解析接口签发（需登录元宝），
# 所以整条链路是：
#   1) POST yuanbao get_parse_result  → playable_url（query 里带 token / eid）
#   2) POST finder-preview get_feed_info（body 用 exportId + generalToken）
#   3) GET 直链 → 若响应头有 X-enclen，前 N 字节用 ISAAC64(decodeKey) 做 XOR 解密
# 没配 cookies/yuanbao.txt 时本段全部不参与，调用方回落到原来的匿名元数据卡片。

YUANBAO_PARSE_API = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
_UA_CHANNELS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_UA_YUANBAO = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

# 元宝前端固定头（2026-11 抓自 yuanbao.tencent.com/chat/<agentid>/<conv>；
# 换版本时只需要动这里，业务参数只有 cookie + body）
_YB_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://yuanbao.tencent.com",
    "referer": "https://yuanbao.tencent.com/chat/naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
    "user-agent": _UA_YUANBAO,
    "t-userid": "b9575f6b0a8c4a55a08096904a5ef20a",
    "x-agentid": "naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
    "x-commit-tag": "72282a0d",
    "x-device-id": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
    "x-hy92": "e963067ffa31002b9646bd0c03000008b1951a",
    "x-hy93": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
    "x-id": "b9575f6b0a8c4a55a08096904a5ef20a",
    "x-instance-id": "5",
    "x-language": "zh-CN",
    "x-os_version": "Mac OS(10.15.7)-Blink",
    "x-platform": "mac",
    "x-requested-with": "XMLHttpRequest",
    "x-source": "web",
    "x-web-third-source": "main",
    "x-webdriver": "0",
    "x-webversion": "2.69.0",
}

_CH_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://channels.weixin.qq.com",
    "User-Agent": _UA_CHANNELS,
    "sec-ch-ua-mobile": "?0",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

_MASK64 = (1 << 64) - 1


class _Isaac64:
    """Go rand.RandCtx64 的移植（微信视频号媒体流 XOR 掩码）。"""

    GOLDEN = 0x9E3779B97F4A7C13

    def __init__(self, key: int):
        self.cnt = 255
        self.seed = [0] * 256
        self.mm = [0] * 256
        self.aa = self.bb = self.cc = 0
        self.seed[0] = key & _MASK64
        self._init()

    @staticmethod
    def _mix(v):
        a, b, c, d, e, f, g, h = v
        a -= e; f ^= h >> 9
        h += a; b -= f
        g ^= a << 9; a += b
        c -= g; h ^= b >> 23
        b += c; d -= h
        a ^= c << 15; c += d
        e -= a; b ^= d >> 14
        d += e; f -= b
        c ^= e << 20; e += f
        g -= c; d ^= f >> 17
        f += g; h -= d
        e ^= g << 14; g += h
        return [a & _MASK64, b & _MASK64, c & _MASK64, d & _MASK64,
                e & _MASK64, f & _MASK64, g & _MASK64, h & _MASK64]

    def _init(self):
        v = [self.GOLDEN] * 8
        for _ in range(4):
            v = self._mix(v)
        for i in range(0, 256, 8):
            v = [(v[j] + self.seed[i + j]) & _MASK64 for j in range(8)]
            v = self._mix(v)
            self.mm[i:i + 8] = v
        for i in range(0, 256, 8):
            v = [(v[j] + self.mm[i + j]) & _MASK64 for j in range(8)]
            v = self._mix(v)
            self.mm[i:i + 8] = v
        self._isaac64()

    def _isaac64(self):
        self.cc = (self.cc + 1) & _MASK64
        self.bb = (self.bb + self.cc) & _MASK64
        aa = self.aa
        for i in range(256):
            r = i % 4
            if r == 0:
                aa = (~(aa ^ ((aa << 21) & _MASK64))) & _MASK64
            elif r == 1:
                aa = (aa ^ (aa >> 5)) & _MASK64
            elif r == 2:
                aa = (aa ^ ((aa << 12) & _MASK64)) & _MASK64
            else:
                aa = (aa ^ (aa >> 33)) & _MASK64
            aa = (aa + self.mm[(i + 128) % 256]) & _MASK64
            x = self.mm[i]
            y = (self.mm[(x >> 3) % 256] + aa + self.bb) & _MASK64
            self.mm[i] = y
            self.bb = (self.mm[(y >> 11) % 256] + x) & _MASK64
            self.seed[i] = self.bb
        self.aa = aa

    def next64(self) -> int:
        r = self.seed[self.cnt]
        if self.cnt == 0:
            self._isaac64()
            self.cnt = 255
        else:
            self.cnt -= 1
        return r


def decrypt_xor(buf: bytearray, enc_len: int, key: int) -> bytearray:
    """前 enc_len 字节用 ISAAC64(key) 的大端字节流逐字节 XOR（原地）。"""
    if not buf or enc_len <= 0:
        return buf
    inst = _Isaac64(key)
    limit = min(enc_len, len(buf))
    i = 0
    while i < limit:
        kb = inst.next64().to_bytes(8, "big")
        for j in range(8):
            if i + j >= limit:
                return buf
            buf[i + j] ^= kb[j]
        i += 8
    return buf


def yuanbao_cookie() -> str:
    """读 cookies/yuanbao.txt（Netscape 或 `a=b; c=d` 都认），返回 Cookie 头。"""
    try:
        from .utils import COOKIE_DIR
    except Exception:  # noqa: BLE001
        COOKIE_DIR = Path("cookies")
    path = COOKIE_DIR / "yuanbao.txt"
    if not path.is_file() or path.stat().st_size <= 0:
        return ""
    pairs: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            f = line.split("\t")
            if len(f) >= 7 and f[5] and f[6]:
                pairs.append(f"{f[5].strip()}={f[6].strip()}")
        elif "=" in line:
            pairs.append(line)
    return "; ".join(pairs)


def _normalize_share_url(text: str) -> str:
    m = re.search(r"https?://[^\s\"'<>，。；、）)\]]+", text or "")
    if not m:
        return ""
    return m.group(0).rstrip("),.;，。")


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers=headers)
    with http_opener("").open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _pick_video(feed: dict) -> tuple[str, str]:
    """按页面同款优先级挑直链：h264 → h265 → 通用 videoUrl。"""
    for key, label in (("h264VideoInfo", "h264"), ("h265VideoInfo", "h265")):
        url = ((feed.get(key) or {}).get("videoUrl") or "").strip()
        if url:
            return url, label
    url = (feed.get("videoUrl") or "").strip()
    return (url, "default") if url else ("", "")


def _clean_video_url(url: str) -> str:
    """原样保留直链（只去首尾空白）。

    2026-11-05 实测：把 query 精简成只剩 encfilekey+token 会被 CDN 判 400
    （X-Errno -5103397）；`sign` / `basedata` / `m` / `uzid` / `hy` / `idx` /
    `X-snsvideoflag` 都是签名的一部分，必须一起带着。
    """
    return (url or "").strip()


def decode_key_of(url: str) -> int:
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return int(q.get("decodeKey", ["0"])[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


def resolve_video(share_text: str, cookie: str = "", timeout: float = 25.0) -> dict:
    """分享链接 → 正片直链 + 元数据。失败抛 RuntimeError（调用方回落到元数据卡片）。

    cookie 为空时自动读 cookies/yuanbao.txt；两者都没有直接抛错。
    """
    link = _normalize_share_url(share_text)
    if not link:
        raise RuntimeError("没找到视频号分享链接")
    cookie = (cookie or yuanbao_cookie()).strip()
    if not cookie:
        raise RuntimeError("没配置元宝登录态（cookies/yuanbao.txt），拿不到带凭证的直链")

    throttle("yuanbao.tencent.com")
    try:
        parse = _post_json(YUANBAO_PARSE_API,
                           {"type": "video_channel_url", "url": link, "scene": 1},
                           {**_YB_HEADERS, "cookie": cookie}, timeout)
    except urllib.error.HTTPError as exc:
        # 401/403 = 登录态过期或被风控；其它 = 接口本身变了
        raise RuntimeError(f"元宝接口 HTTP {exc.code}（元宝登录态可能已失效，重跑 "
                           "login_cookie.py yuanbao 更新）") from exc
    data = parse.get("data") or {}
    playable = (data.get("playable_url") or "").strip()
    if not playable:
        raise RuntimeError("元宝没返回 playable_url："
                           + str(parse.get("msg") or parse.get("errMsg") or parse)[:160])
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(playable).query)
    token = (q.get("token") or [""])[0]
    eid = (q.get("eid") or [""])[0] or (data.get("wx_export_id") or "")
    if not token or not eid:
        raise RuntimeError("元宝返回的凭证不全（缺 token 或 eid）")

    rid = f"{int(time.time()):x}-{os.urandom(4).hex()}"
    api = (INFO_API + f"?_rid={rid}"
           "&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed")
    referer = ("https://channels.weixin.qq.com/finder-preview/pages/feed"
               "?entry_card_type=48&comment_scene=39&appid=0"
               f"&token={urllib.parse.quote(token, safe='')}"
               f"&entry_scene=0&eid={urllib.parse.quote(eid, safe='')}")
    throttle("channels.weixin.qq.com")
    profile = _post_json(api, {"baseReq": {"generalToken": token}, "exportId": eid},
                         {**_CH_HEADERS, "Referer": referer}, timeout)
    if profile.get("errCode"):
        raise RuntimeError(f"视频号返回 error: errCode={profile.get('errCode')} "
                           f"{profile.get('errMsg') or ''}".strip())
    feed = ((profile.get("data") or {}).get("feedInfo") or {})
    url, codec = _pick_video(feed)
    if not url:
        raise RuntimeError("视频号没给视频直链（内容可能已删除或只有图片）")
    meta = parse_feed_info(profile) or {}
    meta.update({"videoUrl": _clean_video_url(url), "codec": codec,
                 "shareUrl": link, "exportId": eid, "generalToken": token})
    logger.info("视频号直链: codec=%s 标题=%s", codec, (meta.get("title") or "")[:20])
    return meta


def download_media(url: str, dest: Path, timeout: float = 180.0) -> Path:
    """下正片到 dest；有 X-enclen 头就先解前 N 字节（ISAAC64），返回落盘路径。"""
    import shutil
    import urllib.request

    if not url:
        raise RuntimeError("没有可下载的直链")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA_CHANNELS,
        # 实测：直链要带视频号 Referer，否则 403
        "Referer": "https://channels.weixin.qq.com/",
    })
    throttle("channels.weixin.qq.com")
    # 2026-11-05 实测：finder.video.qq.com 直连稳（curl --noproxy 200/3.8MB），
    # 走媒体代理反而常握手超时 —— 改直连优先，失败再按配置的代理兜底一次。
    last_err: Exception | None = None
    for idx, opener in enumerate((http_opener("", proxy=""), http_opener("", media=True))):
        try:
            with opener.open(req, timeout=timeout) as resp:
                enc_len = int(resp.headers.get("X-enclen") or resp.headers.get("x-enclen") or 0)
                with open(part, "wb") as fh:
                    shutil.copyfileobj(resp, fh, 1 << 20)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("视频号媒体下载第 %d 次失败：%s: %s", idx + 1, type(exc).__name__, exc)
    else:
        raise RuntimeError(f"视频号媒体下载失败：{last_err}")
    if enc_len > 0:
        key = decode_key_of(url)
        if key:
            buf = bytearray(part.read_bytes()[:enc_len])
            decrypt_xor(buf, enc_len, key)
            with open(part, "r+b") as fh:
                fh.seek(0)
                fh.write(buf)
            logger.info("视频号媒体已解密前 %d 字节", enc_len)
        else:
            logger.warning("响应带 X-enclen=%d 但直链里没有 decodeKey，按明文保存", enc_len)
    part.replace(dest)
    logger.info("视频号媒体已落盘: %s (%.1f MB)", dest.name, dest.stat().st_size / 1048576)
    return dest


# ---------- 统一入口：贴一条视频号链接 → 建任务（单条 API 与批量共用） ----------
#
# 2026-11-05 起 api 单条提交与批量提取都走 process_channel_link：此前这段逻辑
# 内联在 api.py 的 LinkError 分支里，批量入口（batch.py）没接，批量里的视频号
# 链接会直接 FAILURE「暂不支持」。抽成同步函数放这里，两个入口共用同一份流程，
# 以后加能力只改这一处。

# 连元数据都拿不到时给用户的提示（api 400 与批量 FAILURE 共用，别改口径）
CHANNEL_UNRESOLVABLE = ("没取到该视频号作品的元数据（接口限流或链接已失效）。"
                        "请再点一次「提取」；仍失败的话，可以改用「上传文件」提取，"
                        "或把已下载好的视频放进监听目录自动转写。")

# resolve_video 的「决定性失败」标记：作品本身没有视频流（图集/已删除），重试也不会有
_NO_VIDEO_MARKERS = ("没给视频直链",)


def is_channel_share(url: str) -> bool:
    """视频号分享页/预览页链接（单条与批量入口共用的判定）。"""
    raw = (url or "").strip()
    return ("weixin.qq.com/sph" in raw) or ("channels.weixin.qq.com" in raw)


def _resolve_with_retry(raw: str, attempts: int = 3) -> tuple[dict, str]:
    """元宝直链带自动重试（2026-11-05 加）。

    实测腾讯接口有瞬时抖动：连接被对端掐断 / SSL 握手超时，隔几秒重试就好
    （当天 11:25、11:28 连败两次，11:31 原样重放即成功）。返回 (info, 最后一次
    失败原因)，info 非空即成功；「没给视频直链」是决定性失败（作品没有视频流），
    重试无意义，立即返回。
    """
    last = ""
    for attempt in range(attempts):
        try:
            # 单次 12 秒（正常响应 1~3 秒）：失败时能把重试压在按钮可接受的等待内
            return resolve_video(raw, timeout=12), ""
        except Exception as exc:  # noqa: BLE001
            last = str(exc) or type(exc).__name__
            logger.info("视频号直链获取失败（第 %d/%d 次）: %s", attempt + 1, attempts, last)
            if any(m in last for m in _NO_VIDEO_MARKERS):
                break
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
    return {}, last


def _fail_no_video(raw: str, detail: str, meta: dict,
                   task: Optional[object] = None) -> str:
    """绑定了凭证、自动重试后仍拿不到视频 → 任务标 FAILURE（不再静默降级）。

    2026-11-05 用户要求：界面上必须出现视频才算成功；旧行为是降级成一张"成功"
    的文案卡片，用户看到「提取成功」却没视频，不知道该重试。现在红色失败提示里
    直接给出下一步。返回 taskId（前端照常轮询到 FAILURE 显示原因）。
    """
    from .models import TaskDetail, TaskStatus
    from .task_store import store
    from .utils import gen_task_id

    reason = (detail or "未知原因").strip()[:140]
    if meta:
        msg = (f"文案已取到，但视频没拿到（自动重试后仍失败：{reason}）。"
               "请再点一次「提取」——多数是网络抖动，再点一次就成；"
               "连续失败就去「账号」页重扫元宝登录刷新凭证。")
    else:
        # 元数据和直链都没拿到：不能说「文案已取到」，否则就是骗人
        msg = (f"这条作品的信息和视频都没拿到（自动重试后仍失败：{reason}）。"
               "请再点一次「提取」；仍失败的话，链接可能已失效或作品已被删除，"
               "可以改用「上传文件」提取。")
    title = (meta.get("title") or "").strip() or "视频号作品"
    # platform 进 fields：单条新建和批量占位复用（占位可能被 detect_platform 标成
    # 「微信」）都要归到「视频号」，失败列表里平台列不能串行
    fields = {"errorMessage": msg, "title": title, "platform": "视频号"}
    if meta:
        # 文案已经拿到了就别丢：失败归失败，正文仍存进这条记录（历史里能查到）
        desc = (meta.get("description") or "").strip()
        fields.update({"content": desc, "textContent": desc,
                       "cover": meta.get("cover") or None,
                       "uploader": meta.get("author") or None})
    task_id = task.taskId if task is not None else gen_task_id()
    if task is None:
        store.create(TaskDetail(
            taskId=task_id, taskType="videochannel", status=TaskStatus.PENDING,
            webpageUrl=raw, **fields))
    store.update_status(task_id, TaskStatus.FAILURE, **fields)
    logger.info("视频号提取失败(已自动重试仍无视频): %s → %s", raw, reason[:80])
    return task_id


def process_channel_link(raw: str, whisper_model: str = "tiny",
                         task: Optional[object] = None) -> Optional[str]:
    """视频号分享链接统一处理（同步；API 用 to_thread 调，批量在 worker 线程直接调）。

    流程：配了元宝登录态优先「换直链 → 下载 → 转写」（file_transcribe 流水线），
    直链拿不到时**自动重试**；重试仍失败 → 任务标 FAILURE 并提示「再点一次提取」
    （2026-11-05 用户要求：必须视频呈现在界面上才算成功，不再静默降级）。
    只有两种情况保留「链接模式」元数据卡片：没配凭证（设计内行为，前端提交前已
    提醒过）、作品本身没有视频直链（图集/已删除，重试也不会有）。
    task 传批量已登记的占位任务则原地复用（改成实际类型并带上真实标题/封面），
    传 None 就新建任务（单条提交）。返回 taskId，连元数据都拿不到返回 None。
    """
    from .models import TaskDetail, TaskStatus
    from .task_store import store
    from .utils import gen_task_id

    uri = short_uri_from(raw)
    cookie = yuanbao_cookie()
    info: dict = {}
    vid_err = ""

    def _fetch_meta() -> dict:
        """匿名元数据：兜底「链接模式」卡片的素材。失败安静。"""
        if not uri:
            return {}
        try:
            m = fetch_feed_info(uri)
            if m:
                m["short_uri"] = uri
            return m
        except Exception as exc:  # noqa: BLE001
            logger.info("视频号元数据获取失败（忽略）: %s", exc)
            return {}

    if cookie:
        # 2026-11-05 优化：匿名元数据与直链解析互不依赖，串行白等 1~2 秒 —— 并行发
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as ex:
            fm = ex.submit(_fetch_meta)
            rm = ex.submit(_resolve_with_retry, raw)
            meta = fm.result()
            info, vid_err = rm.result()
    else:
        meta = _fetch_meta()

    if info.get("videoUrl"):
        task_id = task.taskId if task is not None else gen_task_id()
        tmp_dir = Path("tmp_uploads")
        tmp_dir.mkdir(exist_ok=True)
        tmp = tmp_dir / f"{task_id}.mp4"
        # CDN 偶发 400/断流（实测 10:14 那次），同一直链补试一次
        dl_err = ""
        for attempt in range(2):
            try:
                download_media(info["videoUrl"], tmp)
                dl_err = ""
                break
            except Exception as exc:  # noqa: BLE001
                dl_err = str(exc) or type(exc).__name__
                logger.warning("视频号视频下载失败（第 %d/2 次）: %s", attempt + 1, dl_err)
                if attempt == 0:
                    time.sleep(2)
        if not dl_err:
            src_name = (info.get("title") or "视频号视频").strip()[:80] + ".mp4"
            cover = info.get("cover") or None
            if task is None:
                task = TaskDetail(
                    taskId=task_id, taskType="file_transcript", status=TaskStatus.PENDING,
                    errorMessage="视频号视频获取中...", platform="视频号", webpageUrl=raw,
                    title=src_name, cover=cover)
                store.create(task)
            else:
                # 批量占位任务原地复用：换成 file_transcript 类型并带上真实标题/封面
                store.update_status(task_id, TaskStatus.PENDING,
                                    taskType="file_transcript", platform="视频号",
                                    title=src_name, cover=cover,
                                    errorMessage="视频号视频获取中...")
            from .file_transcribe import start_file_task

            start_file_task(task, tmp, whisper_model, src_name,
                            ocr_frames=True, platform="视频号")
            return task_id
        vid_err = f"视频下载失败: {dl_err}"

    no_video_work = bool(vid_err) and any(m in vid_err for m in _NO_VIDEO_MARKERS)
    if meta and (not cookie or no_video_work):
        # 没绑定凭证（设计内行为）或作品本身没有视频（图集/已删）→ 卡片就是全部能给的
        return _create_link_mode_task(raw, meta, task)

    if cookie:
        # 绑了凭证却仍没视频：按「必须出视频才算成功」处理 —— 明确失败 + 告诉用户重试
        return _fail_no_video(raw, vid_err, meta, task)
    return None


def _create_link_mode_task(raw: str, meta: dict, task: Optional[object] = None) -> str:
    """视频号「链接模式」（2026-11-04）：贴分享链接能拿到正文文案/作者/封面/互动数据，
    但拿不到视频流（实测 get_feed_info 不返回 h264VideoInfo/h265VideoInfo/videoUrl，
    且 CDN 直链是密文）→ 直接建成一条 SUCCESS 任务，前端出「链接模式」卡片。

    口播语音转文字：元宝直链下载 → 转写（2026-11-05 起不再需要外部下载器）。
    task 传占位任务则原地复用（批量入口）。返回 taskId。
    """
    from .models import TaskDetail, TaskStatus
    from .task_store import store
    from .utils import gen_task_id

    desc = (meta.get("description") or "").strip()
    title = (meta.get("title") or "").strip() or desc[:40] or "视频号作品"
    channel_meta = {
        "likes": meta.get("likes") or "",
        "favCount": meta.get("favCount") or "",
        "forwardCount": meta.get("forwardCount") or "",
        "commentCount": meta.get("commentCount") or "",
        "publishAt": meta.get("publishAt") or "",
        "location": meta.get("location") or "",
        "headImg": meta.get("headImg") or "",
        "tags": meta.get("tags") or [],
        "shortUri": meta.get("short_uri") or "",
        "dynamicExportId": meta.get("dynamicExportId") or "",
        "isVideo": bool(meta.get("isVideo")),
    }
    # 前端据此选提示语：绑了凭证却没视频（图集/作品已删）≠ 没绑定（可去登录）
    try:
        channel_meta["bound"] = bool(yuanbao_cookie())
    except Exception:  # noqa: BLE001
        channel_meta["bound"] = False
    if task is None:
        task_id = gen_task_id()
        store.create(TaskDetail(
            taskId=task_id, taskType="videochannel", status=TaskStatus.PENDING,
            errorMessage="作品内容提取中...", title=title, content=desc,
            textContent=desc, cover=meta.get("cover") or None, platform="视频号",
            uploader=meta.get("author") or None, webpageUrl=raw,
            channelMeta=channel_meta))
    else:
        task_id = task.taskId
        store.update_status(task_id, TaskStatus.PENDING, taskType="videochannel",
                            title=title, content=desc, textContent=desc,
                            cover=meta.get("cover") or None, platform="视频号",
                            uploader=meta.get("author") or None,
                            channelMeta=channel_meta, errorMessage="作品内容提取中...")
    # update_status 到 SUCCESS 会自动写历史 + 归档文案（文案输出/ 目录）
    store.update_status(task_id, TaskStatus.SUCCESS, title=title, content=desc,
                        textContent=desc, cover=meta.get("cover") or None)
    logger.info("视频号链接模式: %s → %s（正文 %d 字）", raw[:60], title[:20], len(desc))
    return task_id