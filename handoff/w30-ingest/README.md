# W30 — ingest 연결 · 두 번째 조건 이미지

## ★ ingest 붙였다 — **수납 성공 (201, `committed_latest: 2`)**

`server/ingest.py`. 스펙(`w30-in/INGEST-API-SPEC.md`)과 파트 이름·순서가 일치한다.

```
① recolor v2 재생성        changed 71 · v2 전체 376청크
② check_v1_payload()       재료 검사 (청크 376)          ← 🔴 보내기 전에
③ POST …/ingest            tar 4,175,141B + slat 439,848B
→ 201 {"asset_id":"v3-moto-b","version":2,"chunks":376,
       "committed_latest":2,"verified":"sha256+byte_length"}
```

🔴 **검사를 진입점 앞에 물렸다.** A5000 도 자기 진입점에 같은 검사를 걸었으니
**양쪽에서 막힌다** — 한쪽이 빠져도 다른 쪽이 잡는다.

⚠️ **recolor 판본의 slat 은 부모 것을 그대로 쓴다.** W30 조사에서 확인한 대로 recolor 는
   slat 을 만들지 않고(언급 0건) 기하도 안 바꾼다(바이트 동일률 89/89 = 100%).
   그래서 v1 의 slat 이 v2 에 대해서도 정본이다 — **새로 만들지 않는다. 만들면 바이트가 갈린다.**

⚠️ tar 는 `mtime=0` 으로 묶는다. 안 그러면 같은 내용이 매번 다른 바이트가 되고,
   `sha256+byte_length` 검증의 재현성이 깨진다.

## 🔴 v2 기반 형태 편집 — **못 했다. A5000 이 내려갔다**

수납 직후 편집을 걸었더니:

```
POST base_version=2 → 200 · [6.0s] 조건 이미지 생성 → [33.0s] failed
★ UPSTREAM_UNREACHABLE — "<EDIT_HOST> 에 못 닿는다 (ConnectError)"
```

이후 `/v2/trellis/health` 도 응답 없음(000). **결정 4 의 마지막 조각은 A5000 이 뜨면
바로 돈다** — 수납이 끝났으니 `409 VERSION_CONFLICT` 는 더 이상 안 난다.

★ 지난 웨이브에 넣은 **연결 실패 분류가 여기서 값을 했다.** 전 같으면 `INTERNAL` 로
  나와 "서버 버그" 로 읽혔을 것이다.

## ★ 두 번째 조건 이미지 — `spiked-cone-rider.png`

| 항목 | 값 |
|---|---|
| 크기 | 1024 × 1024 |
| 머리 | **뾰족한 각진 원뿔 + 길게 휜 뿔 두 개** (아이언맨의 둥근 헬멧과 기하가 완전히 다르다) |
| 배경 | 흰색 단순 · 알파 없음 |
| sha256 | `2a0376817431f42f4d58d8f9…` |

🔴 **오토바이·구도·템플릿을 아이언맨과 똑같이 맞췄다.** 첫 시도에서 다른 템플릿을 써
   차종이 바뀌었는데, **대조군에 머리 외 변수가 끼면 A/B 가 흐려진다.** 다시 만들었다.
   ⇒ 지금 두 이미지의 차이는 **머리 기하 하나뿐**이다.

⚠️ 아이언맨 조건(`handoff/w27e-condition/ironman-rider.png`)의 구도·규격은 **그대로 뒀다.**
