# `unity/` — 앱측 Unity 코드 (계약 아님)

`contract/unity/` 는 **계약**이다 (골든으로 잠겨 있고 맥북 세션만 고친다).
여기는 그 계약을 **쓰는 쪽**이다.

| 파일 | 무엇 | 검증 |
|---|---|---|
| `Runtime/SlatLassoPicker.cs` | 라쏘 호출부 — 복셀 투영(D58) · 교집합 · 지문 | ✅ 헤드리스 골든 |
| `Editor/LassoProbeWindow.cs` | Scene 뷰 마우스 드래그로 그린다 | 🔴 **미검증** (Unity 프로젝트 없음) |
| `Headless/` | 엔진 없이 같은 코드를 돌리는 하네스 | ✅ `server/tests/test_lasso.py` |

## 🔴 LassoVolume 은 재구현하지 않는다

`LassoProbe.csproj` 는 `contract/unity/LassoVolume.cs` 를 **링크로** 컴파일한다 —
복사본이 아니다. 복사하면 드리프트가 생기고, 그러면 "증명된 코드를 썼다" 는 말이
거짓이 된다. `test_headless_harness_links_the_contract_file_instead_of_copying` 가
이 구조를 강제한다.

`SlatLassoPicker` 가 하는 일은 LassoVolume 머리말이 **호출부 책임**이라고 적어 둔
1) 투영 · 3) 교집합·클램프 뿐이다. 판정(PointInPolygon · DominantAxis ·
SolidifyAlongAxis)은 전부 계약 코드를 그대로 부른다.

## 돌리는 법

```bash
# 자산의 slat coords → 하네스 입력
python3 tools/slat_to_probe_json.py coords.npy /tmp/in.json --polygon poly.json

# 판정 (dotnet 8 SDK. Unity 동봉본도 된다)
dotnet run --project unity/Headless -c Release -- /tmp/in.json /tmp/out.json
```

산출물에는 **셀 수 · 지문 · 단계별 계수**가 들어간다. 지문은 계약의
`deltacontract.coords.mask_fingerprint` 와 **같은 값**이어야 한다 — 양쪽이 각자
직렬화하면 어긋나도 예외가 안 나고 "지문 불일치" 로만 보인다.

## 실측 (W17, 합성 자산 3,884복셀)

| 라쏘 | 셀 | 바퀴 | 몸체 | 압출 |
|---|---|---|---|---|
| 몸체만 둘러 그림 | 3,017 | **0 / 844** | 3,017 / 3,040 (99.2%) | +2,592 → 교집합이 2,592 제거 |
| 전부 감쌈 (양성 대조) | 3,884 | 844 / 844 | 3,040 / 3,040 | +2,778 → 2,778 제거 |

★ **바퀴와 몸체의 z 범위는 겹친다** (바퀴 25–35, 몸체 26–47). 어떤 z 밴드도 못 가른다 —
`test_no_z_band_can_separate_the_wheels` 가 전수 탐색으로 증명한다. 라쏘는 갈랐다.

★ 압출이 넣은 셀을 교집합이 **정확히 같은 수만큼** 지웠다. 껍질 표현에서 예상된 것이고
LassoVolume 머리말이 미리 적어 뒀다 — 이번 자산에서 압출은 일을 안 했다.

## W18 — Unity 를 실제로 띄워 대조했다. 🔴 **좌우가 뒤집혀 있었다**

```bash
tools/unity_lasso_check.sh          # 배치모드로 Unity 를 띄우고 골든과 대조
```

임시 프로젝트를 만들고 리포 파일을 **심링크**한다(복사 아님). 실패하면 0 이 아닌
코드로 종료한다 — 로그만 남기면 CI 도 사람도 통과로 읽는다.

### 잡은 것

| | 헤드리스(옛 골든) | Unity |
|---|---|---|
| 투영 / 뒤 / 폴리곤안 | 3,884 / 0 / 3,017 | **같음** |
| 압출후(추가) / 교집합제거 / 축 | 5,609(+2,592) / 2,592 / 1 | **같음** |
| 지문 | `9ad957fb…` | `19ee77cc…` |

**단계별 계수 10항목이 전부 일치하고 지문만 달랐다.** Unity 결과가 옛 골든의
**x 미러(63−x)** 와 바이트 단위로 일치해 원인을 확정했다:

```
Unity 는 왼손 좌표계다.  transform.LookAt →  right = up × fwd
헤드리스는 오른손을 썼다.                    right = fwd × up      ⇒ x 반대
```

**Unity 가 정본이다** (앱이 도는 곳이 거기다). 헤드리스를 고치고 골든을 재생성했다.

⚠️ **개수·계수로는 안 잡힌다.** 자산이 x 대칭이면 셀 수까지 같다. 그래서
`onewheel` 케이스를 넣었다 — 한쪽 바퀴만 잡으므로 좌우가 뒤집히면 **다른 바퀴**가
잡히고, 소속으로 드러난다 (`test_one_wheel_lasso_locks_the_handedness`).

### 상하(원점) 뒤집기는 맞았다

`GuiToScreen`/`ScreenToGui` 를 순수 함수로 분리해 왕복 항등과 "GUI 최상단 y=0 →
화면 y=1920" 을 배치 검사에서 확인했다. 인라인으로 두면 검사할 수 없다.

⚠️ 화면 전체를 감싼 폴리곤(`wide`)은 뒤집어도 같은 것을 잡는다 — 그런 케이스만
있으면 원점 오류를 아무도 못 잡으므로, 배치 검사가 **반전에 민감한 케이스 최소 1건**을
요구한다.

## W19 — 실자산 moto-b (9,150복셀). **뒷바퀴를 뺐다**

```bash
python3 tools/moto_b_cases.py /tmp/cases        # 폴리곤 설계 (설계용 투영은 판정이 아니다)
dotnet run --project unity/Headless -c Release -- /tmp/cases/moto-rear-wheel.json out.json
tools/unity_lasso_check.sh                      # 🔴 정본 게이트 (D65)
python3 tools/lasso_export_mask.py out.json handoff/lasso/moto-b.rear-wheel.mask.json --asset-id v3-moto-b
```

| 라쏘 | 셀 | 자산 대비 | 다리·스윙암(y<46) | 바퀴 원판 밖 |
|---|---|---|---|---|
| **뒷바퀴** (왼쪽을 y=46 에서 자름) | **1,386** | 15.1% | **0** | 44 (3.2%) |
| 대조 — 원판 전체 | 2,310 | 25.2% | **787** | 44 (1.9%) |
| 뒷바퀴 · 25° 사면 | 1,264 | 13.8% | 0 | 90 (7.1%) |

★ **합성차보다 어려운 문제였다.** 접촉대(y 40–47 · z 18–26)에서 다리·스윙암과
바퀴가 **같은 x 범위(28–35)** 를 차지한다 — 깊이로도 안 갈린다. 자산 전체가
**연결성분 1개**라 연결성으로도 안 갈린다. 화면에서 경계를 긋는 것만이 갈랐다.

⚠️ 공짜가 아니다. 왼쪽 호를 포기했다 — 그 대가가 **딸려올 787셀**이었다.

## 🔴 압출(`SolidifyAlongAxis`)은 지금까지 **한 번도 일하지 않았다**

측정 다섯 경우 전부 `solidify_added == intersect_removed` (순증 0):
합성 2,592/2,592 · 2,778/2,778 · 93/93 · 실자산 254/254 · 409/409 · 사면 180/180.

기전이다. **채움 축이 시선 축과 같으면** 채워 넣은 셀이 후보와 (거의) 같은 화면
위치를 갖는다 — 점유돼 있었다면 이미 후보였다. 그래서 교집합이 정확히 같은 수를
지운다. 25° 사면에서도 안 났다: 시차(≈48px)가 폴리곤 반경(≈193px)보다 작았다.

⇒ 압출이 밥값을 하려면 **시차 > 폴리곤 여유**여야 한다. 그 조건은 아직 안 만들어 봤다.

## W21 — `.cbin` 델타를 **오브젝트를 내리지 않고** 씬에 반영했다 (D70)

```bash
python3 tools/build_moto_patch.py /tmp/moto-inplace   # 부모 89청크 + recolor 델타 24
tools/unity_inplace_check.sh /tmp/moto-inplace        # 🔴 정본 게이트
```

**게이트는 절감률이 아니다.** 무엇이 살아남았는가다.

| 검사 | changed/added/removed | Mesh 실제교체 | EntityId 유지 | 재생성 | apply |
|---|---|---|---|---|---|
| ① in-place | 24 / 0 / 0 | **24** | **89/89 (100%)** | 0 | **7.68 ms** |
| ② 음성 대조 (no-op) | 24 / 0 / 0 | **0** | 89/89 (100%) | 0 | 0.00 ms |
| ③ removed (§3-E) | 3 / 0 / **1** | 3 | 88/88 | 0 | 1.24 ms |
| ⑤ 통짜 재생성 대조 | 0 / 89 / **89** | 24 | **0/89** | 89 | 25.21 ms |

🔴 **②가 이 표의 핵심이다.** 아무것도 안 하는 구현이 **EntityId 유지율 100%** 를 받는다.
유지율만 재면 그게 통과한다 — 그래서 "Mesh 가 실제로 바뀐 GameObject 수" 를 같이 잰다.
①은 24, ②는 0 이다. 이 짝이 없으면 지표가 거짓말한다 (방법론 3조).

### removed 가 in-place 품질을 가른다 (DESIGN_INTENT §3-E)

파괴 후 **사전에서도 지운다**. 안 지우면 다음 패치가 이미 파괴된 MeshFilter 에
`ApplyTo` 를 걸고 — Unity 의 가짜 null 때문에 — **예외 없이 아무 일도 안 일어난다.**
③은 파괴 뒤 그 키를 다시 `changed` 로 보내 본다. 사전에 없으므로 새로 만들고,
`UnexpectedChanged = 1` 로 **숫자에 남긴다**. 조용히 넘어가지 않는다.

### 좌표 (D9)

`.cbin` 정점은 복셀 프레임(Z-up)이다. `VoxelToUnity(v) = (v.x, v.z, −v.y)` 하나만 쓴다.
`test_csharp_voxel_to_unity_matches_the_canonical_transform` 이 소스에서 식을 읽어
`frames.VOXEL_TO_GLB` 와 **수치로** 대조한다 — 주석 대조가 아니다.

⚠️ Unity 6.5 는 `GetInstanceID()` 를 폐기했다. `GetEntityId()` 를 쓰고 **int 로 좁히지
않는다** — 좁히면 나중에 서로 다른 오브젝트가 같은 값으로 보이고, 그때 "유지됐다" 가
조용히 거짓이 된다.

⚠️ 기하는 합성 디코더(`occupancy_to_mesh`) 산출이다. **형식·부기·청크 집합은 실물**이고
(89청크·변경 24 = 3090 의 W20 수치와 일치) 기하만 합성이다.
