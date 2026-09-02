#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export DISPLAY="${DISPLAY:-:0}"

run_ui() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    exec "$ROOT/.venv/bin/python" gui_server.py
  fi
  exec python3 gui_server.py
}

if [[ "${1:-}" == "--direct" ]]; then
  run_ui
fi

TITLE="NUC 비전 UI"
CMD="cd $(printf '%q' "$ROOT") && $(printf '%q' "$0") --direct; echo; echo '종료됨 — Enter'; read -r"

if command -v gnome-terminal >/dev/null 2>&1; then
  gnome-terminal --title="$TITLE" -- bash -lc "$CMD"
elif command -v xfce4-terminal >/dev/null 2>&1; then
  xfce4-terminal --title="$TITLE" -e bash -lc "$CMD"
elif command -v konsole >/dev/null 2>&1; then
  konsole -p tabtitle="$TITLE" -e bash -lc "$CMD"
elif command -v xterm >/dev/null 2>&1; then
  xterm -T "$TITLE" -e bash -lc "$CMD"
elif command -v x-terminal-emulator >/dev/null 2>&1; then
  x-terminal-emulator -e bash -lc "$CMD"
else
  echo "터미널 에뮬레이터를 찾지 못했습니다. 직접 실행:" >&2
  echo "  cd $ROOT && python3 gui_server.py" >&2
  exit 1
fi
