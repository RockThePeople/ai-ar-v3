# W27③ⓐⓑ — `replace_region` 을 거부에서 dispatch 로

## 배선 — 섰다

```
자연어 → llm.plan_edit → op
   op == recolor          → 3090 로컬 (기하 불변 · GPU 불필요 · D24)
   op ∈ replace_region     → <EDIT_HOST> POST /v2/trellis/edit   ← 이번에 뚫은 경로
        add · remove
```

`dispatch.CONSUMERS` 표가 소비자를 정하고 여기서 다시 쓰지 않는다. **op 를 갈아끼우지
않는다** — 자동 강등은 게이트가 "형태를 바꿨다" 고 적으면서 색만 바꾼 결과를 재게 만든다 (D26).

주고받는 것은 **이미 계약에 있다**:

| | 정본 |
|---|---|
| 보낼 것 | `BEditRequest` — `asset_id` · `base_version` · `prompt` · `mask` · `seed` · `idempotency_key` |
| 받을 것 | `BChunkResponse` — `chunks` · `removed_chunk_ids` · `bookkeeping_affected_chunk_ids` · `stats` |
| 낼 것 | `PatchPackage` (`changed_chunks` / `removed_chunk_ids`) |

받은 바이트는 **저장 직전에 계약 판본을 검증한다**(`contractguard`). 부분 수신은
성공이 아니다 — 하나라도 못 받으면 전부 버린다.

## 🔴 막힌 것 — 상류에 **커밋된 판본이 없다**

실제로 쐈고, 상류가 정확한 이유를 줬다:

```
POST /v2/assets/v3-moto-b/edits  "이 부분을 아이언맨 머리로 바꿔줘"
  → op=replace_region 로 분류 · dispatch 통과 · <EDIT_HOST> 제출
  → 409 VERSION_CONFLICT: "base_version=1 은 커밋되지 않았다 (committed latest=0)"
```

`<EDIT_HOST>` 에 **커밋된 자산이 하나도 없다** (`v3-moto-b` · duck 둘 다 커밋본 청크 404).
생성은 staging 까지만 하고 commit 을 안 밟았다.

커밋 시도도 해 봤다 — `POST /v2/trellis/assets/{id}/commit` 은
`job_id` + `to_version` + `idempotency_key` 를 받는데, **staging 이 이미 사라졌다**:

```
→ 404 NOT_FOUND: "staging 도 버전도 없다: v1 / …"
```

⇒ **VoxHammer 편집의 전제는 "상류에 커밋된 base 판본"** 이다. 지금 그 전제가 없다.
   생성 직후에 commit 을 밟도록 생성 경로를 늘리거나, A5000 이 기존 자산을 커밋해 줘야 한다.

## 🔴 아이언맨 조건 이미지 — **보낼 곳이 없다**

상류 `/v2/trellis/edit` 는 **JSON 만** 받는다. `content-type: application/json`,
필드는 `asset_id` · `base_version` · `prompt` · `mask` · `seed` · `idempotency_key` —
**이미지 슬롯이 없다.** 조건은 텍스트 프롬프트가 전부다.

그래서 **조건 이미지를 만들지 않았다.** 만들어도 계약상 넘길 자리가 없고, 만들어 두면
"조건을 준비했다" 는 착시가 생긴다 — 자리표시를 안 만드는 것과 같은 이유다.

⇒ 셋 중 하나를 정해야 한다:
   ① 텍스트 조건만으로 간다 (지금 계약 그대로)
   ② 이미지 조건이 필요하면 **계약 변경**을 올린다 (`BEditRequest` 에 이미지 파트)
   ③ W11 처럼 계약 밖 수동 경로로 A5000 에 직접 넣는다 (자동화 안 됨)

W11 교훈은 그대로 유효하다 — **조건 이미지 좌표로 자산 마스크를 판단하지 마라.**
마스크는 클라가 준 복셀이 유일한 진실이다.
