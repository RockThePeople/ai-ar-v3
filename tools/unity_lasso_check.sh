#!/usr/bin/env bash
# Unity 를 **실제로 띄워서** 라쏘를 돌리고 헤드리스 골든과 대조한다 (W18).
#
# 🔴 왜 필요한가. 헤드리스 하네스는 핀홀 카메라를 손으로 구현했다. Unity 는
#    Camera.WorldToScreenPoint 를 쓰고 **좌표계가 왼손**이다. 어긋나면 폴리곤이
#    뒤집혀 엉뚱한 부분이 잡히고 **예외는 안 난다.**
#
# 프로젝트는 임시 디렉터리에 만들고 리포 파일을 **심링크**한다 — 복사하면 드리프트한다.
#
#   tools/unity_lasso_check.sh [케이스디렉터리]
#
# 환경변수 UNITY_BIN 으로 에디터 경로를 지정할 수 있다.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJ="${LASSO_UNITY_PROJECT:-${TMPDIR:-/tmp}/lasso-unity}"
CASES="${1:-$PROJ/Cases}"

UNITY_BIN="${UNITY_BIN:-}"
if [ -z "$UNITY_BIN" ]; then
  UNITY_BIN="$(ls -d /Applications/Unity/Hub/Editor/*/Unity.app/Contents/MacOS/Unity 2>/dev/null | sort -r | head -1 || true)"
fi
[ -n "$UNITY_BIN" ] || { echo "Unity 에디터를 못 찾았다. UNITY_BIN 을 지정해라." >&2; exit 2; }

mkdir -p "$PROJ/Assets/DeltaContract" "$PROJ/Assets/Editor" "$PROJ/Cases"
ln -sf "$REPO/contract/unity/LassoVolume.cs"      "$PROJ/Assets/DeltaContract/LassoVolume.cs"
ln -sf "$REPO/unity/Runtime/SlatLassoPicker.cs"   "$PROJ/Assets/DeltaContract/SlatLassoPicker.cs"
ln -sf "$REPO/unity/Editor/LassoProbeWindow.cs"   "$PROJ/Assets/Editor/LassoProbeWindow.cs"
ln -sf "$REPO/unity/Editor/LassoBatchCheck.cs"    "$PROJ/Assets/Editor/LassoBatchCheck.cs"

echo "프로젝트 $PROJ"
echo "에디터   $UNITY_BIN"
echo "케이스   $CASES"

LASSO_CASE_DIR="$CASES" "$UNITY_BIN" \
  -batchmode -nographics -quit -disable-assembly-updater \
  -projectPath "$PROJ" \
  -executeMethod DeltaContract.EditorTools.LassoBatchCheck.Run \
  -logFile - 2>&1 | grep -E "LassoBatchCheck|error CS|Compilation failed|Aborting" || true

exit "${PIPESTATUS[0]}"
