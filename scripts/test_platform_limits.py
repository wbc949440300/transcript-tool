"""快手 / B站 连续提取风控测试（2026-11-03）

目的：回答"除了抖音，其它平台大量提取有没有限制"——用实测代替经验判断。

方法：
- 逐个提交任务，等它跑完再提交下一条（串行，跟真实使用一致）
- 每条记录：结果、耗时、以及**提交前后平台冷却状态的变化**
- 一出现"风控/验证码/冷却"就立刻停止，不硬冲（硬冲只会加深标记）

注意：转写用 tiny 模型且关掉画面 OCR —— 转写是**本地**的，不产生平台请求，
所以不影响风控结论，只是让测试跑得快些。真正打平台的是"抓取"那一步。
"""

import json
import pathlib
import sys
import time
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:8000"
ROOT = pathlib.Path(__file__).resolve().parent.parent      # 项目根（cookies/ 在它下面）
COOLDOWN = ROOT / "cookies" / ".cooldown.json"

# 测试用的链接：B站取排行榜的真实地址；快手只有一条可用的，就重复跑
BILI = [
    "https://www.bilibili.com/video/BV1TxxC6vEo4",
    "https://www.bilibili.com/video/BV1o1xC6DEAa",
    "https://www.bilibili.com/video/BV1Nax16uEHm",
    "https://www.bilibili.com/video/BV1ugxq6HEfH",
    "https://www.bilibili.com/video/BV1pkxi66EG6",
    "https://www.bilibili.com/video/BV1dRx16pEPw",
]
KS = "https://www.kuaishou.com/short-video/3xgtmts983qd8vs"
KS_REPS = 5

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _json(req, timeout=90):
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return _json(req)


def get(path):
    return _json(urllib.request.Request(BASE + path))


def cooldown_of(platform):
    """该平台当前冷却剩余秒数（读共享的冷却文件）。"""
    try:
        d = json.loads(COOLDOWN.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    now = time.time()
    best = 0.0
    for k, v in (d or {}).items():
        if f":{platform}@" in k or k.endswith(f"@{platform}"):
            best = max(best, max(0.0, float(v.get("until") or 0) - now))
    return best


def wait_done(platform, before_ids, timeout=420):
    """等新任务出现在历史里，返回 (status, error_msg, title)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            rows = (get("/prod-api/history/list?limit=10") or {}).get("data", {}).get("list", [])
        except Exception:
            rows = []
        for r in rows:
            if r.get("task_id") in before_ids:
                continue
            if (r.get("platform") or "") in ("快手", "B站"):
                return r.get("status"), (r.get("error_msg") or ""), (r.get("title") or "")
        time.sleep(4)
    return "TIMEOUT", "等待超时（可能还在转写）", ""


def run(label, urls):
    print(f"\n===== {label}：{len(urls)} 条 =====", flush=True)
    ok = fail = 0
    for i, u in enumerate(urls, 1):
        plat = "kuaishou" if "kuaishou" in u else "bilibili"
        cd_before = cooldown_of(plat)
        if cd_before > 0:
            print(f"  [{i}] 已被冷却（剩余 {cd_before/60:.1f} 分钟）→ 停止，不再硬冲", flush=True)
            break
        try:
            rows = (get("/prod-api/history/list?limit=10") or {}).get("data", {}).get("list", [])
            before = {r.get("task_id") for r in rows}
        except Exception:
            before = set()
        t0 = time.time()
        try:
            r = post("/prod-api/transcript/doTask",
                     {"workUrl": u, "whisperModel": "tiny", "ocrFrames": False})
            code = (r or {}).get("code")
            if code not in (200, None) and code != 0:
                print(f"  [{i}] 提交被拒: {str(r.get('msg'))[:70]}", flush=True)
                fail += 1
                continue
        except Exception as e:
            print(f"  [{i}] 提交异常: {type(e).__name__} {str(e)[:70]}", flush=True)
            fail += 1
            continue
        status, err, title = wait_done(plat, before)
        dt = time.time() - t0
        cd_after = cooldown_of(plat)
        if status == "SUCCESS":
            ok += 1
            flag = ""
            if cd_after > 0:
                flag = f"  ← ⚠ 本次触发了冷却 {cd_after/60:.0f} 分钟"
            print(f"  [{i}] ✅ 成功  {dt:.0f}s  {title[:26]}{flag}", flush=True)
        else:
            fail += 1
            print(f"  [{i}] ❌ {status}  {dt:.0f}s  {err[:70]}", flush=True)
        if cd_after > 0:
            print(f"  → 平台已进入冷却（{cd_after/60:.0f} 分钟），停止测试", flush=True)
            break
        time.sleep(3)          # 条间隔，模拟正常使用节奏
    print(f"  --- {label} 小结：成功 {ok} / 失败 {fail} ---", flush=True)
    return ok, fail


def main():
    op.open(BASE + "/", timeout=10).read()          # 拿会话 cookie
    print("快手每 IP 安全量口径：40 条/天；B站无明确口径。本次为探索性测试。", flush=True)
    run("B站", BILI)
    run("快手（同一条重复）", [KS] * KS_REPS)
    print("\n===== 测试结束 =====", flush=True)


if __name__ == "__main__":
    main()