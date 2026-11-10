#!/usr/bin/env bash
# 앱 서버 종단 curl 시험: 생성 → 편집 → 거절(quality) → 승인 → 3D 유지 → 평가 → 삭제.
#   BASE=http://127.0.0.1:<APP_BIND_PORT> OUT=<결과 폴더> bash server/tools/app_smoke.sh
# 각 응답 JSON 을 OUT/NN-<단계>.json 으로 남긴다. 가짜 모드(APP_MOCK=1)에서 돌리는 것을 전제로 폴링 간격이 짧다.
set -euo pipefail
BASE="${BASE:?BASE=http://127.0.0.1:<port>}"
OUT="${OUT:-./app-smoke-$(date +%Y%m%d-%H%M%S)}"
QR="${QR:-smoke-$(date +%s)}"
DEV="X-Device-Key: 0x02smoketest"
mkdir -p "$OUT"; n=0
save() { n=$((n+1)); printf '%s\n' "$2" > "$OUT/$(printf %02d $n)-$1.json"; echo "[$n] $1: $(printf '%s' "$2" | head -c 220)"; }
req() { curl -s -H "$DEV" -H 'Content-Type: application/json' "$@"; }
field() { python3 -c "import json,sys;d=json.load(sys.stdin);print(d$1)"; }
wait_state() { # job_id 기대상태들
  for _ in $(seq 1 120); do j=$(req "$BASE/app/v1/jobs/$1"); s=$(printf '%s' "$j" | field "['state']")
    case " $2 " in *" $s "*) printf '%s' "$j"; return 0;; esac; sleep 0.5; done; echo "시간 초과: $1 $2" >&2; exit 1; }
save health "$(req "$BASE/app/v1/health")"
save qr-empty "$(req "$BASE/app/v1/qr/$QR")"
r=$(req -X POST "$BASE/app/v1/qr/$QR/asset" -d '{"prompt":"a jeep","seed":42}'); save create "$r"
JID=$(printf '%s' "$r" | field "['job_id']"); AID=$(printf '%s' "$r" | field "['asset_id']")
save generate-committed "$(wait_state "$JID" "committed failed")"
q=$(req "$BASE/app/v1/qr/$QR"); save qr "$q"; V=$(printf '%s' "$q" | field "['version']")
CAM='{"frame":"normalized_voxel","position":[0.1,-1.3,0.4],"forward":[0,1,0],"up":[0,0,1],"fov_y_deg":60,"aspect":0.46}'
r=$(req -X POST "$BASE/app/v1/assets/$AID/edits" -d "{\"base_version\":$V,\"instruction\":\"change the wheels into big offroad wheels\",\"camera\":$CAM,\"volume_cells\":[[31,40,22],[31,40,23]]}"); save edit "$r"
EJ=$(printf '%s' "$r" | field "['job_id']")
save edit-awaiting-2d "$(wait_state "$EJ" "awaiting_2d_review failed")"
save reject-quality "$(req -X POST "$BASE/app/v1/jobs/$EJ/review2d" -d '{"accept":false,"reason":"the wheels look blurry"}')"
save retry-awaiting-2d "$(wait_state "$EJ" "awaiting_2d_review failed")"
save accept "$(req -X POST "$BASE/app/v1/jobs/$EJ/review2d" -d '{"accept":true}')"
j=$(wait_state "$EJ" "awaiting_3d_review failed"); save awaiting-3d "$j"
U=$(printf '%s' "$j" | field "['result']['chunks'][0]['uri']"); H=$(printf '%s' "$j" | field "['result']['chunks'][0]['hash']")
G=$(curl -s "$BASE$U" | sha256sum | cut -c1-64); save staging-chunk-hash "{\"uri\":\"$U\",\"hash_ok\":$([ "$G" = "$H" ] && echo true || echo false)}"
save keep "$(req -X POST "$BASE/app/v1/jobs/$EJ/review3d" -d '{"keep":true}')"
save qr-after "$(req "$BASE/app/v1/qr/$QR")"
r=$(req -X POST "$BASE/app/v1/assets/$AID/ratings" -d '{"stars":4}'); save rating "$r"
save rating-again "$(req -X POST "$BASE/app/v1/assets/$AID/ratings" -d '{"stars":4}')"
save delete "{\"http\":$(curl -s -o /dev/null -w '%{http_code}' -H "$DEV" -X DELETE "$BASE/app/v1/qr/$QR")}"
save qr-deleted "$(req "$BASE/app/v1/qr/$QR")"
echo "OUT=$OUT"
