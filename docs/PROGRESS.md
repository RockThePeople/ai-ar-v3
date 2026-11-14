# PROGRESS — 국소 3D 편집 시스템 (ai-ar-v3) · **rev72**

> **이 파일이 계획의 단일 진실이다.** 매 세션의 첫 동작은 이것을 읽는 것, 마지막은 §7 에 결과를 적는 것.
> 결정 원문·웨이브 기록·종결 갈래는 `DECISIONS_ARCHIVE.md` 에 있다. 여기는 얇게 유지한다.
> 리포 `github.com/RockThePeople/ai-ar-v3` · 배치 `docs/PROGRESS.md`

> rev72 (10-15): 웨이브별 서술(W25~rev71)을 걷어냈다. 닫힌 웨이브의 경위·수치는 git 이력
> (`e1e815f` 이전 판본)과 `DECISIONS_ARCHIVE.md` 에, VoxHammer·편집 서비스의 살아 있는 사실은
> [handoff/README.md](../handoff/README.md) 에 있다.

---

## 0. 운영 원칙

```
 1. 계획 → 승인 → 구현 → 검증.  코드부터 쓰지 않는다
 2. 한 세션 = 한 게이트.  닫히면 /clear
 3. 실패한 접근을 계속 고치지 않는다.  재시도 상한을 넘으면 되돌린다
 4. "필수적으로 구현하되 최소한으로."  스코프 확장은 이 파일을 고친 뒤에만
 5. 결정은 대화가 아니라 파일에 남긴다
 6. 비밀은 코드·문서·커밋에 절대 넣지 않는다 (§5)
 7. 테스트 통과는 동작의 증거가 아니다.  육안 산출물이 없으면 미검증으로 적는다
 8. 세션 간 의존성을 고려해 스케줄링한다.  보고가 없으면 실행 중으로 간주한다
8-a. 웨이브가 끝나면 웨이브 대장과 세션 로그 둘 다 갱신한다 (ARCHIVE)
 9. Chat 이 이 파일을 고치면 다음 웨이브 발사 전에 리포에 반영한다.
    세션은 리포 판본만 근거로 삼는다 — 프롬프트 본문의 조항 인용은 근거가 아니다
9-a. **PROGRESS 전달은 사용자가 `docs/` 디렉터리째 수동 복사한다** (사용자 확정).
    ⇒ 세션은 리포 경로 **`docs/PROGRESS.md`** 를 읽는다.  git pull 이 실패해도 작업이 안 터진다.
    ⚠️ clone/pull 이 로컬 rev 를 덮을 수 있다 — `M docs/PROGRESS.md` 는 정상이다(로컬이 더 새것).
      **덮였으면 사용자에게 알려라.**  조용히 낡은 판본으로 일하지 마라
    ⚠️ 그러므로 **리포 main 의 rev 와 로컬 rev 가 다른 것은 사고가 아니다.**
      "main 은 rev51 이다" 를 미도달로 단정하지 말고 **로컬 `docs/PROGRESS.md` 를 먼저 열어라**
      (A5000 이 rev50 을 읽고 "rev53 미도달" 이라 단정한 실제 사례)
9-b. 엔드포인트·경로를 적을 때 **`<GEN_HOST>` / `<EDIT_HOST>` 한정사를 반드시 붙인다.**
    한정사를 떼면 다른 호스트를 잰 두 보고가 서로 모순처럼 보인다 (W26b 실제 발생)
9-c. "내 담당이 아니다" 로 보고를 끝내지 마라.  **그 파일이 누구 디스크에 있는지,
    `contract/` 인지**를 적어라.  그 두 줄이 없으면 소유권 공백이 생긴다 (W26b CHUNK_RE)
9-d. 🔴 **종결된 갈래의 산출물은 삭제한다.**  남겨 두면 닫힌 항목이 열린 항목처럼
    웨이브마다 "미확인" 으로 보고된다 (dragon-c — D56 으로 종결된 Dragon 갈래의 자산이
    다섯 웨이브 동안 미확인 줄을 만들었다.  사용자 지시로 폐기)
10. 이 파일도 §5 보안 규칙의 대상이다 (public 리포로 내려간다)
```

**방법론 6조** — 이 프로젝트가 실제로 물린 것

```
1. 대리 지표를 의심하라.  너무 깨끗한 숫자는 항진명제다
2. "예외가 안 났다" ≠ "안전하다".  그 경로가 안 돌았을 수 있다
3. 봉쇄와 효능을 둘 다 재라.  보존만 재면 아무것도 안 하는 구현이 전부 통과한다
4. 규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다
5. 자기 검사가 자기 환경에서 안 돌면 자기를 보호하지 않는다
6. 검사 도구의 거짓 양성이 검사 부재보다 나쁠 수 있다.
   실패를 잡는 테스트만 있으면 전부 실패시키는 도구가 만점을 받는다

★ 문턱을 한 점으로 정하지 마라.  그렇게 만든 문턱에 네 번 물렸고 전부 정상 동작을 탈락시켰다
★ 계산 전에 지표의 맹점을 등록하라.  일곱 번 전부 값을 지켰다
★ 수치(실측)와 원인 진단(추론)을 같은 신뢰도로 다루지 마라.  오진은 문서를 타고 되돌아온다 (D71)
```

**역할 경계**

| 세션 | 담당 | 금지 |
|---|---|---|
| Chat | 설계·게이트 정의·태스크 분해·이 파일 갱신 | 대규모 코드 생성 |
| 맥북 | 리포 주인 · 계약 · 서버 로직 · 테스트 · **Unity** | 전략 결정, 스코프 확장 |
| 3090 | 오케스트레이터 · t2i · DebugView(<DEBUGVIEW_PORT>) · 자산 | 계약 파일 수정 |
| A5000 | TRELLIS(<EDIT_HOST>:<PORT>) · VoxHammer · GPU | 계약 파일 수정 |

---

## 1. 대목표

> **자연어로 만든 3D 오브젝트를 AR 공간에 띄우고, 그 일부를 화면에서 골라 자연어로 바꾸면,
> 바뀐 부분만 실제로 바뀌고 그 자리만 교체된다.**

```
(A) 효능      마스크 안이 지시대로 실제로 변한다
(B) 보존      마스크 밖은 바이트 단위로 불변이다
(C) in-place  오브젝트를 내리지 않고 그 자리만 교체된다
(D) AR        실제 AR 공간에 배치되고, 편집 중에도 앵커가 유지된다
```

✅ **(D) 는 실기에서 성립했다** (W26c · W27②). plane 감지 → 탭 배치 → 앵커 유지가 최종 형태이며,
**"오브젝트를 내리지 않는다"(C)의 의미도 AR 앵커 위에서라야 완전해진다** — 앵커가 리셋되면
사용자에겐 오브젝트가 사라졌다 나타난 것이다.
⚠️ `ai-ar-prototype/AR/ARPlacement.cs` 의 교훈: **자동 폴백을 기본 경로에서 빼라.**
평면이 없을 때 카메라 앞 고정 거리에 놓으면 그건 정의상 **화면에 붙은 물체**다.

🔴 **(C) 는 "절감" 이 아니다 (D70).** 전송량 절감은 부산물이고 목표가 아니다.
실측: `changed 76/152`(절반 변경)에서도 `IN_PLACE=True` · EntityId 152/152 · apply 10.35 ms.
⇒ **절감 0% 여도 in-place 는 성립한다.** 진짜 위험은 **`removed` 청크**(GameObject 파괴)다.

---

## 2. 현재 위치

```
✅ 생성        자연어 → Z-Image → BiRefNet → TRELLIS-image-large → .cbin 청크 (end-to-end · W26e)
✅ 라쏘        실기 S22 에서 slat 격자 위로 동작.  moto-b 뒷바퀴 1,386셀 · 오염 0셀
✅ 앱 화면     곡면·색·복셀 단위 표면 하이라이트 (W25)
✅ AR (D)      평면 윤곽선 · 탭 배치 · ARAnchorManager.AttachAnchor (W26c)
✅ 전송        Unity 가 <GEN_HOST> 에서 HTTP 로 청크 수신 · 376/376 sha256 일치 (W27①)
✅ 편집 왕복   ★ 대목표 (A)(B)(C)(D) 실기에서 숫자로 닫힘 — recolor (W27②)
                (A) 바퀴 파랑  (B) changed 밖 305/305 바이트 동일  (C) 교체 70 · 생성 0 · 파괴 0
                (D) 적용 전후 앵커 델타 −0.37mm / 0.000° · 왕복 약 5.8s
✅ 서버 경로   replace_region → <EDIT_HOST> VoxHammer · 조건 이미지 multipart · ingest 단일 경로
▶ 형태 편집    운전자 머리 → 아이언맨 (W27③) — 국소화 ✅ · 효능 🔴 (아래)
🔴 생성 수신   989청크 직렬 40초대 — 병렬 필요 (W27④)
🔴 멀티유저    0회
```

### ▶ W27③ 형태 편집 — 지금 막힌 자리

```
✅ 경로        "라이더 머리를 아이언맨 머리로" → op=replace_region → 조건 이미지(3090 t2i)
               → <EDIT_HOST> repaint → 청크 델타.  changed 26 · removed 1 (예측 상한 13 안)
✅ 봉쇄        마스크 밖 화소 0 변화
✅ (C) removed≠0  실기 S22 — 교체 26 · 파괴 1 · 노드 376→375 · 삼자 일치(보고·파괴·사전제거 1)
               나머지 EntityId 375 유지 · 앵커 델타 0.00mm / 0.000° (W28①)
✅ ingest      3090 → <EDIT_HOST> 수납 201 · committed_latest 2 (recolor v2).  409 는 더 이상 안 난다
✅ 결정 6 실측 halo=2 에서 순증 +17 (halo=0 은 −104) — 볼륨이 있어야 새 기하가 자란다
🔴 효능        아이언맨으로 안 보인다 — 부피는 바뀌고 정체성은 안 바뀐다 (붉은 화소 1 → 5)

연구 트랙(A5000 단독) 이 그 사이 가른 것 — handoff/README.md §5~§8
  · "inversion 이 구조적으로 조건을 이긴다" 는 철회됐다 (re_init 로 조건이 통째로 나옴)
  · 프리미티브 마스크는 돌출부를 흘리고 흘린 셀은 매 스텝 원본으로 강제 복원된다
    ⇒ 마스크는 점유 셀(+볼륨)로, **3D 마스크를 바꾸면 조건 이미지도 다시 만든다**
  · 같은 자산에서 정체성은 **형태 수준으로는 온다**(각진 헬멧·V자 상체).
    1~2복셀 디테일(페이스플레이트 슬릿)은 64³ 에서 안 온다
  · SLat 재사용(재인코딩 제거)이 보존·시간 모두 낫다 — 서비스 경로에 아직 안 들어갔다

다음  ⓪ ★ 조건 A/B — 아이언맨 vs 뿔 원뿔 (handoff/conditions/).  같은 마스크·halo 로 2런이면
         "조건이 SLAT 에 닿는가 / 한계가 해상도인가" 가 갈린다.  A5000 편집 서비스가 떠 있어야 한다
      ① 서비스 경로의 마스크·조건 생성 규칙을 연구 트랙 규칙에 맞춘다 (handoff/README.md §6)
      ② 효능 판정은 색 렌더가 아니라 **기하 렌더**를 같이 본다 (색만 보면 자세 차이를 놓친다)
      ③ 판정 기준을 먼저 적는다 — "정체성이 왔다" 의 기준이 웨이브마다 달랐다
```

**작업 현황**

```
작업 1 (교체)  눈사람 머리 → 호박    ✅ 육안 인정 · 마스크 밖 바이트 100%
작업 2 (색)    moto-b 뒷바퀴 → 주황   ✅ 기하 100% · in-place 24/0/0 · 육안 인정
작업 3 (형태)  moto-b 운전자 머리 → 아이언맨   ▶ W27③
```

---

## 3. 핵심 결정 — 지금도 유효한 것만

### D74. 앱을 만든다 — 검사 하네스가 아니라

```
판정용   slat coords (N,3) — 라쏘가 투영해 고르는 대상.  화면에 안 그린다
표시용   .cbin 청크 메시   — 사람이 보는 것.  청크 1개 = GameObject 1개
⇒ 두 표현을 다른 프레임에 두면 화면과 판정이 갈린다
기존 프로젝트는 "원리" 만 가져온다 — ChunkBin 디코드→Mesh · changed 만 교체 · removed 는 파괴+사전 제거.
계측 하네스(GoldenIndex · PatchInPlaceHarness · ARChainRunner · REPLAY)는 버리고 개발자 토글 뒤로
```

### 앱 기능 명세 (사용자 원문 · **정본**)

```
· 자연어로 희망 오브젝트를 생성하는 기능
· 오브젝트가 생성되면 편집 기능 사용 가능 (연필 모양 이모티콘으로 on/off 토글)
   · 편집 범위는 화면을 Lasso 로 드래그 → 볼륨을 자동으로 인식
   · 볼륨 마스킹이 된 상태에서 지우개 기능 사용 가능
     (드래그하면 범위가 아니라 지나간 궤적에 해당하는 마스킹이 제거됨), 지우개도 on/off
   · 편집 범위 확정 버튼을 누르면 어떻게 수정을 희망하는지 자연어로 입력받음

🔴 지우개는 궤적 기반(브러시)이다.  라쏘처럼 영역을 그리는 게 아니라
   손가락이 지나간 자리만 지운다.  라쏘 = 영역 추가 · 지우개 = 궤적 제거

구현 세부 (Chat 판단 — 명세에 없는 것.  다르면 정정 요청)
  라쏘 누적    다시 그리면 더한다 (지우개가 빼는 도구이므로 대칭)
  범위 초기화  명세에 없어서 뺐다 — 맥북이 "크게 잘못 잡으면 지우개로 다 지워야 한다" 고 지적.  추가 검토
  표시        선택 셀 수.  지문·계수·REPLAY 는 개발자 토글 뒤로
```

### 표시 방식 (사용자 확정)

```
평소   오브젝트를 완전한 메시 표면으로.  복셀은 안 보인다
선택   그 볼륨이 반투명 / 하이라이트로 보인다
방식   ai-ar-v2 MeshObject.cs 선례 — 월드공간 영역 셰이더 + Texture3D 마스크.
       원본 메시 표면에 직접, 블록 없이.  정점 색보다 해상도가 정확하다
⚠️ 선택된 복셀을 큐브로 겹쳐 그리지 마라 — 큐브가 다시 나타나면 지금 불만의 재발이다
⚠️ 셰이더는 IL2CPP 에서 스트립된다 → Always Included
```

### 🔴 AR 표시·조작 (사용자 확정 · W26 추가)

```
① 감지된 평면을 **테두리(윤곽선)로 하이라이트**한다
   ⚠️ "평면 2개 감지" 라는 글자만으로는 안 된다 — 사용자가 어디를 탭해야 할지 모른다
   ⚠️ **면을 색으로 채우지 마라.** ai-ar-v2 의 접지 평면 색 채우기는 참조 대상이 아니다
      (v2 의 나머지는 좋은 선례다 — 그 부분만 빼라)

② 🔴 **손가락으로 오브젝트를 돌리는 기능을 제거한다**
   오브젝트는 앵커에 고정되고, **사람이 움직여서 다른 각도를 본다.**  그것이 AR 이다
   손가락 회전이 있으면 앵커링이 됐는지 안 됐는지 화면에서 구분이 안 된다 —
   회전을 없애는 것 자체가 (D)의 검증 장치다
   ⇒ 회전이 없어도 반대편이 보이면 앵커링이 진짜다
```

### D75. 청크 격자 8³ → 16³ (완료 · main v4)

```
CHUNK_SIZE 8 → 4 · 청크 슬롯 4,096 · 청크당 복셀 64 · CONTRACT_VERSION 4 · DEFAULT_HALO_VOXELS 2
안전장치  CONTRACT_CONSTANTS 가 매니페스트로 나가고 받는 쪽이 assert_contract_compatible 로 거부
🔴 청크 키 인덱스는 0–15.  정규식을 손으로 쓰지 말고 contract 의 uris.py 를 쓴다
🔴 청크가 작아지면 통째로 비워지기 쉽다 → removed = GameObject 파괴 (D70 의 위험 자리)
```

### 결정 1 — base 판본은 **바이트를 옮겨** 세운다

```
A5000 은 3090 이 서빙하는 바로 그 청크를 받아 커밋한다.  재분할하지 않는다
  재디코딩·재분할은 재현되지 않는다(152/152 해시 불일치) → 승계가 무너진다
slat 은 3090 의 slat_coords 지문과 대조해 같은 물체임을 확인한 뒤에만 쓴다
```

### 결정 2 — 형태 편집에는 **조건 이미지가 필수**다

```
텍스트 조건으로는 원본으로 회귀한다 (D40: 조건 이미지가 프롬프트를 이긴다)
3090 이 t2i 로 "바뀐 뒤 전신" 이미지를 만들어 multipart 로 동봉한다.  Unity→3090 은 자연어 그대로
조건을 못 만들면 거부한다 — 조건 없이 몰래 진행하지 않는다 (양쪽 다)
```

### 결정 5 — `idempotency_key` — 필수화는 보류, 지금은 서버 유도

```
계약 3.27.0 필수화 안은 보류 (docs/adr/0001) — Unity 가 키를 안 보낸다.  넣으면 편집 전부 422
서버가 지어내면 같은 요청 두 번이 다른 슬롯 → 재시도가 GPU 재계산.  규칙과 함께 함수를 준다:
server.editreq.derive_idempotency_key() — 내용 파생 · 클라 키 우선 · 마스크 순서/중복 무관
```

### 결정 6 — 라쏘 마스크(선택)와 편집 볼륨(생성 대상)은 다른 것이다

```
선택 표시·(B) 보존 판정 = 점유 셀 (SlatLassoPicker 3단계 `Cells`)
VoxHammer 에 넘기는 것  = 볼륨 (빈 공간 포함 — 2단계 solid 또는 halo)
근거: 라쏘는 정의상 점유 셀만 담는다 → 빈 공간 0% → 새 기하가 자랄 자리가 없다.
      halo=2 에서 순증이 −104 → +17 로 뒤집혔다.  어깨 침범 1.81%
압출(2단계 solid)만으로는 부족하다 — 534셀·빈 10.9% vs halo=2 2,398셀·빈 80.2% (W28②)
⇒ 서버 halo 를 유지하거나 압출 폭을 키운다
```

### 결정 7·8 — 외부 판본은 **ingest 단일 경로**로 커밋한다

```
POST /v2/trellis/assets/{asset_id}/ingest (manifest · slat · input · chunks) — A5000 이 열었다
recolor 우회는 만들지 않는다 — to_version 이 구조적으로 충돌하고(상류는 언제나 base+1),
같은 자리를 칠하고 성형하면 색이 통째로 사라진다.  두 경로가 공존하면 어느 쪽이 도는지 모른다
3090 의 check_v1_payload() 를 ingest 앞에 물린다
```

### W27 원칙 — 단계를 합치지 마라

```
전송 → 편집 왕복 → 생성 수신 순.  각 단계를 따로 판정한다.  ①이 안 되면 ②③은 원인을 못 가른다
판정은 육안이 아니라 바이트 대조 (APK 사본 SHA256SUMS ↔ HTTP 수신)
완료 판정은 응답 모양으로 — 그리고 **위치까지**: `patch.changed_chunks` / `patch.to_version`
  (최상위에서 찾다가 130초를 헛돌았다)
in-place 는 AR 앵커 위에서 잰다.  고정 화면의 "안 사라졌다" 는 (C) 의 절반이다
```

---

## 4. Plan

```
W24~W26f ☑  청크 v4 · 앱 화면 · AR · 서버 라우트 · 생성 end-to-end  (경위는 ARCHIVE)
W27 ① ☑ 전송 376/376   ② ☑ ★★ 편집 왕복 — (A)(B)(C)(D) 전부 닫힘
    ③ ▶ 운전자 머리 → 아이언맨 (replace_region) — 국소화 ✅ · (C) removed≠0 ✅ · 효능 🔴  (§2)
    ④   생성 수신 — 989청크.  keep-alive 로 16.5ms/청크 → 직렬 16초.  병렬 선결
W28 멀티유저 (M6)
```

**실기로 갈 때 반드시**

```
· ai-ar-v2/Tools/install-and-verify.sh 패턴 — 빌드 → APK 검증 → 설치 → logcat
· 🔴 "빌드 완료" 메시지는 증거가 아니다.  APK 안의 값을 열고(unzip -p … level*),
  진짜 판정은 앱이 실제로 보낸 요청 로그다 (세션 9 발생 → 세션 11 재발)
· Editor 골든과 실기 지문을 대조하라.  카메라 FOV·해상도·aspect 가 달라 경계 셀이 갈릴 수 있고
  예외가 안 나고 조용히 틀린다
· 실기 확정: Galaxy S22 (SM-S901N · Android 14 · arm64 · ARCore 1.54).  INFRA 의 "A37 현행" 은 오기
· 폰 → <GEN_HOST>:<DEBUGVIEW_PORT> 직접 도달은 미확인.  adb reverse 로 평문 HTTP 왕복은 확인됨
```

---

## 5. 🔴 자주 밟는 함정 — 작업 전에 읽는다

```
좌표 (여섯 번 물렸다)
  D9    GLB Y-up ↔ 복셀 Z-up.  voxel = (x, −z, y)
  D9-b  to_glb 말미의 z-up→y-up 회전.  그걸 안 거친 메시는 직접 걸어야 한다
  D28   격자 정본은 slat coords.  표면 복셀화는 진단용이고 마스크 근거가 될 수 없다
  D35   좌우 대칭 자산은 X 반전을 IoU 로 못 가린다 → 마스크를 X 대칭으로
  D64   Unity 는 왼손 좌표계다.  right = up × fwd.  헤드리스가 오른손이면 x 미러.
        ★ 개수·계수 10항목이 전부 일치해도 지문만 틀린다 — 원리적으로 안 잡힌다
  정본  server/pipeline/frames.py

델타·마스크
  D34   .cbin 은 slat coords 를 담지 않는다.  역산하지 마라 (두 번 실패).  이송받는다
  D24-a recolor 에서 canonicalize() 재호출 금지 — 기하 바이트 보존이 조용히 사라진다
  해시 비교로 델타를 정하지 마라 — 재디코딩하면 152/152 해시가 전부 다르다.  점유 비교로
  좌표 스케일 금지 (6-이웃 s=2.0 → 0%) · 소수 평행이동 금지 (+0.5 에서 78% 소실)
  D68   압출(solidify)은 사실상 비활성.  채움 축 = 시선 축이면 채운 셀이 이미 후보다

측정·게이트
  D33   바닥값은 자산별·영역별.  남의 자산 값을 쓰면 3.2배 틀린다
  D37   표본이 작으면 비율 단독 판정 거부.  원시 개수를 비율보다 앞에
  D73   🔴 무채색 자산에서 hue 이동은 무의미하다.  moto-b 뒷바퀴 채도 0.028 →
        검정→주황이 hue 3.6° 로 나온다(문턱 30°).  is_achromatic 이 없었으면
        효능 탈락으로 기록되고 예외는 하나도 안 났다
        ⇒ before 유채색이면 hue · 무채색이면 다른 지표(sat_delta·ΔE 후보).  문턱은 아직 없다
  D19-b 🔴 2D 화소 수로 3D 오염을 가늠하지 마라.  색 렌더에서 146화소 다른데
        3D 셀은 787, 정점은 19,172 다.  투영이 깊이를 뭉갠다 — 5배 이상 과소평가
  D5-a  신규·제거를 쌍으로.  성분은 z범위·x중심을 병기
  D60   산출물만 보는 골든은 중간 단계를 안 지킨다.  단계별 계수까지, 그리고 비대칭 케이스를

운영
  D27-c 웨이브마다 새 브랜치를 판다.  재사용하면 머지 시점 뒤에 푸시한 것이 상대에게 안 보인다
  D27   인계는 명시적 전송.  확인은 원격에서 읽고, 수신 세션이 보고해야 완료.
        브랜치도 마찬가지다 — "리포에 커밋했다" 가 "상대가 볼 수 있다" 를 뜻하지 않는다
  D46   /healthz 200 은 "화면이 뜬다" 가 아니다.  매 웨이브 / /real /runs 응답 코드를 적어라
  D47   DebugView 는 로컬 직접 접속이 정본.  ngrok 아님 (벽시계 과금·5GB/월)
  D48   게이트 표시는 3분류 — 해당 없음 / 미결 / 미도착
  D19   판정 그림: 형태 = 깊이맵 · 색 = before/after 색 렌더 쌍 · 생성 = 깊이맵
        ★ 시선 축을 자산이 정하게 하라 — 긴 축이 시선과 나란하면 부품이 전부 겹친다
  D23   LLM 은 좌표를 만들지 않는다.  {op, target_prompt} 만
  D43   rembg 는 건너뛰지 않는다 — 편집 조건 이미지는 RGB 로 보낸다
```

---

## 6. 보안 — 커밋 전 매번

```
금지   API 키 · 공인 IP · 포트 조합 · 터널 URL · 공유 시크릿 · 실제 호스트명 · 홈 경로 · SSH 포트
방법   전부 환경변수.  GEN_HOST / EDIT_HOST / SSH_PORT / ASSETGEN_URL / DEBUGVIEW_PORT /
       T2I_OUT_DIR / ANTHROPIC_API_KEY / LLM_MODEL
값 보관  ~/.ai-ar-v3-hosts (600) · ~/.env (600).  둘 다 리포 밖
검사   gitleaks pre-commit
경고   ai-ar-v2 의 StreamingAssets/blockedit_local_key.txt 에 실제 키가 있다 — 복사하지 마라
```

---

## 7. 세션 로그 (최근만 · 전체는 ARCHIVE)

| 날짜 | 세션 | 결과 |
|---|---|---|
| 11-12 | 3090 app12 | S10: A5000 잡 폴링 연결 오류(TransportError·본문 없는 5xx) 재시도 1·2·4·8 s 뒤 8 s, 연속 60 s 넘으면 WORKER_UNREACHABLE. A5000 오류 본문 있는 5xx·4xx 는 바로 실패. 접수 POST 는 재시도 안 함(A5000 은 앞 작업 끝나기 전 같은 키를 재생하지 않음). 접수·폴링 회차 기록(실패 포함)·/clients 표 |
| 11-12 | 3090 app13 | S11 `APP_REALSTYLE`(기본 1): 상세화가 {subject_detailed, style, style_words} — 기본 실물형, 양식은 사용자 원문에 있을 때만(style_words 원문 검사). 금지어(figure·toy·miniature·cartoon·chibi·stylized·game asset·figurine·low poly) 1회 재요청 뒤 삭제·기록. T2I 꼬리에서 game asset 삭제(+`{style} style`), 자산 style 열, 인페인팅 문장 + `, {style} style`, 보정에 자산 양식 알림·금지어 재요청. /clients 양식 판정. 끔 = S6 그대로 |
| 11-12 | 3090 app14 | S12 `APP_RELPHRASE`(기본 1): 보정 템플릿 REFINE_SYSTEM_REL — 바뀐 부위를 대상 바로 뒤 관계어(wearing·holding·with)로, 대상 전체 교체는 이름 바꿈, 제거는 관계어 없이 긍정 묘사, 나열 중간 삽입 금지·겹치는 옛 묘사 삭제, 명령문 금지. 문장 첫머리·쉼표 뒤 명령형 동사면 1회 재요청(S11 금지어와 한 번에), 남으면 기록. /clients '보정 템플릿' 제목·명령형 표시. 가짜 보정도 명령문을 붙이지 않음. 끔 = S4–S11 템플릿. 사용자 결정: S11 이전 자산(양식 기록 없음)은 생성 원문의 양식어(cartoon·toy·miniature·chibi·low poly·장난감 같은·만화풍 등)로 양식, 없으면 실물형(장면 문장은 보지 않음) |
| 11-12 | 3090 app15 | 사용자 결정: 기기에서 자산을 삭제해도 A5000 워커 저장소(SLat 등)를 지우지 않는다(`APP_WORKER_DELETE`, 기본 0). 앱 서버 보관(1.0.2 9절)은 그대로. assets.worker_kept, /clients 목록에 'A5000 보관 / 지워짐'. app-api 9절 문구 갱신, 응답 모양 변경 없음 |
| 11-12 | 3090 app16 | S13 `APP_REGIONREFINE`(기본 1): 보정 전에 조건 렌더(재시도 재사용), 라쏘 겹친 이미지·확대·mask_ratio 를 보정에 Claude 이미지 입력으로, 출력에 selected_part·region_fit·region_reason. mask_ratio<0.6 인데 주어가 바뀌면·바뀐 부위가 첫 구에 없으면 같은 1회 재요청. 분류에도 겹친 렌더+2d_edit(원문 재해석 금지). 이미지 거부면 글자만(기록). 기록에는 이미지 base64 대신 크기·sha256. `APP_REJECTRAW`(기본 1): prompt 재시도 보정에 사용자 원문 우선. prompt 재시도 조건 렌더 재사용, 'real-world proportions' 문구 삭제. /clients 라쏘 이미지·selected_part·region_fit |
| 11-12 | 3090 app16b | S13b `APP_INPAINTPROMPT`(기본 1): 보정 출력에 inpaint_prompt(바뀐 부위 먼저·옛 부위 없음·맥락 한 구절·좌우 없음·40단어) — 인페인팅에 쓰고 장면 문장(new_scene_prompt)은 자산에 그대로. selected_part·target·inpaint_prompt 의 좌우·위치 단어는 지시에 없으면 서버가 지움. 분류: 칠하기·영역을 놓쳤다는 말은 region(APP_REGIONREFINE 일 때). /clients 인페인팅 문장·장면 문장 나란히. 앱 API refine 모양 그대로(4필드) |
| 11-12 | 3090 app16c | S13c: 거절 분류 안내문의 좌우·위치 단어도 서버가 지움(APP_REGIONREFINE 일 때, 거절 이유 원문에 좌우 말이 있으면 둠) |
| 11-12 | 3090 app17 | S14 논문 재현 모드: 마커별 `raw_prompt`(qr_flags 표, `server/tools/papermode.py flag`) — 켜진 마커의 편집은 보정·분류 LLM 없이 지시문 원문을 인페인팅 문장으로, 거절 이유 원문을 새 문장으로 seed+1(region 분기 없음), 장면 문장 = 이전 + '; ' + 지시문. 조건 렌더·볼륨·3D 설정은 그대로. `papermode.py copy-sample` 로 샘플 v1 을 새 일반 자산으로 복사해 빈 마커에 묶음. /clients '그대로 모드' 표시. 앱 API 변경 없음. 수정: 그대로 모드는 생성에도(입력 문장 그대로 T2I, 꼬리 없음, T2I seed = 요청 seed, TRELLIS seed 42), `papermode.py generate` 로 서버가 그 마커에서 생성(300자 넘는 논문 문장). 덧붙임: 생성 기록에 배경 제거 전 원본(image.png) 보관·/clients 나란히, 상세화 얼굴 규칙 `APP_FACEHINT`(기본 1) |
| 11-13 | 3090 app18 | S15 app-api 1.0.5: 작업 객체에 `prompts`(input·detailed·style·inpaint·mask_ratio·region_fit·refine_retried, 4.4) 항상. 생성은 상세화 뒤 detailed(T2I 문장 전체)·style 을 넣고 나서 generating/t2i 로. 편집 style 은 작업 생성 때(자산 양식, 없으면 realistic), mask_ratio 는 조건 렌더 뒤, region_fit·refine_retried 는 보정 뒤(품질 재시도 시도는 null), inpaint 는 보낼 때. 그대로 모드는 detailed=input·나머지 null. health app_api 1.0.5. 기존 필드 변경 없음 |
| 11-13 | 3090 app19 | S17 app-api 1.0.6: `GET /app/v1/assets/{asset_id}/history`(3.8) — 그 자산의 생성·편집 작업 전부(created_at 오름차순, 진행 중 포함), 작업 prompts(input·detailed·style·mask_ratio·region_fit), 시도별 refine·inpaint·refine_retried·review·accepted, outcome, error_code. 서버 기록에서만 꺼냄(LLM·계산 없음). 시도별 refine·refine_retried·accepted 저장 추가(작업 객체 응답 그대로). S17 전 작업은 저장된 값에서만(refine 은 마지막 보정과 같은 문장일 때, accepted 는 거절/3D 진행으로). 삭제·없는 자산 404. health 1.0.6 |
| 11-14 | 3090 app22 | S22 이름만으로 정해지는 대상(헬로키티 등) 양면 얼굴: 상세화가 `named_subject`·`subject_name` 도 판정(`APP_NAMED_NODETAIL` 기본 1, 끔이면 요청이 예전과 같음). true 면 T2I 문장 = 영어 이름 + ", full body, one face on the front, plain smooth back of the head"(방식 D, 상세화·얼굴 규칙 문장 안 씀), 양식 꼬리 그대로. 작업 기록 `named`, `/clients` 한 줄. 시험 20회: A 양면 2/4·B 1/4·C 0/4(헬로키티 C 는 머리만)·D 1/4(전신)·E 2/4 → D 확정(사용자). 계약 변경 없음 |
| 11-14 | 3090 app20 | S20 app-api 1.1.0 에이전트 편집(10절): camera·volume_cells 없는 편집 → 시점 4장(캐시, 반영 직후 미리 렌더) → 오케스트레이터(AGENT_MODEL claude-opus-5-5) 상자 → 역투영(`automask.py`) → 마스크 확인 → 보정(기존 Haiku) → 인페인팅 → 채점 4항목 → 되돌아가기(②③ 마스킹·① 보정·④ seed, 최대 3) → 3D → 자동 반영. `agent.py` 제어 루프, events·이미지 경로, history events, `/clients` 표시, 가짜 모드 고정 순서. health 1.1.0 + features agent_edit. `APP_AGENT` 기본 1. 1.0.6 흐름 그대로 |
| 11-14 | 3090 app20b | S20b 진행 보고 한국어: 에이전트 보정만 스키마에 `inpaint_ko`(한국어 한 줄, 비었거나 한글 없으면 같은 호출 안 재요청 1회, 그래도 없으면 "부위 → 새 물건"). 이벤트 "그릴 내용: …" + `events[].detail` = 인페인팅에 보낸 영어 문장 전체(마스크 확인·채점 불합격은 고칠 방법 원문). 그대로 모드 마커 에이전트 편집 = 보정 없이 지시문 그대로("그릴 내용: 지시문 그대로"). 1.0.6 수동 흐름 요청 그대로, health 1.1.0 그대로 |
| 11-14 | 3090 app20d | S20d 자동 마스킹 좌우 규칙: 지시문 좌우를 코드로 판정(오른쪽·왼쪽 = 보는 사람, 오른손 등 몸 부위 = 캐릭터; 애매하면 오케스트레이터 `side`·`side_frame`), 역투영 뒤 앞 시점에서 몸 중심 대비 반대쪽이면 대칭면으로 뒤집음(LLM 없음), 마스크 확인에 기대 위치 문장. 마스크 다시 칠하기 별도 한도 `AGENT_MAX_MASK_RETRIES`=2(`agent.mask_retries`), 되돌아가기는 채점 뒤만 3. 마스킹 effort medium(`AGENT_MASK_EFFORT`). 가짜 모드 `X-Mock-Agent: pass/backtrack/below_bar/mask_retry` |
| 11-14 | 3090 app20e | S20e app-api 1.1.1: 자동 볼륨 마스킹 제거(`APP_AUTOMASK` 기본 0 → camera·volume_cells 없는 요청 400). 라쏘 + `"agent": true` = 에이전트가 라쏘 볼륨 다듬기(stage `mask_refine`): 판단 1회(렌더·라쏘 겹침 2장, keep/old_object/new_space 상자) → 잘라내기(깊이 d₀+T, 원래 물건 상자 안 2T / 옆 keep∪old+3% / 빈 셀 new∪keep+3%, recolor 빈 셀 0) → replace·remove 합치기(26-이웃, old+5%·d₀+2T·라쏘 상자+M). T=clamp(0.10·L,3,16), M=clamp(0.13·L,4,16)(`AGENT_DEPTH_RATIO`·`AGENT_MERGE_RATIO`). 시점 미리 렌더는 자동 마스킹 때만. health 1.1.1 + agent_lasso |
| 11-14 | 3090 app20f | S20f 빠른 시험: 인페인팅을 Gemini 이미지 편집(마스크 없음, 렌더 1장 + 프롬프트)으로 — 토글 `APP_INPAINT_BACKEND=gemini`(기본 local, 에이전트 편집만), `GEMINI_IMAGE_MODEL`(기본 gemini-3-pro-image), REST generateContent(httpx, 새 의존성 없음). 5후보(사용자 + 4시점) 중 보이는 라쏘 점유 셀 최대 시점의 렌더·카메라로 Gemini·3D, 볼륨은 라쏘 U 그대로. 불합격은 원인 무관 프롬프트 다시. 마스크 밖 화소 차이 기록. S20e 브랜치 위. 2.6 이미지 차이로 볼륨 넓히기(`APP_GROW_FROM_IMAGE`, 기본 0): 차이 문턱 0.12 → 작은 조각 제거 → 라쏘 투영과 닿는 성분 = 실루엣 → 카메라 방향, 원래 물건 깊이 ±T 안 빈 셀만, 차이 30% 넘으면 다시 그리기 |
| 11-15 | 3090 app20f-2 | S20f-2: 기본 `gemini-3.1-flash-lite-image`, 입력 1장(≤1024²)·출력 1K·이미지만·후보 1(400 이면 옵션 빼고 다시), 캐시 `data_dir/gemini_cache`(렌더 해시·프롬프트·모델; 시험 `GEMINI_CACHE_LOOSE`), 호출 수·토큰 `calls.jsonl`. 2.6 6번 잔여물 합치기(씨앗 U∩O, 26-이웃, S 안·깊이 ±2T·라쏘 상자 ±M, 씨앗 2배 넘으면 절반 → 안 합침). 정면 반대 방위 가중치 0.5(판단에 user_view_side). replace·remove 프롬프트 "Completely remove …", 채점 원래 물건 남으면 ① 불합격. 시험 `APP_AGENT_NO_COMMIT`(A5000 판본 4시점 미리보기, 앱 판본 그대로) |
| 11-15 | 3090 app20f-3 | S20f-3 · 계약 10.2 1.1.1 보충(S20f): Gemini 일 때 `agent.max_backtracks` 1, stage `view_select`(방향 고르기)·`volume_grow`(볼륨 넓히기), 이미지 키 `views`(5방향)·`grow`(넓힌 윤곽). 8084 가짜 모드는 실제 Gemini 없이 같은 stage·키·고정 문구(방위 90°, +2,140셀) |
| 11-15 | 3090 app21 | S21 app-api 1.2.0 체인 평가(계약 11절): 평가 review·signer_pubkey → ERC-4337 v0.7 UserOp(P256Account 팩토리, Pimlico 가스·후원, EntryPoint getUserOpHash) → 기기 서명·번들러 제출 → 감시(3초, 5분) → confirmed·tx·feedback_index·readFeedback. prepare 는 awaiting·expired·failed. 피드백 파일 ERC-8004 필수 칸 + keccak. 가짜 모드 /mock-bundler 서명 실제 검사. /admin(Register·평가·조정), /app/v1/agent-registration.json. 미세 조정(APP_AGENT_TUNE). 새 의존성 없음(순수 파이썬 keccak·ABI·P-256, 운영자 tx 는 foundry cast). chainId 11155111 아니면 끔 |
| 11-11 | 3090 app11 | S9 `/clients` 입력 전문: LLM 호출마다 요청 본문 전체(system·messages·output_config, 키 없음)·응답 원문(usage·stop_reason) 회차별, 인페인팅 요청·워커 응답 settings, A5000 render/edit/generate 요청·결과 전문, T2I 명령·스크립트 설정. `/clients/api/jobs/<id>` 작업 JSON. WebGL 없어도 페이지가 그려지게 |
| 11-11 | 3090 app10 | S8: prompt 거절 뒤 재보정 new_scene_prompt 가 직전과 같으면(소문자·공백·끝 마침표 정규화) 보정 LLM 1회 더(`refine_changed`, 직전 문장·거절 이유·guidance 전달). 그래도 같으면 진행하고 `refine_unchanged` 기록·/clients 표시 |
| 11-11 | 3090 app9 | S7 잘라 인페인팅(`APP_INPAINT_CROP`, 기본 0 — 사용자 결정: 켬이 확대 조각에 장면을 다시 그리고 R→B): 마스크 상자 ×2(최소 384) 정사각을 1024² 로 키워 워커에 보내고 결과를 원래 자리에 붙임(마스크 밖 = 워커 합성과 같은 흰 배경 RGB, 경계 4 px 안쪽 섞기). numpy+zlib(`server/app/cropinpaint.py`). `/clients` 에 상자·자른 입력/결과 |
| 11-11 | 3090 app7 | S6 상세 프롬프트: 생성 입력을 subject_detailed(물체 + 부품 3–5 + 색·재질)로 상세화(영어도), T2I 고정 꼬리를 논문 X3 형식으로, 편집 new_scene_prompt 상세 유지 + 인페인팅 꼬리 `simple clean 3D game asset style`, 서버 문장 상한 800자. /clients 에 문장 사슬 표시 |
| 11-11 | 3090 app8 | S5b: S5(미리보기 좌우 반전) 되돌림, app-api 1.0.4. V1 은 앱이 `ToUnity`/`ToVoxel` = `(x, z, y)` 로 처리(사용자 결정). 앱 미리보기 = 저장본 바이트 그대로(시험 1건) |
| 11-11 | 3090 app6 | S5 V1 좌우 반전(앱 미리보기만 반전, `APP_PREVIEW_MIRROR`) — S5b 로 되돌림, 서비스에 반영된 적 없음 |
| 11-11 | 3090 app5 | E1 폰 흉내 종단(`server/tools/app_e2e.py`, 공인 주소 경유): 14/14 단계 통과, 장시간 편집 10회 실패 0·메모리 변화 없음. 생성 48 s, 편집 전송→2D 34 s, 승인→3D 39–40 s |
| 11-11 | 3090 app4 | S4: 생성 프롬프트 영어 변환(이미 영어면 그대로, 실패면 원문), 보정 부정 표현 금지(검사·1회 재시도). Haiku 보정 20건 부정어 0(재시도 4, 모두 remove), 지연 1.45→1.56 s |
| 11-10 | 3090 app3 | S3 `/clients`: 삭제 보관(app-api 1.0.2 9절, 앱 API 404 그대로)·활동 기록 목록·상세(3D 전·후 비교·되돌린 결과 archive). 외부 공개·용량 무시(사용자 결정). 실제 데이터 캡처 3장 |
| 11-10 | 3090 app2 | S2 LLM 어댑터(`server/app/llm_adapter.py`): Claude(구조화 출력, effort low)·규칙 기반 폴백, 보정·거절 분류·호출 기록·health llm. 35건 평가 haiku 보정 20/20·op 20/20·분류 14/15, sonnet 20/20·19/20·14/15. 새 코드 `CLASSIFY_FAILED` 는 계약 1.0.1 표에 없음(보고) |
| 11-10 | 3090 app1b | A5000 실제 연결 완료: 생성(T2I 교대 → `/generate`) 55 s · camera 편집 · discard · DELETE 실제 통과, slat 좌표 A4 경로로 응답. 샘플 6개(21 판본) A5000 과 해시 일치 — 판본 간 바뀐 청크를 **해시로** 가른다(A5000 manifest 는 판본마다 전체를 새 판본으로 적음). 가짜 원천 sample-jeep(계약 7절) |
| 11-10 | 3090 app1 | A1 실제 연결: camera 렌더 편집 2건(되돌림·유지) 통과, 전송 → 2D 33–34 s(view 57 s). camera 응답 `view: null` 은 앱 내부 기록에만. A2 경로는 A5000 워커에 아직 없어 생성·discard·삭제는 가짜 워커 시험만 |
| 11-10 | 3090 app1 | 앱 서버 v1(`server/app/`, app-api 1.0.1 · `docs/app-api-v1.md`) — `/app/v1` 전 엔드포인트·SQLite·상태 머신·가짜 모드·디버그 페이지. 시험 23건(가짜 워커 포함), 기존 398 passed·6 failed(test_lasso, 변경 전과 같음). 가짜 모드 종단 18단계·실제 모드 편집 1건(2D 승인 → 3D 유지, view 경로) 통과. contract/ 수정 없음 · 새 의존성 없음 |
| 10-15 | A5000 정리 | 웨이브 산출물 `w*/`·`*-in*/`·웨이브별 handoff 삭제 → 유효 사실을 [handoff/README.md](../handoff/README.md) 로 통합. VoxHammer 89줄 diff 를 `handoff/voxhammer/` 에 보존. **PROGRESS rev72 — 웨이브 서술(W25~rev71) 제거, §2·§3·§4 를 현재 상태와 유효 결정만으로 재작성** · 미머지 브랜치(contract/w27-idem · w30-ingest(w27c·w29 포함) · macbook/w28-triple)를 main 에 병합하고 그 웨이브 폴더도 정리. 조건 이미지 A/B 쌍은 `handoff/conditions/` 로 |
| 08-05 | 맥북 | ✅ **언블록 — main 이 v4.** PR #8·#7 머지. D27-c 를 CLAUDE.md 에 반영 |
| 08-05 | 3090 W24② | 머지 대기로 정확히 멈춤. 계약 판본 표시 — 숫자를 안 박고 비교, 4로 갈아끼워 실제로 켜지는지 확인 |
| 08-05 | 맥북 W24① | ★ **계약 v4 완료 — conformance 53/53.** 낡은 전제 넷 적발. mirror_check 가 rc=0 으로 아무것도 안 하고 있었다 |
| 08-05 | ★사용자 | 청크 8³→16³ 확정. VoxHammer 통일은 기각(recolor 기하 100% 보존을 잃는다) |
| 08-05 | 맥북 W23 | UI 명세 전부 구현 · 궤적 지우개 실측(1,328→1,199). 화면이 큐브인 원인 = **자산이 합성 산출물** |
| 08-05 | ★사용자 | **앱 기능 명세 확정** · W22 중단("구현하려는 것과 거리가 멀다") |
| 08-05 | 맥북 W22 | 실기 터치 라쏘 통과 — 셀 1386 · 지문 골든 동일. S22 확정 |
| 08-05 | ★사용자 | **작업 2 육안 인정** — 뒷바퀴 색 변경 확인 |
| 08-05 | 3090 W21 | in-place 24/0/0 · 보존 100%. 효능은 무채색이라 hue 3.6°(D73) → 육안으로 닫힘 |
| 08-05 | 3090 W20 | ★ 작업 2 닫힘. 기하 바이트 100% · 3D 뷰 9/10 · 자기 색 오진 정정(D71) |
| 08-05 | 맥북 W19 | ★ 라쏘 관문 닫힘. moto-b 뒷바퀴 오염 0셀 |
| 08-05 | 3090 W19 | ★★ 델타 43.2% · 승계 100% → D56 절감 근거 폐기 |
| 08-04 | 맥북 W18 | 🔴 손잡이 오류 발견·수정(D64) — 계수 전부 일치, 지문만 불일치 |
| 08-04 | ★사용자 | **(C) 를 절감 → in-place 로 재정의 (D70)** |

---

## 8. 비목표

```
· 부품 분할(OmniPart/PartCrafter) — 보류.  진단은 맞다(D69 오염 교환비를 없앤다).
  🔴 그러나 둘 다 생성기다 — TRELLIS 뒤에 붙는 게 아니라 TRELLIS 를 대체한다.
  대체하면 VoxHammer 가 떨어져 나가고 편집 엔진 재탐색 = W6~W16 재현이다.
  ⇒ 같은 이득을 싸게: 두 시점 라쏘(교집합).  새 모델·VRAM 불필요
  ⚠️ "청크를 1복셀로" 는 부품 분할과 상충한다 — 마스크가 부품 경계를 따르면
    청크를 잘게 할 이유가 오히려 준다.  그리고 removed 증가로 D70 에 역행한다

· Dragon / VoxHammer 머리 3개 갈래 — D56 으로 종결.  성과·한계는 ARCHIVE 에
  (D51 버그 수정 · 머리 3개 달성 · D55 국소 편집 미성립 = 가장 값진 negative result)
· TRELLIS.2 이전 — 해상도가 병목이 아니었다 (D49-a)
· 멀티뷰 편집→재구성 — 원리만 채택, 파이프라인 보류 (D59)
· 마스크의 voxel 단위 부피 채움 · 겹치는 편집 충돌 해소 · 영속 저장 · 본격 인증
```

---

> 더 필요하면 `DECISIONS_ARCHIVE.md` — D1~D75 원문 · 웨이브 대장 · 세션 로그 전체 ·
> Dragon 갈래 상세 · 모든 실측 수치.
