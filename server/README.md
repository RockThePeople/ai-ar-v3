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
metrics.py     D5 / D5-a / D5-b 지표 — 효능 · 보존 · 절감
tests/         합성 픽스처 관통 · 음성 대조 2종 · D9 48순열 전수 탐색
```

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
