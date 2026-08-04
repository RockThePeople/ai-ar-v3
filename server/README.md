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

| `op` | 소비자 | GPU | 비고 |
|---|---|---|---|
| `replace_region` | assemble **또는** VoxHammer | assemble ✗ / VoxHammer ✓ | 호박 머리는 assemble 로 달성됨 (W5 육안 통과) |
| `recolor` | **recolor 경로** ([pipeline/recolor.py](pipeline/recolor.py), D24) | ✗ | 복셀 격자 미경유 · **기하 바이트 보존** |
| `add` | 🔴 **VoxHammer 전용** | ✓ | assemble 로 보내면 **조용히 아무 일도 안 일어난다** (D22 ①) |
| `remove` | 미정 | — | 아직 0회 |

🔴 **방어는 소비자 쪽이다.** 소비자는 자기가 처리 못 하는 `op` 를 받으면 **거부**한다.
`add` 를 assemble 로 보내는 것이 가장 위험하다 — 예외도 안 나고 화면도 안 바뀐다.

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
