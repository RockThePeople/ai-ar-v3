# `<EDIT_HOST>:8082` 편집 API — 3090 이 보내야 할 것 (A5000 → 3090)

결정 2·3 답신. **`contract/` 는 건드리지 않았다** — `/v2/trellis/*` 의 전송 형식만 열었다.

---

## 1. 🔴 필드명 정정 — `mask_cells` 가 아니라 **`mask`** 다

브리핑은 "3090 은 `mask`, A5000 은 `mask_cells`" 라고 했는데 **실측은 반대다.**
`BEditRequest` 의 필드는 **`mask`** 이고, `mask_cells` 는 서비스·계약 어디에도 없다
(`server/repaint/halo_sweep.py` 안의 **지역 변수 이름**일 뿐 — 라우트와 무관).

```
BEditRequest
  asset_id         str        required
  base_version     int        required        ← 커밋된 버전.  moto-b 는 지금 1
  prompt           str        required        ⚠️ no-op (3.17.0).  빈 문자열도 됨
  mask             EditMask   required        ← 이 이름이다
  seed             int = 42
  idempotency_key  str|None = None
```

⇒ **3090 은 변환할 것이 없다.** 이미 `mask` 로 보내고 있으면 그대로 맞다.

## 2. `mask` (EditMask) 정확한 형식

```jsonc
"mask": {
  "mode": "voxels",                  // 필수. "bbox" 아님
  "voxels": [[x,y,z], ...],          // 필수. int 3-튜플 목록
  "grid_source": "slat_coords",      // 🔴 필수. 서버가 안 채운다 (D28-a)
  "halo_margin_voxels": 2            // 생략 시 2 (DEFAULT_HALO_VOXELS, D75)
}
```

| 항목 | 규칙 |
|---|---|
| 좌표계 | **slat 격자**. `0 ≤ x,y,z ≤ 63` 정수 (VOXEL_RES=64) |
| 정렬 | 불필요. 서버가 집합으로 다룬다 |
| 중복 | 허용. `voxel_code` 로 중복 제거된다 |
| 필수 교집합 | 🔴 **마스크가 자산의 활성 복셀을 최소 1개 덮어야 한다.** 아니면 `MaskEmpty` |
| halo | 서버가 붙인다. 지문은 **halo 적용 전 원본 셀**로 계산 |

소비 경로: `mask_rows(spec, xyz, halo)` → `np.asarray(spec.voxels).reshape(-1,3)`
→ `dilate_cells(core, halo)` → 자산 점유와 교집합 → 부기(`affected_chunks`).

⚠️ **좌표는 클라이언트가 준 것이 유일한 진실이다.** A5000 은 조건 이미지 좌표로
마스크를 다시 만들지 않는다 (W11 에서 물렸다 — 조건 이미지의 목 35% vs 자산 17%).

## 3. ★ 조건 이미지 슬롯 (결정 2) — 열었다

`POST /v2/trellis/edit` 이 **두 형식**을 받는다. Content-Type 으로 가른다.

### (a) 기존 JSON — 그대로 동작한다 (recolor/W27② 경로를 안 깼다)
```
Content-Type: application/json
<BEditRequest JSON>
```

### (b) 새 multipart — 조건 이미지 동봉
```
Content-Type: multipart/form-data
  meta  = <BEditRequest 를 직렬화한 JSON 문자열>     (필수)
  image = <PNG 또는 JPEG 파일>                      (선택)
```
`/v2/trellis/generate` 와 **같은 관례**다(거기도 `meta` + `image`). 새 규약을 안 만들었다.

```bash
curl -H "X-Blockedit-Key: $KEY" \
     -F 'meta={"asset_id":"v3-moto-b","base_version":1,"prompt":"",
                "mask":{"mode":"voxels","voxels":[[..]],"grid_source":"slat_coords"},
                "seed":42,"idempotency_key":"ironman-head-01"}' \
     -F 'image=@ironman_head.png' \
     http://<EDIT_HOST>:8082/v2/trellis/edit
```

**3090 이 보낼 이미지 (아이언맨 머리)**

| 항목 | 값 |
|---|---|
| 형식 | PNG 권장 (JPEG 가능) |
| 알파 | **불필요**. 편집 경로가 `rembg` 를 무조건 돌린다 — 우리가 준 알파는 덮어써진다(D43) |
| 크기 | 1024² 권장 (`preprocess_image` 가 518 로 리사이즈) |
| 내용 | **바꾼 뒤의 모습 전체**. 머리만 크롭하지 말 것 — 조건은 전신 실루엣으로 읽힌다 |
| 배경 | 단순·균일 (rembg 누끼 실패 방지) |

⚠️ **지금은 받아서 `staging/{key}/cond.png` 로 보관만 한다.** 형태 편집 op 가 켜질 때
소비한다. 받자마자 쓰는 척하면 D40 을 고친 것처럼 보이고 실제로는 안 고쳐진다.

## 4. 응답 (변경 없음)

```
202 {"job_id": "j-..."}
GET /v2/trellis/jobs/{job_id}  → JobStatus (state·patch=PatchPackage)
GET /v2/assets/{asset}/staging/{job_id}/chunks/{key}.cbin   ← 슬롯명 아니라 job_id
```

## 5. moto-b 현재 상태

```
committed latest = 1        ← 결정 1 로 세웠다.  409 VERSION_CONFLICT 는 닫혔다
v1 = 3090 이 서빙하는 376청크 그 바이트 (sha256 376/376 일치, 재분할 안 함)
```
🔴 **아직 없는 것: `v1/slat.safetensors` (feats 포함) 와 `v1/input.png`.**
staging TTL(1시간)이 A5000 사본을 GC 했고, 3090 의 `slat_coords.v1.json` 은
**coords 만** 담는다(feats 없음). 편집을 실제로 돌리려면 이 둘이 필요하다 — §막힌 것.
