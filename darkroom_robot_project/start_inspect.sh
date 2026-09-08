#!/usr/bin/env bash
# 운영 UI(gui_server)와 실행기(robot_client)를 같이 켠다.
# 수집 UI(collect_ui)와는 카메라를 같이 쓰지 않는다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export DISPLAY="${DISPLAY:-:0}"
export PYTHONUNBUFFERED=1

LOG_DIR="${DARKROOM_LOG_DIR:-/tmp/darkroom}"
mkdir -p "$LOG_DIR"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

usage() {
  echo "사용: $0 [--start|--restart|--stop|--status]"
  echo "  기본은 --restart (검사 UI·실행기를 끄고 다시 켠다)"
}

inspect_pids() {
  pgrep -f "$PY -u gui_server.py" || true
  pgrep -f "$PY -u robot_client.py" || true
}

collect_running() {
  # Cursor 컴파일 등 명령줄에 collect_ui.py 만 있는 프로세스는 제외한다.
  local line
  while IFS= read -r line; do
    case "$line" in
      *cursorsandbox*|*py_compile*|*pgrep*) continue ;;
      *[Pp]ython*collect_ui.py*) return 0 ;;
    esac
  done < <(pgrep -af 'collect_ui\.py' 2>/dev/null || true)
  return 1
}

stop_inspect() {
  # 수집 UI는 건드리지 않는다.
  pkill -f "$PY -u gui_server.py" 2>/dev/null || true
  pkill -f "$PY -u robot_client.py" 2>/dev/null || true
  local i
  for i in 1 2 3 4 5 6 7 8; do
    if ! ss -ltn 2>/dev/null | grep -q ':8585'; then
      return 0
    fi
    sleep 0.2
  done
}

status_inspect() {
  echo "DISPLAY=$DISPLAY"
  echo "python=$PY"
  if ss -ltn 2>/dev/null | grep -q ':8585'; then
    echo "gui_server: 포트 8585 대기 중"
  else
    echo "gui_server: 꺼짐"
  fi
  if pgrep -f "$PY -u robot_client.py" >/dev/null 2>&1; then
    echo "robot_client: 실행 중"
  else
    echo "robot_client: 꺼짐"
  fi
  if collect_running; then
    echo "collect_ui: 실행 중 (검사와 동시에 켜지 말 것)"
  fi
}

wait_port() {
  local i
  for i in $(seq 1 25); do
    if ss -ltn 2>/dev/null | grep -q ':8585'; then
      return 0
    fi
    sleep 0.2
  done
  echo "gui_server 가 포트 8585를 열지 못했습니다. 로그: $LOG_DIR/gui_server.log" >&2
  tail -n 30 "$LOG_DIR/gui_server.log" >&2 || true
  return 1
}

start_inspect() {
  if collect_running; then
    echo "수집 UI가 켜져 있습니다. 카메라를 같이 쓸 수 없어서 검사를 시작하지 않습니다." >&2
    echo "수집 창을 닫은 뒤 다시 실행하세요." >&2
    exit 1
  fi
  if ! command -v xset >/dev/null 2>&1 || ! xset q >/dev/null 2>&1; then
    echo "DISPLAY=$DISPLAY 에 연결하지 못했습니다. 모니터가 붙은 세션에서 실행하세요." >&2
    exit 1
  fi

  : > "$LOG_DIR/gui_server.log"
  : > "$LOG_DIR/robot_client.log"

  setsid env DISPLAY="$DISPLAY" PYTHONUNBUFFERED=1 \
    "$PY" -u gui_server.py \
    >> "$LOG_DIR/gui_server.log" 2>&1 < /dev/null &
  echo "gui_server pid=$!  log=$LOG_DIR/gui_server.log"

  wait_port

  setsid env DISPLAY="$DISPLAY" PYTHONUNBUFFERED=1 \
    "$PY" -u robot_client.py \
    >> "$LOG_DIR/robot_client.log" 2>&1 < /dev/null &
  echo "robot_client pid=$!  log=$LOG_DIR/robot_client.log"
  echo "검사 UI와 실행기를 켰습니다. 창에 '실행기 연결됨'이 보이면 검사를 시작할 수 있습니다."
}

cmd="${1:---restart}"
case "$cmd" in
  --help|-h) usage ;;
  --status) status_inspect ;;
  --stop) stop_inspect; echo "검사 UI·실행기를 종료했습니다." ;;
  --start)
    if ss -ltn 2>/dev/null | grep -q ':8585'; then
      echo "이미 포트 8585가 열려 있습니다. 다시 켜려면 $0 --restart" >&2
      status_inspect
      exit 1
    fi
    start_inspect
    ;;
  --restart)
    stop_inspect
    start_inspect
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
