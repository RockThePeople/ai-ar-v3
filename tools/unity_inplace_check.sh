#!/usr/bin/env bash
# `.cbin` 델타를 Unity 씬에 **in-place** 로 반영하고 잰다 (W21 · D70).
#
# 게이트는 절감률이 아니다. EntityId 유지율 · 재생성 수 · apply 시간 ·
# changed/added/removed(셋으로) · **실제 교체 수**(음성 대조)다.
#
#   tools/unity_inplace_check.sh [자료디렉터리]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJ="${LASSO_UNITY_PROJECT:-${TMPDIR:-/tmp}/lasso-unity}"
DATA="${1:-${TMPDIR:-/tmp}/moto-inplace}"

UNITY_BIN="${UNITY_BIN:-}"
if [ -z "$UNITY_BIN" ]; then
  UNITY_BIN="$(ls -d /Applications/Unity/Hub/Editor/*/Unity.app/Contents/MacOS/Unity 2>/dev/null | sort -r | head -1 || true)"
fi
[ -n "$UNITY_BIN" ] || { echo "Unity 에디터를 못 찾았다. UNITY_BIN 을 지정해라." >&2; exit 2; }

[ -d "$DATA/parent" ] || { echo "자료가 없다: $DATA (tools/build_moto_patch.py 로 만든다)" >&2; exit 2; }

mkdir -p "$PROJ/Assets/DeltaContract" "$PROJ/Assets/Editor"
ln -sf "$REPO/contract/unity/LassoVolume.cs"       "$PROJ/Assets/DeltaContract/LassoVolume.cs"
ln -sf "$REPO/contract/unity/ChunkBin.cs"          "$PROJ/Assets/DeltaContract/ChunkBin.cs"
ln -sf "$REPO/unity/Runtime/SlatLassoPicker.cs"    "$PROJ/Assets/DeltaContract/SlatLassoPicker.cs"
ln -sf "$REPO/unity/Runtime/ChunkSceneApplier.cs"  "$PROJ/Assets/DeltaContract/ChunkSceneApplier.cs"
ln -sf "$REPO/unity/Editor/LassoProbeWindow.cs"    "$PROJ/Assets/Editor/LassoProbeWindow.cs"
ln -sf "$REPO/unity/Editor/LassoBatchCheck.cs"     "$PROJ/Assets/Editor/LassoBatchCheck.cs"
ln -sf "$REPO/unity/Editor/InPlaceBatchCheck.cs"   "$PROJ/Assets/Editor/InPlaceBatchCheck.cs"

echo "프로젝트 $PROJ · 자료 $DATA"
INPLACE_DIR="$DATA" "$UNITY_BIN" \
  -batchmode -nographics -quit -disable-assembly-updater \
  -projectPath "$PROJ" \
  -executeMethod DeltaContract.EditorTools.InPlaceBatchCheck.Run \
  -logFile - 2>&1 | grep -E "InPlaceCheck|error CS|Compilation failed|Aborting" || true

exit "${PIPESTATUS[0]}"
