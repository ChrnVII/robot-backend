#!/usr/bin/env bash
# quick_start_up.sh —— 手动启动控制、感知与 FastAPI 三进程（同步可选）。
# 调试/验证用；生产环境建议使用 systemd（deploy/*.service + inspection-robot.target）。
#
# 用法:
#   deploy/quick_start_up.sh start     启动全部（默认）
#   deploy/quick_start_up.sh stop      停止全部（只停本脚本托管的进程）
#   deploy/quick_start_up.sh stop --all   连同「进程在跑但没有 pidfile」的残留一起停
#   deploy/quick_start_up.sh restart   重启
#   deploy/quick_start_up.sh status    查看各组件真实状态（含孤儿进程对账）
#   deploy/quick_start_up.sh check     仅冒烟检查（配置 + PostgreSQL 连通 + 串口权限）
#   deploy/quick_start_up.sh install   安装/刷新 systemd 单元并启动（按当前路径渲染）
#
# 环境变量:
#   ROBOT_ENV_FILE   凭据文件路径（默认 <项目根>/robot.env）
#   ROBOT_CONFIG     进程配置文件（默认 configs/hardware.yaml）
#   BACKEND_PORT     后端端口（默认 8100）
#   SERVICE_USER     systemd 服务用户（install 渲染时用，默认当前用户）
#
# 状态判定原则（v2）：pidfile 只是线索，不是结论。
#   - pidfile 里的 PID 必须「活着 + 命令行对得上模块」，否则视为无效（防 PID 复用）；
#   - start 启动前先确认没有同名进程 / 端口 / IPC 锁被占，否则直接拒绝，不再静默继续；
#   - 就绪判定改为真实探活：robot-core 走一次 IPC health，backend 校验 coreStatus。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="${ROBOT_ENV_FILE:-$PROJECT_DIR/robot.env}"
CONFIG="${ROBOT_CONFIG:-configs/hardware.yaml}"
BACKEND_PORT="${BACKEND_PORT:-8100}"
HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/healthz"
LOG_DIR="logs"
RUN_DIR="run"

#: robot-core 在 <sock>.lock 上持 flock，抢不到就退出（见 app/ipc/json_socket.py）。
CORE_LOCK="$RUN_DIR/robot-core.sock.lock"
#: 组件顺序：停止时反序使用。
COMPONENTS="robot_core sensor_worker inference_worker inspection_worker sync_agent backend"
#: stop --all：连同「进程在跑但没有 pidfile」的残留一起停。默认只在显式传参时打开。
FORCE_ALL=false

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
      warn "$ENV_FILE 含 CRLF 换行，自动转为 LF"
      sed -i 's/\r$//' "$ENV_FILE"
    fi
    set -a; . "$ENV_FILE"; set +a
    log "已加载凭据: $ENV_FILE"
  else
    warn "未找到 $ENV_FILE（可 cp robot.env.example robot.env 创建），假定凭据已在当前 shell 环境中"
  fi
  export ROBOT_PLATFORM_CONFIG="$CONFIG"
  local missing=()
  [ -z "${ROBOT_DB_PASSWORD:-}" ] && missing+=("ROBOT_DB_PASSWORD")
  [ -z "${CAMERA_PASSWORD:-}" ] && missing+=("CAMERA_PASSWORD")
  if [ "${#missing[@]}" -gt 0 ]; then
    die "缺少必需变量: ${missing[*]}（检查 $ENV_FILE 或当前环境）"
  fi
}

ensure_venv() {
  # 非本机 Linux 创建的 venv 没有 bin/python，会被这里识别并重建
  if [ ! -x .venv/bin/python ]; then
    warn ".venv 不可用（缺失或平台不兼容），执行 uv sync 重建…"
    command -v uv >/dev/null 2>&1 || die "未找到 uv：请先安装（curl -LsSf https://astral.sh/uv/install.sh | sh）"
    uv sync --frozen
  fi
  [ -x .venv/bin/python ] || die ".venv/bin/python 仍不可用，请检查 uv sync 输出"
}

check_audio_deps() {
  # 麦克风采集的系统依赖：libportaudio2（sounddevice 运行时必需，PyPI wheel 不带）
  # 与 alsa-utils（amixer/arecord，启动预检与增益固化用）。
  local missing=()
  ldconfig -p 2>/dev/null | grep -qi libportaudio || missing+=(libportaudio2)
  command -v amixer >/dev/null 2>&1 || missing+=(alsa-utils)
  if [ "${#missing[@]}" -gt 0 ]; then
    if command -v apt-get >/dev/null 2>&1 && [ "${ROBOT_SKIP_APT:-false}" != "true" ]; then
      warn "缺少系统依赖: ${missing[*]}，尝试 apt 安装…"
      sudo apt-get update -qq || true
      sudo apt-get install -y "${missing[@]}" || die "apt 安装失败，请手动执行: sudo apt install ${missing[*]}"
    else
      die "缺少系统依赖: ${missing[*]}，请手动执行: sudo apt install ${missing[*]}"
    fi
  fi
  .venv/bin/python -c 'import sounddevice' 2>/dev/null \
    || die "sounddevice 无法加载（通常仍是 libportaudio2 未装或 .venv 未重建）"
  # 硬件模式预检：按名字找配置的输入设备（device: "USB Audio"）。
  if grep -A 4 '^  audio:' "$CONFIG" 2>/dev/null | grep -q 'mode: hardware\|device:'; then
    .venv/bin/python - <<'PY' || warn "未按名字匹配到音频输入设备（可先继续，录音时会回退默认并打 WARN）"
import sys, sounddevice as sd
name = "USB Audio"
inputs = [str(d["name"]) for d in sd.query_devices() if d["max_input_channels"] > 0]
print("[quick-start] 音频输入设备:", inputs)
if not any(name.lower() in n.lower() for n in inputs):
    sys.exit(1)
PY
  fi
}

warn_audio_deps() {
  # start 只做软提示：开发阶段不该因为没插麦克风就拒绝启动控制链路。
  .venv/bin/python -c 'import sounddevice' 2>/dev/null \
    || warn "sounddevice 不可用（缺 libportaudio2 或 venv 未重建），录音相关功能会失败"
}

check_serial_access() {
  # 串口直读（environment / battery）要求当前用户能读写设备节点。
  # systemd 单元用 SupplementaryGroups=dialout 解决；本脚本继承调用者的组。
  local port
  for port in /dev/ttyS0 /dev/ttyS5; do
    if [ ! -e "$port" ]; then
      warn "$port 不存在，对应通道会一直是 unavailable（页面显示横杠）"
    elif [ ! -r "$port" ] || [ ! -w "$port" ]; then
      warn "$port 当前用户不可读写；若日志报 Permission denied，执行: sudo usermod -aG dialout $USER 后重新登录"
    fi
  done
}

# ------------------------------------------------------------------ 进程身份

#: 组件 → 命令行里可识别的模块片段。pidfile 里记的 PID 必须能对上它。
pattern_of() {
  case "$1" in
    backend) printf 'uvicorn app.main:app' ;;
    robot_core|sensor_worker|inference_worker|inspection_worker|sync_agent)
      printf 'app.robot_core.processes.%s' "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

#: 该 PID 是否确实是「<组件> 的 python -m 进程」。命令行对不上就当它无效（PID 被复用）。
#: 直接读 /proc/<pid>/cmdline（NUL 分隔），不受 ps 版本/格式影响。
is_ours() {
  local pid="$1" name="$2" args pattern
  pattern="$(pattern_of "$name")"
  args="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$args" in
    *python*-m*"$pattern"*) return 0 ;;
  esac
  return 1
}

#: 进程是否还在运行。僵尸进程算「已死」——它只是等父进程回收，端口和串口都已释放。
proc_alive() {
  local stat state
  stat="$(cat "/proc/$1/stat" 2>/dev/null || true)"
  if [ -z "$stat" ]; then
    return 1
  fi
  stat="${stat##*)}"   # 丢掉 "pid (comm)"，comm 里可能有空格或括号
  stat="${stat# }"
  state="${stat%% *}"
  [ "$state" != "Z" ]
}

pid_of() {
  local name="$1" pidfile="$RUN_DIR/$1.pid" pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$pid" 2>/dev/null || return 1
  proc_alive "$pid" || return 1
  is_ours "$pid" "$name" || return 1
  printf '%s' "$pid"
}

#: 按命令行找同名进程（不依赖 pidfile）。输出空格分隔的 PID 列表。
pids_by_name() {
  local name="$1" pattern pid out=""
  pattern="$(pattern_of "$name")"
  for pid in $(pgrep -f -- "$pattern" 2>/dev/null || true); do
    if proc_alive "$pid" && is_ours "$pid" "$name"; then
      out="${out:+$out }$pid"
    fi
  done
  printf '%s' "$out"
}

proc_cwd() {
  readlink "/proc/$1/cwd" 2>/dev/null || printf '?'
}

port_pids() {
  command -v ss >/dev/null 2>&1 || return 0
  ss -ltnp "sport = :$BACKEND_PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true
}

#: 端口上是否已有监听者。不依赖 -p，普通用户也能用（看不到对方 PID 时仍能判定「被占」）。
port_listening() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -ltn "sport = :$BACKEND_PORT" 2>/dev/null | grep -q LISTEN
}

#: 该 flock 文件当前是否空闲（能抢到锁 = 没有别的实例持锁）。
lock_free() {
  local lockfile="$1"
  mkdir -p "$(dirname "$lockfile")"
  exec 8>>"$lockfile"
  if flock -n 8; then
    exec 8>&-
    return 0
  fi
  exec 8>&-
  return 1
}

acquire_lock() {
  # 脚本自身互斥：两个 start 并发时，后到的直接退出，避免各写一份 pidfile。
  mkdir -p "$RUN_DIR"
  exec 9>>"$RUN_DIR/quick-start.lock"
  if flock -n 9; then
    return 0
  fi
  # 锁拿不到，但占锁的可能是旧版本遗留的子进程（它们继承了 fd 9）。
  # 只有当「确实还有另一个脚本实例在跑」时才拒绝，否则继续——不然会永久卡死。
  local others
  others="$(pgrep -f '(bash|sh) .*quick_start_up\.sh' 2>/dev/null | grep -vw "$$" | tr '\n' ' ' || true)"
  if [ -n "$others" ]; then
    die "另一个 quick_start_up.sh 正在执行 (pid=$others)，稍后重试"
  fi
  warn "$RUN_DIR/quick-start.lock 被遗留进程占用（当前没有脚本在跑），继续执行"
}

require_tools() {
  # 状态判定依赖这几个工具；缺了会让「孤儿检测」静默失效，所以宁可直接报错。
  local tool missing=""
  for tool in flock pgrep ss; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      missing="$missing $tool"
    fi
  done
  if [ -n "$missing" ]; then
    die "缺少命令:$missing（Debian/Ubuntu: sudo apt install util-linux procps iproute2）"
  fi
}

# ---------------------------------------------------------------- 启动前预检

ensure_not_foreign() {
  local name="$1" pids p
  if [ -n "$(pid_of "$name" || true)" ]; then
    return 0
  fi
  pids="$(pids_by_name "$name")"
  if [ -z "$pids" ]; then
    return 0
  fi
  for p in $pids; do
    warn "$name 存在本脚本未托管的进程 pid=$p（cwd=$(proc_cwd "$p")）"
  done
  die "$name 已有实例在运行但不在 $RUN_DIR/$name.pid；确认后执行: $0 stop --all"
}

preflight() {
  local pids p name
  # robot-core 已在跑（且 PID 对得上）时不做锁校验：start 是幂等的，重复执行只会跳过。
  if [ -z "$(pid_of robot_core || true)" ] && ! lock_free "$CORE_LOCK"; then
    die "$CORE_LOCK 被占用，说明已有 robot-core 在服务但不在 $RUN_DIR/robot_core.pid；先执行: $0 status"
  fi
  if port_listening; then
    pids="$(port_pids)"
    if [ -z "$(pid_of backend || true)" ]; then
      for p in $pids; do
        warn "端口 $BACKEND_PORT 已被 pid=$p 占用（cwd=$(proc_cwd "$p")）"
      done
      die "端口 $BACKEND_PORT 已被占用；确认后执行: $0 stop --all（查占用者: sudo ss -ltnp 'sport = :$BACKEND_PORT'）"
    fi
  fi
  for name in robot_core inspection_worker sync_agent backend; do
    ensure_not_foreign "$name"
  done
}

# -------------------------------------------------------------------- 启动

assert_alive() {
  # 只确认「进程没有秒退」；真正的就绪由 wait_for_core / wait_for_health 判定。
  local name="$1"
  sleep 1.5
  if [ -z "$(pid_of "$name" || true)" ]; then
    die "$name 启动即退出，查看 $LOG_DIR/$name.log"
  fi
}

start_component() {
  local name="$1" module="$2" pid
  pid="$(pid_of "$name" || true)"
  if [ -n "$pid" ]; then
    warn "$name 已在运行 (pid $pid)，跳过；要加载新代码请用: $0 restart"
    return
  fi
  ensure_not_foreign "$name"
  # 9>&- 必不可少：否则子进程继承脚本的锁 fd，脚本退出后锁仍被服务代持，后续 stop 全被卡住。
  nohup .venv/bin/python -m "$module" --config "$CONFIG" >> "$LOG_DIR/$name.log" 2>&1 9>&- &
  printf '%s' "$!" > "$RUN_DIR/$name.pid"
  assert_alive "$name"
  log "$name 已启动 (pid $(cat "$RUN_DIR/$name.pid"))，日志 $LOG_DIR/$name.log"
}

start_backend() {
  local pid pids
  pid="$(pid_of backend || true)"
  if [ -n "$pid" ]; then
    warn "backend 已在运行 (pid $pid)，跳过；要加载新代码请用: $0 restart"
    return
  fi
  if port_listening; then
    pids="$(port_pids)"
    die "端口 $BACKEND_PORT 已被占用${pids:+（pid=$pids）}，拒绝重复启动；查占用者: sudo ss -ltnp 'sport = :$BACKEND_PORT'"
  fi
  nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" \
    >> "$LOG_DIR/backend.log" 2>&1 9>&- &
  printf '%s' "$!" > "$RUN_DIR/backend.pid"
  assert_alive backend
  log "backend 已启动 (pid $(cat "$RUN_DIR/backend.pid"))，日志 $LOG_DIR/backend.log"
}

assert_stopped() {
  # sensor-worker / inference-worker 必须真的退干净：inspection-worker 要接管它们的 socket，
  # 端口/socket 还被占着就会「启动成功但链路是坏的」。
  local name="$1" pids
  pids="$(pids_by_name "$name")"
  if [ -n "$pids" ]; then
    die "$name 仍在运行 (pid=$pids)，inspection-worker 无法接管 socket；先执行: $0 stop --all"
  fi
}

# ---------------------------------------------------------------- 就绪判定

core_state() {
  # 真去问一次 robot-core：能答上 health 才算就绪，socket 文件存在不算。
  .venv/bin/python - "$RUN_DIR/robot-core.sock" <<'PY'
import json, socket, sys
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(sys.argv[1])
        client.sendall(json.dumps({"operation": "health"}).encode() + b"\n")
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            buffer += chunk
    reply = json.loads(buffer)
except Exception:
    sys.exit(1)
if not reply.get("ok"):
    sys.exit(1)
print((reply.get("result") or {}).get("state", ""))
PY
}

health_core_status() {
  # 打印 healthz 的 coreStatus；只有等于 ok 才算通过（HTTP 200 在 core 挂掉时也会返回）。
  .venv/bin/python - "$HEALTH_URL" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        payload = json.load(response)
except Exception:
    sys.exit(1)
status = str((payload.get("data") or {}).get("coreStatus") or "")
print(status)
sys.exit(0 if status == "ok" else 1)
PY
}

wait_for_core() {
  local i state
  # 硬件模式下 core.start() 要先 self_test 串口/相机，给足 30s。
  for i in $(seq 1 60); do
    if state="$(core_state)"; then
      log "robot-core 就绪 (state=${state:-unknown})"
      return 0
    fi
    if [ -z "$(pid_of robot_core || true)" ]; then
      die "robot-core 已退出（未能绑定 IPC），查看 $LOG_DIR/robot_core.log"
    fi
    sleep 0.5
  done
  die "robot-core 30s 内未响应 IPC health，查看 $LOG_DIR/robot_core.log"
}

wait_for_paths() {
  local name="$1"; shift
  local i path missing
  # inspection-worker 要先加载模型再绑定 socket，给足 30s。
  for i in $(seq 1 60); do
    missing=""
    for path in "$@"; do
      [ -S "$path" ] || missing="$missing $path"
    done
    if [ -z "$missing" ]; then
      log "$name socket 就绪"
      return 0
    fi
    if [ -z "$(pid_of "$name" || true)" ]; then
      die "$name 已退出，查看 $LOG_DIR/$name.log"
    fi
    sleep 0.5
  done
  die "$name 的 socket 30s 内未就绪:$missing"
}

wait_for_health() {
  local i status
  for i in $(seq 1 30); do
    if status="$(health_core_status)"; then
      log "healthz 就绪 (coreStatus=$status): $HEALTH_URL"
      return 0
    fi
    if [ -z "$(pid_of backend || true)" ]; then
      die "backend 已退出，查看 $LOG_DIR/backend.log"
    fi
    sleep 0.5
  done
  die "backend 15s 内未就绪（coreStatus 非 ok），查看 $LOG_DIR/backend.log"
}

# ---------------------------------------------------------------- 数据库/配置

prepare_database() {
  case "${ROBOT_AUTO_MIGRATE:-true}" in
    false|False|FALSE|0|no|NO) ;;
    *) .venv/bin/python -m app.robot_core.storage.migrate --config "$CONFIG" upgrade ;;
  esac
  .venv/bin/python -m app.robot_core.storage.migrate --config "$CONFIG" check
}

sync_enabled() {
  .venv/bin/python -c 'import sys; from app.robot_core.common.config import load_config; sys.exit(0 if load_config(sys.argv[1]).sync_enabled else 1)' "$CONFIG"
}

# -------------------------------------------------------------------- 停止

stop_component() {
  local name="$1" pid i p pids killed="" alive
  pid="$(pid_of "$name" || true)"
  pids="$(pids_by_name "$name")"
  if [ -z "$pid" ] && [ -z "$pids" ]; then
    rm -f "$RUN_DIR/$name.pid"
    return
  fi

  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    killed="$pid"
  fi

  for p in $pids; do
    if [ "$p" = "$pid" ]; then
      continue
    fi
    if [ "$FORCE_ALL" = true ]; then
      warn "$name 停止未受管进程 pid=$p（cwd=$(proc_cwd "$p")）"
      kill "$p" 2>/dev/null || true
      killed="$killed $p"
    else
      # 不在 pidfile 里的进程不擅自杀：可能是别的终端/别的配置在跑的实例。
      warn "$name 还有未受管进程 pid=$p（不在 $RUN_DIR/$name.pid）；确认后执行: $0 stop --all"
    fi
  done

  if [ -z "$killed" ]; then
    return
  fi

  for i in $(seq 1 60); do
    alive=""
    for p in $killed; do
      if proc_alive "$p"; then
        alive="$alive $p"
      fi
    done
    if [ -z "$alive" ]; then
      rm -f "$RUN_DIR/$name.pid"
      log "$name 已停止"
      return
    fi
    sleep 0.5
  done
  die "$name 未在 30 秒内退出:$alive"
}

# -------------------------------------------------------------------- 入口

do_check() {
  load_env
  ensure_venv
  check_audio_deps
  check_serial_access
  mkdir -p "$LOG_DIR" data run
  log "冒烟检查: robot-core --check（验证配置 + PostgreSQL 连通；不打开串口/相机）"
  .venv/bin/python -m app.robot_core.processes.robot_core --config "$CONFIG" --check
  log "冒烟检查通过（硬件端口可用性需 start 后看 status / healthz）"
}

do_install() {
  # 渲染 systemd 单元模板（按当前目录/用户/端口）→ /etc/systemd/system 并启动。
  # 项目挪位置、换用户、换端口后，重新执行一次本命令即可。
  if [ ! -f "$ENV_FILE" ]; then
    die "未找到 $ENV_FILE：请先 cp robot.env.example robot.env 并填写"
  fi
  load_env
  ensure_venv
  check_audio_deps
  mkdir -p "$LOG_DIR" data run
  prepare_database
  local service_user="${SERVICE_USER:-${SUDO_USER:-$USER}}" staged
  staged="$(mktemp -d)"
  local amixer_bin
  amixer_bin="$(command -v amixer || printf '/usr/bin/amixer')"
  for unit in deploy/*.service.in; do
    sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__SERVICE_USER__|$service_user|g" \
        -e "s|__BACKEND_PORT__|$BACKEND_PORT|g" \
        -e "s|__BIN_AMIXER__|$amixer_bin|g" \
        "$unit" > "$staged/$(basename "$unit" .in)"
  done
  sudo cp "$staged"/*.service deploy/inspection-robot.target /etc/systemd/system/
  rm -rf "$staged"
  sudo systemctl daemon-reload
  sudo systemctl disable --now sensor-worker.service inference-worker.service || true
  sudo systemctl enable --now inspection-robot.target \
    || warn "启动失败，查看: journalctl -u backend -e"
  sudo systemctl restart robot-core.service inspection-worker.service backend.service
  if sync_enabled; then
    sudo systemctl enable --now sync-agent.service
  else
    sudo systemctl disable --now sync-agent.service || true
  fi
  log "systemd 单元已按 $PROJECT_DIR 渲染并启动（服务用户: $service_user）"
}

do_start() {
  load_env
  ensure_venv
  mkdir -p "$LOG_DIR" data run
  preflight
  warn_audio_deps
  check_serial_access
  prepare_database
  log "冒烟检查…（只验配置 + 数据库，硬件端口在 start 后由探活确认）"
  .venv/bin/python -m app.robot_core.processes.robot_core --config "$CONFIG" --check

  start_component robot_core app.robot_core.processes.robot_core
  wait_for_core
  stop_component sensor_worker
  stop_component inference_worker
  assert_stopped sensor_worker
  assert_stopped inference_worker
  start_component inspection_worker app.robot_core.processes.inspection_worker
  # inspection-worker 同时提供 sensor-worker.sock 与 inspection-worker.sock，并独占模型。
  wait_for_paths inspection_worker "$RUN_DIR/sensor-worker.sock" "$RUN_DIR/inspection-worker.sock"
  if sync_enabled; then
    start_component sync_agent app.robot_core.processes.sync_agent
  else
    stop_component sync_agent
  fi
  start_backend
  wait_for_health
  log "全部组件已启动（配置: $CONFIG）"
  log "接口地址: http://<机器人IP>:${BACKEND_PORT}/api/v1　healthz: $HEALTH_URL"
}

do_stop() {
  local name
  # 与启动相反的顺序停止
  for name in backend sync_agent inspection_worker inference_worker sensor_worker robot_core; do
    stop_component "$name"
  done
  rm -f "$RUN_DIR"/*.pid
}

do_status() {
  local name pid pids p state
  for name in $COMPONENTS; do
    pid="$(pid_of "$name" || true)"
    if [ -n "$pid" ]; then
      printf '%-20s RUNNING (pid %s)\n' "$name" "$pid"
      continue
    fi
    pids="$(pids_by_name "$name")"
    if [ -n "$pids" ]; then
      printf '%-20s ORPHAN  (pid %s) 进程在跑但不受本脚本管理\n' "$name" "$pids"
      for p in $pids; do
        printf '%-20s         cwd=%s\n' '' "$(proc_cwd "$p")"
      done
      continue
    fi
    printf '%-20s STOPPED\n' "$name"
  done

  pids="$(port_pids)"
  if [ -n "$pids" ]; then
    for p in $pids; do
      log "端口 ${BACKEND_PORT} <- pid $p（cwd=$(proc_cwd "$p")）"
    done
  else
    warn "端口 ${BACKEND_PORT} 无人监听"
  fi

  if state="$(health_core_status)"; then
    log "healthz OK (coreStatus=$state)"
  else
    warn "healthz 不就绪 (coreStatus=${state:-unreachable}): $HEALTH_URL"
  fi

  if [ -n "$(pids_by_name robot_core)" ] && [ -z "$(pid_of robot_core || true)" ]; then
    warn "有 robot-core 在跑但不在 $RUN_DIR/robot_core.pid：\"$0 start\" 会拒绝启动，先 \"$0 stop --all\""
  fi
}

COMMAND="${1:-start}"
case "${2:-}" in
  --all) FORCE_ALL=true ;;
  '') ;;
  *) die "未知参数: ${2}（仅支持 --all）" ;;
esac

case "$COMMAND" in
  start|stop|restart|status) require_tools ;;
esac

case "$COMMAND" in
  start|stop|restart) acquire_lock ;;
esac

case "$COMMAND" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  do_status ;;
  check)   do_check ;;
  install) do_install ;;
  *)       die "用法: $0 {start|stop [--all]|restart|status|check|install}" ;;
esac
