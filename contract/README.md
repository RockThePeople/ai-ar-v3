# cbin-delta

**3D 메시의 공간 청크 델타 전송** — 참조 구현.

한 오브젝트를 `64³` 복셀 격자 → `8³` 청크(512슬롯)로 자르고,
편집이 건드린 청크만 전송·교체한다. 나머지는 **부모 바이트를 그대로 승계**한다.

```
편집 전체 재전송   17.22 MiB
청크 델타          2.72 MiB   (16%)      — 마스크 24/152 청크
조립 델타          6.95 MiB   (41%)      — 부기 66/152 청크
```

네트워크·GPU·모델 의존이 **없다.** numpy 만 있으면 돌고, pydantic 은 선택이다.

---

## 무엇이 들어 있나

```
python/deltacontract/
  coords.py      좌표계 변환 · Morton 정렬 · 마스크 팽창 · 마스크 지문
  chunkbin.py    .cbin 인코딩/디코딩 (40바이트 헤더 · magic CBN1)
  partition.py   메시 → 청크 분할
  assemble.py    다른 자산의 일부를 마스크 자리에 끼워넣기
  schemas.py     와이어 스키마 (pydantic, 선택)
  errors.py      오류 코드 13종
  uris.py        청크 URI 조립

unity/
  ChunkBin.cs        .cbin 디코더 (C#)
  ChunkContracts.cs  DTO 미러 — Python 스키마와 필드가 1:1
  LassoVolume.cs     화면 자유곡선(2DOF) → 3D 부피 마스크

conformance/
  test_contract.py   50개 테스트
  run_conformance.py pydantic 없이도 도는 러너
  golden/            골든 벡터 200개 (.cbin) + golden.json
  mirror_check.py    C# 미러 필드 대조 (AST, pydantic 불필요)
```

## 돌려보기

```bash
cd conformance && python3 run_conformance.py
# 47 passed, 0 failed, 4 skipped     (pydantic 없을 때)
# 51 passed, 0 failed, 0 skipped     (pydantic 있을 때)
```

**골든 벡터 200개가 바이트 단위로 잠겨 있다.** 인코더를 고치면 즉시 깨진다.

---

## 좌표계

```
NORMALIZED   [-0.5, 0.5]³      오브젝트 로컬
VOXEL        [0, 64)³          희소 latent 격자
CHUNK        [0, 8)³ = 512     VOXEL // 8      ← 전송·교체 단위
MESH_RES     256               VOXEL × 4       (FlexiCubes 내부 격자)
```

⚠️ **정렬은 Morton(`canonical`)이다.** `sorted()` 를 쓰면 같은 집합이 다른 순서로
나가고, 두 목록을 비교하는 쪽에서 조용히 어긋난다.

⚠️ NORMALIZED 허용 오차는 `1/(2·MESH_RES)`. FlexiCubes 경계 정점은 `[-0.5,0.5]` 를
이만큼 벗어난다(실측 0.26%, 최대 4.14e-4). **버그가 아니므로 클램프하지 마라.**

---

## 델타의 핵심 — 부기(bookkeeping)로 정한다, 해시 비교가 아니다

```python
∀ c ∈ 부기 :  c ∈ changed_chunks  ∨  c ∈ removed_chunk_ids
```

**"아무 데도 안 넣기"는 거부한다** — 빠뜨린 것과 비었다고 알려준 것을 구분할 수 없다.

🔴 **해시 비교로 "무엇이 바뀌었나"를 정하지 마라.** 같은 입력을 다시 디코딩하면
**152/152 청크가 전부 다른 해시**가 나온다(기하 변화는 중앙값 0.0002셀 = 부동소수 잡음).
해시 비교는 절감률 0% 를 낸다.

⇒ 마스크 + halo → 청크 = 이번 연산이 새 바이트를 책임지는 집합.
그 밖은 **부모 바이트를 승계**한다(재디코딩 결과를 쓰지 않는다).

**"나머지가 안 망가진다"를 보장하는 건 청킹이 아니라 승계다.**
청킹은 그걸 할 수 있는 주소 체계를 줄 뿐이다.

---

## 마스크 — 편집과 조립의 판정 기준이 다르다

```
편집   마스크 = 바뀔 자리       → 마스크 밖 변경은 위반
조립   마스크 = 비울 자리만     → 마스크 밖 변경은 정상
                                  (기증자 위치는 offset 이 정한다)
```

**`PatchPackage.op` 로 판별한다.** 호출자 플래그에 의존하면 두 소비자가 각자 틀린다 —
실제로 그랬다.

---

## 조립 — 스케일이 없는 이유

`assemble.py` 는 스케일 인자를 **받지 않는다.**

```
좌표를 2배 하면 이웃이 이웃이 아니게 된다
실측 6-이웃 유지율:  s=1.5 → 50%,  s=2.0 → 0%
디코더가 고립 복셀마다 조각난 표면을 만든다 (렌더가 색종이 조각이 된다)
```

크기는 `donor_crop_fraction`(크롭 비율)으로만 고른다.
배치는 **정수 평행이동만** — 소수 이동은 `rint` 의 half-to-even 때문에 서로 다른
복셀을 한 칸으로 뭉갠다(실측 `+0.5` 에서 4,110 → 914복셀, 78% 소실).

세 규칙 모두 `place_cells()` 가 **예외로 거부**한다. 문서가 아니라 코드로 강제한다.

---

## 🔴 청크 바이트를 받아오는 두 경로 — 손으로 URL 을 만들지 마라

W3/3090 이 여기서 **181개 청크를 전부 404 로 받았다.** 원인은 둘 다 "옆 경로에서
유추했다" 이고, 둘 다 계약 함수를 썼으면 처음부터 안 났다.

### ① 응답 항목에는 `uri` 가 없다 — `chunk_id` 다

같은 "청크 1개" 를 가리키는 모양이 **두 개**이고 필드 이름이 다르다.

| | 쓰는 곳 | 식별 필드 |
|---|---|---|
| `ChunkEntry` | `ChunkManifest.chunks` (Unity 가 받는 매니페스트) | **`uri`** — `/v2/assets/{asset}/chunks/{key}.v{n}.cbin` |
| `BChunkResponse.chunks[]` | A5000 → 3090 잡 결과 | **`chunk_id`** — `"3_1_5"` 문자열. `uri` **없음** |

A5000 실측(2026-08-04) 응답 항목:

```json
{"chunk_id": "0_3_1", "hash": "6f73cb…", "byte_length": 1308,
 "vertex_count": 29, "index_count": 114, "voxel_count": 2, "version": 1}
```

`entry["uri"]` 로 읽으면 `KeyError` 다. 매니페스트 모양을 잡 응답에 기대하지 마라.

### ② 커밋 전 바이트는 staging 에 있고, 그 경로만 접두사가 비대칭이다

`generate`/`edit`/`assemble` 결과는 **커밋 전까지 staging** 에 있다. 그리고
staging 경로는 일반 청크 경로에서 유추할 수 **없다** — 두 군데가 다르다:

```
일반 청크 (커밋 후)   /v2/trellis/assets/{asset}/chunks/{key}.v{n}.cbin
                       └─ 3090↔A5000 은 /v2/trellis 접두사        버전 있음

staging (커밋 전)     /v2/assets/{asset}/staging/{job}/chunks/{key}.cbin
                       └─ **/v2/trellis 가 붙지 않는다**           버전 **없음**
```

버전이 없는 것은 실수가 아니다 — 커밋되지 않았으므로 `v{n}` 이 존재하지 않고,
바이트를 식별하는 것은 `(asset_id, job_id, chunk_key)` 다.

**그래서 함수를 써라.** 두 홉(Unity←3090, 3090←A5000)이 같은 문자열이다.

```python
from deltacontract.uris import chunk_uri, staging_chunk_uri

chunk_uri(asset_id, chunk_key, version)      # 커밋 후
staging_chunk_uri(asset_id, job_id, chunk_key)  # 커밋 전 (ephemeral)
```

`staging_chunk_uri()` 는 `validate_job_id()` 로 traversal 도 같이 막는다. 손으로
f-string 을 쓰면 그 검증까지 건너뛴다.

> **왜 404 가 최악의 실패 모양인가.** 경로가 틀렸는지, 잡이 없는지, 커밋이 안 됐는지,
> 권한이 없는지가 전부 같은 404 로 보인다. 원인이 드러나지 않으므로 **추측이 시작되고**,
> W3 은 그 자리에서 "재생성하면 되겠지" 로 갈 뻔했다 — A5000 은 같은 `asset_id`
> 재생성을 `VersionConflict` 로 거부하므로 그건 자산을 잃는 길이었다.
> 이미 끝난 잡에서 다시 받아오는 것이 정답이다.

---

## 라이선스

`LICENSE` 를 참조. 골든 벡터는 합성 픽스처(`conformance/fixture.py`)에서 생성됐고
외부 자산을 포함하지 않는다.
