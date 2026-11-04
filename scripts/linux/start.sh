#!/usr/bin/env bash
# ============================================================
#  文案提取工具 · Ubuntu 一键启动（Ubuntu 20.04+ / amd64）
# ============================================================
#  与 Windows 版的差别（重要，先读一遍）：
#    · 视频号抓取**不可用**：那个下载器是 Windows 专属（UAC/证书/系统代理）。
#      代码会明确提示「请把视频同步到监听目录，由本服务转写」——
#      把视频号文件拷进 本包目录/wx_downloads/ 即可照常转写，其余功能不受影响。
#    · 桌面气泡通知自动跳过（Windows 专属）。
#    · 其余功能（链接抓取、上传转写、监视目录、OCR、画面文字、导出、
#      AI 纠错/笔记、批量、历史归档）与 Windows 版完全一致。
#
#  常用命令：
#      bash start.sh              前台启动（Ctrl+C 停止）
#      bash start.sh --daemon     安装为 systemd 服务并后台常驻（SSH 断开不停）
#      bash start.sh --status     查看状态
#      bash start.sh --logs       实时日志
#      bash start.sh --stop       停止服务
#      bash start.sh --check      只做环境自检，不启动
# ============================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/transcript_tool"
VENV="$HERE/.venv"
SERVICE_NAME="transcript-tool"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

# 时区：不设会让所有时间偏 8 小时（文件名、时间轴、日期分组全错）
export TZ=Asia/Shanghai

SUDO=""
[ "$(id -u)" = "0" ] || { command -v sudo >/dev/null 2>&1 && SUDO="sudo"; }

PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
PY=""
REQ_PY_MIN_MINOR=10          # 需要 Python 3.10+

say()  { echo "* $*"; }
warn() { echo "!! $*"; }
die()  { echo "!! $*" >&2; exit 1; }

# ---------- 环境自检 ----------
find_python() {
  for c in python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
        PY="$(command -v "$c")"; return 0
      fi
    fi
  done
  # 退一步：系统 python3 存在但版本低
  if command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
    warn "系统 Python 版本低于 3.10：$("$PY" -V 2>&1)"
    return 1
  fi
  return 2
}

ensure_ffmpeg() {
  if command -v ffmpeg >/dev/null 2>&1; then
    say "ffmpeg：$(command -v ffmpeg)"
    return 0
  fi
  warn "未检测到 ffmpeg，尝试自动安装（需要网络与 sudo）..."
  if $SUDO apt-get update -y && $SUDO apt-get install -y ffmpeg; then
    say "ffmpeg 安装完成：$(command -v ffmpeg)"; return 0
  fi
  warn "ffmpeg 自动安装失败。程序仍能启动，但转写会失败。请手动执行："
  warn "    sudo apt update && sudo apt install -y ffmpeg"
  return 1
}

ensure_python() {
  if find_python; then say "Python：$PY（$("$PY" -V 2>&1)）"; return 0; fi
  warn "需要 Python 3.10+。尝试用 apt 安装 python3 / python3-venv ..."
  if $SUDO apt-get update -y && $SUDO apt-get install -y python3 python3-venv python3-pip; then
    find_python && { say "Python 就绪：$PY"; return 0; }
  fi
  die "无法自动准备 Python 3.10+。请手动安装后重试：
     sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
}

ensure_venv() {
  [ -x "$VENV/bin/python" ] && return 0
  say "创建虚拟环境：$VENV"
  "$PY" -m venv "$VENV" || die "创建 venv 失败 —— 可能需要先装 python3-venv：
     sudo apt install -y python3-venv"
}

pip_install() {
  say "安装 Python 依赖（首次约 2-5 分钟）..."
  local idx="${TRANSCRIPT_PIP_INDEX:-$PIP_MIRROR}"
  # ★ 镜像优先（2026-11-02 修）：原来是"官方源失败才换镜像"，但国内访问 PyPI
  #   常常是「慢而不失败」—— 回退永远不触发，装依赖能挂 20 分钟以上
  #   （在 Ubuntu 容器里实测踩到）。改成默认先走国内镜像，失败再回官方源。
  "$VENV/bin/python" -m pip install --upgrade pip -q --timeout 30 --retries 2 -i "$idx" 2>/dev/null || true
  if "$VENV/bin/python" -m pip install -r "$APP/requirements.txt" \
        -q --timeout 30 --retries 2 -i "$idx"; then
    say "依赖安装完成"
    return 0
  fi
  say "镜像源失败，改用官方 PyPI 重试..."
  "$VENV/bin/python" -m pip install -r "$APP/requirements.txt" \
      -q --timeout 30 --retries 2 || die "依赖安装失败，请检查网络"
  say "依赖安装完成"
}

ensure_deps() {
  # 用能否 import 关键包判断，而不是看 pip 列表（更快更准）
  if "$VENV/bin/python" -c "import fastapi, uvicorn, faster_whisper, yt_dlp" 2>/dev/null; then
    return 0
  fi
  pip_install
}

ensure_browser() {
  if "$VENV/bin/python" -m playwright install --dry-run chromium 2>/dev/null | grep -q "not installed"; then
    say "下载 Playwright 浏览器内核（抖音/小红书渲染通道需要，约 130MB）..."
    "$VENV/bin/python" -m playwright install chromium \
      || warn "浏览器内核下载失败 —— 抖音/小红书通道不可用，其余功能正常"
  else
    say "浏览器内核已就绪"
  fi
  # Chrome 的系统依赖（libnss3 等）在精简版 Ubuntu 上常缺，缺了 chromium 起不来
  if ! "$VENV/bin/python" -m playwright install-deps chromium >/dev/null 2>&1; then
    warn "浏览器系统依赖未能自动安装（多为缺 sudo 权限）。若抖音/小红书报错，请手动执行："
    warn "    $SUDO $VENV/bin/python -m playwright install-deps chromium"
  fi
}

prepare_env() {
  mkdir -p "$HERE/wx_downloads" "$APP/tasks" "$APP/tmp_uploads" 2>/dev/null || true
  [ -f "$APP/.env" ] && return 0
  if [ -f "$APP/.env.example" ]; then
    cp "$APP/.env.example" "$APP/.env"
    say "已从 .env.example 创建 .env（AI 纠错/笔记需填 TRANSCRIPT_LLM_API_KEY）"
  fi
}

check_only() {
  echo "================ 环境自检 ================"
  find_python && say "Python  $PY ($("$PY" -V 2>&1))" || warn "Python 3.10+ 缺失（会自动安装）"
  command -v ffmpeg >/dev/null 2>&1 && say "ffmpeg  $(command -v ffmpeg)" || warn "ffmpeg 缺失（会自动安装）"
  command -v systemctl >/dev/null 2>&1 && say "systemd 可用" || warn "无 systemd（--daemon 不可用，仍可前台运行）"
  [ -f "$APP/run.py" ] && say "代码   $APP/run.py 存在" || warn "代码缺失：$APP/run.py"
  echo "=========================================="
}

start_foreground() {
  ensure_ffmpeg || true
  ensure_python
  ensure_venv
  ensure_deps
  ensure_browser
  prepare_env
  say "启动服务：http://127.0.0.1:${PORT}"
  say "（首次转写会自动下载语音模型；Ctrl+C 停止）"
  cd "$APP" || die "无法进入 $APP"
  PORT="$PORT" HOST="$HOST" "$VENV/bin/python" run.py
}

install_service() {
  command -v systemctl >/dev/null 2>&1 || die "本机没有 systemd，无法安装为服务；请用 bash start.sh 前台运行。"
  ensure_ffmpeg || true
  ensure_python
  ensure_venv
  ensure_deps
  ensure_browser
  prepare_env
  say "写入 systemd 服务：$SERVICE_FILE"
  $SUDO tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Transcript Tool (文案提取工具)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP
ExecStart=$VENV/bin/python $APP/run.py
Restart=always
RestartSec=3
Environment=TZ=Asia/Shanghai
Environment=PORT=$PORT
Environment=HOST=$HOST
Environment=PYTHONIOENCODING=utf-8

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now "$SERVICE_NAME"
  sleep 2
  say "已安装并启动。访问：http://127.0.0.1:${PORT}"
  say "查看状态：bash start.sh --status    查看日志：bash start.sh --logs"
}

case "${1:-}" in
  --check)    check_only ;;
  --daemon)   install_service ;;
  --stop)     $SUDO systemctl stop "$SERVICE_NAME" 2>/dev/null && say "已停止" || warn "服务未运行或未安装" ;;
  --status)   $SUDO systemctl status "$SERVICE_NAME" --no-pager 2>/dev/null || warn "服务未安装" ;;
  --logs)     $SUDO journalctl -u "$SERVICE_NAME" -f --no-pager ;;
  ""|--foreground) start_foreground ;;
  *)          echo "用法: bash start.sh [--daemon|--status|--logs|--stop|--check]" ;;
esac