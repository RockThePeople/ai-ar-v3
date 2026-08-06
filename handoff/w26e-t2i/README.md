# W26e — t2i 배선 · 생성 end-to-end

## 🔴 스테이징 경로가 받는 것 — **`job_id` (j-…)** 다

커밋 전 청크는 staging 에 있고 경로는 `uris.staging_chunk_uri(asset_id, job_id, key)` 다.
가운데 자리는 **잡 id (`j-…`)** 이지 **슬롯 디렉터리명(`gen-…`)이 아니다.**

```
/v2/assets/<asset_id>/staging/j-bdea2dd82125/chunks/0_10_3.cbin   ← 200
/v2/assets/<asset_id>/staging/gen-<slot>/chunks/0_10_3.cbin       ← 404 (전부)
```

`<EDIT_HOST>` 가 처음에 디렉터리명으로 쳐서 509개 전부 404 였다. **W27 에서 Unity 가
같은 자리에 물릴 수 있다** — 경로를 손으로 만들지 말고 계약 함수를 불러라. 접두사가
비대칭이고 버전이 없어서 손으로 만들면 404 만 나온다.

⚠️ 상류의 `BChunkResponse.chunks` 항목은 **`chunk_id`** 를 쓴다. 계약 매니페스트의
`ChunkEntry.uri` 와 모양이 다르다 — W3 에서 181청크 전부 404 났던 자리다.

⚠️ 상류 완료 판정은 `state` 가 아니라 **응답 모양**(`"chunks" in payload and
"to_version" in payload`)으로 한다. `state` 로 판정하면 그 필드를 안 채우는 판본에서
영원히 돈다 — `b_client._poll` · `submit_trellis.py` 와 같은 규약이다.

## t2i 배선 방식 — subprocess (선택이 아니라 제약)

Z-Image 와 BiRefNet 은 **numpy/torch 판본이 충돌**해 한 프로세스에서 임포트하면 죽는다
(W2 실측). 그래서 각자의 conda 파이썬으로 **따로 띄우고 파일로만 주고받는다.**

`server/t2i.py` 는 리포 밖 `_tools/` 의 **이미 도는 두 스크립트를 부른다.** 재구현하지
않았다 — 두 벌이 되면 그때부터 어느 쪽이 진짜인지 매번 확인해야 한다.

경로·환경은 전부 환경변수다 (§6): `T2I_TOOLS_DIR` · `ZIMAGE_PYTHON` · `BIREFNET_PYTHON`
· `T2I_PROMPT_TEMPLATE`. 하나라도 없으면 **`T2I_UNAVAILABLE` 로 멈춘다** — 자리표시
이미지를 만들어 내지 않는다. 빈 이미지를 넘기면 그 뒤 파이프라인이 전부 정상 동작하면서
다른 물체를 만든다.

## 생성 end-to-end 실측 — **50.1초 · 989청크 전부 v4**

`POST /v2/assets` · `"a yellow rubber duck"` · seed 42:

| 단계 | 누적 초 |
|---|---|
| t2i (Z-Image + BiRefNet) | 28.1 |
| 상류 structure → slat → chunk | 40.1 |
| 청크 수신 · **바이트 검증** · 저장 | 50.1 |

`stage_detail`: `청크 989 · 판본 {4: 989}` — 가드가 전 청크를 읽고 판본별로 셌다.
두 자리 인덱스 청크 **683개** 가 포함된다 (`0_10_3` 등) — `<EDIT_HOST>` 의 CHUNK_RE
수정이 이 경로로 확인됐다.

⚠️ W26 의 "38초" 는 **기존 스크립트 경로** 값이다. 새 라우트는 50.1초 — 차이는 청크가
96 → 989 로 늘어난 수신 시간이다 (v4 격자). 폴링 상한 **120초** 는 그대로 유효하다
(실측의 2.4배).

⚠️ 이 한 번의 값이다. 프롬프트·큐 상태에 따라 달라진다.
