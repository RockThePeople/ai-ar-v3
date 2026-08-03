# CLAUDE.md — ai-ar-v3

## 이 리포는 무엇인가

자연어로 만든 3D 오브젝트의 **일부만** 화면에서 골라 자연어로 바꾸는 시스템이다.
사용자가 AR 화면에서 라쏘로 영역을 집고 "머리를 할로윈 호박으로" 라고 말하면,
마스크 안쪽만 실제로 바뀌고, 마스크 바깥은 바이트 단위로 불변이며, 네트워크로는
바뀐 청크만 오간다. 세 가지 성질 — **효능(A) · 보존(B) · 절감(C)** — 이 동시에
성립하는 것이 목표다. 오브젝트는 64³ 복셀 그리드를 8³ 청크로 쪼갠 `.cbin` 세트로
표현되고, 편집은 청크 단위 델타로 전송된다. 백본은 TRELLIS 1 (image-large) 하나로
생성·편집을 통일한다.

> **계획의 단일 진실은 [docs/PROGRESS.md](docs/PROGRESS.md) 다.**
> 매 세션의 첫 동작은 그 파일을 읽는 것이고, 마지막 동작은 §6 세션 로그에 결과를
> 적는 것이다. 거기 없는 일은 하지 않는다. 거기 있는 순서를 건너뛰지 않는다.
> 스코프를 넓히려면 코드가 아니라 그 파일을 먼저 고친다.

---

## 기기별 경계 — 각 세션은 남의 담당 파일을 수정하지 않는다

| 세션 | 담당 | 건드리지 않는 것 |
|---|---|---|
| **맥북** (이 리포의 주인) | 리포 구조 · `contract/` · 테스트 · `docs/` · `main` 브랜치 · DebugView 프런트 | GPU 서버 위에서만 도는 코드, 전략 결정 |
| **3090** | 오케스트레이터 · t2i (Z-Image) · DebugView 서버 · 델타 조립 | `contract/` 파일 수정, `main` 직접 푸시 |
| **A5000** | TRELLIS 1 · VoxHammer · 복셀/SLat 추론 · 벤치·로그 | `contract/` 파일 수정, 오케스트레이터 |

경계 규칙:

- **`contract/` 는 맥북 세션만 고친다.** GPU 세션은 읽기만 한다. 계약이 바뀌어야
  한다고 판단되면 코드를 고치지 말고 맥북 세션에 올린다.
- 자기 담당이 아닌 디렉터리의 파일을 수정하지 않는다. 필요하면 요청한다.
- 한 세션 = 한 게이트. 게이트가 닫히면 `/clear` 하고 새 세션.

---

## 개발 명령

```bash
# 최초 1회 — numpy 가 없으면 러너가 import 단계에서 죽는다 (맥북 system python 실측)
python3 -m venv .venv && .venv/bin/pip install numpy "pydantic>=2.0"

# 계약 적합성 — 이 리포에서 가장 중요한 검사
cd contract/conformance && python3 run_conformance.py
#   51 passed / 0 failed / 0 skipped   ← pydantic 있을 때 (게이트 기준)
#   47 passed / 0 failed / 4 skipped   ← pydantic 없을 때 (스키마 4건 skip)
# ↑ 위 venv 를 쓸 때는 python3 대신 ../../.venv/bin/python 으로 부른다.

# C# 미러 필드 대조 (pydantic 불필요)
cd contract/conformance && python3 mirror_check.py

# S2 파이프라인 — 합성 픽스처 관통 + D5 지표. GPU·네트워크·실자산을 쓰지 않는다
python3 -m pytest server/tests/ -q

# pytest 가 있으면 동일한 테스트를 이렇게도 돌린다
python3 -m pytest contract/conformance/

# 비밀 스캔 — 커밋 전 항상
gitleaks detect --source . --no-banner
pre-commit install          # 최초 1회
pre-commit run --all-files
```

의존성: `numpy` 필수, `pydantic` 선택(있어야 51/51). GPU·네트워크·모델 의존 없음.

---

## 보안 규칙 — 커밋 전 매번 (docs/PROGRESS.md §7)

**다음을 코드와 문서에 절대 넣지 않는다:**

- API 키 · 공유 시크릿
- 공인 IP
- 포트 조합
- ngrok URL
- 실제 호스트명
- 홈 경로 (`/Users/...`, `/home/...`)

**전부 환경변수로만 참조한다:**

| 이름 | 뜻 |
|---|---|
| `GEN_HOST` | 생성측(3090) 호스트 |
| `EDIT_HOST` | 편집측(A5000) 호스트 |
| `ASSETGEN_URL` | 자산 생성 서비스 URL |
| `DEBUGVIEW_PORT` | DebugView 바인딩 포트 |
| `T2I_OUT_DIR` | t2i 산출 이미지 디렉터리 |

**문서에는 `<EDIT_HOST>:<PORT>` 형태로만 쓴다.** 실제 값을 예시로도 쓰지 않는다.

- `.env.example` 에는 **키 이름만**. 값은 비워 둔다.
- `.env` 는 `.gitignore` 로 봉쇄돼 있고 gitleaks pre-commit 이 한 번 더 막는다.
- ⚠️ **`ai-ar-v2` 의 `StreamingAssets/blockedit_local_key.txt` 에는 실제 64-hex 키가
  들어 있다. 이 리포로 복사하지 않는다.** 파일명 자체가 `.gitignore` 에 박혀 있다.

---

## 계약 변경 규칙

`contract/` 는 `cbin-delta` 를 **있는 그대로** 편입한 것이다. 골든 벡터 200개가
바이트 단위로 잠겨 있고 conformance 51건이 그것을 지킨다.

1. **`contract/` 변경은 별도 커밋으로 한다.** 다른 변경과 섞지 않는다.
   커밋 메시지는 `contract: ...` 로 시작한다.
2. 계약을 바꾸면 `run_conformance.py` 를 돌려 결과를 커밋 메시지에 적는다.
   골든 벡터가 깨졌다면 왜 깨져도 되는지를 `docs/adr/` 에 남긴 뒤에만 재생성한다.
3. `cbin-delta` 를 **재구현하거나 리팩터링하지 않는다.** 있는 그대로 쓴다.
4. **새 의존성을 추가하기 전에 사용자에게 확인받는다.** 라이브러리 하나가
   GPU 서버 두 대의 환경 재구축을 부르는 프로젝트다.

---

## 방법론 — 이 프로젝트가 여섯 번 물린 것 (contract/FINDINGS.md)

```
대리 지표를 의심해라 — 너무 깨끗한 숫자는 항진명제다
"예외가 안 났다" ≠ "안전하다" — 그 경로가 안 돌았을 수 있다
봉쇄와 효능을 둘 다 재라 — 보존만 재면 아무것도 안 하는 구현이 전부 통과한다
규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다
자기 검사가 자기 환경에서 안 돌면 자기를 보호하지 않는다
```

**테스트 통과는 동작의 증거가 아니다.** "그 코드가 떴는가" 와 "그게 실제로 무엇을
바꿨는가" 는 별개의 사실이다. 육안 산출물이 없으면 `docs/PROGRESS.md` 에
**미검증**으로 적는다. conformance 51/51 은 인코더의 자기 일관성만 잠근다 —
시스템이 동작한다는 증거가 아니다.

---

## 디렉터리

```
docs/          PROGRESS.md (단일 진실) · adr/ (결정 기록)
contract/      cbin-delta 편입분 — 맥북 세션만 수정
  python/      deltacontract 패키지 (인코더/좌표/마스크/청크/assemble)
  unity/       ChunkBin.cs · ChunkContracts.cs · LassoVolume.cs
  conformance/ 골든 벡터 200 · 적합성 51건
server/        오케스트레이터 · DebugView — 아직 비어 있다 (S2 에서 구현)
.github/       CI
```
