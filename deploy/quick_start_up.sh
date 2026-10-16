#!/usr/bin/env bash
# quick_start_up.sh —— 手动快速启动整个后端（robot-core 五进程 + FastAPI）。
# 调试/验证用；生产环境建议使用 systemd（deploy/*.service + inspection-robot.target）。
#
# 用法:
#   deploy/quick_start_up.sh start     启动全部（默认）
#   deploy/quick_start_up.sh stop      停止全部
#   deploy/quick_start_up.sh restart   重启
#   deploy/quick_start_up.sh status    查看各组件状态
#   deploy/quick_start_up.sh check     仅冒烟检查（配置 + PostgreSQL 连通）
#   deploy/quick_start_up.sh install   安装/刷新 systemd 单元并启动（按当前路径渲染）
#
# 环境变量:
#   ROBOT_ENV_FILE   凭据文件路径（默认 <项目根>/robot.env）
#   ROBOT_CONFIG     进程配置文件（默认 configs/hardware.yaml）
#   BACKEND_PORT     后端端口（默认 8100）
#   SERVICE_USER     systemd 服务用户（install 渲染时用，默认当前用户）

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="${ROBOT_ENV_FILE:-$PROJECT_DIR/robot.env}"
CONFIG="${ROBOT_CONFIG:-configs/hardware.yaml}"
BACKEND_PORT="${BACKEND_PORT:-8100}"
HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/healthz"
LOG_DIR="logs"
RUN_DIR="run"

log()  { printf '[quick-start] %s\n' "$*"; }
warn() { printf '[quick-start] 警告: %s\n' "$*"; }
die()  { printf '[quick-start] 错误: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux*) ;;
  *) die "此脚本面向 Linux 机器人；当前平台 $(uname -s) 请按 deploy/README.md 手动启动。" ;;
esac

load_env() {
  if [ -f "$ENV_FILE" ]; then
    if grep -q $'\r' "$ENV_FILE" 2>/dev/null; then
      warn "$ENV_FILE 含 Windows 换行（CRLF），自动转为 LF"
      sed -i 's/\r$//' "$ENV_FILE"
    fi
    set -a; . "$ENV_FILE"; set +a
    log "已加载凭据: $ENV_FILE"
  else
    warn "未找到 $ENV_FILE（可 cp robot.env.example robot.env 创建），假定凭据已在当前 shell 环境中"
  fi
  local missing=()
  [ -z "${ROBOT_DB_PASSWORD:-}" ] && missing+=("ROBOT_DB_PASSWORD")
  [ -z "${CAMERA_PASSWORD:-}" ] && missing+=("CAMERA_PASSWORD")
  if [ "${#missing[@]}" -gt 0 ]; then
    die "缺少必需变量: ${missing[*]}（检查 $ENV_FILE 或当前环境）"
  fi
}

ensure_venv() {
  # 复制来的 Windows venv 没有 bin/python，会被这里识别并重建
  if [ ! -x .venv/bin/python ]; then
    warn ".venv 不可用（缺失或来自 Windows 复制），执行 uv sync 重建…"
    command -v uv >/dev/null 2>&1 || die "未找到 uv：请先安装（curl -LsSf https://astral.sh/uv/install.sh | sh）"
    uv sync --frozen
  fi
  [ -x .venv/bin/python ] || die ".venv/bin/python 仍不可用，请检查 uv sync 输出"
}

pid_of() {
  local pidfile="$RUN_DIR/$1.pid" pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile")"
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s' "$pid"
}

assert_alive() {
  # 启动后稍候确认进程仍存活，捕获 import/配置错误导致的秒退
  local name="$1" pid
  sleep 0.3
  if ! pid="$(pid_of "$name")"; then
    die "$name 启动即退出，查看 $LOG_DIR/$name.log"
  fi
}

start_component() {
  local name="$1" module="$2" pid
  if pid="$(pid_of "$name")"; then
    warn "$name 已在运行 (pid $pid)，跳过"
    return
  fi
  nohup .venv/bin/python -m "$module" --config "$CONFIG" >> "$LOG_DIR/$name.log" 2>&1 &
  printf '%s' "$!" > "$RUN_DIR/$name.pid"
  assert_alive "$name"
  log "$name 已启动 (pid $(cat "$RUN_DIR/$name.pid"))，日志 $LOG_DIR/$name.log"
}

start_backend() {
  local pid
  if pid="$(pid_of backend)"; then
    warn "backend 已在运行 (pid $pid)，跳过"
    return
  fi
  nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" \
    >> "$LOG_DIR/backend.log" 2>&1 &
  printf '%s' "$!" > "$RUN_DIR/backend.pid"
  assert_alive backend
  log "backend 已启动 (pid $(cat "$RUN_DIR/backend.pid"))，日志 $LOG_DIR/backend.log"
}

wait_for_socket() {
  local i
  for i in $(seq 1 40); do
    if [ -S "$RUN_DIR/robot-core.sock" ]; then
      log "robot-core socket 就绪"
      return 0
    fi
    sleep 0.5
  done
  die "robot-core socket 20s 内未就绪，查看 $LOG_DIR/robot_core.log"
}

wait_for_health() {
  local i
  for i in $(seq 1 30); do
    if .venv/bin/python -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=3)" \
        "$HEALTH_URL" >/dev/null 2>&1; then
      log "healthz OK: $HEALTH_URL"
      return 0
    fi
    sleep 0.5
  done
  die "backend 健康检查 15s 未通过，查看 $LOG_DIR/backend.log"
}

do_check() {
  load_env
  ensure_venv
  mkdir -p "$LOG_DIR" data run
  log "冒烟检查: robot-core --check（验证配置 + PostgreSQL 连通）"
  .venv/bin/python -m app.robot_core.processes.robot_core --config "$CONFIG" --check
  log "冒烟检查通过"
}

do_install() {
  # 渲染 systemd 单元模板（按当前目录/用户/端口）→ /etc/systemd/system 并启动。
  # 项目挪位置、换用户、换端口后，重新执行一次本命令即可。
  if [ ! -f "$ENV_FILE" ]; then
    die "未找到 $ENV_FILE：请先 cp robot.env.example robot.env 并填写"
  fi
  ensure_venv
  mkdir -p "$LOG_DIR" data run
  local service_user="${SERVICE_USER:-${SUDO_USER:-$USER}}" staged
  staged="$(mktemp -d)"
  for unit in deploy/*.service.in; do
    sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__SERVICE_USER__|$service_user|g" \
        -e "s|__BACKEND_PORT__|$BACKEND_PORT|g" \
        "$unit" > "$staged/$(basename "$unit" .in)"
  done
  sudo cp "$staged"/*.service deploy/inspection-robot.target /etc/systemd/system/
  rm -rf "$staged"
  sudo systemctl daemon-reload
  sudo systemctl enable --now inspection-robot.target \
    || warn "启动失败，查看: journalctl -u backend -e"
  log "systemd 单元已按 $PROJECT_DIR 渲染并启动（服务用户: $service_user）"
}

do_start() {
  load_env
  ensure_venv
  mkdir -p "$LOG_DIR" data run
  log "冒烟检查…"
  .venv/bin/python -m app.robot_core.processes.robot_core --config "$CONFIG" --check

  start_component robot_core        app.robot_core.processes.robot_core
  wait_for_socket
  start_component sensor_worker     app.robot_core.processes.sensor_worker
  start_component inference_worker  app.robot_core.processes.inference_worker
  start_component inspection_worker app.robot_core.processes.inspection_worker
  start_component sync_agent        app.robot_core.processes.sync_agent
  start_backend
  wait_for_health
  log "全部组件已启动。前端地址: http://<机器人IP>:${BACKEND_PORT}"
}

do_stop() {
  local name pid stopped=0
  # 与启动相反的顺序停止
  for name in backend sync_agent inspection_worker inference_worker sensor_worker robot_core; do
    if pid="$(pid_of "$name")"; then
      kill "$pid" 2>/dev/null || true
      log "$name 已发送停止信号 (pid $pid)"
      stopped=1
    fi
  done
  # 兜底：清理未写入 pid 文件的残留进程
  pkill -f "app.robot_core.processes" 2>/dev/null || true
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  rm -f "$RUN_DIR"/*.pid
  [ "$stopped" = 1 ] || log "没有在运行的组件"
}

do_status() {
  local name pid
  for name in robot_core sensor_worker inference_worker inspection_worker sync_agent backend; do
    if pid="$(pid_of "$name")"; then
      printf '%-20s RUNNING (pid %s)\n' "$name" "$pid"
    else
      printf '%-20s STOPPED\n' "$name"
    fi
  done
  if [ -x .venv/bin/python ] && .venv/bin/python -c \
      "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=3)" \
      "$HEALTH_URL" >/dev/null 2>&1; then
    log "healthz OK: $HEALTH_URL"
  else
    warn "healthz 不可达: $HEALTH_URL"
  fi
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  do_status ;;
  check)   do_check ;;
  install) do_install ;;
  *)       die "用法: $0 {start|stop|restart|status|check|install}" ;;
esac
