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
