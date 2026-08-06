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
# 🔴 XR 설정(ARCore 로더)이 없으면 AR 세션이 아예 안 뜬다 — 그런데 예외는 안 나고
#    "평면을 못 찾는다" 로만 보인다. v2 가 이미 구성해 둔 것을 가져온다.
# ⚠️ 디렉터리 유무로 판단하면 안 된다 — Unity 가 XR 패키지 때문에 **껍데기만** 만들어 두고,
#    그러면 cp 가 건너뛰어 **ARCore 로더가 빠진 채** 빌드된다. 파일 단위로 채운다.
mkdir -p "$PROJ/Assets/XR"
for item in Loaders Settings XRGeneralSettingsPerBuildTarget.asset XRGeneralSettingsPerBuildTarget.asset.meta; do
  [ -e "$PROJ/Assets/XR/$item" ] || cp -R "$V2/Assets/XR/$item" "$PROJ/Assets/XR/" 2>/dev/null
done
if [ -f "$PROJ/Assets/XR/Loaders/ARCoreLoader.asset" ]; then
  echo "XR ok — ARCore 로더 있음"
else
  echo "❌ ARCore 로더가 없다 — AR 세션이 안 뜨고 '평면을 못 찾는다' 로만 보인다" >&2; exit 2
fi

# 리포 파일은 **심링크**. 복사하면 드리프트한다.
ln -sf "$REPO/contract/unity/LassoVolume.cs"      "$PROJ/Assets/DeltaContract/LassoVolume.cs"
ln -sf "$REPO/contract/unity/ChunkBin.cs"         "$PROJ/Assets/DeltaContract/ChunkBin.cs"
# 🔴 계약 상수(DeltaConstants)는 **복제하지 않는다.** 미러 파일을 그대로 링크한다 —
#    v4 에서 CHUNK_SIZE 가 8 → 4 로 바뀌었고, 앱이 제 값을 들고 있으면 조용히 어긋난다.
#    (Newtonsoft 는 프로젝트 매니페스트에 이미 있다)
ln -sf "$REPO/contract/unity/ChunkContracts.cs"   "$PROJ/Assets/DeltaContract/ChunkContracts.cs"
ln -sf "$REPO/unity/Runtime/SlatLassoPicker.cs"   "$PROJ/Assets/DeltaContract/SlatLassoPicker.cs"
ln -sf "$REPO/unity/Runtime/VoxelFrame.cs"        "$PROJ/Assets/DeltaContract/VoxelFrame.cs"
ln -sf "$REPO/unity/Runtime/AssetScale.cs"       "$PROJ/Assets/DeltaContract/AssetScale.cs"
ln -sf "$REPO/unity/Runtime/ArPlacement.cs"      "$PROJ/Assets/DeltaContract/ArPlacement.cs"
ln -sf "$REPO/unity/Runtime/PlaneOutline.cs"     "$PROJ/Assets/DeltaContract/PlaneOutline.cs"
ln -sf "$REPO/unity/Runtime/LassoCase.cs"         "$PROJ/Assets/DeltaContract/LassoCase.cs"
ln -sf "$REPO/unity/Runtime/LassoEditApp.cs"       "$PROJ/Assets/DeltaContract/LassoEditApp.cs"
# ChunkSceneApplier·TouchLassoController 는 **앱에서 걷어냈다** (W23).
#   in-place 계측 하네스는 unity_inplace_check.sh 쪽에 그대로 남아 있다.
ln -sf "$REPO/unity/Editor/V3AppBuild.cs"         "$PROJ/Assets/Editor/V3AppBuild.cs"
ln -sf "$REPO/unity/Runtime/ChunkSurface.shader"  "$PROJ/Assets/DeltaContract/ChunkSurface.shader"
ln -sf "$REPO/unity/Runtime/PlaneLine.shader"     "$PROJ/Assets/DeltaContract/PlaneLine.shader"

# 🔴 자산 기하 — 이게 없으면 화면에 **아무것도 안 뜬다** (좌표만으로는 안 보인다)
# 🔴 W25 — 기본값이 **리포 밖**(`$REPO/../.inplace`)을 가리키고 있었다. 그 결과
#    3090 이 실물 자산을 리포에 올려도 빌드는 계속 **내 합성본**을 집어갔고,
#    화면은 큐브인 채였다. "자산을 고쳤는데 화면이 그대로" 의 원인이 이것이다.
#    §7-A 계열이다 — 소스가 맞아도 **배포물이 다른 것을 담는다.**
#
#    ⇒ 기본값은 **리포 안 실물 자산**이다. 리포 밖을 보려면 명시적으로 지정해야 한다.
#    (변수명도 정리했다: V3_CHUNK_DIR 은 v3 시절 이름이라 이제 v4 자산과 어긋난다.
#     ASSET_DIR 을 쓰고, 옛 이름은 당분간 받아 준다.)
ASSET_DIR="${ASSET_DIR:-${V3_CHUNK_DIR:-$REPO/assets/${ASSET_ID:-moto-b}}}"
CHUNKS="$ASSET_DIR/parent"
if [ -d "$CHUNKS" ]; then
  mkdir -p "$PROJ/Assets/StreamingAssets/chunks"
  rm -f "$PROJ/Assets/StreamingAssets/chunks/"*.cbin
  cp "$CHUNKS"/*.cbin "$PROJ/Assets/StreamingAssets/chunks/"
  (cd "$CHUNKS" && ls *.cbin | sed 's/\.cbin$//') > "$PROJ/Assets/StreamingAssets/chunks.txt"
  N=$(wc -l < "$PROJ/Assets/StreamingAssets/chunks.txt" | tr -d ' ')
  echo "자산 $ASSET_DIR · 청크 ${N}개 → StreamingAssets"
  # 실물인지 **숫자로** 남긴다 — 합성본은 청크당 면이 복셀×12 라 훨씬 적다.
  [ -f "$ASSET_DIR/SHA256SUMS" ] && echo "   SHA256SUMS 있음 ($(wc -l < "$ASSET_DIR/SHA256SUMS" | tr -d ' ')줄)"
else
  echo "❌ 청크 디렉터리가 없다: $CHUNKS — 화면에 기하가 안 뜬다" >&2; exit 2
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
# 🔴 APK **안의 청크**가 리포의 실물과 같은 바이트인가. 경로만 맞고 내용이 옛것이면
#    화면은 여전히 큐브인데 "빌드 성공" 이라 아무도 못 잡는다 (§7-A).
if [ -f "$ASSET_DIR/SHA256SUMS" ]; then
  SAMPLE="$(head -1 "$PROJ/Assets/StreamingAssets/chunks.txt")"
  TMPB="$(mktemp)"
  if unzip -p "$APK" "assets/chunks/$SAMPLE.cbin" > "$TMPB" 2>/dev/null && [ -s "$TMPB" ]; then
    GOT="$(shasum -a 256 "$TMPB" | awk '{print $1}')"
    WANT="$(grep -E "(^|[ /])$SAMPLE\.cbin$" "$ASSET_DIR/SHA256SUMS" | awk '{print $1}' | head -1)"
    if [ -n "$WANT" ] && [ "$GOT" = "$WANT" ]; then
      echo "✅ APK 안 청크 $SAMPLE.cbin 이 리포 실물과 **바이트 동일** ($GOT)"
    else
      echo "❌ APK 안 청크가 SHA256SUMS 와 다르다: got=$GOT want=${WANT:-없음}" >&2; exit 1
    fi
  else
    echo "❌ APK 에서 청크를 못 꺼냈다: assets/chunks/$SAMPLE.cbin" >&2; exit 1
  fi
  rm -f "$TMPB"
fi

"$ADB" shell true >/dev/null 2>&1 || { echo "⚠️ 기기가 없다 — 설치·실행은 건너뛴다"; exit 0; }

# ── ④ 설치 · 실행 · logcat. **앱이 실제로 낸 로그**가 진짜 판정이다
"$ADB" install -r "$APK" >/dev/null 2>&1 && echo "설치 ok"
"$ADB" logcat -c
"$ADB" shell monkey -p com.aiarv3.lassoprobe -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 12
echo "── logcat"
"$ADB" logcat -d | grep -E "LassoW22|Unity.*Exception|FATAL" | tail -30
