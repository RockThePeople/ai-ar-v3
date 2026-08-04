# `server/handoff/` — A5000 이 **리포 없이** 돌리는 것

A5000 은 리포를 클론하지 않는다. `scp` 로 파일을 받는다. 그런데 W11 에서
`gate_g2` 와 `provenance.py` 가 A5000 에 없어서 **동등 구현을 직접 만들었다** —
정본이 없으면 각자 만들고, 각자 만들면 갈라진다.

여기 있는 것이 **인계 정본**이다.

## 보내는 쪽 (3090)

```bash
python -m server.handoff.pack --out /tmp/handoff-w12
scp -r /tmp/handoff-w12 <EDIT_HOST>:<dest>/
```

`pack` 이 만드는 것:

| 파일 | 무엇 |
|---|---|
| `slatmask.py` | 마스크 산출기 (D28 · D28-a · D35-a) |
| `gatecheck.py` | 게이트 판정 — 방향(D38) · 바닥값(D33) · halo(D37) |
| `provenance.py` | 인계본 검사 (D27-b) |
| `MANIFEST.json` | 각 파일의 sha256 + 필수 API 목록 + git 커밋 |

## 받는 쪽 (A5000) — **이걸 통과해야 인계 완료다** (D27④)

```bash
cd <dest>/handoff-w12
python verify.py            # ① sha256  ② 필수 API
```

`verify.py` 는 두 겹으로 본다:

1. **바이트 동일성** — `MANIFEST.json` 의 sha256 과 대조
2. **필수 API 존재** — 약속한 심볼이 실제로 있는가

②가 W11 을 잡는 검사다. 그때 **sha256 은 일치했는데 API 는 없었다** —
`require_slat_grid()` · `is_x_symmetric()` · `grid_source=` 셋 다 부재였고,
①만 돌렸으면 통과했을 것이다.

## 의존성

`numpy` 와 `deltacontract` 만. **리포의 다른 모듈을 import 하지 않는다** —
그게 이 디렉터리가 따로 있는 이유다. 그 제약이 깨지면 `test_handoff.py` 가 잡는다.

⚠️ 그래서 `frames.py` 의 상수 일부가 `slatmask.py` 에 **복사**돼 있다. 두 곳에
있는 것은 위험하지만, import 하면 단독 실행이 막힌다. **드리프트는 테스트가 막는다.**
