# 인프라 정본 — 새 프로젝트 시작용

조사 2026-08-03. 두 리포(`ai-ar-v2`, `ai-ar-prototype`)의 **문서·코드 기재**를 전수 조사한 결과다.

> ## 🔴 이 문서는 레닥션되어 있다 (public 리포 · `docs/PROGRESS.md` §7)
>
> 원본에는 공인 IP · ngrok URL · 홈 경로 · 포트 조합이 그대로 있었다. 전부 아래
> 자리표시자로 치환했다. **실제 값을 이 파일에 되돌려 쓰지 마라** — 각자 `.env`
> 에 두고 환경변수로 읽는다 (`.env.example` 참고).
>
> | 자리표시자 | 뜻 | 대응 환경변수 |
> |---|---|---|
> | `<GEN_HOST>` | 3090 / 워크스테이션 | `GEN_HOST` |
> | `<EDIT_HOST>` | A5000 | `EDIT_HOST` |
> | `<MACBOOK_LAN>` | 맥북 LAN 주소 | — |
> | `<SITE_SUBNET>` | 두 서버가 함께 있는 서브넷 | — |
> | `<HOME>` | 사용자 홈 디렉터리 | — |
> | `<USER>` | 서버 계정명 | — |
> | `<ASSETGEN_PORT>` | TRELLIS 1 자산 생성 | `ASSETGEN_URL` 에 포함 |
> | `<DEBUGVIEW_PORT>` | 오케스트레이터 · DebugView | `DEBUGVIEW_PORT` |
> | `<EDIT_V2_PORT>` | TRELLIS.2 (레거시) | — |
> | `<LEGACY_PORT>` | Block 서버 (레거시) | — |
> | `<VIEWER_PORT>` | 로컬 뷰어 | — |
> | `<NGROK_API_PORT>` | ngrok 로컬 검사 API | — |
> | `<DEBUGVIEW_NGROK_HOST>` | DebugView 터널 호스트 | — |
> | `<LEGACY_NGROK_HOST>` | 레거시 터널 호스트 | — |
>
> ⚠️ **rev5 기준 일부 내용은 이미 낡았다.** `docs/PROGRESS.md` §2 D4·D6 가
> 그 자리의 정본이다 — 특히 `:<DEBUGVIEW_PORT>` 는 회수됐고(D4), 3090 은
> TRELLIS 가중치를 받지 않는다(D6). 충돌하면 PROGRESS 가 이긴다.

🔴 **라이브 상태는 확인 못 했다.** ngrok 두 URL 이 빈 응답을 줬는데, 무료 플랜
인터스티셜(헤더 필요) 때문인지 터널이 내려간 것인지 여기서는 못 가른다.
**§8 의 확인 명령을 먼저 돌려라.**

---

## 1. 기기

| 역할 | 하드웨어 | 주소 | 비고 |
|---|---|---|---|
| **3090 / 워크스테이션** | RTX 3090 **24 GB** · 시스템 RAM **124 GB** | `<GEN_HOST>` | Linux. 사용자 `<USER>`, 홈 `<HOME>` |
| **A5000** | RTX A5000 **24 GB** | `<EDIT_HOST>` | **sshd 없음** (SSH:22 refused 는 정상) |
| **맥북** | — | `<MACBOOK_LAN>` (LAN) | Unity 빌드 호스트 |
| **폰 (현행)** | Galaxy A37 5G · SM-A376N · Android 16 · Exynos 1480 | — | ARCore 인증됨 |
| **폰 (구)** | Galaxy S22 · SM-S901N · Android 14 · 1080×2340 · Adreno 730 | — | `ai-ar-v2` 검증에 쓰인 기기 |

```
두 서버는 같은 서브넷 <SITE_SUBNET> = 같은 L2, 라우팅 홉 없음 (직결 LAN, 1홉)
A5000 방화벽: <GEN_HOST> 발 트래픽에 한해 <EDIT_V2_PORT>/<ASSETGEN_PORT> 인바운드 개방
```

### 🔴 인바운드는 **연속된 네 자리뿐이고 전부 차 있다**

```
<EDIT_V2_PORT>     A5000   TRELLIS.2 HTTP API              (ai-ar-v2)
<ASSETGEN_PORT>    A5000   /lab + /v2/trellis (TRELLIS 1)  (ai-ar-prototype)
                                            ← 같은 호스트, <EDIT_V2_PORT> 와 공존
<DEBUGVIEW_PORT>   3090    /v2 + debugview                 (ai-ar-prototype)
<LEGACY_PORT>      3090    Block 서버 + ngrok 레거시 터널   (ai-ar-v2, 레거시)
```

**`:<VIEWER_PORT>` 은 공인 IP 로 도달 불가** — refused 가 아니라 **timeout** 이다.
새 서비스를 띄우려면 위 넷 중 하나를 회수해야 한다.

⚠️ `<ASSETGEN_PORT>` 는 **호스트가 다르면 다른 것**이다:
`ai-ar-v2` 문서의 "TRELLIS 1 :<ASSETGEN_PORT>" 는 **3090 loopback**,
`ai-ar-prototype` 의 `:<ASSETGEN_PORT>` 는 **A5000** 이다. 혼동 주의.

---

## 2. URL / 터널

| URL | 대상 | 프로젝트 |
|---|---|---|
| `https://<DEBUGVIEW_NGROK_HOST>` | → 3090 `:<DEBUGVIEW_PORT>` | ai-ar-prototype |
| `https://<LEGACY_NGROK_HOST>` | → 3090 `:<LEGACY_PORT>` | ai-ar-v2 (씬에 구워져 있음) |

```
브랜디드 도메인(ai-ar-prototype)  벽시계 과금 $0.02/h — 세션 끝나면 내려라
                                  전송 5 GB/월 ≈ 생성 750회
무료 터널(duckbill)                동시 터널 1개 제한 (ERR_NGROK_334)
                                  **재시작하면 URL 이 바뀌고 → APK 재빌드 필요**
무료 플랜 인터스티셜               모든 요청에 헤더 `ngrok-skip-browser-warning: 1`
ngrok 로컬 API                     http://localhost:<NGROK_API_PORT>/api/tunnels
```

---

## 3. 서비스 · API

### 3-1. A5000 `:<EDIT_V2_PORT>` — TRELLIS.2 (ai-ar-v2)

```
POST /generate   multipart: file=<RGBA png>, resolution=<int>
                 → 200 {"job_id":"job_<hex>","status":"queued","resolution":int}
POST /edit       multipart: file=<원본 GLB>, condition_image=<png>,
                            mask=<JSON {space,resolution,occupancy}>, resolution, steps,
                            growth_margin(기본 0)
                 → 200 {"job_id","status":"queued","type":"edit","resolution"}
GET  /jobs/{id}          → {"status":"queued"|"running"|"done"|"failed"}
GET  /jobs/{id}/result   → GLB 바이트 (magic b"glTF" 확인 필수)
GET  /health             → {"status":"ok","model_loaded":bool}
폴링 2.0s
```

### 3-2. A5000 `:<ASSETGEN_PORT>` — TRELLIS 1 (ai-ar-prototype)

```
/lab/*              무인증 (개발 도구). **쓰기 엔드포인트 추가 금지**
/v2/trellis/edit    인증
/v2/trellis/assemble  인증 (조립, 3.23.0 신설)
/v2/trellis/health  build · build_dirty · build_untracked · started_at ·
                    gpu_mem_gb · trellis_variant · slat_resolution
산출물  /lab/runs/edit_00NN/files/
```

### 3-3. 3090 `:<ASSETGEN_PORT>` — TRELLIS 1 loopback (ai-ar-v2)

```
POST /generate      {"prompt":str, "style"?, "simplify"?, "texture_size"?,
                     "ss_sampling_steps"?, "seed"?}  → 202 {"job_id":"<hex12>"}
GET  /jobs/{id}     → ready 시 {"asset_url":"http://127.0.0.1:<ASSETGEN_PORT>/files/<id>.glb","bbox":[w,h,d]}
GET  /files/{n}.glb → GLB
GET  /health        → {"status":"ok","pipeline":...,"model":...,"ready":bool}
폴링 1.0s · 모델 text-xlarge · 자산 해상도 64³ 고정
뷰어  ~/trellis_demo.sh → http://localhost:<VIEWER_PORT>/viewer.html   ⚠️ 충돌 가능
```

### 3-4. 3090 `:<DEBUGVIEW_PORT>` — ai-ar-prototype 오케스트레이터

```
POST /v2/assets                    생성
POST /v2/assets/{id}/edits         편집
POST /v2/assets/{id}/assemble      조립  (계약 3.24.0 신설)
GET  /v2/jobs/{job_id}             폴링   ⚠️ 잡이 **인메모리** — 재기동하면 전부 소실
GET  /v2/assets/{id}/manifest
GET  /v2/assets/{id}/chunks/{key}.v{n}.cbin
GET  /v2/assets/{id}/staging/{job_id}/chunks/{key}.cbin
GET  /v2/health                    build · build_dirty · build_untracked · started_at ·
                                   upstream_ok · upstream · jobs
/edit/{job_id}                     debugview (무인증)
```

### 3-5. 3090 `:<LEGACY_PORT>` — Block 서버 (ai-ar-v2, FastAPI `app.py`)

```
POST /blocks/generate              {"prompt":str} → {"job_id"}
POST /blocks/edit                  {object_id, block_ids|mask, instruction} → {"job_id"}
GET  /blocks/jobs/{job_id}
GET  /blocks/objects/{id}.glb
GET  /blocks/objects/{id}/occupancy
GET  /health
GET  /debug/jobs                   ← **인증 면제**
GET  /debug/file/{job_id}/{filename}
```

---

## 4. 기기별 라이브러리 · 모델

### 3090

```
conda 환경 두 개 — ⚠️ 한 프로세스에서 같이 임포트하면 안 된다
  zimage    <HOME>/anaconda3/envs/zimage/bin/python    torch 2.6.0+cu124 · numpy 2.x
  birefnet  <HOME>/anaconda3/envs/birefnet/bin/python  torch 2.6.0 · numpy 1.26.4

모델
  Tongyi-MAI/Z-Image-Turbo   t2i. bfloat16 · 1024×1024 · steps 9 · guidance 0.0 · seed 42
  ZhengPeng7/BiRefNet        배경 제거 (0.2B, MIT). RMBG-2.0 이 게이트 403 이라 대체
  TRELLIS 1 (text-xlarge)    :<ASSETGEN_PORT> loopback

산출물  <HOME>/trellis_out/  ·  t2i 는 <HOME>/trellis_out/t2i
        <T2I_OUT_DIR>/objects/<object_id>.glb
디버그  00_prompt.txt · 01_t2i_raw.png · 02_rgba.png · 03_final.glb · 04_meta.json
```

### A5000

```
TRELLIS.2-4B          microsoft/TRELLIS.2-4B  (단일 체크포인트, 512³/1024³/1536³)
  스테이지 설정        slat_flow_img2shape_dit_1_3B_512_bf16
                      slat_flow_imgshape2tex_dit_1_3B_512_bf16
TRELLIS 1             microsoft/TRELLIS-image-large — VoxHammer 가 벤더링한 전체 소스 +
                      가중치 3.1 GB, vendor/trellis1/ 로 복사 완료
DINOv3                facebook/dinov3-vitl16-pretrain-lvd1689m  (gated:manual, 승인 완료)
                      ⚠️ transformers 5.14.1 의 DINOv3ViTModel 은 몽키패치 필요
CUDA                  12.4 권장 · Python 3.8+
```

**실측 VRAM (24 GB 대비)**

```
생성 512³               2,976 MiB (12.1%)   7.9s
생성 1024³              5,348 MiB (21.8%)   28.3s
DINOv3 조건 512³        3,898 MiB (15.9%)   32.9s
DINOv3 1024_cascade    16,054 MiB (65.4%)   119s
VoxHammer 편집 피크     14,180 MiB (57.7%)  ~201s
─ ai-ar-prototype 실측 ─
기동 직후               6,012 MiB
편집 1회 후            13,928 MiB
조립 1회 후            13,864 MiB          ← 편집보다 적다
```

⚠️ **엔드포인트 안에서는 상주 디코더를 쓴다.** 랩 스크립트가 사본을 하나 더 올려
OOM 이 났던 것이지 조립이 무거운 게 아니다.

### 맥북

```
Unity 6000.5.3f1  (/Applications/Unity/Hub/Editor/6000.5.3f1/Unity.app/Contents/MacOS/Unity)
adb               .../PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb
```

### 서버 파이썬 (ai-ar-v2 `server/.venv`, **Python 3.14**)

```
fastapi 0.139.2 · uvicorn 0.51.0 · pydantic 2.13.4 · starlette 1.3.1
httpx 0.28.1 · trimesh 4.12.2 · numpy 2.5.1 · pytest 9.1.1

🔴 requirements.txt 에 선언됐는데 **설치 안 된 것 둘**
   Pillow>=10.0      없으면 블록 색이 전부 회색으로 폴백
   anthropic>=0.69   없으면 키가 있어도 llm.py:128 에서 ImportError
```

### Unity 패키지 (ai-ar-v2)

```
com.unity.xr.arfoundation      6.5.0
com.unity.xr.arcore            6.5.0
com.unity.cloud.gltfast        6.9.0
com.unity.nuget.newtonsoft-json 3.2.2
com.unity.ugui                 2.5.0
com.unity.test-framework       1.7.0
⚠️ com.unity.inputsystem 은 manifest 에 없다 — AR Foundation 을 통해 전이 의존으로 들어온다

빌드   minSdk 26 · ARM64 · IL2CPP · OpenGLES3(자동 off) · Portrait
       insecureHttpOption = AlwaysAllowed
       ARCore Requirement = Required
       패키지 com.blockedit.mvp
평문 HTTP 는 **두 관문 다** 필요: AndroidCleartextPostProcessor + insecureHttpOption
```

⚠️ `ai-ar-prototype` 의 Unity 는 **OpenGLES3 가 아니라 Vulkan**, minSdk 26 동일.
정본 파일은 `unity/BlockEdit/Assets/Scripts/DeltaContract/ChunkContracts.cs`.

---

## 5. 환경변수 (ai-ar-v2 `server/config.py`)

| 변수 | 코드 기본값 | 역할 |
|---|---|---|
| `MOCK_MODE` | `True` | TRELLIS/LLM 우회, 스키마 동일한 합성 응답 |
| `GRID_SIZE` | **`32`** | 복셀 격자 한 변 |
| `BLOCK_SIZE_M` | **`0.046875`** | 블록 월드 크기 → footprint **1.5 m** |
| `ASSETGEN_URL` | `http://127.0.0.1:<ASSETGEN_PORT>` | TRELLIS 1 |
| `ASSETGEN2_URL` | `""` | A5000 TRELLIS.2. **비면 t2i 가 RGBA 에서 멈춘다** |
| `ASSETGEN2_RESOLUTION` | `512` | 512³/1024³ |
| `GEN_PIPELINE` | **`t2i`** | `t2i` \| `trellis1` |
| `EDIT_STEPS` | `12` | A5000 `/edit` denoising |
| `ANTHROPIC_API_KEY` | `""` | 비면 규칙 폴백 |
| `LLM_MODEL` | `claude-sonnet-5` | |
| `TRELLIS_TIMEOUT_S` | `300.0` | |
| `BLOCKEDIT_SHARED_KEY` | `""` | 비면 인증 off |
| `ZIMAGE_PYTHON` | `<HOME>/anaconda3/envs/zimage/bin/python` | |
| `BIREFNET_PYTHON` | `<HOME>/anaconda3/envs/birefnet/bin/python` | |
| `T2I_OUT_DIR` | `<HOME>/trellis_out/t2i` | |
| `T2I_PROMPT_TEMPLATE` | `{prompt}, full body, 3/4 view, product photography, studio lighting, plain background, centered` | 납작한 GLB 방지 |
| `RUN_REAL_TRELLIS` | unset | `1` → 실 GPU 테스트 |

`.env` 로더는 `server/.env` 를 읽지만 **그 파일은 현재 없다**(`.env.example` 만 있음).
실제 환경변수가 항상 우선한다.

**Unity 측**: `BLOCKEDIT_SERVER_URL`(빌드 타임에 씬에 **구워진다**) · `BLOCKEDIT_APK_PATH` ·
`BLOCKEDIT_SHARED_KEY` · `BLOCKEDIT_CAPTURE_DIR` · `BLOCKEDIT_SKIP_BUILD` · `BLOCKEDIT_LOCAL_ENV`

---

## 6. 인증

```
헤더      X-Blockedit-Key
환경변수   BLOCKEDIT_SHARED_KEY   (비면 인증 off)
비교      secrets.compare_digest
면제      /debug/*  는 면제.  /health 는 **면제 아님**
클라 조회 순서  StreamingAssets/blockedit_local_key.txt → env → ~/.blockedit_local.env
                → PlayerPrefs → EditorPrefs
지문      sha256 앞 4바이트.  shasum -a 256 | cut -c1-8
```

### 🔴 비밀 감사 — 리포를 원격에 올리기 전에 반드시

```
<HOME>/ai-ar-v2/blockedit-mvp/unity/Assets/StreamingAssets/blockedit_local_key.txt
  → 실제 64-hex 키가 들어 있다 (gitignore 됨, 하지만 APK 에는 평문으로 들어간다)
```

---

## 7. 🔴 문서끼리 값이 다른 곳 — 코드가 정본

| 항목 | 코드 | 문서 |
|---|---|---|
| `GRID_SIZE` / `BLOCK_SIZE_M` | **32 / 0.046875** (footprint 1.5 m) | CLAUDE.md·README 는 16 / 0.015 (0.24 m) — **낡음** |
| `GEN_PIPELINE` | **`t2i`** | BLOCKEDIT_V2 는 "기본 trellis1" — 낡음 |
| `MOCK_MODE` | **`True`** | CLAUDE.md 는 "기본 false" — 낡음 |
| 인증 ON/OFF | — | CLAUDE.md·BLOCKEDIT_V2 = ON / README·ISSUES = OFF — **미해결** |
| `activeInputHandler` | **2 (Both)** | README 일부가 "0" — 낡음 |
| 키 생성 | — | `.env.example` 은 `token_urlsafe(32)`, CLAUDE.md 는 `openssl rand -hex 32`. **런북 추출기는 hex 만 매칭** |
| ngrok TLD | `.ngrok-free.dev` | README 예시가 `.ngrok-free.app` — 오타 |

**없는 문서**: `P0_A_REPORT.md` 가 4회 인용되는데 리포에 없다.

---

## 8. 라이브 상태 확인 — 새 대화 시작하면 이것부터

```bash
# 1) 무엇이 떠 있나
curl -s http://<GEN_HOST>:<DEBUGVIEW_PORT>/v2/health | python3 -m json.tool
curl -s http://<EDIT_HOST>:<ASSETGEN_PORT>/v2/trellis/health | python3 -m json.tool
curl -s http://<GEN_HOST>:<LEGACY_PORT>/health
curl -s http://<EDIT_HOST>:<EDIT_V2_PORT>/health

# 2) ngrok 살아 있나 (인터스티셜 헤더 필수)
curl -s https://<DEBUGVIEW_NGROK_HOST>/v2/health -H 'ngrok-skip-browser-warning: 1'
curl -s https://<LEGACY_NGROK_HOST>/health -H 'ngrok-skip-browser-warning: 1'
curl -s localhost:<NGROK_API_PORT>/api/tunnels | python3 -c 'import sys,json;print(json.load(sys.stdin)["tunnels"][0]["public_url"])'

# 3) 인증 켜져 있나 — 401 이면 ON
curl -s -o /dev/null -w '%{http_code}\n' http://<GEN_HOST>:<LEGACY_PORT>/health

# 4) GPU 점유
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv
```

**`build` / `build_dirty` / `started_at` 을 커밋 시각과 대조해라** —
"테스트가 통과했다" 와 "그 코드가 떠 있다" 는 다른 사실이다.

---

## 9. 운영 규칙 (실제로 사고가 났던 것들)

```
setsid --fork 로 띄우고 PPID=1 확인       세션이 끝나면 자식 프로세스가 같이 죽는다
                                          (A5000 :<ASSETGEN_PORT> 가 실제로 이렇게 내려갔다)
비밀은 프로세스 기동 **전에** 리포 밖 600 파일에
키 교체와 서버 기동을 같은 명령에 묶지 마라   재기동이 조용히 키를 무효화한다
로그는 > 로 덮어써라
시연 중 3090 을 재기동하지 마라              잡이 인메모리라 통째로 사라진다
ngrok 브랜디드 도메인은 세션 끝나면 내려라    벽시계 과금
```
