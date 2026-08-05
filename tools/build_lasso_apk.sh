#!/usr/bin/env bash
# 라쏘 실기 앱 빌드 → **APK 안 검증** → 설치 → 실행 → logcat (W22 ①②).
#
# 🔴 §7-A: "빌드 완료" 는 증거가 아니다. 씬에 구워진 값이 APK 안에 들어갔는지는
#    **APK 를 열어서** 본다. 진짜 판정은 **앱이 실제로 낸 로그**다.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJ="${V3_APP_PROJECT:-${TMPDIR:-/tmp}/v3-lasso-app}"
V2="${V2_UNITY:-$HOME/ai-ar-v2/blockedit-mvp/unity}"
CASE="${V3_CASE_FILE:-moto-rear-wheel.case}"
APK="$PROJ/Builds/LassoProbe.apk"

UNITY_BIN="${UNITY_BIN:-$(ls -d /Applications/Unity/Hub/Editor/*/Unity.app/Contents/MacOS/Unity 2>/dev/null | sort -r | head -1)}"
ADB="${ADB:-$(dirname "$UNITY_BIN")/../../../PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb}"
[ -x "$ADB" ] || ADB="$(command -v adb || true)"

# ── ① 프로젝트 골격: v2 에서 **패키지·XR 설정만** 가져온다 (AR Foundation 6.5 · 같은 에디터 판본)
if [ ! -d "$PROJ/Assets" ]; then
  echo "프로젝트 생성 $PROJ"
  mkdir -p "$PROJ/Assets/Scenes" "$PROJ/Assets/DeltaContract" "$PROJ/Assets/Editor" "$PROJ/Assets/StreamingAssets"
  cp -R "$V2/Packages" "$PROJ/Packages"
  cp -R "$V2/ProjectSettings" "$PROJ/ProjectSettings"
fi

# 리포 파일은 **심링크**. 복사하면 드리프트한다.
ln -sf "$REPO/contract/unity/LassoVolume.cs"      "$PROJ/Assets/DeltaContract/LassoVolume.cs"
ln -sf "$REPO/contract/unity/ChunkBin.cs"         "$PROJ/Assets/DeltaContract/ChunkBin.cs"
ln -sf "$REPO/unity/Runtime/SlatLassoPicker.cs"   "$PROJ/Assets/DeltaContract/SlatLassoPicker.cs"
ln -sf "$REPO/unity/Runtime/LassoCase.cs"         "$PROJ/Assets/DeltaContract/LassoCase.cs"
ln -sf "$REPO/unity/Runtime/TouchLassoController.cs" "$PROJ/Assets/DeltaContract/TouchLassoController.cs"
ln -sf "$REPO/unity/Runtime/ChunkSceneApplier.cs" "$PROJ/Assets/DeltaContract/ChunkSceneApplier.cs"
ln -sf "$REPO/unity/Editor/V3AppBuild.cs"         "$PROJ/Assets/Editor/V3AppBuild.cs"
ln -sf "$REPO/unity/Runtime/VoxelUnlit.shader"    "$PROJ/Assets/DeltaContract/VoxelUnlit.shader"

# 🔴 자산 기하 — 이게 없으면 화면에 **아무것도 안 뜬다** (좌표만으로는 안 보인다)
CHUNKS="${V3_CHUNK_DIR:-$REPO/../.inplace}/parent"
if [ -d "$CHUNKS" ]; then
  mkdir -p "$PROJ/Assets/StreamingAssets/chunks"
  cp "$CHUNKS"/*.cbin "$PROJ/Assets/StreamingAssets/chunks/" 2>/dev/null || true
  (cd "$CHUNKS" && ls *.cbin | sed 's/\.cbin$//') > "$PROJ/Assets/StreamingAssets/chunks.txt"
  echo "청크 $(wc -l < "$PROJ/Assets/StreamingAssets/chunks.txt" | tr -d ' ')개 → StreamingAssets"
else
  echo "⚠️ 청크 디렉터리가 없다: $CHUNKS — 화면에 기하가 안 뜬다"
fi

CASE_SRC="${V3_CASE_DIR:-${TMPDIR:-/tmp}/lasso-unity/Cases}/$CASE"
[ -f "$CASE_SRC" ] || { echo "케이스가 없다: $CASE_SRC" >&2; exit 2; }
cp "$CASE_SRC" "$PROJ/Assets/StreamingAssets/$CASE"
echo "케이스 $CASE ($(wc -l < "$CASE_SRC") 줄) → StreamingAssets"

# ── ② 빌드 (V3_SKIP_BUILD=1 이면 기존 APK 를 그대로 쓴다)
if [ "${V3_SKIP_BUILD:-0}" != "1" ]; then
V3_APK_PATH="Builds/LassoProbe.apk" V3_CASE_FILE="$CASE" "$UNITY_BIN" \
  -batchmode -quit -nographics -disable-assembly-updater \
  -projectPath "$PROJ" -buildTarget Android \
  -executeMethod DeltaContract.EditorTools.V3AppBuild.Build \
  -logFile - 2>&1 | grep -E "V3AppBuild|error CS|BuildFailed|Aborting|Exception" || true
BUILD_RC="${PIPESTATUS[0]}"
fi
[ -f "$APK" ] || { echo "❌ APK 가 없다 (빌드 실패)" >&2; exit 1; }

# ── ③ 🔴 APK **안**을 연다. 씬 YAML 이 맞다고 APK 도 맞은 게 아니다 (§7-A)
echo "── APK 검증 $(ls -lh "$APK" | awk '{print $5}')"
# ⚠️ Unity 6(Android)는 StreamingAssets 를 `assets/<파일>` 에 넣는다.
#    `assets/bin/Data/StreamingAssets/…` 를 기대하면 **APK 는 멀쩡한데 검사가 실패**한다 —
#    W22 에서 실제로 그랬다. 검사 도구의 거짓 경보는 검사 부재만큼 나쁘다 (방법론 6조).
# ⚠️ `set -o pipefail` + `head` 는 unzip 에 SIGPIPE 를 보내 **멀쩡한 추출을 실패로 만든다.**
#    W22 에서 이 조합이 거짓 경보를 냈다. 파이프로 자르지 말고 파일로 뽑아서 본다.
INNER=""; TMPC="$(mktemp)"
for cand in "assets/$CASE" "assets/bin/Data/StreamingAssets/$CASE"; do
  if unzip -p "$APK" "$cand" > "$TMPC" 2>/dev/null && [ -s "$TMPC" ]; then INNER="$cand"; break; fi
done
if [ -n "$INNER" ]; then
  echo "✅ APK 안에 케이스가 있다: $INNER · $(sed -n 1p "$TMPC")"
  echo "   APK 안의 기대 지문: $(grep 'EXPECT mask_fingerprint' "$TMPC" | awk '{print $3}')"
  echo "   원본과 바이트 동일: $(cmp -s "$TMPC" "$CASE_SRC" && echo YES || echo NO)"
else
  echo "❌ APK 안에서 케이스를 못 찾았다 — 씬은 맞아도 배포물이 틀렸다는 뜻이다 (§7-A)"
  unzip -l "$APK" | grep -iE "streamingassets|\.case" || true; exit 1
fi
rm -f "$TMPC"
"$ADB" shell true >/dev/null 2>&1 || { echo "⚠️ 기기가 없다 — 설치·실행은 건너뛴다"; exit 0; }

# ── ④ 설치 · 실행 · logcat. **앱이 실제로 낸 로그**가 진짜 판정이다
"$ADB" install -r "$APK" >/dev/null 2>&1 && echo "설치 ok"
"$ADB" logcat -c
"$ADB" shell monkey -p com.aiarv3.lassoprobe -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 12
echo "── logcat"
"$ADB" logcat -d | grep -E "LassoW22|Unity.*Exception|FATAL" | tail -30
