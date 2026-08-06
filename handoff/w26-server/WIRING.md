# W26② — Unity ↔ 서버 배선 계약 (3090)

Unity 가 서버를 부른 적이 **0회**다. 코드 전에 계약을 확정한다.

🔴 **계약을 새로 짓지 않았다.** 네 흐름 전부 `contract/python/deltacontract/schemas.py`
와 `uris.py` 에 이미 있다. 아래는 "무엇을 쓰는가" 의 목록이지 새 제안이 아니다.

---

## 0. 지금 실제로 떠 있는 것 — 실측 (D46: `/healthz` 200 은 증거가 아니다)

`<GEN_HOST>:<PORT>` 에 요청을 쏜 결과:

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/healthz` | **200** |
| GET | `/runs` (DebugView) | **200** |
| GET | `/v2/assets/{id}/slat_coords.v{n}.json` | **200** |
| GET | `/v2/assets` · `/v2/jobs/{id}` · `/v2/assets/{id}/chunks/{key}.v{n}.cbin` | **404** |
| POST | `/v2/assets` · `/v2/assets/{id}/edits` | **404** |

⇒ **라쏘가 쓸 좌표 엔드포인트 하나만 서 있다.** 생성·편집·폴링·청크 전송은 **없다.**
   포트는 `:8083`(DebugView 서버) 하나이고 `:8084` 는 별건이다.

---

## 1. 자연어 생성

| | 정본 |
|---|---|
| 요청 | `GenerateRequest` — `session_id` · `raw_prompt` · `spatial_context` · `seed`(42) |
| 응답 | `JobStatus` (비동기) |
| 완료 시 | `JobStatus.manifest` = `ChunkManifest` |
| 청크 URI | `uris.chunk_uri(asset_id, chunk_key, version)` |

**실측 소요 — 38초** (`a red fire hydrant`, seed 42, 콜드 스타트 포함):

| 단계 | 초 |
|---|---|
| Z-Image + BiRefNet (3090) | 27 |
| TRELLIS 제출 → 청크 저장 (A5000) | 11 |

⇒ **Unity 폴링 상한 제안: 120초** (실측 38초의 3배 + 큐 대기 여유).
   폴링 간격은 2초 — A5000 잡 상태가 그 주기로 바뀐다.
   ⚠️ 한 번 잰 값이다. 프롬프트·해상도·큐 상태에 따라 달라진다.

---

## 2. 편집 — **마스크를 무엇으로 받는가**

`EditRequest` — `session_id` · `base_version` · `raw_prompt` · **`mask`** · `seed` · `idempotency_key`

마스크는 `EditMask` 다. **복셀 좌표 목록**이다 — 청크 목록도, 지문도 아니다:

```json
{ "mode": "voxels",
  "voxels": [[x,y,z], ...],          // SLat 격자 좌표 (0..63)
  "halo_margin_voxels": 2,
  "grid_source": "slat_coords" }     // 🔴 필수. 서버가 채워 넣지 않는다
```

🔴 `grid_source` 는 **생략 불가**다 (D28-a). 스키마가 `mode="voxels"` 에서 없으면
거부한다. 서버가 기본값으로 메우면 잘못된 격자가 침묵으로 정본을 참칭한다.

지문은 **대조용으로 따로** 온다 — `SlatCoordsResponse.fingerprint`(서버가 보낸 좌표)와
`PatchPackage.mask_fingerprint`(편집에 실제로 쓰인 마스크). 마스크 자체를 지문으로
보내지 않는다.

응답 완료 시 `JobStatus.patch` = `PatchPackage`:
`changed_chunks: Dict[str, ChunkEntry]` · **`removed_chunk_ids: List[str]`** ·
`contract: ContractInfo` · `from_version` → `to_version`.

---

## 3. 폴링

`JobStatus.state` ∈ `queued` · `running` · `succeeded` · `failed` — **넷이다.**

🔴 **정정.** 이 문서는 처음에 `cancelled` 를 넣어 다섯이라 적었다. **틀렸다** — 계약을 안 열고 기억으로 썼다. `deltacontract.schemas.JobStatus` 의 Literal 은 넷이고, `server/tests/test_routes_v2.py::test_job_states_are_contract_literals` 가 그것을 계약에서 직접 읽어 잠근다.

⚠️ **클라이언트가 `cancelled` 를 기다리면 영원히 안 온다.** 취소는 지금 계약에 없는 개념이다 — 필요하면 계약 변경으로 올려야 한다 (contract/ 는 맥북 담당).
(+ `progress` 0..1 · `stage` · `stage_detail` · `error` · `error_code`).
생성·편집 **같은 규약**이다.

---

## 4. 청크 전송 · 계약 가드

`uris.py` 가 이미 조립한다 — `chunk_uri` · `slat_coords_uri` · `staging_chunk_uri`.

`ChunkManifest.contract` / `PatchPackage.contract` 가 `ContractInfo` 이고 거기에
**`chunk_size`(4) · `chunk_grid_res`(16) · `contract_version`(4)** 가 이미 들어 있다.
받는 쪽이 `assert_contract_compatible` 로 거부한다. **추가로 실을 필드는 없다.**

---

## 🔴 배선 전에 반드시 풀어야 할 것 — A5000 이 아직 **v3** 를 낸다

이번 생성 실측에서 확인했다:

```
A5000 이 선언한 계약: contract_version 3 · chunk_size 8 · chunk_grid_res 8
첫 청크 헤더 계약판본 바이트: 3
v4 클라이언트로 디코드 → ChunkBinError: 계약 버전 불일치: file=3, local=4
```

⇒ **생성 경로를 그대로 Unity 에 연결하면 화면이 통째로 빈다.** 맥북이 앱에서 겪은
   그 예외와 같은 것이고, 가드는 의도대로 동작하는 것이다.
⇒ 지금 리포의 `assets/` 는 3090 이 v4 로 재분할해 넣은 것이라 앱이 뜬다. 하지만
   **새로 생성하면 다시 v3 다.**

**A5000 에 요청**: `:8082` 의 `deltacontract` 를 v4 로 올려 달라. 그 전까지 생성
엔드포인트는 배선해도 앱에서 쓸 수 없다. (편집 경로도 같다.)
