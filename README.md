# ai-ar-v3

> 자연어로 만든 3D 오브젝트의 일부를 화면에서 골라 자연어로 바꾸면,
> **바뀐 부분만 실제로 바뀌고 바뀐 바이트만 오간다.**

- 계획의 단일 진실 — **[docs/PROGRESS.md](docs/PROGRESS.md)**
- 세션 규칙 · 기기별 경계 · 보안 규칙 — **[CLAUDE.md](CLAUDE.md)**

## 빠른 시작

```bash
cd contract/conformance && python3 run_conformance.py
```

`numpy` 가 필요하고 `pydantic` 은 선택이다 (있으면 51/51, 없으면 47 passed / 4 skipped).
GPU·네트워크·모델 의존은 없다.

## 구조

| 경로 | 내용 |
|---|---|
| [docs/](docs/) | `PROGRESS.md` (단일 진실) · `INFRA.md` · `DESIGN_INTENT.md` · [adr/](docs/adr/) |
| [contract/](contract/) | `.cbin` 포맷 계약 — `cbin-delta` 를 있는 그대로 편입 |
| [server/](server/) | S2 순수 파이프라인 + D5 지표. HTTP·DebugView 는 아직 없다 |
| [.github/](.github/) | CI (conformance + gitleaks) |

## 현재 위치

`docs/PROGRESS.md` §5 의 **S2 (관통)** — 맥북 담당 순수 로직이 합성 픽스처로 돌고 있다.
효능·보존·절감이 **동시에** 성립하는 것을 처음으로 확인했다 (`server/tests/`).
남은 것은 실자산 연결과 DebugView(3090 담당).
