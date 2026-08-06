# W26f — 한국어 지시 → op · 라우트 테스트 · 자산 HTTP 제공

## ★ 맥북이 기다리는 답: **받아진다**

| asset_id | 청크 | manifest | 청크 GET |
|---|---|---|---|
| **`v3-moto-b`** | 376 | `GET /v2/assets/v3-moto-b/manifest.v1.json` → 200 | 200 · sha256 일치 · v4 |
| **`v3-dragon-c`** | 485 | `GET /v2/assets/v3-dragon-c/manifest.v1.json` → 200 | 200 · sha256 일치 · v4 |

두 자리 인덱스 청크가 각각 180 · 195개 포함된다. 매니페스트의 `contract` 에
`chunk_size 4 · chunk_grid_res 16 · contract_version 4` 가 실려 나간다.

청크 URI 는 매니페스트의 `chunks[key].uri` 를 **그대로** 쓰면 된다 —
손으로 만들지 마라 (`uris.chunk_uri` 가 정본).

### 저장소가 두 종류를 같이 낸다

읽기 루트는 **목록**, 쓰기는 **하나**다:

```
읽기: <ASSET_STORE_ROOT>(런타임 생성물) → 리포 assets/ → ASSET_READ_ROOTS
쓰기: <ASSET_STORE_ROOT> 만            ← 리포에는 아무것도 안 쓴다
```

한 루트만 두면 리포 자산이 통째로 안 보인다 — W26f 에서 실제로 그렇게 됐다
(`모르는 자산이다: 'v3-moto-b'`). 생성물은 한 건에 989청크라 리포에 쌓으면 안 되고,
그렇다고 리포 자산을 잃으면 앱이 못 뜬다. 그래서 읽기만 여러 루트다.

## ★ 한국어 지시가 op 로 뚫린다

`llm.py`(`plan_edit`) 로 op 를 뽑고, 못 하는 op 는 `dispatch.py` 가 **거부**한다.
실측 (라이브 라우트 · 실자산 moto-b):

| 지시 | 결과 |
|---|---|
| `뒷바퀴를 파랑으로` | **succeeded** · changed 71 / removed 0 |
| `바퀴를 빨갛게 칠해줘` | **succeeded** · changed 71 / removed 0 |
| `이 부분을 뾰족하게` | **UNSUPPORTED_OP** — op=`replace_region` · "recolor 경로는 정점 색만 갈아끼운다 (D24) → assemble 또는 VoxHammer 로" |
| `뒷바퀴를 없애줘` | **UNSUPPORTED_OP** — op=`remove` · "청크를 비울 수단이 없다" |

🔴 **이 배선이 진단을 가른다.** 전에는 `이 부분을 뾰족하게` 도 `COLOR_NOT_UNDERSTOOD`
였다 — "색을 못 읽었다" 는 **틀린 진단**이다. 그건 색 문제가 아니라 이 소비자가 못 하는
op 이고, 고칠 사람도 방법도 다르다.

### 색 어휘를 넓혔다 (활용형)

한국어는 활용한다 — `빨강 / 빨간 / 빨갛게 / 붉은` 이 다 같은 색이다. 어간만 넣으면
`빨갛게` 를 놓친다(W26f 실측에서 실제로 놓쳤다). `빨간 차의 바퀴를 파랑으로` 처럼
색이 둘이면 **뒤에 나온 것**을 쓴다 — 앞을 쓰면 배경 묘사에 끌려간다.

⚠️ **이건 어휘표지 이해가 아니다.** 표에 없는 표현은 못 읽고 그때는 거부한다.
   표를 넓힌 것으로 "자연어를 이해한다" 고 말하지 않는다.
⚠️ `COLOR_NOT_UNDERSTOOD` 거부는 **유지한다.** 기본색을 칠하면 무엇을 요청하든 같은
   색이 되는 서버가 되고, 그 서버는 언제나 "성공" 을 보고한다.

## 라우트 자동 테스트 — 11건

`server/tests/test_routes_v2.py`. 상류·GPU·t2i 를 안 쓰고 리포 실물만 읽는다.
**세 건이 실제 결함을 잡았다:**

1. 🔴 `manifest.v99.json` 이 **200** 을 냈다 — 없는 판본에 v1 을 조용히 돌려줬다.
   docstring 에는 "옛 판본으로 대신 답하지 않는다" 고 적어 놓고 **구현이 없었다.**
2. 그 탓에 `base_version=99` 편집도 409 가 아니라 200 이었다.
3. `JobStatus.state` 는 **넷**(`queued·running·succeeded·failed`)이다.
   W26 배선 문서에 다섯(`cancelled` 포함)이라 적었는데 **틀렸다** — 계약을 안 열고
   기억으로 썼다. 문서를 고쳐야 한다.

## 남은 약점 (손대지 않음)

③ 잡 인메모리 (재시작 시 폴링 404 — "실패" 가 아니라 "없다") · 무인증
