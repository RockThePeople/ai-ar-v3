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

## 라이선스

`LICENSE` 를 참조. 골든 벡터는 합성 픽스처(`conformance/fixture.py`)에서 생성됐고
외부 자산을 포함하지 않는다.
