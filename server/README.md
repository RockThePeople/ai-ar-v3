# server/ — S2 순수 로직

**네트워크도 GPU도 쓰지 않는다.** 전부 합성 픽스처로 테스트된다. 그래야 3090 이
자산을 확보하는 동안 맥북이 병렬로 완성할 수 있다 (`docs/PROGRESS.md` rev5 W2).

```
pipeline/
  frames.py    🔴 D9 좌표 규약. voxel = (x, -z, y). GLB 적재가 반드시 통과한다
  voxelize.py  GLB/glTF → 64³ occupancy · occupancy → 합성 메시
  mask.py      bbox 마스크 + halo 팽창   🔴 팽창은 클램프 **뒤에**
  splice.py    contract 의 assemble 래퍼. 스케일 없음 · 정수 평행이동만
  delta.py     부기를 **배치에서** 유도 (diff 금지)
  package.py   .cbin 세트 + manifest. 부기 밖은 **부모 바이트 승계**
llm.py         instruction → 편집 스펙. 단일 호출 · 구조화 출력
               🔴 LLM 은 좌표를 만들지 않는다. 만들면 CoordinateLeak 로 거부
metrics.py     D5 / D5-a / D5-b 지표 — 효능 · 보존 · 절감
tests/         합성 픽스처 관통 · 음성 대조 3종 · D9 48순열 전수 탐색
```

## D26 — `op` → 소비자 매핑표 (**정본**)

`llm.py` 는 `{op, target_prompt, factor}` 만 낸다. **소비자별 분기는 llm.py 밖에 둔다** —
안으로 들어오면 게이트가 LLM 출력에 의존하게 된다.

능력표 정본은 [dispatch.py](dispatch.py) 의 `CONSUMERS` 이고,
[tests/test_dispatch.py](tests/test_dispatch.py) 가 그 표를 통째로 잠근다.

| `op` \ 소비자 | `assemble` | `recolor` | `voxhammer` |
|---|:---:|:---:|:---:|
| `replace_region` | ✅ | ✗ | ✅ |
| `recolor` | 🔴 ✗ | ✅ | 🔴 ✗ |
| `add` | 🔴 ✗ | ✗ | ✅ |
| `remove` | ✅ | ✗ | ✅ |

GPU: `assemble` ✗ · `recolor` ✗ · `voxhammer` ✓

**🔴 표시가 조용한 실패를 내는 조합이다** — 예외도 안 나고 화면도 안 바뀐다:

- **`add` → assemble**: 마스크를 비우고 도너를 정수 이동으로 끼우는 연산이라 없던
  가지를 뻗게 할 수 없다 (D22 ①). 신규 복셀이 0에 가깝게 나오고 "모델이 약하다"로 오독된다.
- **`recolor` → assemble**: `occupancy_to_mesh` 가 정점·면만 내서 **색이 통째로 사라진다**
  (D24 원인 진단). 결과는 "색이 안 바뀌었다"로 보인다.
- **`recolor` → VoxHammer** *(W8 판단)*: 레벨1의 정의는 **기하 불변**인데 VoxHammer는
  재디코딩하므로 기하가 흔들린다 (마스크 밖 IoU 0.853 = 잡음 바닥값 대비 2.10배).
  색만 바꾸자고 그 경로를 타면 **레벨1의 판정 조건 자체가 무너지고** GPU까지 쓴다.

🔴 **방어는 소비자 쪽이다.** `dispatch.check_supported()` 가 예외를 던진다 —
`bool` 을 돌려주지 않는다(검사하고도 무시할 수 있으면 그게 곧 조용한 실패다).
**자동 강등도 없다**: `add` → `replace_region` 으로 내려 주면 게이트가 "레벨2를 했다"고
적으면서 실제로는 레벨1 결과를 잰다.

⚠️ 폴백(키 없음)은 `op` 를 추측하지 않고 항상 `replace_region` 을 낸다. `spec.source`
가 `"fallback"` 이면 **op 를 신뢰하지 마라** — 자연어 경로가 실제로 돈 것이 아니다.

## 색 편집(레벨1)이 왜 별도 경로인가 — D24

S2 관통은 `.cbin → 복셀화 → occupancy_to_mesh → 재인코딩` 인데, **`occupancy_to_mesh`
가 정점·면만 낸다.** 그 경로를 타면 색이 통째로 사라진다. 색은 자산에 실제로 있다
(base `flags=0b0011`) — **버리는 건 우리 경로다.**

`recolor.py` 는 복셀 격자를 아예 거치지 않는다. 그래서 이 경로의 강점은 속도가 아니라
**기하가 바이트 단위로 보존된다**는 것이다 (W6/3090 실측 53/53 · hue 이동 31.3° ·
절감 70.46%). `tests/test_recolor.py` 가 `positions`·`indices` 바이트 동일성을 못박는다.

🔴 이 경로에서 `chunkbin.canonicalize()` 를 **다시 부르면 안 된다.** 정렬 키의
tie-break 가 속성 raw 바이트라서, 색을 바꾼 뒤 재정규화하면 정점 순서가 재배열되고
기하 바이트가 통째로 달라진다 — 유일한 강점이 사라진다.

## 이 코드를 쓸 때 반드시 알아야 할 것

- **GLB 좌표를 변환 없이 복셀 격자에 넣지 마라** (D9). `load_mesh()` 가 기본값으로
  `frames.GLB_TO_VOXEL` 을 적용한다. 항등을 쓰면 IoU 0.19 대가 나오고 **예외는 안 난다**.
- **`inherited_byte_identity` 는 보존의 증거가 아니다** (D5-b). 부기 밖은 부모 바이트를
  물려주니 100% 가 당연하다. 진짜 보존은 `preservation_geometry_distance` 이고,
  그건 **잡음 바닥값 없이는 판정을 거부**한다 (W3-A5000 이 대조군으로 만든다).
- 합성 디코더는 복셀-국소라 마스크 밖 churn 이 **구조적으로 0** 이다. 실자산에서는
  절대 안 나온다 (A5000 실측 4.97배).

기하·인코딩·부기 규칙의 정본은 `contract/python/deltacontract/` 다. 이 패키지는
그것을 **호출**할 뿐 재구현하지 않는다.

```bash
python3 -m pytest server/tests/ -q
```

## 아직 없는 것 — 다음 웨이브

| 것 | 담당 | 근거 |
|---|---|---|
| HTTP 오케스트레이터 (잡 인메모리) | 3090 | PROGRESS §5 S2 |
| DebugView 4분할 (원본/결과/마스크/변경청크) | 3090 + 맥북 프런트 | PROGRESS §5 S2-6 |
| 실자산(눈사람 · 호박) 연결 | 3090 | PROGRESS rev5 W2 |

## 여기 코드를 쓸 때의 규칙

- `contract/` 의 `deltacontract` 를 **임포트해서** 쓴다. 복사·재구현하지 않는다.
- 호스트·포트·경로는 전부 환경변수. `CLAUDE.md` 의 보안 규칙을 따른다.
- 새 의존성을 추가하기 전에 사용자에게 확인받는다 (`requirements.txt`).

## D54 — overflow 부기의 **연결성분 필터** (`pipeline/delta.py`)

편집이 마스크 밖에 만든 신규 복셀도 부기에 들어가야 한다. 안 넣으면 클라이언트가 옛
기하를 들고 남는다. 그런데 runG 실측 overflow **602복셀 중 404 가 전역 리메시 잡음**이고,
전부 넣으면 **80 / 124청크 = 64.5%** 가 델타에 끌려와 절감률이 죽는다.

신호와 잡음을 가르는 것은 **개수가 아니라 연결성**이다 (D29-a 와 같은 논리).

```python
ov = classify_overflow(before_cells, after_cells, mask,
                       noise_max_component=<A5000 이 잰 값>)   # 없으면 예외
bk = derive_bookkeeping_with_overflow(spliced, produced_keys, overflow=ov)
#   부기 = (마스크 + halo) ∪ 신호 overflow 청크
```

| 규칙 | 왜 |
|---|---|
| 문턱 = 잡음 최대 연결성분 + 1, **인자로 받는다** | 한 점으로 정하지 않는다 (D39-a). 값은 A5000 이 잰다 |
| 값이 없으면 `OverflowThresholdUnknown` | 기본값을 주면 그게 곧 한 점 문턱이다 |
| **해시 비교 금지** — 점유(before/after)로 유도 | 재디코딩하면 152/152 청크가 다른 해시를 낸다 |
| 원시 개수(`n_signal_voxels`/`n_noise_voxels`)를 항상 들고 다닌다 | D37 의 교훈 |

**음성 대조**: 고립 복셀만 있는 입력에서 `signal_chunks == []` 이고 부기가 그대로다
(`test_pure_noise_does_not_grow_the_bookkeeping`, `test_noise_only_overflow_leaves_bookkeeping_unchanged`).

## D51 — W10~W13 보존 수치는 **무효** (`metrics.py`)

`torch.isin` 이 행이 아니라 원소 단위라 VoxHammer 보존이 **13 / 8,511 = 0.15%** 였다.
수정 후 **7,608 (89%)**. "마스크 밖 3.48배 초과" 는 누출이 아니라 **보존이 꺼져 있었다**는 뜻이다.

| | 값 | 상태 |
|---|---|---|
| W10 ~ W12 | 리포에 옮기지 않았다 | 🔴 폐기 — 꺼내려 하면 `DiscardedMeasurement` |
| W13 | 0.7753 (13/8,511) | 🔴 폐기 — `excess_ratio` 가 예외를 던진다 |
| runF | 0.2737 → **1.23×** (7,608/8,511) | ✅ 정본 |
| runG | 0.2399 → 계산 **1.08×** / 보고 1.16× | ✅ 정본, ⚠️ 분모 불일치 |

⚠️ runG 의 보고 초과배수 1.16× 는 기록된 바닥값 0.2231 과 맞지 않는다 (계산 1.08×).
1.16 이 나오려면 분모가 **0.2068** 이어야 한다 — 마스크가 W13→W14 로 바뀌면 '마스크 밖'
영역 자체가 달라지므로 바닥값도 **그 마스크로 다시 재야 한다** (D33).
`require_consistent_ratio()` 가 이 불일치를 예외로 올린다. 어느 쪽이 맞는지는 A5000 이 잰다.
