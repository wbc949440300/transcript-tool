"""接口自检（2026-11-03）

为什么要有它：**改完接口只测一条正常路径是不够的。**
真实事故：给 /media/proxy 加"换直链"时删掉了一个变量赋值，只测了抖音（返回
video/mp4，走不到补 Content-Type 那个分支）—— 测试通过，B站 一用就 500。

所以这个脚本的原则是：**按"分支"选样例，不是按"能跑通"选样例。**
- 媒体代理要分别覆盖：抖音（video/mp4）、B站（octet-stream → 需补类型）
- 每个接口至少一条真实数据；没有真实数据的显式跳过，不假装通过

用法：
    python scripts/smoke_test.py            # 跑全部
    python scripts/smoke_test.py --verbose  # 打响应片段

退出码非 0 表示有失败项，可直接接进打包前的检查。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8000"
VERBOSE = False

_cj = http.cookiejar.CookieJar()
_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


def _req(path, method="GET", body=None, headers=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with _op.open(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode(), {}


def _json_of(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}


def _tasks_by_platform():
    """从历史里每个平台取一条**成功的**任务，作为媒体类接口的真实样例。"""
    st, raw, _ = _req("/prod-api/history/list?limit=200", timeout=40)
    if st != 200:
        return {}
    out = {}
    for r in (_json_of(raw).get("data") or {}).get("list") or []:
        if r.get("status") != "SUCCESS":
            continue
        p = r.get("platform") or ""
        if p and p not in out and r.get("task_id"):
            out[p] = r["task_id"]
    return out


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "✅" if ok else "❌"
    line = f"  {mark} {name}"
    if detail:
        line += f"  — {detail}"
    print(line, flush=True)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    try:
        _op.open(BASE + "/", timeout=10).read()      # 拿会话 cookie
    except Exception as e:  # noqa: BLE001
        print(f"服务没起来（{e}），先启动 run.py", flush=True)
        return 2

    print("\n[基础接口]", flush=True)
    for path in ("/prod-api/health", "/prod-api/risk/status",
                 "/prod-api/settings", "/prod-api/login/status"):
        st, raw, _ = _req(path, timeout=40)
        ok = st == 200 and (_json_of(raw).get("code") in (200, None))
        check(path, ok, f"http={st}")

    st, raw, _ = _req("/prod-api/proxy/check", method="POST", timeout=90)
    check("POST /proxy/check", st == 200, f"http={st}")

    print("\n[媒体：按分支选样例 —— 不同平台返回的 Content-Type 不同]", flush=True)
    tasks = _tasks_by_platform()
    # 抖音返回 video/mp4；B站返回 application/octet-stream（需要补类型）。
    # 这两个必须都覆盖 —— 只测其中一个会漏掉分支。
    for plat in ("抖音", "B站", "快手"):
        tid = tasks.get(plat)
        if not tid:
            print(f"  ⏭  {plat} 没有可作为样例的成功任务，跳过", flush=True)
            continue
        st, raw, hdrs = _req(
            f"/prod-api/media/proxy?taskId={tid}",
            headers={"Range": "bytes=0-65535"}, timeout=60)
        ctype = (hdrs.get("Content-Type") or "").split(";")[0]
        ok = st in (200, 206) and len(raw) > 0
        check(f"/media/proxy [{plat}]", ok, f"http={st} {len(raw)}B {ctype}")
        st2, raw2, _ = _req(f"/prod-api/media/cover?taskId={tid}", timeout=40)
        # 404 + "封面" 是"这个任务本来就没有封面"（平台没给，如快手），
        # 属于预期结果，不算故障 —— 但要显式跳过而不是假装通过，避免掩盖真的回归。
        if st2 == 404 and "封面" in raw2.decode("utf-8", "replace"):
            print(f"  ⏭  /media/cover [{plat}] 该任务没有封面（平台未提供，非故障）", flush=True)
        else:
            check(f"/media/cover [{plat}]", st2 == 200 and len(raw2) > 0,
                  f"http={st2} {len(raw2)}B")
        # 服务端抽帧（结果页「视频截图」走的那条路）。
        # 首次请求会真的跑一次 ffmpeg（10~40 秒），所以给足超时。
        # 校验 JPEG 魔数 ff d8 —— 只判 http 200 不够：错误页也可能是 200。
        st3, raw3, _ = _req(f"/prod-api/media/frame?taskId={tid}&i=0&n=5&t={int(time.time())}",
                            timeout=300)
        check(f"/media/frame [{plat}]",
              st3 == 200 and raw3[:2] == b"\xff\xd8",
              f"http={st3} {len(raw3)}B" + ("" if raw3[:2] == b"\xff\xd8" else " 非JPEG"))

    print("\n[刷新直链：三个平台都要能刷]", flush=True)
    for plat in ("抖音", "B站", "快手"):
        tid = tasks.get(plat)
        if not tid:
            print(f"  ⏭  {plat} 无样例，跳过", flush=True)
            continue
        # 刷新会**真的重取一次平台详情**，走的是抓取通道 —— 连着刷会撞上平台级
        # 节流（默认 3s）和平台自身的限速，表现为 "read operation timed out" 502。
        # 那不是代码故障。这里刻意留出间隔，让自检测的是"代码对不对"，
        # 而不是"平台此刻心情如何"。
        if plat != "抖音":
            time.sleep(10)
        st, raw, _ = _req("/prod-api/transcript/refreshMedia", method="POST",
                          body={"taskId": tid}, timeout=180)
        j = _json_of(raw)
        urls = ((j.get("data") or {}).get("videoUrlList") or [])
        msg = str(j.get("msg") or "")
        if st == 200 and j.get("code") == 200 and urls:
            check(f"refreshMedia [{plat}]", True, f"http={st} {len(urls)} 条")
        elif any(k in msg for k in ("风控", "验证码", "冷却", "限流")):
            # 平台拦了 —— 不是代码坏了。显式标成「已知状况」，不计入失败，
            # 但也不冒充通过：真回归会以别的方式（例如全平台都 502）暴露出来。
            print(f"  ⚠  refreshMedia [{plat}] 平台风控中（非代码故障）：{msg[:60]}", flush=True)
        else:
            check(f"refreshMedia [{plat}]", False, f"http={st} {msg[:60]}")

    bad = [r for r in RESULTS if not r[1]]
    print(f"\n===== {len(RESULTS) - len(bad)}/{len(RESULTS)} 通过 =====", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())