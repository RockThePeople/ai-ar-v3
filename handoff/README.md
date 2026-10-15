# handoff — 살아 있는 사실만 남긴 정리본

> 2026-10-15 정리. 웨이브 산출물(`w1/`~`w34/`, `*-in*/`)과 웨이브별 핸드오프 README 들을
> 지우고, **지금 시스템에 유효한 것**과 **시스템을 고칠 때 결정적인 것**만 이 문서로 모았다.
> 원문은 git 이력(이 정리 커밋의 부모)에 남아 있다. 수치를 인용하기 전에 §7 무효화 목록과 대조하라.
>
> 표기: **실측**(돌려서 나온 값) · **코드확인**(파일 인용) · **추론** · **미검증**

---

## 0. 이 디렉터리에 남은 것

```
handoff/README.md                          이 문서
handoff/voxhammer/edit_pipeline.f33cd96.patch
        ★ VoxHammer 에 가한 변경 전부 (89줄, 1파일).  VoxHammer 체크아웃에는 **커밋 없이
          작업 트리 diff 로만** 있었다 — 그 체크아웃이 사라지면 이것이 유일한 사본이다 (§3)
handoff/lasso/                             라쏘 입력 slat 좌표·마스크 픽스처
        server/tests/test_lasso.py · test_routes_v2.py · tools/build_moto_patch.py ·
        tools/moto_b_cases.py 가 경로로 읽는다.  옮기면 테스트가 깨진다
handoff/w20-recolor/result.json            server/tests/test_inplace.py 가 읽는 기준값
handoff/conditions/                        형태 편집 조건 이미지 A/B 쌍 (§8 첫 항목의 입력)
        ironman-rider.png     sha256 0ee13d02…  아이언맨 머리 라이더 · 전신 · 정측면
        spiked-cone-rider.png sha256 2a037681…  뿔 달린 원뿔 머리 — **머리 기하만 다르고** 차종·구도·템플릿 동일
        *-prompt.txt          각 이미지를 만든 t2i 프롬프트
```

---

## 1. 시스템 지도 — 두 경로

```
제품 경로   Unity(라쏘·자연어) → 3090 오케스트레이터 → A5000 편집 서비스 → 청크 델타 → Unity
            계약: contract/ (cbin-delta v4 · chunk_size 4 · chunk_grid_res 16)
연구 경로   A5000 한 대에서 TRELLIS + VoxHammer 단독.  델타·청킹·Unity 는 관심 밖
            주제: 연쇄 편집에서 재인코딩을 없애는 SLat 재사용 (§4~§6)
```

자연어 → op 분기 (`dispatch.CONSUMERS` 가 정본):

```
recolor                         → 3090 로컬 (기하 불변 · GPU 불필요)
replace_region · add · remove   → <EDIT_HOST> POST /v2/trellis/edit
```

🔴 **op 를 몰래 강등하지 않는다.** 형태 편집을 색 편집으로 바꿔 치면 게이트가 "형태를
바꿨다" 고 적으면서 색만 바뀐 결과를 재게 된다. 조건 이미지를 못 만들면 조건 없이
진행하지 않는다 — 그건 원본으로 회귀하는 길이고 "편집이 약하다" 로 오독된다.

---

## 2. A5000 편집 서비스 — 3090·Unity 가 지켜야 할 것

### 2-1. 편집 요청 `POST /v2/trellis/edit`

```
BEditRequest
  asset_id         str        required
  base_version     int        required   커밋된 버전
  prompt           str        required
  mask             EditMask   required   ← 필드명은 mask (mask_cells 아님)
  seed             int = 42
  idempotency_key  str|None
```

```jsonc
"mask": {
  "mode": "voxels",
  "voxels": [[x,y,z], ...],          // slat 격자 · 0..63 정수 · 정렬·중복 무관
  "grid_source": "slat_coords",      // 🔴 필수. 서버가 채우지 않는다
  "halo_margin_voxels": 2            // 생략 시 2. 서버가 붙인다
}
```

- 마스크가 자산의 활성 복셀을 **최소 1개** 덮어야 한다. 아니면 `MaskEmpty`
- **좌표는 클라이언트가 준 것이 유일한 진실이다.** 조건 이미지 좌표로 마스크를 다시
  만들지 않는다 (조건 이미지와 자산은 비율이 다르다 — 목 35% vs 17% 로 물렸다)
- 두 형식을 받는다: `application/json` (BEditRequest) 또는
  `multipart/form-data` (`meta`=BEditRequest JSON, `image`=조건 이미지). generate 와 같은 관례
- 조건 이미지: 1024² · **바뀐 뒤의 전신 모습**(크롭 금지) · 단순 배경 · 알파 불필요
  (편집 경로가 rembg 를 무조건 돌려 덮는다)

응답·폴링:

```
202 {"job_id": "j-..."}
GET /v2/trellis/jobs/{job_id}                                → JobStatus (patch = PatchPackage)
GET /v2/assets/{asset}/staging/{job_id}/chunks/{key}.cbin    ← 가운데는 job_id(j-…). 슬롯명(gen-…) 아님
```

🔴 **경로를 손으로 만들지 마라.** `uris.staging_chunk_uri` · `uris.chunk_uri` · 매니페스트의
`chunks[key].uri` 를 그대로 쓴다. 손으로 만든 경로는 509/509 · 181/181 전부 404 를 냈다.
🔴 상류 `BChunkResponse.chunks` 항목은 `chunk_id` 를 쓴다 — 매니페스트 `ChunkEntry.uri` 와 모양이 다르다.
🔴 완료 판정은 `state` 가 아니라 **응답 모양**(`"chunks" in p and "to_version" in p`)으로 한다.
🔴 **부분 수신은 성공이 아니다.** 하나라도 못 받으면 전부 버리고, 저장 직전 계약 판본을 검증한다.

### 2-2. 청크 키 — v4 범위는 0–15

`CHUNK_GRID_RES` 8 → 16 이후 인덱스는 0–15 다. 서비스 쪽 검증 정규식이 0–7 만 받아
415청크 중 372개가 422 를 낸 적이 있다. **정규식을 손으로 쓰지 말고 `contract/…/uris.py` 를 쓴다.**

### 2-3. 커밋된 판본이 갖춰야 할 파일 — 커밋 **전에** 검사

```
{DATA_ROOT}/assets/{asset_id}/
├── committed.json          {"latest": N}       없으면 409 VERSION_CONFLICT
└── vN/
    ├── chunks/{key}.cbin                        없으면 404
    ├── manifest.json                            없으면 부분 수신을 성공으로 오인
    ├── slat.safetensors    coords+feats+메타     🔴 없으면 202 뒤 워커에서 FileNotFoundError
    └── input.png                                🔴 없으면 202 뒤 워커에서 FileNotFoundError
```

뒤 둘은 **요청은 성공했는데 결과가 없는** 모양으로 죽는다.
`slat.safetensors` 메타에 `norm_mean`·`norm_std`·`slat_space="denormalized"` 가 없으면 다음 편집이
막다른 판본이 된다. staging TTL(1시간)이 A5000 사본을 GC 하므로 커밋을 미루지 마라.

### 2-4. 외부 판본 수납 `POST /v2/trellis/assets/{asset_id}/ingest`

3090 이 만든 판본을 커밋된 base 로 세우는 **단일 경로**다 (recolor 우회 없음).

```
multipart: meta={"version":N} · manifest · slat · input · chunks(chunks.tar.gz)
→ 201 {"asset_id","version","chunks","committed_latest","verified":"sha256+byte_length"}
거부(422): 파트 누락 · slat 메타 누락 · 청크 sha256/길이 불일치 · 매니페스트↔tar 집합 불일치 ·
           contract_version/chunk_size 불일치
```

전부 통과한 뒤에만 원자적으로 교체한다. `version > latest` 면 `committed.json` 이 갱신된다.

### 2-5. idempotency_key — 계약상 Optional, 서버가 내용에서 유도

필수화(계약 3.27.0 안)는 **보류**됐다 — Unity 가 키를 안 보내서 넣으면 모든 편집이 422 가 된다
(`docs/adr/0001-idempotency-key-required-deferred.md`). 서버가 난수로 지어내면 같은
요청 두 번이 다른 슬롯을 가져가 **재시도가 GPU 재계산**이 된다.
3090 은 클라가 안 보내면 `server.editreq.derive_idempotency_key(asset_id, base_version,
raw_prompt, mask_voxels, seed)` 로 **내용에서 유도**한다. 클라가 보낸 키는 덮지 않는다.
마스크 순서·중복은 키를 안 흔든다(서비스가 정렬 불필요·중복 허용이므로).

### 2-6. 판본 동기화·수납 (3090 쪽 규칙)

- 커밋 여부는 **상류가 실제로 409 를 준 뒤에만** 판단한다. 없는 엔드포인트의 404 를
  "커밋 안 됨" 으로 읽어 멀쩡한 v1 을 막은 적이 있다
- ingest 전에 `check_v1_payload()` 로 재료를 검사한다 — 서비스 진입점과 **양쪽에서** 막는다
- `chunks.tar.gz` 는 `mtime=0` 으로 묶는다. 안 그러면 같은 내용이 매번 다른 바이트가 된다
- recolor 판본은 slat 을 만들지 않는다 → **부모 slat 을 그대로** 싣는다. 새로 만들면 바이트가 갈린다
- 상류 연결 실패는 `UPSTREAM_UNREACHABLE` 로 분류된다 (`INTERNAL` 아님). A5000 편집 서비스가
  내려가 있으면 이게 뜬다 — 연구 트랙이 FLUX 를 돌리려고 서비스를 내려 두는 일이 잦다

### 2-7. 저장소 (3090)

읽기 루트는 **목록**(`ASSET_STORE_ROOT` → 리포 `assets/` → `ASSET_READ_ROOTS`), 쓰기는
`ASSET_STORE_ROOT` 하나. 슬롯 스캔은 읽기 루트 mtime 으로 캐시한다(TTL 없음 — 만료 전 404 의
원인을 못 찾는다). 이 캐시로 병렬 8 청크 전송이 0.4배 → 1.5배가 됐다 (실측).

---

## 3. VoxHammer 변경분 — `voxhammer/edit_pipeline.py` 한 파일

기준 rev `f33cd96`. 적용: `cd $VOXHAMMER_DIR && git apply <이 리포>/handoff/voxhammer/edit_pipeline.f33cd96.patch`

| 묶음 | 줄 | 켜짐 | 무엇 |
|---|---|---|---|
| **D51** | 7 | 항상 | `torch.isin` 이 element-wise 라 보존이 7,608 → **13** 으로 무너졌다. `x*4096+y*64+z` 1D 인코딩으로 **행 멤버십** 검사 |
| **W34** SLat 재사용 | 41 | `slat_path`/`mask_cells` 인자 | `run_edit(..., slat_path=, mask_cells=)`. 생성 SLat 을 `slat_src` 로 직접 주입, 복셀 목록을 마스크로 직접 사용. `slat_space != denormalized` 면 거부 |
| **W38** cfg=0 게이트 | 8 | 항상 | stage-2 cfg=0 일 때 음 분기 생략. **출력 76/76 비트 동일** · 편집 단계 −17% · CPU RSS −12.4 GiB |
| **W39** 어텐션 마스크 | 33 | 🔴 `W39_ATTN_MASK=1` 일 때만 | self-attention 에서 편집 Query → 보존 Key 차단. 기본 꺼짐 (§8) |

래퍼(이 리포 `tools/`): `edit_slat_reuse.py`(SLat 재사용 편집 + `--emit_slat`) ·
`gen_asset.py`(생성 + SLat 포획) · `run_voxhammer_checked.sh`(원저자 경로 + 삼킨 예외·
features 재생성 검사 + 줄마다 경과초) · `inpaint_offload.py`(FLUX + cpu offload) ·
`exr_mask_postprocess.py`(OpenCV EXR 부재 우회) · `make_mask_glb.py` · `chunk_compare.py` ·
`slat_capture.py` · `debugview_standalone.py`/`start_debugview.sh`.

⚠️ **미해결**: `tools/` 스크립트 몇 개가 절대경로를 상수로 갖고 있다(`slat_capture` 임포트,
`deltacontract` 경로). 다른 기계로 옮기면 고쳐야 하고, 리포 보안 규칙(홈 경로 금지)에도
걸린다 — 커밋 전에 환경변수로 빼야 한다.

---

## 4. 돌리는 법 — 최소

```bash
export VOXHAMMER_DIR=<VoxHammer 체크아웃>
export VH_TOOLS=<이 리포>/tools
export TRACK_ROOT=<산출물 루트>/vh-track
export PY="$VOXHAMMER_DIR/.venv/bin/python"      # 🔴 이것만 쓴다. conda·시스템 파이썬엔 bpy/trimesh 없음
```
이 넷은 어디에도 export 돼 있지 않다 — 매 세션 직접 잡는다.

```
(0) 생성     $PY $VH_TOOLS/gen_asset.py --image <1024² PNG> --out_dir $TRACK_ROOT/<자산>
             → model.glb · slat.safetensors(★) · input.png
(1) 마스크   $PY $VH_TOOLS/make_mask_glb.py --model … --out …/mask.glb --center=-0.02,0.345,0.0 …
             (음수 인자는 `=` 로 붙인다)
(2) 조건렌더 cd $VOXHAMMER_DIR && $PY utils/render_rgb_and_mask.py … && $PY $VH_TOOLS/exr_mask_postprocess.py <cond>
(3) 인페인트 편집 서비스를 내린 뒤  $PY $VH_TOOLS/inpaint_offload.py --image_path … --mask_path … --prompt "…"
             → 2d_render.png · 2d_mask.png · 2d_edit.png (셋 다 필수)
(4-A) 원저자 $VH_TOOLS/run_voxhammer_checked.sh --input_model … --mask_glb … --image_dir …
(4-B) Ours   $PY $VH_TOOLS/edit_slat_reuse.py --slat … --mask_ply … --image_dir … --output … \
                 --emit_slat …/slat.safetensors --timing …
```

### 함정 — 실제로 물린 것

1. 🔴 **연쇄 편집은 매 편집에 `--emit_slat` 을 줘야 한다.** 안 주면 SLat 이 안 남아 다음 회차가 불가능하다.
   원저자 경로(4-A)는 SLat 포획 훅이 아예 없다. 포획 메타에 `norm_mean`/`norm_std` 가 없으면 저장을 거부한다.
2. `--is_text` 는 `type=bool` 이다 — `--is_text False` 도 참이다. 이미지 조건이면 **옵션을 빼라**.
3. `inference.py` 의 `main()` 은 죽은 코드다. CLI 로는 `skip_step`·`re_init`·`cfg` 를 못 바꾼다.
4. `extract_feature.py` 가 **임포트 시점에** `torch.set_grad_enabled(False)` 를 한다. SLat 재사용 경로는
   그 임포트를 안 거치므로 grad 가 켜진 채 `to_glb` 에서 죽는다 → `edit_slat_reuse.py` 가 `no_grad()` 로 감싼다.
   **전역 grad 상태를 가정하지 마라.**
5. FLUX 인페인트(최대 22.79 GiB) 전에 편집 서비스(약 6 GB 상주)를 **반드시 내린다.** `.to("cuda")` 는 원리상
   안 들어간다(가중치 34.25 GB) — `enable_model_cpu_offload()` 필수. 산출은 안 바뀐다(원저자 동봉본 대비 PSNR 45.37 dB).
   되살릴 때는 `tools/restart_edit_service.sh`.
6. STEP1(렌더)만 캐시된다(`transforms.json`+`mesh.ply`). STEP2 feature(24s)·STEP3 마스크 복셀화(38s)는 매번 돈다.
7. **편집 경로에 시드가 없다** (`edit_pipeline.py` 의 `manual_seed` 가 주석 처리). 시드를 걸어도 feats 는
   CUDA 커널 비결정성으로 흔들린다 — **바닥값 max|Δ| ≈ 4.7e-02**. 종단 `max|Δ| = 0` 을 합격 기준으로 걸지 마라.
   coords(점유)는 결정적이다.
8. 원저자 재인코딩 경로의 분산 출처는 **둘**이다: `bpy_render.py` 의 시드 없는 `np.random`(카메라 포즈 —
   `np.random.seed(0)` 으로 잠김) + CYCLES 128샘플·GPU 디노이징(`cycles.seed` 미설정 — 안 잠김).
9. TRELLIS **생성**은 비결정적이다(같은 시드에 정점 303,946 vs 304,020). A/B 는 반드시 **같은 자산 위에서**.
   SLat 셀 수는 결정적이다.
10. 편집 산출 GLB 를 다시 청킹하면 부모와 바이트 동일한 청크가 **0개**다(두 경로 모두, 전역 디코드 성질).
    보존(B)은 디코더가 아니라 **승계**(부기 밖 청크를 부모 `.cbin` 에서 바이트 복사)가 만든다.
    그래서 부기는 해시가 아니라 **점유 비교**로 정한다 — 해시로 정하면 절감이 0% 가 된다.
11. `127.0.0.1` 의 200 은 바인딩 확인일 뿐 외부 도달이 아니다. `/healthz` 200 도 라우트 존재의 증거가 아니다.
12. `nvidia-smi` 가 안 되면 드라이버가 빠졌을 수 있다(커널 갱신 후 DKMS 미빌드로 하루 멈춘 적 있다).
    `/dev/nvidia*` · `lsmod | grep nvidia` 로 가른다.
13. DebugView 는 편집 서비스와 **분리된 프로세스**다(`setsid`, GPU 0). 서비스를 내려도 안 죽는다.
    목록 카드에 `<model-viewer>` 를 넣으면 WebGL 컨텍스트 상한(~16)을 넘는다 — 정지 썸네일을 쓴다.

---

## 5. 확정된 결과 — 현재 기준선 (W41·W42, slat 격자, 반복 측정)

🔴 **이전 수치(275~297s · 0.6635 → 0.9733 · "23배")와 섞지 마라.** 그것들은 W38 게이트 전이고,
GLB 표면 샘플링 격자 값이며, "Baseline 이 흔들린다"(0.6635↔0.8279)는 **다른 자산**(재생성본) 간 차이였다.

### 5-1. SLat 재사용이 하는 일

원저자 경로는 매 편집마다 GLB → 150뷰 렌더 → DINOv2 → 인코더로 SLat 을 **재구성**한다.
그 왕복이 **점유 21.2% 손실**(13,460 → 11,467셀 · IoU 0.7410)이고 회차마다 반복된다.
Ours 는 직전 회차의 SLat 을 그대로 쓴다 → 재구성 0회(생성 자산) 또는 1회(업로드 자산).
⚠️ "손실 0" 이 아니라 **"반복·누적 제거"** 다.

### 5-2. 시간 (W41 · 3런 평균±표준편차)

```
                     Baseline           Ours
t1s-snowman   wall   277.75 ± 1.18s     129.04 ± 4.52s    2.15×
t2s-moto      wall   259.37 ± 2.12s     115.79 ± 3.54s    2.24×
사라지는 단계          렌더 83.3 + feature 24.5 + 마스크 39.7 = 147.6s (t1s)
편집 단계              두 팔 통계적으로 같다 (93.02 vs 92.19)
GPU 피크               15.00 → 10.61 GiB
```
속도 이득은 전부 **안 하는 일**에서 나온다. 하드웨어 무관한 구조적 제거다.

### 5-3. 보존 (W42 · 고정 영역 채점)

```
t1s (보존 영역 부모 10,290셀)   Baseline IoU 0.7507 · 사라짐 1,550   │ Ours 0.9861 · 사라짐 1
t2s (보존 영역 부모  2,983셀)   Baseline IoU 0.8570 · 사라짐   220   │ Ours 0.9796 · 사라짐 0
```
Ours 의 feats 흔들림은 마스크 **안**에 몰리고, Baseline 은 마스크 **밖**(보존 영역)에 몰린다
(밖 mean|Δ| 4,340~11,500배). 원본 잠재가 손실 없이 들어가면 편집은 오히려 **더** 먹는다
(마스크 안 신규 t1s 1,061 → 1,472).

### 5-4. 제품 경로의 첫 형태 편집 (3090 → <EDIT_HOST>, W27③)

```
"라이더 머리를 아이언맨 머리로" · 머리 마스크 476셀(지문 9e237b53…) · 조건 이미지 = 바뀐 뒤 전신
changed 26 · removed 1 (예측 상한 13 안) · 마스크 밖 화소 변화 0  → 봉쇄 ✅
머리 영역 붉은 화소 1 → 5 · 평균 색 그대로                         → 효능 🔴
```
이 런은 라쏘 점유 셀(빈 공간 0%) 마스크였다 — 결정 6(볼륨 전달) 이전 구성이다.
§6 규칙 4·5(점유 셀+볼륨 마스크, 마스크와 일치하는 조건 재생성)를 서비스 경로에 아직 안 넣었다.

### 5-5. 연쇄 편집 — 성립

SLat_v2(호박 머리) → 하단부 선인장 SLat_v3 완주. 1차 편집 결과 보존 IoU 0.9803 (1,846셀 중 9셀 소실).

---

## 6. 편집 품질을 좌우하는 규칙 — 전부 실측에서

1. **프롬프트는 바꿀 대상을 주어로, 최종 상태를 서술한다.**
   ❌ `"A halloween pumpkin instead of the head."` → 호박이 하나 더 생김 · 주황 8.0%
   ✅ `"The head is a large orange halloween pumpkin with deep vertical ridges…"` → 주황 32.9%
   `instead of` · `replace X with Y` · 명령형은 "그 물건을 어딘가 놓아라" 로 읽힌다.
2. **마스크(와 자산) 안에 프롬프트를 만족시키는 다른 답이 있으면 그 답이 나온다.**
   모자가 있으면 모자만 호박이 됐다. 모자 없는 자산으로 바꾸자 됐다.
3. **마스크는 대상과 변경 여지를 동시에 정한다.** 좁히면 실루엣을 못 바꾼다
   (2D 마스크 온-오브젝트 40% → 크게 바뀜 · 85% → 전혀 안 바뀜).
4. 🔴 **마스크는 프리미티브가 아니라 점유 셀로 만든다.** 타원체는 곡률 밖 돌출부(당근 코, 손잡이 끝의 손)를
   흘리고, 흘린 셀은 **매 스텝 원본으로 강제 복원**된다 (흘린 73셀 중 73셀 불변). 점유 셀 마스크로 바꾸자
   코 28/28 → 0/28, 보존 비용 없음 (W42).
5. 🔴 **3D 마스크를 바꾸면 조건(2d_mask·2d_edit)도 다시 만든다.** 원저자 후처리는 `마스크 깊이 <= 모델 깊이`
   라 돌출부에서 2D 마스크에 구멍이 뚫린다(옛 마스크가 당근 bbox 의 31.7% 만 덮음). 조건까지 갱신하자
   t2s 에서 옛 조건의 "손 흔드는 자세" 가 사라지고 두 손이 손잡이로 내려왔다 (W43). 기하 렌더로만 보인다.
6. **SS 하드 오버라이트는 "점유된 기하" 만 지킨다.** 16³ 토큰이 보존되려면 그 안에 "점유 & 마스크 밖" 복셀이
   하나라도 있어야 한다 → 빈 공간(격자의 ~83%)은 마스크와 무관하게 항상 편집 가능하다 (W40, 코드확인).
7. 실루엣이 다른 교체는 3D 로 온다. **1~2복셀 크기 디테일**(페이스플레이트 슬릿·호박 세로 골)은 안 온다.
   같은 성질의 디테일도 편집 볼륨이 크면 온다(선인장 가시) — 해상도 대비 **절대 크기** 문제로 보인다 (미검증).
8. **원본 생성 프롬프트를 자산과 함께 저장해야 한다** — 규칙 1 이 그 구조를 따라가야 성립한다.
   업로드 자산은 프롬프트가 없으므로 캡션 경로가 따로 필요하다.

---

## 7. 🔴 철회·무효 — 되살리지 마라

| 주장 | 실제 |
|---|---|
| "inversion 이 구조적으로 조건을 이긴다" | `re_init=True` 로 조건이 통째로 나왔다. "기본 설정에서 그렇다" 로 축소 |
| "64³ 해상도가 병목" | 같은 격자에서 바퀴살까지 생성했다 |
| "조건이 SLAT 에 안 닿는다" | 위와 같은 실측으로 기각 |
| "stage-2 cfg=0 이 원인" | 원저자도 그 값으로 정체성 교체를 한다 |
| "빈 공간이 필요조건"(D22②) | 모델이 아니라 **지표**의 조건이었다 |
| "아이언맨 정체성은 4판 모두 안 왔다" | W42 기하 렌더에서 **형태 수준으로 왔다**(각진 헬멧·V자 상체, 머리 IoU 0.21). 재판정 필요 |
| "SDXL 로 갈아타자" | Nunchaku INT4 FLUX 가 낫다 (§8) |
| "Baseline 이 런마다 흔들린다(0.6635↔0.8279)" | 다른 자산 간 차이였다. 같은 자산에서 coords 표준편차 **0** |
| "마스크 밖 602 / 1,845" | 기준 오류. 실제 5 / 0 |
| D51 이전 보존 수치 전부 | `torch.isin` 결함으로 무효 (다섯 웨이브분) |

**항진명제 지표 — 정의상 그 값이 나온다:**
- `마스크 안 신규 = 0` — 마스크가 점유 셀로만 이루어지면(또는 전부 점유면) 정의상 0. **빈 칸을 포함한 고정 영역**으로 채점한다 (`tools/w42_region_eval.py`)
- 마스크 셀 기준 `밖_IoU`·`밖_생김` — 마스크 정의가 바뀌면 같은 결과가 안↔밖으로 재분류될 뿐이다. 마스크가 다른 런끼리 비교하지 않는다
- 보존 영역 잠재 유사도 — 하드 오버라이트가 강제로 같게 만든다. 품질 지표가 아니라 **강제치환 단위 테스트**다
- "연결 성분 1개" — 순환 도로가 전부 잇는 자산에서는 무엇이 나오든 1개

---

## 8. 열린 문제 — 다음에 할 일

**제품 경로 바로 다음 런 — 조건 A/B** (`handoff/conditions/`). 같은 마스크·같은 halo 로
아이언맨 vs 뿔 달린 원뿔 두 런. 산출이 잡음 수준으로 같으면 조건이 SLAT 에 안 닿는 것이고,
뚜렷이 다르면 조건은 일하고 한계는 해상도다. v2(ingest 로 커밋됨) 기반 편집이 A5000 서비스
다운(`UPSTREAM_UNREACHABLE`)으로 못 돌았다 — 서비스를 올리면 바로 돈다.
⚠️ 마스크는 결정 6 대로 볼륨(halo)으로, 조건은 그 마스크와 맞춰 만든다 (§6 규칙 4·5).
⚠️ 빈 공간 비율의 분모 정의가 맥북(halo=2 80.2%)과 A5000(73.0%)이 다르다. 셀 수는 2,398 로 같다.

**Challenge: 마스크 안(편집) 품질이 원저자만 못하다는 육안 인상.** 첫 과제는 고치는 게 아니라 **재는 것**이다.
VoxHammer Table 1 과 같은 지표(CLIP-I / DINO-I)를 써야 직접 비교가 선다. 가설 후보(미검증):
생성 SLat vs 인코더 SLat 분포 불일치 · 셀 밀도 17% 차 · 재인코딩의 뭉갬이 편집 자유도를 높였을 가능성(거래) ·
**절대 크기**(가장 유력 — 같은 자산에서 마스크 크기만 바꾼 한 런으로 갈린다).

```
1. 다회차 열화 곡선 — 연구 주장의 본체.  지금까지 1회차만 반복 측정했다
   ⚠️ 연쇄의 "보존" 기준(직전 회차 vs 전체 이력)을 먼저 정한다
2. 아이언맨 정체성 재판정 — 기존 판정이 어떤 판본·뷰였는지 대조
3. W39 어텐션 마스크 — 신호는 확실하나(coords IoU 0.6438, feats 안 106배) 육안상 **나빠졌다**
   (부유 파편·마스크 밖 +41.8% 셀).  기본 꺼짐 유지.  다른 자산 재현 · 반대 방향 차단은 안 했다
4. Nunchaku INT4 FLUX — 가중치는 받아 뒀다(DiT 6.31 GiB + 별도 리포 nunchaku-t5 AWQ INT4 2.78 GiB).
   🔴 `pip install nunchaku` 는 **동명이인 패키지**다.  공식 휠은 torch 2.8+/cu12.8+ 만 있고 우리는
   torch 2.4+cu118 → **별도 venv·별도 프로세스**가 권장안 (사용자 결정 대기).  A5000 은 FP4 가속이 없어 INT4
5. t2s Baseline 편집량 급감(W41 2,491 → W42 1,788) 원인 미규명
6. 경계 soft mask(dilation + falloff)가 VoxHammer 코드에 실제로 있는지 코드로 확인
7. 단계 A(RunPod A6000 48GB) — 같은 GA102·같은 대역폭이라 "VRAM 만 바뀐" 비교.
   vCPU 수를 기록하고 A5000 수치와 섞지 않는다.  A5000 에서 끝낼 것을 먼저 끝낸다
```

선행연구 메모: "같은 3D 자산에 순차 독립 편집을 가하고 열화·속도를 정량 평가한 논문" 은 검색 범위에서
못 찾았다(2D 는 MagicBrush·MSE-Bench 로 확립). Fast3Dcache 는 캐싱 방향이 같아 인용 필수.
"z_0 loss" 라 부르지 말고 **inversion reconstruction error** 로 쓴다.

---

## 9. 보고 형식

실측 / 코드확인 / 추론 / 미검증 을 구분한다 · 예측을 먼저 적는다 · 한 웨이브 = 한 질문 ·
육안 산출물이 없으면 미검증 · **누가 어디서 쟀는지** 적는다 · 인용하는 문서의 **시점**을 확인한다.
