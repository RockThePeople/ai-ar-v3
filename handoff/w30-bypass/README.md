# W30 — ingest 없이 "recolor 판본 위 형태 편집" 이 되는가

**답: 기하는 된다. 부기가 깨진다.** ingest 는 여전히 필요하되, **급하지 않다.**

## ① recolor 가 slat 을 건드리나 — **안 건드린다. 아예 안 만든다**

`server/pipeline/recolor.py` 에 `slat`·`safetensors` 언급이 **0건**이고, recolor 산출
디렉터리에 safetensors 가 **0개**다. `recolor_chunk()` 는 `mesh.colors` 만 새로 쓰고
positions·indices 는 손대지 않는다 (docstring: "기하 바이트는 손대지 않는다").

```python
new = colors.copy()
new[sel, :3] = rgba[:3]          # 색만
mesh.colors = new
return encode(mesh), n_changed   # positions·indices 는 decode 된 그대로
```

⇒ **v2(recolor of v1)의 slat 은 "v1 과 같다" 가 아니라 "v1 것밖에 없다".**
   앞서 기하 바이트 동일률 89/89 = 100% 로도 확인했다.

## ② 상류가 base_version 을 무엇에 쓰나 — **slat·input 위치 + 판본 검증**

A5000 스펙(`EDIT-API-SPEC.md` · `V1-REQUIRED-FILES.md`) 기준:

| 쓰임 | 근거 |
|---|---|
| `vN/slat.safetensors` 를 연다 | `_load_slat` (ops.py:83) |
| `vN/input.png` 을 연다 | `pipe.get_cond` (ops.py:263) |
| **커밋된 판본 이하인지 검증** | 실측 409 `base_version=2 은 커밋되지 않았다 (committed latest=1)` |
| `to_version = base_version + 1` 로 응답 | 실측 `patch v1→v2` |

⚠️ `prompt` 는 **no-op** 이다 (3.17.0). 실제 입력은 mask + 조건 이미지다.

⇒ **`base_version=1` 제출은 상류가 허용한다** (커밋된 판본이라서). 그러니 클라가 v2 여도
   v1 기준으로 편집을 걸 수 있다 — 상류 쪽 장애물은 없다.

## ③ 부기가 어긋나나 — **어긋난다. 두 군데**

### (가) `to_version` 충돌 — **구조적이다**

`BEditRequest` 에 `to_version` 필드가 **없다**. 상류는 언제나 `base_version + 1` 을 준다.
클라가 이미 v2(recolor)를 갖고 있는데 v1 기준 편집을 걸면 상류가 또 **v2** 를 준다.

실측으로 확인했다 — 내 store 의 v2 가 그 자리에서 **덮였다**:
`{1: ('parent', 376), 2: ('v2', 23)}` — recolor 의 71청크짜리 v2 가 형태 편집의 23청크로 바뀌었다.

⇒ 판본 번호를 3090 이 다시 매기지 않으면 **두 개의 다른 v2** 가 생긴다.

### (나) 색 손실 — **마스크에 달렸다. 일반 보장이 없다**

v1 기준 편집의 changed 청크는 slat 에서 **재디코딩**되므로 v1 색을 갖는다.
그 청크가 recolor 가 칠한 청크와 겹치면 **거기서 색이 원복된다.**

이번 쌍은 겹치지 않는다:

| | 청크 |
|---|---|
| recolor (뒷바퀴 1,386셀) | 72 (실제 patch 71) |
| 형태 편집 (머리 476셀 + halo1) | 49 (실제 changed 26) |
| **겹침** | **0** |

⚠️ **이건 이 두 마스크가 물리적으로 멀어서다** — 뒷바퀴와 머리. 사용자가 **머리를 칠하고
   그 머리를 다시 성형**하면 겹침은 100% 이고 색이 통째로 사라진다. 우회를 일반 규칙으로
   삼으면 그 경우에 조용히 틀린다.

## 결론

| | |
|---|---|
| 기하·slat | **문제 없다** — recolor 는 slat 을 안 만들고 기하를 안 바꾼다 |
| 상류 수용 | **된다** — `base_version=1` 은 커밋된 판본이라 통과한다 |
| 판본 번호 | 🔴 **3090 이 다시 매겨야 한다** (상류는 언제나 base+1) |
| 색 보존 | 🔴 **마스크가 겹치면 잃는다** — 일반 보장 없음 |

⇒ **ingest 는 "형태 편집 연쇄" 와 "겹치는 마스크" 두 경우에만 필요하다.** 지금 사용자
   시나리오(뒷바퀴 색 → 머리 성형)는 우회로 된다. 급하지 않다.

⇒ 우회를 쓰려면 3090 이 둘을 해야 한다:
   ① 상류가 준 `to_version` 을 **믿지 말고** 로컬 최신+1 로 다시 매긴다
   ② changed 청크와 이전 recolor 청크의 **겹침을 계산해, 겹치면 거부하거나 재적용**한다
   ⚠️ 아직 **구현하지 않았다.** 지금 하면 ingest 가 열렸을 때 두 경로가 공존한다 —
      어느 쪽이 도는지 모르게 되는 것이 이 프로젝트가 물린 모양이다. **결정 대기.**
