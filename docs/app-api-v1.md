# 앱 API 계약 v1 — QR AR 3D 편집 (Unity 클라이언트 ↔ 앱 서버)

판본: **app-api 1.0.3** (2026-11-10, 수행사 chief). 기준: `APP_SPEC.md` 4절 초안 + 사용자 결정(2026-11-10). 청크 형식(`.cbin`)과 청크·manifest 경로는 ai-ar-v3 계약 3.28.0(`contract/`)을 그대로 쓴다. 이 문서는 그 위에 앱 흐름(QR·생성·편집 확인·평가·삭제)만 더한다.

바뀌면 판본을 올리고 끝의 변경 기록에 적는다. 클라이언트는 `GET /app/v1/health` 의 `app_api` 로 판본을 확인한다.

---

## 1. 접속

| 항목 | 값 |
|---|---|
| 기본 주소 | `http://{PUBLIC_HOST}:{PUBLIC_PORT}`. 클라이언트 설정 파일 1곳에 하드코딩한다. 값은 사용자가 서버 `.env` 와 같게 넣는다 |
| 프로토콜 | HTTP/1.1 평문(HTTPS 없음). Android 9+ 는 평문 허용 설정이 필요하다 |
| 본문 | 요청·응답 모두 `application/json; charset=utf-8`. 예외: 청크(`application/octet-stream`), 미리보기 이미지(`image/png`) |
| 기기 식별 | 모든 `/app/v1` 요청에 헤더 `X-Device-Key: <기기 공개키>`. 서버는 길이 1–200 의 불투명 문자열로 다룬다. 권장 형식: secp256k1 압축 공개키 hex(`0x02…`/`0x03…`, 68자). 나중에 ERC-4337 계정 키로 쓰기 위해서다. 지금은 서명을 검사하지 않는다 |
| 공유 토큰(선택) | 헤더 `X-App-Token`. 서버 `.env` 에 `APP_SHARED_TOKEN` 이 있을 때만 검사한다. 기본 꺼짐 |
| 시간 | 서버 시각은 UNIX 초(float) |

### 오류 응답 (모든 4xx·5xx 공통)

```json
{"error_code": "ASSET_LOCKED", "message": "사람이 읽는 설명(한국어)", "detail": {"job_id": "j-…"}}
```

`detail` 은 없을 수 있다. 클라이언트는 `error_code` 로만 분기한다. 표는 6절에 있다.

---

## 2. 좌표

### 2.1 프레임 (계약 3.28.0, 변경 없음)

| 이름 | 정의 |
|---|---|
| NORMALIZED | float, 자산 정육면체 [−0.5, +0.5]³. 축은 **복셀 축**(SLat, z 가 위) |
| VOXEL | int [0, 64)³ 셀 인덱스. 셀 (i, j, k) 의 중심 = NORMALIZED `((i + 0.5)/64 − 0.5, (j + 0.5)/64 − 0.5, (k + 0.5)/64 − 0.5)` |
| CHUNK | VOXEL ÷ 4 → 16³ 슬롯. 청크 키 `"{cx}_{cy}_{cz}"` |
| Unity 자산 로컬 | 청크 메시를 붙이는 부모 Transform(이하 **자산 루트**)의 로컬 좌표. `VoxelFrame.ToUnity(v) = (v.x, v.z, −v.y)`, 역변환 `VoxelFrame.ToVoxel(u) = (u.x, −u.z, u.y)` (`unity/Runtime/VoxelFrame.cs`, 유일한 정의). 자산 루트 스케일은 **0.5 m**(자산 정육면체 한 변, APP_SPEC D13). 기존 런타임의 `AssetScale.RootScale` 0.24 m 는 이 앱에서 쓰지 않는다. 카메라·볼륨 값은 NORMALIZED 라 스케일과 무관하다 |

`.cbin` 정점과 slat coords 는 NORMALIZED(복셀 축)로 오고, 클라이언트가 `ToUnity` 로 바꿔 자산 루트 아래에 놓는다. 이 동작은 기존 런타임과 같다.

### 2.2 카메라 (편집 요청의 `camera`)

라쏘를 확정한 순간의 AR 카메라를 **자산 로컬 NORMALIZED, 복셀 축**으로 보낸다. 서버는 이 카메라로 조건 렌더 1장을 만든다(D8).

```json
"camera": {
  "frame": "normalized_voxel",
  "position": [0.12, -1.35, 0.42],
  "forward":  [-0.07, 0.95, -0.30],
  "up":       [0.0, 0.30, 0.95],
  "fov_y_deg": 63.2,
  "aspect": 0.4615,
  "screen_px": [1080, 2340]
}
```

Unity 계산(의사 코드):

```csharp
Transform root = assetRoot;                         // 청크 메시의 부모(RootScale 포함)
Camera cam = arCamera;
Vector3 pU = root.InverseTransformPoint(cam.transform.position);   // 자산 로컬(Unity 축), NORMALIZED 단위
Vector3 fU = root.InverseTransformDirection(cam.transform.forward).normalized;
Vector3 uU = root.InverseTransformDirection(cam.transform.up).normalized;
position = VoxelFrame.ToVoxel(pU);
forward  = VoxelFrame.ToVoxel(fU);
up       = VoxelFrame.ToVoxel(uU);
fov_y_deg = cam.fieldOfView;                        // 세로 화각(투영 행렬 기준, AR 물리 카메라 값)
aspect = (float)Screen.width / Screen.height;
```

| 필드 | 규칙 |
|---|---|
| `frame` | 고정 문자열 `"normalized_voxel"`. 다르면 `INVALID_CAMERA` |
| `position` | NORMALIZED 단위. 자산 정육면체 밖이어야 한다(원점까지 거리 > 0.5). 아니면 `INVALID_CAMERA` |
| `forward`, `up` | 단위 벡터(서버가 다시 정규화). 두 벡터 사이 각이 80–100° 밖이면 `INVALID_CAMERA` |
| `fov_y_deg` | 10–120 |
| `aspect` | 화면 가로/세로. 0.2–5 |
| `screen_px` | 기록용(선택) |

서버 렌더 규칙:
- 1024×1024 정사각으로 렌더한다.
- 정사각의 화각은 `max(fov_y, fov_x)` 이고, `fov_x = 2·atan(aspect·tan(fov_y/2))` 이다. 세로 화면이면 세로 화각이 그대로 쓰이고, 화면에 보이던 영역이 렌더 안에 모두 들어간다.
- 2D 마스크는 편집 볼륨 셀을 같은 카메라로 렌더한 것이다(가림 반영). 라쏘 다각형은 쓰지 않는다.

> ⚠️ **확인 항목 V1(미검증).** Unity 는 왼손 좌표계이고 서버 렌더러(Blender)는 오른손 좌표계다. 같은 숫자의 카메라로 렌더한 서버 이미지가 폰 화면과 **좌우 반전**일 가능성이 있다(추론). 클라이언트 쪽에서 첫 실기 편집 때 "폰 화면 캡처 + 보낸 camera JSON" 1쌍을 수행사에 전달해 주면, 서버 렌더(`preview.render_url`)와 비교해 확정한다. 반전이면 서버가 처리하고, 이 계약의 숫자 규약은 바꾸지 않는다.
>
> ⚠️ **사전 시험 X1 진행 중.** 임의 시점(뒤·위·근접·폰 화각)의 편집 품질을 시험한다. 결과에 따라 서버가 거리·화각을 표준값으로 바꿔 렌더하는 "방향만 쓰기"로 갈 수 있다. 그래도 클라이언트가 보내는 값과 형식은 바뀌지 않는다.

### 2.3 편집 볼륨 셀 (`volume_cells`)

회전체 볼륨(APP_SPEC F5)을 64³ 로 복셀화한 **편집 볼륨 셀**이다. 빈 공간을 포함한다.

```json
"volume_cells": [[31, 40, 22], [31, 40, 23], [32, 40, 22]]
```

| 규칙 | 값 |
|---|---|
| 좌표 | VOXEL 정수 `[i, j, k]`(복셀 축). NORMALIZED 점 p → `i = floor((p.x + 0.5)·64)`, 0–63 으로 자른다. j·k 도 같다 |
| 중복 | 서버가 제거한다 |
| 개수 | 1 ≤ N ≤ 65,536(64³ 의 25 %). 넘으면 `MASK_TOO_LARGE`, 0 이면 `MASK_EMPTY` |
| halo | 서버가 2셀을 더한다(계약 기본값). 클라이언트는 더하지 않는다 |
| 화면 표시 | "선택 셀" = 점유 셀(slat coords) ∩ 편집 볼륨 셀. 클라이언트에서만 계산한다. 서버에 보내는 것은 편집 볼륨 셀이다 |

점유 셀은 `GET /v2/assets/{asset_id}/slat_coords.v{n}.json`(계약 3.26.0 이후 경로)으로 받는다.

---

## 3. 엔드포인트

### 3.1 상태 확인

`GET /app/v1/health` → 200

```json
{"ok": true, "app_api": "1.0.3", "contract_version": 4, "mock": false,
 "workers": {"a5000": "up", "inpaint": "up", "llm": "configured"}}
```

`mock: true` 면 가짜 응답 모드다(7절).

### 3.2 QR

**`GET /app/v1/qr/{qr_id}`** — QR 에 묶인 자산을 조회한다.
- `qr_id`: QR 내용에서 만든 문자열, `[A-Za-z0-9_-]{1,64}`. 형식이 다르면 `INVALID_QR`. 스테이션 방식(D13, QR 2장이 자산 바닥 대각 모서리)에서는 스테이션 이름(예: `st-01`)을 보낸다. 서버는 qr_id 를 불투명 문자열로만 다루므로 바뀌는 것이 없다.
- 200: 자산 있음.
```json
{"qr_id": "booth-07", "asset_id": "a-3f9c2e1d", "version": 4, "is_creator": true,
 "scene_prompt": "An Ironman riding a motorcycle",
 "manifest_uri": "/v2/assets/a-3f9c2e1d/manifest.v4.json",
 "slat_coords_uri": "/v2/assets/a-3f9c2e1d/slat_coords.v4.json",
 "active_job": null,
 "can_rate": true, "rate_range": [2, 4]}
```
- `can_rate`: 이 기기(`X-Device-Key`)가 평가할 커밋 편집이 있으면 `true`. 즉 이 기기의 직전 평가 이후(평가한 적이 없으면 생성 이후) 커밋된 편집 판본이 1개 이상 있다는 뜻이다. 누가 한 편집이든 센다. `rate_range` 는 그때 `POST ratings` 가 기록할 `version_range` 와 같은 값이고, `can_rate: false` 면 `null` 이다. 앱 [평가하기] 버튼은 이 값으로 정한다(앱 재시작 뒤에도 맞음).
- `active_job`: 이 자산에 진행 중인 작업이 있으면 `{"job_id", "kind", "state", "is_mine"}`, 없으면 `null`.
- 404 `QR_EMPTY`: 자산 없음(생성 가능). 생성이 진행 중이면 200 으로 `asset_id` 와 `version: 0`, `active_job` 이 채워져 오고, `manifest_uri`·`slat_coords_uri`·`scene_prompt` 는 `null`, `can_rate` 는 `false` 다.

**`POST /app/v1/qr/{qr_id}/asset`** — 생성.
- 요청: `{"prompt": "a man riding a motorcycle", "seed": 42}` (`seed` 선택, 기본 42). `prompt` 길이는 1–300 자이고, 어느 언어든 된다(서버가 영어로 바꾼다).
- 202: `{"job_id": "j-…", "asset_id": "a-…"}`
- 409 `QR_TAKEN`: 이미 자산이 있거나 생성 중이다.
- 이 요청을 보낸 기기가 생성자로 기록된다.

**`DELETE /app/v1/qr/{qr_id}`** — 삭제(생성자만).
- 204: 삭제됨.
- 403 `NOT_CREATOR`, 404 `QR_EMPTY`, 409 `ASSET_LOCKED`(진행 중 작업 있음).
- 앱에서 보이는 결과: QR 이 다시 비고(`GET qr` → 404 `QR_EMPTY`), 그 자산의 manifest·청크·작업·미리보기 경로는 앱 API 에서 404 가 된다. 서버 안의 기록은 지우지 않고 보관본으로 남는다(9절).

### 3.3 편집

**`POST /app/v1/assets/{asset_id}/edits`**

```json
{"base_version": 4,
 "instruction": "change the man into Ironman",
 "camera": { … 2.2절 … },
 "volume_cells": [[31,40,22], …]}
```

- 202: `{"job_id": "j-…"}`
- 오류:

| HTTP | error_code | 뜻 |
|---|---|---|
| 409 | `VERSION_CONFLICT` | `base_version` 이 최신 커밋 판본이 아니다. `detail.latest_version` 을 준다 |
| 409 | `ASSET_LOCKED` | 같은 자산에 진행 중인 작업이 있다(누구의 것이든). `detail.job_id` |
| 409 | `UNCOMMITTED_PENDING` | 3D 확인을 기다리는 판본이 있다. `detail.job_id` |
| 400 | `INVALID_CAMERA` / `MASK_EMPTY` / `MASK_TOO_LARGE` / `INVALID_REQUEST` | 입력 오류 |
| 403 | `READ_ONLY` | 샘플 자산(8절) |
| 404 | `ASSET_NOT_FOUND` | 자산 없음 |

- `instruction` 길이는 1–300 자, 어느 언어든 된다.

**오류 검사 순서(서버, 1.0.1).** 여러 조건이 동시에 해당하면 아래 순서에서 **먼저 걸린 하나**만 돌려준다.

1. 400 입력 오류: `X-Device-Key` 누락, 본문 형식 → `INVALID_REQUEST` / `camera` → `INVALID_CAMERA` / `volume_cells` → `MASK_EMPTY`·`MASK_TOO_LARGE`
2. 404 `ASSET_NOT_FOUND`
3. 403 `READ_ONLY`(샘플 자산)
4. 409 잠금: 그 자산의 진행 중 작업이 `awaiting_3d_review` 면 `UNCOMMITTED_PENDING`, 그 밖의 진행 중 상태면 `ASSET_LOCKED`. 자산당 진행 중 작업은 1건이라 둘이 함께 나오지 않는다.
5. 409 `VERSION_CONFLICT`

따라서 잠금과 판본 불일치가 함께 있으면 **잠금 코드**(`ASSET_LOCKED` 또는 `UNCOMMITTED_PENDING`)를 준다. 판본은 잠금이 풀린 뒤 다시 확인하면 된다. 2·3 은 같은 자산에 함께 해당할 수 없어 순서가 결과를 바꾸지 않는다. 클라이언트 목의 순서(400 → 403 → 404 → 잠금 → 판본)와 결과가 같다.

**`GET /app/v1/jobs/{job_id}`** → 200 작업 객체(4절). 없으면 404 `JOB_NOT_FOUND`.

**`POST /app/v1/jobs/{job_id}/review2d`** — 2D 결과 승인/거절. `awaiting_2d_review` 에서만 받는다.
- 승인: `{"accept": true}` → 200 작업 객체(`editing_3d`)
- 거절: `{"accept": false, "reason": "the helmet looks too small"}` → 200 작업 객체. 서버가 이유를 분류해 다음 상태가 된다:
  - `refining`: 프롬프트 문제, 보정부터 다시
  - `inpainting`: 품질 문제, 같은 프롬프트에 seed 를 바꿔 다시
  - `needs_redraw`: 영역 문제, 작업이 끝난다. 앱은 라쏘를 다시 그려 새 편집을 보낸다
  - `failed` + `RETRY_LIMIT`: 이번이 세 번째 거절
- 다른 상태에서 부르면 409 `WRONG_STATE`. 거절인데 `reason` 이 비면 400 `INVALID_REQUEST`.

**`POST /app/v1/jobs/{job_id}/review3d`** — 3D 결과 유지/되돌리기. `awaiting_3d_review` 에서만 받는다.
- `{"keep": true}` → 200 작업 객체(`committed`, `result.manifest_uri` 채워짐)
- `{"keep": false}` → 200 작업 객체(`discarded`). 앱은 갖고 있던 부모 청크로 복원한다.
- 30분 동안 응답이 없으면 서버가 자동으로 discard 한다(`expires_at`).

**`POST /app/v1/jobs/{job_id}/cancel`** — `awaiting_2d_review` 에서 그만둘 때. 그 밖의 상태(대기·실행 중)에서는 409 `WRONG_STATE`. 200 작업 객체(`cancelled`).

리뷰·취소는 작업을 만든 기기만 할 수 있다. 아니면 403 `NOT_OWNER`.

### 3.4 미리보기 이미지

`awaiting_2d_review` 의 작업 객체 `preview` 에 들어 있다. 경로 예:

```
GET /app/v1/jobs/{job_id}/preview/{attempt}/render.png   원본 조건 렌더(라쏘 시점, 1024²)
GET /app/v1/jobs/{job_id}/preview/{attempt}/edit.png     인페인팅 결과(1024², 마스크 밖은 원본과 같음)
GET /app/v1/jobs/{job_id}/preview/{attempt}/mask.png     2D 마스크(흰색 = 편집 영역)
```

`attempt` 는 1부터 센다. 작업이 끝난 뒤 24시간 동안 받을 수 있다.

### 3.5 청크·manifest (계약 3.28.0 경로 그대로)

| 경로 | 내용 |
|---|---|
| `GET /v2/assets/{asset_id}/manifest.v{n}.json` | 커밋 판본 n 의 `ChunkManifest`(`chunks: {키: ChunkEntry}`) |
| `GET /v2/assets/{asset_id}/chunks/{key}.v{m}.cbin` | 청크 바이트. `ChunkEntry.uri` 를 그대로 쓴다. m 은 그 청크가 마지막으로 바뀐 판본이고 캐시 키로 쓴다 |
| `GET /v2/assets/{asset_id}/staging/{job_id}/chunks/{key}.cbin` | 3D 확인 전(미커밋) 편집 청크. 작업 객체 `result.chunks[].uri` 로 온다 |
| `GET /v2/assets/{asset_id}/slat_coords.v{n}.json` | 점유 셀 좌표 |

`ChunkEntry`: `{uri, hash (sha256 hex), byte_length, vertex_count, index_count, voxel_count, version}`. 받은 바이트의 sha256 이 `hash` 와 다르면 버리고 다시 받는다.

**편집 반영 순서(클라이언트):**
1. `awaiting_3d_review` 가 되면 `result.chunks` 를 모두 받는다(staging 경로). 지금 화면의 해당 키 청크와 부모 바이트를 메모리에 보관한다.
2. 받은 청크로 교체하고, `result.removed_chunk_ids` 의 청크를 숨긴다(부모는 보관). 앵커는 움직이지 않는다.
3. 유지 → `review3d keep:true` → 응답의 `result.manifest_uri` 로 판본 번호를 갱신한다. 청크 바이트는 그대로 두고 캐시 키만 판본 경로로 바꾼다(해시 같음).
4. 되돌리기 → `review3d keep:false` → 보관한 부모 청크로 복원하고, 숨긴 청크를 다시 보인다.

### 3.6 평가

**`POST /app/v1/assets/{asset_id}/ratings`** `{"stars": 4}`
- `stars`: 정수 0–5.
- 201:
```json
{"rating_id": "rt-…", "asset_id": "a-…", "stars": 4, "value": 80, "decimals": 0, "tag1": "starred",
 "version_range": [2, 5], "feedback_uri": "/app/v1/ratings/rt-….json", "feedback_hash": "sha256 hex"}
```
- `version_range`: 이 기기의 직전 평가 이후 커밋된 판본 범위.
- 409 `NOTHING_TO_RATE`: 그 뒤 커밋된 편집이 없다.
- `GET /app/v1/ratings/{rating_id}.json`: 피드백 파일(JSON). 모양은 ERC-8004 `giveFeedback` 인자와 같다. 체인에는 지금 올리지 않는다.

### 3.7 샘플 (Android 렌더 비용 측정용)

`GET /app/v1/samples` → 200

```json
{"samples": [
  {"name": "moto",      "asset_id": "sample-moto",      "versions": [1],             "note": "트랙 A 생성 자산"},
  {"name": "snowman",   "asset_id": "sample-snowman",   "versions": [1],             "note": "트랙 A 생성 자산"},
  {"name": "chair",     "asset_id": "sample-chair",     "versions": [1],             "note": "트랙 A 생성 자산"},
  {"name": "jeep",      "asset_id": "sample-jeep",      "versions": [1,2,3,4,5,6],   "note": "원본 + 연속 편집 5회(제안 경로)"},
  {"name": "cottage",   "asset_id": "sample-cottage",   "versions": [1,2,3,4,5,6],   "note": "원본 + 연속 편집 5회"},
  {"name": "adventurer","asset_id": "sample-adventurer","versions": [1,2,3,4,5,6],   "note": "원본 + 연속 편집 5회"}]}
```

- 다운로드는 3.5 경로 그대로다: `/v2/assets/sample-jeep/manifest.v6.json` → `chunks[].uri`.
- 샘플은 읽기 전용이다(편집·삭제는 403 `READ_ONLY`). QR 에 묶이지 않는다.
- 제공 시점: 앱 서버 S1 완료 뒤. 가짜 응답 모드에서는 `sample-jeep` v1–v6 만 있다.

---

## 4. 작업 객체

### 4.0 항상 채워지는 필드 (1.0.1)

아래 필드는 서버가 **언제나 null 이 아닌 값**으로 채운다. 나머지 필드는 키는 항상 있고, 값이 `null` 일 수 있다.

| 응답 | 항상 채움 |
|---|---|
| 작업 객체 | `job_id`, `kind`, `state`, `stage`, `asset_id`, `is_mine`, `progress`, `created_at`, `updated_at`, `elapsed_s` (생성 작업도 `asset_id` 는 작업을 만들 때 정해진다) |
| QR 조회 200 | `qr_id`, `asset_id`, `version`(생성 중이면 0), `is_creator`, `can_rate` |
| 생성 202 | `job_id`, `asset_id` |
| 편집 202 | `job_id` |
| 평가 201 | `rating_id`, `asset_id`, `stars`, `value`, `decimals`, `tag1`, `version_range`, `feedback_uri`, `feedback_hash` |
| health | `ok`, `app_api`, `contract_version`, `mock` |
| 오류 응답 | `error_code`, `message` |

클라이언트가 필수로 처리하는 필드(작업 `job_id`·`kind`·`state`·`asset_id`, QR `qr_id`·`asset_id`·`version`·`is_creator`, 생성 `job_id`·`asset_id`, 편집 `job_id`, 평가 `rating_id`·`asset_id`·`stars`·`value`·`version_range`, health `ok`·`app_api`)는 모두 이 표 안에 있다.

```json
{"job_id": "j-7d2c…", "kind": "edit", "asset_id": "a-3f9c2e1d", "qr_id": "booth-07", "is_mine": true,
 "state": "awaiting_2d_review", "stage": "inpainting", "progress": 1.0,
 "queue_pos": null, "created_at": 1762750000.1, "updated_at": 1762750061.4, "elapsed_s": 61.3,
 "attempt": 1, "max_attempts": 3,
 "refine": {"target_element": "the man", "replacement": "Ironman", "op": "replace",
            "new_scene_prompt": "An Ironman riding a motorcycle"},
 "preview": {"attempt": 1,
             "render_url": "/app/v1/jobs/j-7d2c…/preview/1/render.png",
             "edit_url":   "/app/v1/jobs/j-7d2c…/preview/1/edit.png",
             "mask_url":   "/app/v1/jobs/j-7d2c…/preview/1/mask.png"},
 "review": null,
 "result": null,
 "expires_at": null,
 "error_code": null, "error_message": null}
```

| 필드 | 설명 |
|---|---|
| `kind` | `generate` / `edit` |
| `state` | 4.1 표 |
| `stage` | 진행 표시용 단계 이름(4.2). 상태보다 세밀하다 |
| `progress` | 0–1, 현재 단계 안의 진행. 추정치이고 단조 증가는 보장하지 않는다 |
| `queue_pos` | GPU 대기 순번(0 = 바로 다음). 대기 중이 아니면 `null` |
| `attempt` | 2D 시도 번호(1–3). 생성 작업은 `null` |
| `refine` | 프롬프트 보정 결과(편집). 보정 전이면 `null` |
| `preview` | 마지막 2D 시도의 이미지. 아직 없으면 `null` |
| `review` | 마지막 거절 처리: `{"reason", "category": "prompt"|"quality"|"region", "guidance"}` |
| `result` | 생성: `committed` 에서 `{"to_version": 1, "manifest_uri"}`. 편집: `awaiting_3d_review` 에서 `{"base_version", "to_version", "chunks": [ChunkEntry(staging uri)], "removed_chunk_ids": [...]}`, `committed` 뒤에는 `manifest_uri` 추가 |
| `expires_at` | `awaiting_3d_review` 의 자동 discard 시각 |

### 4.1 상태

| state | kind | 뜻 | 클라이언트 동작 |
|---|---|---|---|
| `queued` | 둘 다 | 서버 대기열 | 폴링, `queue_pos` 표시 |
| `generating` | generate | 생성 중(T2I → 3D → 청크화) | 폴링 |
| `refining` | edit | 프롬프트 보정 LLM | 폴링 |
| `inpainting` | edit | 조건 렌더 + 인페인팅 | 폴링 |
| `awaiting_2d_review` | edit | 2D 확인 대기 | 폴링 멈춤, 확인 화면(F6) |
| `needs_redraw` | edit | 영역 문제로 종료 | 라쏘 다시 그리기 안내, 새 편집 |
| `editing_3d` | edit | 3D 편집 | 폴링 |
| `awaiting_3d_review` | edit | 3D 확인 대기 | 폴링 멈춤, 청크 교체·확인(F7) |
| `committed` | 둘 다 | 반영됨(끝) | 판본 갱신 |
| `discarded` | edit | 되돌림·자동 폐기(끝) | 부모 청크 복원 |
| `cancelled` | edit | 사용자 취소(끝) | – |
| `failed` | 둘 다 | 실패(끝), `error_code` | 6절 표대로 안내 |

전이: `queued → refining → inpainting → awaiting_2d_review → {editing_3d | refining | inpainting | needs_redraw | failed(RETRY_LIMIT) | cancelled}`, `editing_3d → awaiting_3d_review → {committed | discarded}`. 생성은 `queued → generating → committed`. 어느 실행 상태에서든 `failed` 로 갈 수 있다.

### 4.2 stage 값

`queued`, `t2i`, `generating`, `chunking`(생성) / `refining`, `rendering`, `inpainting`, `classifying`, `editing_3d`, `chunking`(편집). 화면 문구는 클라이언트가 정한다. 권장: refining = "보정", rendering·inpainting = "인페인팅", editing_3d·chunking = "3D 편집".

### 4.3 예상 시간 (추론, 실측 구간의 합)

| 구간 | 시간 |
|---|---|
| 생성(T2I 교대 포함) | 약 90–120 s |
| 편집 전송 → 2D 확인 | 약 35–60 s(라쏘 시점 1장 렌더 + INT4 인페인팅 26 s + LLM) |
| 2D 승인 → 3D 확인 | 약 40–45 s |
| 품질 재시도 1회 | 약 26 s(렌더 재사용) |

---

## 5. 폴링 규칙

- 작업을 만든 뒤 `GET /app/v1/jobs/{job_id}` 를 **1초 간격**으로 부른다.
- `awaiting_2d_review`, `awaiting_3d_review`, `needs_redraw`, 끝 상태(`committed`, `discarded`, `cancelled`, `failed`)가 되면 멈춘다. 리뷰 POST 뒤 다시 시작한다.
- 네트워크 오류가 나면 2·4·8 s 로 늘려 다시 시도하고, 최대 8 s 간격으로 계속한다. 작업은 서버에서 계속 돈다.
- 앱을 다시 켜면 `GET /app/v1/qr/{qr_id}` 의 `active_job` 으로 이어서 폴링한다.
- 서버 쪽 시간 제한: 실행 상태가 10분을 넘으면 `failed(TIMEOUT)`.
- 다른 사용자의 commit 반영(멀티유저 동기화)은 v1 범위 밖이다. 같은 QR 을 보는 다른 기기는 `GET /app/v1/qr/{qr_id}` 의 `version` 이 바뀐 것을 보고 manifest 를 다시 받으면 된다(권장 간격 10 s, 선택).

---

## 6. 실패 코드

| error_code | HTTP / 작업 | 뜻 | 앱 안내 |
|---|---|---|---|
| `INVALID_REQUEST` | 400 | 필드 누락·형식 오류 | 개발 오류 |
| `INVALID_QR` | 400 | qr_id 형식 위반 | QR 다시 인식 |
| `INVALID_CAMERA` | 400 | camera 규칙 위반(2.2) | 개발 오류 |
| `MASK_EMPTY` / `MASK_TOO_LARGE` | 400 | 볼륨 셀 0개 / 65,536 초과 | 라쏘 다시 |
| `NOT_CREATOR` / `NOT_OWNER` / `READ_ONLY` | 403 | 권한 없음 | 버튼 숨김 |
| `QR_EMPTY` / `ASSET_NOT_FOUND` / `JOB_NOT_FOUND` | 404 | 없음 | – |
| `QR_TAKEN` | 409 | 이미 자산이 있거나 생성 중 | 다시 조회 |
| `VERSION_CONFLICT` | 409 | 판본이 바뀜 | manifest 다시 받고 다시 시도 |
| `ASSET_LOCKED` | 409 | 진행 중 작업 있음 | 기다리기 |
| `UNCOMMITTED_PENDING` | 409 | 3D 확인 대기 판본 있음 | 기다리기 |
| `WRONG_STATE` | 409 | 그 상태에서 못 하는 요청 | 작업 다시 조회 |
| `NOTHING_TO_RATE` | 409 | 평가할 편집 없음 | 버튼 비활성 |
| `REFINE_INVALID` | 작업 failed | 보정 LLM 출력이 스키마 위반(재시도 후) | 다시 시도 |
| `CLASSIFY_FAILED` | 작업 failed | 거절 이유 분류 LLM 출력이 스키마 위반(재시도 후). 1.0.3 추가 | 다시 시도 |
| `LLM_UNAVAILABLE` | 작업 failed | LLM 호출 실패·키 없음 | 잠시 뒤 다시 |
| `T2I_FAILED` / `GENERATE_FAILED` | 작업 failed | 생성 실패 | 다시 시도 |
| `RENDER_FAILED` / `INPAINT_FAILED` / `EDIT_FAILED` | 작업 failed | 편집 단계 실패 | 다시 시도 |
| `RETRY_LIMIT` | 작업 failed | 2D 거절 3회 | "다시 시도 / 취소" |
| `WORKER_UNREACHABLE` | 503 / 작업 failed | GPU 워커 연결 실패 | 잠시 뒤 다시 |
| `TIMEOUT` | 작업 failed | 실행 10분 초과 | 다시 시도 |
| `SERVER_RESTART` | 작업 failed | 서버 재기동으로 실행 중 작업 중단 | 다시 시도 |
| `INTERNAL` | 500 | 그 밖의 서버 오류 | 다시 시도 |

---

## 7. 가짜 응답 모드 (`mock: true`)

서버 `.env` 에 `APP_MOCK=1` 을 넣으면 GPU·LLM 을 부르지 않고, **이 문서와 같은 모양**의 응답을 고정 시간으로 낸다. Unity 개발용이다.

- QR: 처음에는 모든 qr_id 가 404 `QR_EMPTY` 다. 생성하면 3 s 뒤 `committed`, 자산은 `sample-jeep` v1 의 사본(새 asset_id)이다.
- 편집:
  - `refining` 1 s → `inpainting` 2 s → `awaiting_2d_review`. 미리보기는 고정 PNG 3장이다.
  - 거절 이유 분류는 이유 문자열에 `area`/`영역`/`region` 이 있으면 region, `prompt`/`프롬프트` 이 있으면 prompt, 그 밖에는 quality 다.
  - 승인 → `editing_3d` 2 s → `awaiting_3d_review`. 청크는 `sample-jeep` 의 실제 변경분이다. k 번째 편집은 v k → v k+1 이고, 5회가 넘으면 v5 → v6 을 반복한다.
- 판본·잠금·오류 코드 규칙은 실제 모드와 같다(`VERSION_CONFLICT`, `ASSET_LOCKED`, `WRONG_STATE` 등 재현 가능).
- 강제 실패: 요청 헤더 `X-Mock-Fail: <error_code>` 를 주면 해당 작업이 다음 단계에서 그 코드로 실패한다(가짜 모드 전용).

---

## 8. 샘플 자산 출처 (기록)

- `sample-moto`·`sample-snowman`·`sample-chair`: 트랙 A 동결 자산 `vhl-ta-moto`·`vhl-ta-snowman`·`vhl-ta-chair` v1(정점색 `.cbin`).
- `sample-jeep`·`sample-cottage`·`sample-adventurer`: 논문 연속 5회 편집의 제안 경로(P1) 원본과 r1–r5 결과(`.cbin`, 봉합 켬).
- 면 수는 원본 TRELLIS 메시 대비 약 20–51배다(크기 축소 안 함, 지시 유효).

## 9. 데이터 보존 (1.0.2)

- **앱에서 보이는 동작:** 자산을 삭제하면 QR 이 비고, 그 자산의 manifest·청크·slat coords·작업·미리보기는 앱 API(`/app/v1`, `/v2/assets`)에서 404 다. 같은 QR 에 새로 생성하면 새 `asset_id` 가 붙는다. 이 동작은 1.0.1 과 같다.
- **서버 안 보관:** 삭제한 자산의 QR 등록 이력, 장면 문장, 판본·청크, 작업 기록(상태 이력·LLM 호출 기록), 미리보기·T2I 이미지, 평가는 지우지 않는다. 보관본으로 남기고 운영자용 조회 페이지(`/clients`, 앱 API 아님)에 "삭제됨"으로 표시한다.
- A5000 워커 저장소(SLat 등)는 삭제 때 지운다. 지난 판본의 3D 보기는 앱 서버에 남은 청크로 만든다.
- 평가 기록의 `asset_id` 는 남고 `asset_deleted: true` 로 표시한다(1.0.0 과 같음).
- 클라이언트 응답 모양은 바뀌지 않는다.

## 변경 기록

| 판본 | 날짜 | 내용 |
|---|---|---|
| 1.0.3 | 2026-11-10 | 자산 루트 스케일 0.5 m(D13), qr_id 에 스테이션 이름 허용(서버 변경 없음), 실패 코드 `CLASSIFY_FAILED` 추가(S2 구현에 있던 코드). 응답 모양 변경 없음 |
| 1.0.2 | 2026-11-10 | 사용자 요청: 자산 삭제 때 서버 기록·판본·이미지를 보관본으로 남김(9절, 3.2 삭제 설명). 앱 응답 모양 변경 없음 |
| 1.0.1 | 2026-11-10 | Unity C1 질문 반영: 편집 요청 오류 검사 순서(잠금 > 판본), 항상 채워지는 필드 표(4.0), QR 조회에 `can_rate`·`rate_range` 추가. 기존 필드 변경 없음 |
| 1.0.0 | 2026-11-10 | 첫 판. APP_SPEC 4절 대비 바뀐 것: 기기 키를 본문 대신 헤더 `X-Device-Key` 로, `cancel`·`health`·`samples`·미리보기 경로 추가, `failed(RETRY_LIMIT)`, 3D 확인 30분 자동 discard |
