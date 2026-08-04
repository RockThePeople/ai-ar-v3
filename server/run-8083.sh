#!/usr/bin/env bash
# `:8083` 스켈레톤 기동.
#
# ★ 반드시 setsid --fork 로 띄운다. 과거에 Claude Code 세션이 끝나면서 자식
#   프로세스가 함께 죽어 서비스가 조용히 내려간 적이 있다. setsid 로 새 세션을
#   만들고 fork 하면 부모가 즉시 죽어 init(PID 1)이 입양한다 → PPID=1.
#   기동 후 이 스크립트가 PPID 를 직접 확인하고, 1 이 아니면 **실패로 끝낸다.**
#
# 포트는 DEBUGVIEW_PORT 환경변수로만 받는다 (PROGRESS §7). 기본값은 8083.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
PY="${PYTHON:-$REPO/.venv/bin/python}"
PORT="${DEBUGVIEW_PORT:-8083}"
LOG="${LOG:-$HOME/ai-ar-v3-assets/_logs/skeleton-$PORT.log}"

[ -x "$PY" ] || { echo "python 이 없다: $PY  (python3 -m venv .venv 먼저)"; exit 1; }
mkdir -p "$(dirname "$LOG")"

if ss -ltn "sport = :$PORT" | grep -q LISTEN; then
  echo "이미 :$PORT 를 누가 쓰고 있다. 먼저 내려라:"; ss -ltnp "sport = :$PORT"; exit 1
fi

echo "기동: :$PORT  log=$LOG"
DEBUGVIEW_PORT="$PORT" setsid --fork "$PY" "$HERE/skeleton.py" >> "$LOG" 2>&1

# bind 될 때까지 기다린다 — "떴다" 를 로그가 아니라 포트로 판정한다.
for _ in $(seq 1 40); do
  ss -ltn "sport = :$PORT" | grep -q LISTEN && break
  sleep 0.25
done

PID="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
[ -n "$PID" ] || { echo "FAIL — :$PORT 에 bind 되지 않았다"; tail -20 "$LOG"; exit 1; }

PPID_OF="$(ps -o ppid= -p "$PID" | tr -d ' ')"
echo "── 기동 확인 ──"
ps -o pid,ppid,user,lstart,cmd -p "$PID"
[ "$PPID_OF" = "1" ] || { echo "FAIL — PPID=$PPID_OF (1 이어야 한다). 세션이 끝나면 죽는다"; exit 1; }
echo "OK — PPID=1 확인"
