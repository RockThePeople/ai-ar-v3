#!/usr/bin/env bash
# 앱 서버 v1 기동·정지·상태. 설정은 app.env(APP_ENV_FILE) — 포트·주소를 이 스크립트에 적지 않는다.
#   bash server/run-app.sh start|stop|restart|status
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
PY="${PYTHON:-$REPO/.venv/bin/python}"
ENV_FILE="${APP_ENV_FILE:-$HOME/.config/vhl-app/app.env}"
# app.env 는 아직 없는 변수에만 넣는다(이미 설정된 환경변수가 우선 — 내부 규약 2절)
if [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    [ -n "$v" ] && [ -z "${!k+x}" ] && export "$k=$v"
  done < "$ENV_FILE"
fi
PORT="${APP_BIND_PORT:?APP_BIND_PORT 가 없다 (app.env)}"
DATA="${APP_DATA_DIR:-$HOME/vhl-app-data}"
LOG="${LOG:-$DATA/logs/app-server.log}"
pid_of() { ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true; }
start() {
  [ -x "$PY" ] || { echo "python 이 없다: $PY (PYTHON=... 으로 지정)"; exit 1; }
  if [ -n "$(pid_of)" ]; then echo "이미 :$PORT 에 떠 있다 (pid $(pid_of))"; exit 1; fi
  mkdir -p "$(dirname "$LOG")"
  echo "── start $(date -Is) commit $(git -C "$REPO" rev-parse --short HEAD)" >> "$LOG"
  (cd "$REPO" && APP_ENV_FILE="$ENV_FILE" setsid --fork "$PY" -u -m server.app.main >> "$LOG" 2>&1)
  for _ in $(seq 1 80); do [ -n "$(pid_of)" ] && { echo "ready :$PORT pid $(pid_of) log $LOG"; return 0; }; sleep 0.25; done
  echo "FAIL — 20 s 안에 안 떴다"; tail -20 "$LOG"; exit 1
}
stop() { local p; p="$(pid_of)"; [ -n "$p" ] || { echo "떠 있지 않다"; return 0; }; kill "$p"
  for _ in $(seq 1 40); do [ -z "$(pid_of)" ] && { echo "stopped ($p)"; return 0; }; sleep 0.25; done; kill -9 "$p" || true; }
status() { curl -s -m 5 "http://127.0.0.1:$PORT/app/v1/health" || { echo "응답 없음"; exit 1; }; echo; }
case "${1:-}" in start) start;; stop) stop;; restart) stop; start;; status) status;; *) echo "usage: $0 start|stop|restart|status"; exit 2;; esac
