# server/ — S2 순수 로직

**네트워크도 GPU도 쓰지 않는다.** 전부 합성 픽스처로 테스트된다. 그래야 3090 이
자산을 확보하는 동안 맥북이 병렬로 완성할 수 있다 (`docs/PROGRESS.md` rev5 W2).

```
pipeline/
  voxelize.py  GLB/glTF → 64³ occupancy · occupancy → 합성 메시
  mask.py      bbox 마스크 + halo 팽창   🔴 팽창은 클램프 **뒤에**
  splice.py    contract 의 assemble 래퍼. 스케일 없음 · 정수 평행이동만
  delta.py     부기를 **배치에서** 유도 (diff 금지)
  package.py   .cbin 세트 + manifest. 마스크 밖은 **부모 바이트 승계**
metrics.py     D5 지표 — 효능 · 보존 · 절감
tests/         합성 픽스처(구 + 육면체) 관통 + 음성 대조
```

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
