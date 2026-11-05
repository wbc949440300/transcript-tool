"""打包「完全离线」绿色包：自带 Python 运行时，目标机器免装 Python、免联网。

用法（在 transcript_tool/ 下）:
    python scripts/build_offline.py --check     # 只盘点，不写盘
    python scripts/build_offline.py             # 打包（约 6GB，需十几分钟）

与 build_release.py 的区别（后者 36MB，依赖目标机器已装 Python 且能联网）：
  · 内置 嵌入式 Python + 全部 site-packages（含 CUDA 运行库）
  · 内置 Playwright 浏览器内核、whisper 模型、ffmpeg
  · 顶层 start.bat 把 PLAYWRIGHT_BROWSERS_PATH / HF_HOME 指向包内
  · 目标机器上：解压 → 双击 start.bat，全程不需要网络

可裁剪项（想缩体积就删，删了不影响其它功能）:
  python\\Lib\\site-packages\\nvidia\\   ≈2.0GB  删掉则只能用 CPU 转写（慢约 10 倍）
  runtime\\models\\huggingface\\          ≈2.3GB  删掉则首次转写要联网下载

已默认砍掉的（零功能损失，见 SLIM_SKIP_* 注释）:
  gradio / pandas / matplotlib   —— 应用 0 引用，相互依赖均为 extra
  ffplay.exe                     —— 应用只用 ffmpeg / ffprobe
  ★ CUDA（nvidia）**保留**：目标机器有 NVIDIA 显卡，砍掉会让转写慢约 10 倍。

来源路径可用环境变量覆盖（默认取自当前机器）:
  OFFLINE_PY_EMBED   嵌入式 Python 解压目录（缺省会自动下载到 dist/.offline-cache）
  PLAYWRIGHT_BROWSERS_PATH / HF_HOME   浏览器内核与模型缓存
  OFFLINE_FFMPEG_BIN ffmpeg bin 目录（缺省用系统 PATH 里的 ffmpeg）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release as br  # noqa: E402  复用代码收集、文档文本、版本号逻辑

ROOT = br.ROOT
DIST = br.DIST
CACHE = DIST / ".offline-cache"
PY_EMBED_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
PY_EMBED_MIRRORS = [
    "https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip",
    "https://mirrors.tuna.tsinghua.edu.cn/python/3.11.9/python-3.11.9-embed-amd64.zip",
]

# 嵌入式 Python 不带这些 VC++ 运行时，而 onnxruntime / ctranslate2 / cv2 / av
# 这些编译扩展都要用 —— 缺了会在导入时报"DLL load failed"（2026-11-02 实测踩到）
VC_RUNTIME_DLLS = [
    "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "vcamp140.dll", "vccorlib140.dll", "vcomp140.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll",
]

START_BAT = """@echo off
title Transcript Tool
rem Fully offline build: bundled Python + browsers + models + ffmpeg.
rem Everything below points INSIDE this folder; no network and no system Python needed.
setlocal
set "HERE=%~dp0"
set "PATH=%HERE%python;%HERE%python\\Scripts;%PATH%"
set "PLAYWRIGHT_BROWSERS_PATH=%HERE%runtime\\playwright"
set "HF_HOME=%HERE%runtime\\models\\huggingface"
"%HERE%python\\python.exe" "%HERE%transcript_tool\\scripts\\launch.py"
"""

DOC_OFFLINE = """\
========================================
   离线版说明（先看这个）
========================================

本包是**完全离线版**：自带 Python 运行时、浏览器内核、语音模型、ffmpeg。
目标机器上**不需要装 Python，也不需要联网**。

【你要做的只有两步】

  第 1 步：解压
     把 zip 右键 →「解压到当前文件夹」。
     ★ 不要直接在压缩包里双击，要先把文件解压出来。
     ★ 建议解压到独立目录（例如 D:\\文案提取工具），保持目录层次不变。

  第 2 步：双击 start.bat
     会出现一个黑色窗口（程序本体，别关它）。
     首次启动约 10-30 秒（在自检，不下载任何东西）。
     看到  Uvicorn running on http://127.0.0.1:8000  就成功了，
     浏览器会自动打开；没打开就手动访问  127.0.0.1:8000

【包内结构（不要改动层次）】

   文案提取工具-vX.X.X\\
     ├─ start.bat             ← 双击这个
     ├─ ① 使用说明（先看这个）.txt      ← 界面怎么用、数据存在哪
     ├─ ② 遇到问题看这里.txt
     ├─ ③ 第三方组件与许可声明.txt
     ├─ python\\               ← 自带的 Python 运行时（约 2.9GB）
     ├─ runtime\\
     │    ├─ playwright\\      ← 浏览器内核（约 0.7GB）
     │    └─ models\\          ← 语音识别模型（约 2.3GB）
     └─ transcript_tool\\      ← 程序本体（含 ffmpeg）

【想缩小体积？删这两个目录即可，不影响其它功能】

   python\\Lib\\site-packages\\nvidia\\   ≈2.0GB
      删掉 → 只能用 CPU 转写，速度慢约 10 倍。
      （只有目标机器有 NVIDIA 显卡时它才有用；没有显卡本来就自动走 CPU）

   runtime\\models\\huggingface\\         ≈2.3GB
      删掉 → 首次转写时要联网下载语音模型。

【界面怎么用 / 数据存在哪】

   看《① 使用说明（先看这个）.txt》，里面写全了。

【和其它版本的区别】

   本版：免装 Python、免联网，但包很大（解压后约 6GB）。
   体积小的版本（36MB）：需要目标机器先装 Python 3.10+，且首次要联网下 2GB。
"""


def _run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def ensure_embed_python(check_only: bool) -> Path | None:
    """找（或下载）嵌入式 Python 的解压目录。"""
    env = os.getenv("OFFLINE_PY_EMBED", "").strip()
    if env and Path(env, "python.exe").is_file():
        return Path(env)
    dst = CACHE / "py-embed"
    if (dst / "python.exe").is_file():
        return dst
    if check_only:
        print(f"[!] 嵌入式 Python 不在 {dst} —— 打包时会自动下载")
        return None
    dst.mkdir(parents=True, exist_ok=True)
    zpath = CACHE / "py-embed.zip"
    urls = [PY_EMBED_URL, *PY_EMBED_MIRRORS]
    for url in urls:
        try:
            print(f"[i] 下载嵌入式 Python: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "transcript-tool/1.0"})
            with urllib.request.urlopen(req, timeout=600) as r, open(zpath, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(dst)
            print(f"[i] 解压完成 -> {dst}")
            return dst
        except Exception as exc:  # noqa: BLE001
            print(f"[!] {url} 失败: {exc}")
    print("[!] 所有源都失败。可手动下载 embeddable zip，用 OFFLINE_PY_EMBED 指向解压目录")
    return None


# 可安全砍掉的包：应用代码 0 引用，且它们之间的相互依赖全部是各自的
# "extra"（huggingface_hub[gradio]、fsspec[dataframe]、pandas[matplotlib]），
# 不是硬依赖 —— 2026-11-02 用 importlib.metadata 逐一核实过（含反向依赖）。
# 砍掉不损失任何功能：gradio 81MB + pandas 70MB + matplotlib。
SLIM_SKIP_PACKAGES = ("gradio", "pandas", "matplotlib")
# ffplay 是自带播放器，应用只用 ffmpeg / ffprobe（砍掉省 ~102MB）
SLIM_SKIP_FILES = ("ffplay.exe",)


def _matched(parts: tuple[str, ...], skip: tuple[str, ...]) -> bool:
    """路径任一层命中 skip（支持 `pkg` / `pkg-1.2.3.dist-info` / `pkg.libs` 三种形态）。"""
    for part in parts:
        low = part.lower()
        for s in skip:
            if low == s or low.startswith(s + ".") or low.startswith(s + "-"):
                return True
    return False


def copy_tree(src: Path, dst: Path, label: str, skip: tuple[str, ...] = ()) -> int:
    if not src.is_dir():
        print(f"[!] 缺少 {label}: {src}")
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = skipped = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if skip and _matched(rel.parts, skip):
            skipped += 1
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        n += 1
    size = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file())
    tail = f"（跳过 {skipped} 个）" if skipped else ""
    print(f"    {label}: {n} 个文件, {size / 1073741824:.2f} GB {tail}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只盘点，不写盘")
    args = ap.parse_args()

    version = br.read_version()
    print(f"离线包版本: v{version}")

    code_files = br.collect()
    print(f"代码侧: {len(code_files)} 个文件")

    py_embed = ensure_embed_python(args.check)
    base_prefix = Path(sys.base_prefix)
    site_packages = base_prefix / "Lib" / "site-packages"
    browsers = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH")
                    or (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"))
    hf_home = Path(os.getenv("HF_HOME") or (Path.home() / ".cache" / "huggingface"))

    print("\n将要打入的来源：")
    for label, p in [("嵌入式 Python", py_embed), ("site-packages", site_packages),
                     ("Playwright 内核", browsers), ("whisper 模型", hf_home)]:
        ok = "✓" if p and Path(p).is_dir() else "✗ 缺失"
        size = ""
        if p and Path(p).is_dir():
            try:
                size = f"  {sum(x.stat().st_size for x in Path(p).rglob('*') if x.is_file()) / 1073741824:.2f} GB"
            except Exception:  # noqa: BLE001
                pass
        print(f"  [{ok}] {label:<16} {p}{size}")

    if args.check:
        print("\n[--check] 未写盘。")
        return 0
    if py_embed is None or not site_packages.is_dir():
        print("[!] 关键来源缺失，中止。")
        return 1

    name = f"文案提取工具-v{version}-离线版"
    stage = DIST / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    print("\n组装中（约 6GB，请耐心）：")

    # 1) 嵌入式 Python + VC 运行时补齐 + 完整 site-packages
    pydst = stage / "python"
    shutil.copytree(py_embed, pydst, dirs_exist_ok=True)
    for d in VC_RUNTIME_DLLS:
        src = base_prefix / d
        if src.is_file():
            shutil.copy2(src, pydst / d)
    # 允许 site-packages：_pth 里加一行 + 打开 import site
    # ★ 嵌入式 Python 一旦存在 ._pth 就进入"受限模式"：脚本目录与 cwd **都不进
    #   sys.path**（PYTHONPATH 也会被忽略）。所以必须在这里显式加上代码目录，
    #   否则 run.py 会报 "No module named 'app'"（2026-11-02 实测踩到）。
    #   相对路径以 python.exe 所在目录为基准。
    pth = next(pydst.glob("python*._pth"), None)
    if pth is not None:
        pth.write_text(
            "python311.zip\n"
            ".\\Lib\\site-packages\n"
            ".\n"
            "..\\transcript_tool\n"
            "import site\n",
            encoding="ascii")
    print("    嵌入式 Python 就绪，开始拷 site-packages（2.9GB）...")
    copy_tree(site_packages, pydst / "Lib" / "site-packages", "site-packages",
              skip=SLIM_SKIP_PACKAGES)

    # 2) 浏览器内核 / 模型
    copy_tree(browsers, stage / "runtime" / "playwright", "Playwright 内核")
    copy_tree(hf_home, stage / "runtime" / "models" / "huggingface", "whisper 模型")

    # 3) 代码 + ffmpeg + 离线安装包
    inner = stage / "transcript_tool"
    for f in code_files:
        dst = inner / f.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
    ffmpeg_bin = os.getenv("OFFLINE_FFMPEG_BIN", "").strip()
    if ffmpeg_bin:
        copy_tree(Path(ffmpeg_bin), inner / "tools" / "ffmpeg" / "bin", "ffmpeg")
    else:
        exe = shutil.which("ffmpeg")
        if exe:
            b = Path(exe).parent
            copy_tree(b, inner / "tools" / "ffmpeg" / "bin", "ffmpeg",
                      skip=SLIM_SKIP_FILES)
        else:
            print("[!] 找不到 ffmpeg，离线包会缺它（目标机器需自备）")

    # 4) 顶层入口 + 文档
    (stage / "start.bat").write_text(START_BAT, encoding="ascii", newline="\r\n")
    (stage / "① 使用说明（先看这个）.txt").write_text(br.DOC_INTRO, encoding="utf-8-sig")
    (stage / "② 遇到问题看这里.txt").write_text(br.DOC_TROUBLE, encoding="utf-8-sig")
    (stage / "③ 第三方组件与许可声明.txt").write_text(br.DOC_THIRDPARTY, encoding="utf-8-sig")
    (stage / "④ 离线版说明.txt").write_text(DOC_OFFLINE, encoding="utf-8-sig")

    total = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    (stage / "版本信息.txt").write_text(
        f"版本号：{version}（离线版）\n"
        f"打包时间：{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"是否自带运行时：是（免装 Python、免联网）\n"
        f"解压后体积：约 {total / 1073741824:.1f} GB\n",
        encoding="utf-8-sig")

    print(f"\n组装完成，解压后体积约 {total / 1073741824:.2f} GB")
    print(f"   文件夹: {stage}")
    print("   压缩包未生成 —— 这个体量建议直接拷文件夹，或手工压缩后传输")
    return 0


if __name__ == "__main__":
    sys.exit(main())