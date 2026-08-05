"""최근 산출물 목록 — **스캔해서 모은다. 손으로 등록하지 않는다.**

사용자 요청: DebugView 에서 최근 생성·수정 5건을 목록으로 보고 클릭하면 상세를 본다.

────────────────────────────────────────────────────────────────────────
왜 스캔인가
────────────────────────────────────────────────────────────────────────
산출물이 리포 밖 여러 곳에 흩어져 있다 — `ai-ar-v3-assets/` · `ai-ar-data/assets/` ·
`ai-ar-v3/w{N}/` · 그리고 A5000 쪽(3090 에 없다). 등록을 손으로 하게 만들면
등록을 빼먹은 산출물이 "없는 것" 이 되고, 그게 이 프로젝트가 여섯 번 물린 모양이다.

스캔 대상은 **환경변수 목록**으로 받는다 (`RUNS_SCAN_DIRS`, `:` 구분). import 로
묶지 않는다 — 산출물을 만드는 쪽이 무엇을 노출하든 이 모듈은 안 고친다.
`recolor` 자리·W9 자리와 같은 규칙이다.

────────────────────────────────────────────────────────────────────────
🔴 없는 값은 "미도착" 이다. 추정으로 채우지 않는다 (원칙 7)
────────────────────────────────────────────────────────────────────────
`manifest.json` / `judgment.json` / `metrics.json` 중 **있는 것으로** 항목을
구성한다. 없는 필드는 화면에 "미도착" 으로 나간다. 특히:

  · 게이트 판정은 **여기서 계산하지 않는다.** `gate_g2()` 가 정본이고 이 모듈은
    기록된 결과를 **읽기만** 한다. 판정 파일이 없으면 "미도착" 이다 —
    화면이 문턱을 다시 적는 순간 게이트와 화면이 갈라지고, 갈라진 줄 아무도 모른다.
  · A5000 산출물은 지금 3090 에 없다. 추정하지 않고 **"A5000 미인계"** 로 낸다.

────────────────────────────────────────────────────────────────────────
🔴 원시 개수를 비율보다 앞에 둔다 (D37)
────────────────────────────────────────────────────────────────────────
halo 계열은 표본이 45~48복셀이라 비율에 유효숫자가 없다. `metrics.py` 의
`HaloBandMeasurement.describe()` 가 "원시 개수를 **항상** 앞에 둔다" 로 잠가 뒀고,
이 화면도 같은 순서를 쓴다. 비율은 뒤에 "참고" 로 붙인다.

────────────────────────────────────────────────────────────────────────
보안 (§7)
────────────────────────────────────────────────────────────────────────
공개 URL 로 나가는 화면이다. **절대경로·호스트·키를 렌더하지 않는다.**
자산 id 와 스캔 루트 기준 상대경로까지만 낸다. `run_id` 는 경로의 해시라
경로를 되돌릴 수 없고, 라우트가 임의 경로를 열지 못한다.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: A5000 이 낼 판정 형식 (정본). 측정치와 **판정**은 다른 것이고, 지금 인계본에는
#: 측정치만 있다 — `w12-out/judgment.json` 최상위 키는 counts/head_sweep/
#: after_components/efficacy/halo 뿐이고 gate 필드가 **하나도 없다**.
#: 판정은 NOTE 산문에만 있었다. 그래서 화면이 "미도착" 으로 낸다 — 옳은 표시다.
#:
#:   {"gate_g2": {"efficacy": true|false|null,
#:                "preservation": true|false|null,
#:                "saving": true|false|null},
#:    "gate_notes": {"preservation": "눈사람 바닥값 부재 (D33-a)"},   # 미결 사유 (선택)
#:    "op": "add"}
#:
#: 🔴 화면은 이 블록을 **읽기만** 한다. `gate_g2()` 가 정본이고 여기서 다시 계산하지 않는다.
GATE_FORMAT_HINT = "judgment.json 에 gate_g2 · gate_level1 블록이 없다 — 측정치만 있다"

__all__ = [
    "MISSING",
    "NOT_APPLICABLE",
    "UNDECIDED",
    "PENDING_A5000",
    "MODELS",
    "STAGE_PAIR",
    "DRAGON_WAVES",
    "TASK_ASSETS",
    "Run",
    "detail_kind",
    "a5000_dirs",
    "scan_dirs",
    "recent_runs",
]

#: 🔴 게이트 상태는 **세 가지다.** 하나로 묶으면 "빠뜨린 것" 과 "비었다고 알려준 것" 이
#: 구분되지 않는다 — 이 프로젝트의 전칭 규칙이 화면에서 깨지는 자리다.
#:
#:   해당 없음  G2 는 **편집 게이트**다. 생성물에는 애초에 적용되지 않는다 — 미도착이 아니다
#:   미결       판정에 필요한 값이 없어서 못 정한다 (예: 그 자산의 잡음 바닥값 부재 → 보존 미결)
#:   미도착     judgment.json 이 없거나, 있어도 **gate_g2 블록이 없다**
NOT_APPLICABLE = "해당 없음"
UNDECIDED = "미결"

#: 값이 없을 때 화면에 내는 것. 빈 문자열이나 0 으로 채우지 않는다.
MISSING = "미도착"
PENDING_A5000 = "A5000 미인계"

_RECORD_FILES = ("judgment.json", "metrics.json", "manifest.json")
_MAX_DEPTH = 3


def scan_dirs() -> List[Path]:
    """`RUNS_SCAN_DIRS`(`:` 구분) → 존재하는 디렉터리 목록."""
    raw = os.environ.get(
        "RUNS_SCAN_DIRS",
        ":".join(
            [
                str(Path.home() / "ai-ar-v3-assets"),
                str(Path.home() / "ai-ar-data" / "assets"),
                str(Path.home() / "ai-ar-v3-runs"),
            ]
        ),
    )
    dirs = [p for p in (Path(x).expanduser() for x in raw.split(":") if x.strip()) if p.is_dir()]
    for a in a5000_dirs():          # A5000 인계분도 같은 목록에 든다
        if a not in dirs:
            dirs.append(a)
    return dirs


#: A5000 이 육안 인계로 밀어 준 산출물의 **파일명 → 라벨**. 화이트리스트다 —
#: 임의 파일명을 경로로 받지 않는다 (§7, 공개 URL).
DELIVERED = {
    # ── W21 라쏘 교환비(D69)의 나머지 절반 — 오염이 **육안으로** 어떻게 보이는가.
    #    왼쪽부터 편집 전 · 오염 0셀 마스크 · 원판 전체 마스크 · 차이(빨강)다.
    "w21_contamination_compare.png": (
        "★ 오염 대조 — 편집 전 / 오염 0셀(1,386) / 원판 전체(2,310) / 차이(빨강)", True),
    # ── W15 (D51 보존 수정). 판정 대상은 **측면 머리의 주둥이 + 뿔**이라
    #    heads_zoom_runG 가 정본이다. 깊이맵 정본(D19)은 그 다음이다.
    "DELIVER_heads_zoom_runG.png": ("★ runG 측면 머리 — 주둥이 + 뒤로 젖힌 뿔", True),
    "DELIVER_heads_zoom_runF.png": ("runF 측면 머리 — 보존만 고친 단계", False),
    "DELIVER_depth_runG_pair.png": ("runG 편집 전 / 편집 후 (전신)", False),
    # ── W13 (마스크 2D 경로). 수치는 철회됐지만 **그림은 그림이다** — 무엇을
    #    보고 무엇을 놓쳤는지가 다음 작업의 참고다.
    "DELIVER_depth_w13_pair.png": ("W13 편집 전 / 편집 후", True),
    "DELIVER_heads_zoom.png": ("W13 머리 확대 — 뭉툭한 몽둥이", False),
    "DELIVER_depth_w13_4views.png": ("W13 4방향", False),
    "DELIVER_depth_w13_front.png": ("W13 정면 깊이맵", False),
    "mask_overlay.png": ("마스크 오버레이", False),
    "mask_on_render.png": ("렌더 위 마스크", False),
    "2d_mask.png": ("2D 마스크", False),
    # ── W12
    "DELIVER_depth_w12_wide.png": ("편집 전 / 편집 후 (넓게)", True),
    "_paired_4dir.png": ("4방향 — 같은 방위끼리 (위 before / 아래 after)", False),
    "DELIVER_depth_w12.png": ("갈라짐 부위 확대", False),
    "DELIVER_depth_w11_runA_FAILED.png": ("★ 대조 — W11 실패본 (목이 하나로 굵어지기만)", False),
}

#: 🔴 **두 단계 개선을 한 화면에서** 본다. 위가 ①, 아래가 ②.
#: 따로 걸면 "보존을 고쳐서 좋아진 것" 과 "마스크를 고쳐서 좋아진 것" 이 안 갈린다.
#: 둘 다 **전폭**이다 — 판정 대상이 주둥이·뿔 실루엣이라 줄이면 판정을 못 한다.
STAGE_PAIR: Tuple[Tuple[str, str], ...] = (
    ("DELIVER_heads_zoom_runF.png",
     "① runF — 보존 버그(D51)만 고침. 입력 바이트는 W13 과 동일하다"),
    ("DELIVER_heads_zoom_runG.png",
     "② runG — ① + 깊이 여유 마스크(W14) ★ 판정 대상"),
)

#: 🔴 A5000 이 **무효를 선언한** 웨이브. 선언문 원문 (`w15-out/judgment.json`
#: `gate_notes.invalidated`):
#:
#:   "All W10-W13 preservation/halo/overflow numbers were measured with preservation
#:    structurally disabled (13/8511 preserved). They are void."
#:
#: 여기 규칙을 새로 만들지 않는다 — **선언된 것을 옮겨 적을 뿐이다.** 이 표가 없으면
#: 글롭이 주워 온 옛 웨이브의 철회된 숫자가 화면에서 현행 수치와 똑같이 보인다.
#: 이 프로젝트가 여섯 번 물린 그 모양이다 (너무 깨끗한 숫자를 의심하라).
INVALIDATED: Dict[str, str] = {
    w: ("보존이 구조적으로 꺼진 채 측정됐다 (8,511 중 13만 보존) — "
        "보존·halo·overflow 수치가 <b>무효</b>다. A5000 이 W15(D51)에서 선언했다")
    for w in ("w10-out", "w11-out", "w12-out", "w13-out")
}

#: 🔴 Dragon 갈래로 돌린 웨이브. **선언이다** — 추론하지 않는다.
#: `w12-out/judgment.json` 에는 asset_id 가 아예 없어서 내용으로 못 가른다.
#: (자산 이름에 dragon 이 들어가는 경우는 그대로 따로 잡힌다.)
DRAGON_WAVES = {f"w{n}-out" for n in range(10, 16)}

#: 현행 작업의 자산 — **아는 것만 적는다.** 작업 1·3 의 자산은 아직 없다.
#: 모르는 것을 "현행" 으로 넣으면 종결된 갈래가 현행처럼 읽힌다.
TASK_ASSETS = {
    "moto-b": "작업 2 — Black Motorcycle with driver",
}

#: 3D 뷰어에 걸 GLB **화이트리스트**. 임의 파일명을 경로로 받지 않는다 (§7).
MODELS = {
    "dragon-c_before.glb": "편집 전 (dragon-c)",
    "runC.glb": "편집 후 (runC)",
    "runF.glb": "① runF — 보존만 고침 (D51)",
    "runG.glb": "② runG — + 깊이 여유 마스크 ★",
}


def a5000_dirs() -> List[Path]:
    """A5000 인계 디렉터리들. **글롭으로 줍는다 — 손으로 등록하지 않는다.**

    웨이브마다 `w{N}-out/` 이 새로 생긴다. 한 개짜리 환경변수로 두면 새 웨이브가
    올 때마다 사람이 값을 바꿔야 하고, 안 바꾼 웨이브는 화면에서 **없는 것**이 된다.
    W15 가 실제로 그 자리였다 — `A5000_RUNS_DIR` 이 `w12-out` 을 가리키는 동안
    `w15-out` 은 도착해 있는데도 목록에 없었다.
    """
    raw = os.environ.get("A5000_RUNS_GLOB", str(Path.home() / "ai-ar-v3" / "w*-out")).strip()
    if not raw:
        return []
    out: List[Path] = []
    for pat in raw.split(":"):
        pat = pat.strip()
        if not pat:
            continue
        p = Path(pat).expanduser()
        if any(ch in p.name for ch in "*?["):
            out.extend(sorted(q for q in p.parent.glob(p.name) if q.is_dir()))
        elif p.is_dir():
            out.append(p)
    return out


def _load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Run:
    """산출물 한 건. **없는 값은 None 이고 화면이 '미도착' 으로 렌더한다.**"""

    run_id: str
    path: Path
    rel: str                      # 스캔 루트 기준 상대경로 (절대경로는 안 낸다)
    mtime: float
    kind: str                     # generate | edit | recolor | 미상
    asset_id: Optional[str]
    headline: str                 # 종류별 한눈 결과 (원시 개수 우선)
    gate: str                     # 통과/실패/미결/해당 없음/미도착 — **읽기만 한다**
    gate_reason: str = ""         # 왜 그 상태인가. 미결·미도착은 이유 없이 두지 않는다
    gate_detail: Dict[str, Any] = field(default_factory=dict)
    records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pending_reason: Optional[str] = None

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.mtime, timezone.utc).astimezone().strftime(
            "%m-%d %H:%M"
        )

    @property
    def delivered(self) -> Dict[str, str]:
        """A5000 이 렌더해 보낸 그림. **다시 렌더하지 않는다** — 깊이 카메라가 그쪽 것이다."""
        return {n: lbl for n, (lbl, _) in DELIVERED.items() if (self.path / n).is_file()}

    @property
    def delivered_canonical(self) -> Optional[str]:
        for n, (_, canon) in DELIVERED.items():
            if canon and (self.path / n).is_file():
                return n
        return None

    #: 청크에서 조립해 거는 모델의 가상 파일명. 실제 파일은 `_glbcache/` 에 있고
    #: 이 이름은 **라우트 파라미터**로만 쓰인다 — 경로를 파라미터로 받지 않는다 (§7).
    AUTO_BEFORE = "_from_chunks.glb"
    AUTO_AFTER = "_from_chunks_after.glb"

    @property
    def models(self) -> Dict[str, str]:
        """3D 뷰어에 걸 GLB. **깊이맵만으로는 "3D 로 정말 바뀌었나" 에 답이 안 된다.**

        `dragon-c_before.glb` 는 3090 이 dragon-c 의 `.cbin` 청크를 이어 붙여 만든 것이다
        (A5000 에 요청하지 않았다). `.cbin` 은 디코더가 낸 **실제 표면 메시**를 담으므로
        대용물이 아니라 원본 그대로다.

        🔴 D9 — `.cbin` 정점은 복셀 프레임(Z-up) 이고 `runC.glb` 는 GLB(Y-up) 이다.
           그대로 나란히 걸면 before 만 90° 누워 보이고 좌우 비교가 뜻을 잃는다.
           export 때 `frames.VOXEL_TO_GLB` 를 건다 (매직 회전을 쓰지 않는다).
        """
        # ① 인계받은 GLB 가 있으면 그것이 정본이다.
        out = {p.name: MODELS.get(p.name, p.stem) for p in sorted(self.path.glob("*.glb"))}
        if out:
            return out
        # ② 없으면 `.cbin` 에서 **직접 조립한다** — A5000 에 요청하지 않는다.
        #    `.cbin` 은 디코더가 낸 실제 표면 메시라 대용물이 아니다 (W12 선례).
        if self.chunk_dir is not None:
            out[self.AUTO_BEFORE] = ("청크에서 조립 (편집 전)"
                                     if self.after_chunk_dir else "청크에서 조립")
        if self.after_chunk_dir is not None:
            out[self.AUTO_AFTER] = "청크에서 조립 (편집 후)"
        return out

    @property
    def no_3d_reason(self) -> Optional[str]:
        """3D 를 못 거는 이유. 🔴 **조용히 빼지 않는다** — 게이트 3분류와 같은 논리다.

        빼 버리면 "3D 가 없는 산출물" 과 "3D 를 붙이는 걸 빠뜨린 화면" 이 구분되지
        않는다. 그 구분이 안 되면 화면을 믿을 수 없다.
        """
        if self.models:
            return None
        if self.pending_reason:
            return "산출물이 아직 3090 에 없다"
        return "GLB 도 `.cbin` 청크 세트도 없다 — 조립할 재료가 없다"

    def model_path(self, name: str) -> Optional[Path]:
        """가상 이름 → 실제 파일. 화이트리스트 밖이면 None (라우트가 404 를 낸다)."""
        if name not in self.models:
            return None
        if name == self.AUTO_BEFORE and self.chunk_dir is not None:
            from .glbbuild import cached_glb
            return cached_glb(self.chunk_dir, f"{self.path.name}-before")
        if name == self.AUTO_AFTER and self.after_chunk_dir is not None:
            from .glbbuild import cached_glb
            return cached_glb(self.after_chunk_dir, f"{self.path.name}-after")
        p = self.path / name
        return p if p.is_file() else None

    @property
    def contract_version(self) -> Optional[int]:
        """이 산출물이 **선언한** 계약 판본. 없으면 None — 추정하지 않는다."""
        c = (self.records.get("manifest") or {}).get("contract") or {}
        v = c.get("contract_version")
        return int(v) if isinstance(v, int) else None

    @property
    def stale_contract(self) -> Optional[str]:
        """구 계약이면 사유. 🔴 **판본을 손으로 적지 않고 계약 상수에서 읽는다.**

        D75 가 청크 격자를 8³ → 4³ 로 바꾸면서 `CONTRACT_VERSION` 이 3 → 4 가 된다.
        옛 `"3_1_5"` 와 새 `"3_1_5"` 는 **다른 공간의 다른 자리**라, 구 계약 산출물을
        새 계약 자산과 나란히 놓으면 청크 키가 같아 보이면서 전혀 다른 곳을 가리킨다.
        여기 숫자를 박아 두면 계약이 또 올라갈 때 이 표시가 조용히 틀린다 —
        그래서 `deltacontract.CONTRACT_VERSION` 을 그대로 읽는다.

        판본을 **선언하지 않은** 산출물은 "미기록" 이다. 구 계약으로 단정하지 않는다 —
        빠뜨린 것과 옛것은 다른 사실이다.
        """
        from deltacontract import CONTRACT_VERSION  # type: ignore[import-not-found]

        v = self.contract_version
        if v is None:
            return None if not self.chunk_dir else "계약 판본 미기록 — 매니페스트에 없다"
        if v == CONTRACT_VERSION:
            return None
        return (f"구 계약 (v{v}) — 현재는 v{CONTRACT_VERSION} 다. 청크 키가 같아 보여도 "
                "다른 공간의 다른 자리다 (D75). 재분할 전까지 참고 기록으로만 본다")

    @property
    def track(self) -> str:
        """어느 갈래인가 — `current` | `dragon`.

        🔴 Dragon 갈래는 **종결됐다** (D56). 그런데 산출물은 디스크에 그대로 있고
        글롭이 계속 줍는다. 한 표에 섞어 놓으면 종결된 갈래의 런이 현행 작업의
        직전 결과처럼 읽힌다 — 목록은 시간순이지 갈래순이 아니기 때문이다.
        지우지 않고 **갈라서** 보여준다: 지우면 대조군이 없어진다.
        """
        hay = f"{self.asset_id or ''} {self.rel}".lower()
        if self.path.name in DRAGON_WAVES or "dragon" in hay:
            return "dragon"
        return "current" if self.path.name in TASK_ASSETS else "legacy"

    @property
    def invalidated(self) -> Optional[str]:
        """이 산출물의 수치가 **철회됐는가.** 철회 사유를 그대로 낸다."""
        return INVALIDATED.get(self.path.name)

    @property
    def stage_pair(self) -> List[Tuple[str, str]]:
        """두 단계 개선 그림 (①→②). 둘 다 있을 때만 낸다 — 한쪽만이면 대비가 아니다."""
        if all((self.path / n).is_file() for n, _ in STAGE_PAIR):
            return list(STAGE_PAIR)
        return []

    @property
    def note_sections(self) -> List[Tuple[str, List[str]]]:
        """NOTE 에서 **판정에 걸리는 절**을 그대로 낸다. 요약하면 뜻이 바뀐다.

        "주의" 만 찾던 것을 넓혔다 — W15 의 판정 문장은 `## ★ 얼굴은 생겼는가` 에
        산문으로 있고, 불릿이 아니라서 예전 추출기로는 **한 줄도 안 걸렸다.**
        화면에 안 뜨는 판정문은 없는 것과 같다.
        """
        want = ("주의", "얼굴", "한 줄")
        out: List[Tuple[str, List[str]]] = []
        for f in sorted(self.path.glob("NOTE-*.md")):
            txt = f.read_text(encoding="utf-8", errors="replace")
            for block in txt.split("\n## ")[1:]:
                head, _, body = block.partition("\n")
                if not any(w in head for w in want):
                    continue
                lines = [
                    ln.strip().lstrip("- ").strip()
                    for ln in body.splitlines()
                    if ln.strip() and not ln.startswith(("|", "#"))
                ]
                if lines:
                    out.append((head.strip(), lines))
        return out

    @property
    def gate_notes(self) -> Dict[str, str]:
        """`judgment.json` 의 `gate_notes` — A5000 이 게이트 옆에 적어 둔 단서.

        게이트가 "실패" 로 찍히는데 그 옆의 단서가 화면에 없으면, 화면이 사실의
        절반만 보여주게 된다. **판정은 안 바꾸고 단서만 같이 낸다.**
        """
        n = (self.records.get("judgment") or {}).get("gate_notes") or {}
        return {k: str(v) for k, v in n.items() if isinstance(v, (str, int, float))}

    @property
    def component_sets(self) -> List[Tuple[str, List[Dict[str, Any]]]]:
        """(런 이름, 성분 목록). 한 파일에 런이 여럿이면 **각각** 낸다 (D37 원시 개수)."""
        j = self.records.get("judgment") or {}
        top = j.get("after_components")
        if isinstance(top, list) and top:
            return [("", top)]
        out = []
        for name, blk in _runs_block(j).items():
            comps = (blk or {}).get("components")
            if isinstance(comps, list) and comps:
                out.append((name, comps))
        return out

    @property
    def after_chunk_dir(self) -> Optional[Path]:
        """recolor 의 **편집 후** 청크. before 만 보여주면 색 변경이 안 보인다."""
        for name in ("chunks_level1", "chunks_after", "recolor"):
            d = self.path / name
            if d.is_dir() and any(d.glob("*.cbin")):
                return d
        return None

    @property
    def chunk_dir(self) -> Optional[Path]:
        d = self.path / "chunks"
        return d if d.is_dir() and any(d.glob("*.cbin")) else None


def _kind_of(rec: Dict[str, Dict[str, Any]], path: Path) -> str:
    """종류 추론. **모르면 '미상' 이다** — 그럴듯한 기본값을 고르지 않는다."""
    # 🔴 `op` 가 인계 그림보다 **우선**이다. 그림은 그 런에 무엇이 붙어 있는지를
    #    말할 뿐이고, 무엇을 한 편집인지는 판정 파일이 말한다. W21 에서 실제로
    #    뒤집혔다 — recolor 런에 대조 그림을 붙였더니 종류가 edit 가 됐다.
    j = rec.get("judgment") or {}
    for key in ("kind", "op", "edit_kind"):
        v = j.get(key)
        if isinstance(v, str):
            if v in ("recolor",):
                return "recolor"
            if v in ("add", "remove", "replace_region", "edit"):
                return "edit"
            if v in ("generate", "create"):
                return "generate"
    if any((path / n).is_file() for n in DELIVERED):
        return "edit"
    if (path / "chunks_level1").is_dir() or (path / "recolor").is_dir():
        return "recolor"
    m = rec.get("manifest") or {}
    if m.get("a5000_job_id") and m.get("is_initial") is not False:
        return "generate"
    if m:
        return "generate"
    return "미상"


def _fmt_headline(kind: str, rec: Dict[str, Dict[str, Any]]) -> str:
    """한눈 결과. 🔴 **원시 개수가 비율보다 앞이다** (D37).

    비율만 크게 띄우면 halo 계열(표본 45~48복셀)에서 유효숫자 없는 숫자가
    권위를 갖는다. 개수를 먼저 쓰고 비율은 "참고" 로 붙인다.
    """
    met = rec.get("metrics") or {}
    man = rec.get("manifest") or {}
    j = rec.get("judgment") or {}
    src = {**met, **j}

    def g(*names):
        for n in names:
            if n in src and src[n] is not None:
                return src[n]
        return None

    parts: List[str] = []
    if kind == "edit":
        # A5000 judgment 형식: {"efficacy": {new, removed, ...}, "after_components": [...]}
        # 런이 여럿이면 **마지막 런**(가장 최근 개선분)을 한눈에 쓰고 나머지 수를 붙인다.
        # 전부 늘어놓으면 목록 한 줄에 안 들어가고, 하나만 쓰고 말하지 않으면
        # 다른 런이 없는 것이 된다.
        nested = _runs_block(rec.get("judgment") or {})
        if nested:
            name, blk = list(nested.items())[-1]
            g = blk.get("gate_g2") or {}
            bits = [name.split("_")[0]]
            if blk.get("head_count") is not None:
                bits.append(f"머리 {blk['head_count']}개")
            if g.get("new") is not None or g.get("removed") is not None:
                bits.append(f"신규 {g.get('new', MISSING)} / 제거 {g.get('removed', MISSING)}")
            if g.get("largest_cc_frac") is not None:
                bits.append(f"참고 최대성분 {g['largest_cc_frac']:.3f}")
            if len(nested) > 1:
                bits.append(f"외 {len(nested) - 1}런")
            return " · ".join(bits)

        eff = (rec.get("judgment") or {}).get("efficacy") or {}
        comps = (rec.get("judgment") or {}).get("after_components") or []
        if eff or comps:
            if comps:
                parts.append(f"머리 {len(comps)}개")
            if eff:
                parts.append(f"신규 {eff.get('new', MISSING)} / 제거 {eff.get('removed', MISSING)}")
                if eff.get("largest_cc_frac") is not None:
                    parts.append(f"참고 최대성분 {eff['largest_cc_frac']:.3f}")
            return " · ".join(parts)
        new, rm = g("efficacy_new_voxels", "n_new"), g("efficacy_removed_voxels", "n_removed")
        heads = g("head_count", "n_heads")
        if heads is not None:
            parts.append(f"머리 {heads}개")
        if new is not None or rm is not None:
            parts.append(f"신규 {new if new is not None else MISSING}"
                         f" / 제거 {rm if rm is not None else MISSING}")
        save = g("transfer_saving")
        if save is not None:
            parts.append(f"참고 절감 {float(save) * 100:.1f}%")
    elif kind == "recolor":
        # D72 형식 — 원시 개수를 앞에 둔다 (D37). hue 는 무채색 원본에서 뜻이 없어서
        # 뒤에 붙이고, 그 사실(`is_achromatic`)을 같이 적는다.
        ed = (rec.get("judgment") or {}).get("efficacy_detail")
        ip = (rec.get("judgment") or {}).get("in_place_detail")
        if ed is not None and ip is not None:
            parts.append(f"변경 청크 {ip.get('changed')}")
            parts.append(f"정점 {ed.get('after', {}).get('n_vertices', MISSING):,}"
                         if isinstance(ed.get("after", {}).get("n_vertices"), int)
                         else f"정점 {MISSING}")
            if ed.get("is_achromatic"):
                parts.append(f"무채색 원본 — hue {ed.get('hue_shift_deg')}° 는 무의미")
            else:
                parts.append(f"hue {ed.get('hue_shift_deg')}°")
            return " · ".join(parts)
        hue = g("hue_shift_deg", "hue_shift", "mean_hue_shift")
        chg = g("changed_chunks", "n_changed")
        if chg is not None:
            parts.append(f"변경 청크 {chg}")
        parts.append(f"hue 이동 {float(hue):.1f}°" if hue is not None else f"hue 이동 {MISSING}")
        save = g("transfer_saving")
        if save is not None:
            parts.append(f"참고 절감 {float(save) * 100:.1f}%")
    elif kind == "generate":
        cc = man.get("chunk_count")
        vc = man.get("voxel_count_total")
        parts.append(f"청크 {cc}" if cc is not None else f"청크 {MISSING}")
        parts.append(f"복셀 {vc:,}" if isinstance(vc, int) else f"복셀 {MISSING}")
    return " · ".join(parts) if parts else MISSING


def _runs_block(j: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """`judgment["runs"]` — **한 파일에 런이 여럿**일 수 있다 (W15: runF·runG).

    W12 까지는 디렉터리 하나 = 런 하나였다. W15 인계본은 같은 자산에 대한 두 실행을
    한 파일에 담는다 — 그게 이번 인계의 요지(①보존 수정 → ②마스크 개선)라서다.
    최상위만 보던 코드는 이 형식에서 게이트를 **못 찾고 "미도착" 으로 낸다.**
    """
    runs = j.get("runs")
    return {k: v for k, v in runs.items() if isinstance(v, dict)} if isinstance(runs, dict) else {}


def _gate_blocks(j: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """{런 이름: gate_g2}. 최상위 우선, 없으면 런별로 모은다."""
    top = j.get("gate_g2") or j.get("gate_level1") or j.get("gate")
    if isinstance(top, dict) and top:
        return {"": top}
    out = {}
    for name, blk in _runs_block(j).items():
        g = blk.get("gate_g2") or blk.get("gate_level1") or blk.get("gate")
        if isinstance(g, dict) and g:
            out[name] = g
    return out


def _status_of(gate: Dict[str, Any], notes: Dict[str, Any]) -> Tuple[str, str]:
    """게이트 블록 하나 → (상태, 사유). **여기서 문턱을 다시 적지 않는다.**"""
    vals = {k: v for k, v in gate.items() if isinstance(v, bool) or v is None}
    if any(v is False for v in vals.values()):
        return "실패", "미달: " + ", ".join(k for k, v in vals.items() if v is False)
    undecided = [k for k, v in vals.items() if v is None]
    if undecided:
        why = "; ".join(f"{k} — {notes[k]}" if k in notes else k for k in undecided)
        return UNDECIDED, f"판정에 필요한 값이 없다: {why}"
    if vals and all(v is True for v in vals.values()):
        return "통과", ""
    return MISSING, GATE_FORMAT_HINT


def _gate_of(rec: Dict[str, Dict[str, Any]], kind: str) -> Tuple[str, Dict[str, Any], str]:
    """게이트 요약 → (상태, 상세, 사유). **여기서 판정하지 않는다** — 읽기만 한다.

    `gate_g2()` 가 정본이다. 화면이 문턱을 다시 적어서 만들어 내지 않는다 (W5 규약).
    상태는 셋으로 갈린다 (`NOT_APPLICABLE` / `UNDECIDED` / `MISSING`) — 하나로
    묶으면 "빠뜨렸다" 와 "비었다고 알려줬다" 가 구분되지 않는다.
    """
    if kind == "generate":
        return NOT_APPLICABLE, {}, "G2 는 편집 게이트다 — 생성물에는 적용되지 않는다"

    j = rec.get("judgment") or {}
    if not j:
        return MISSING, {}, "judgment.json 이 없다"

    blocks = _gate_blocks(j)
    if not blocks:
        return MISSING, {}, GATE_FORMAT_HINT

    notes = j.get("gate_notes") or {}
    per = {name: _status_of(g, notes) for name, g in blocks.items()}

    if len(blocks) == 1 and "" in blocks:
        st, why = per[""]
        return st, blocks[""], why

    # 런이 여럿이다. 상태가 갈리면 **묶지 않는다** — 묶는 순간 어느 런이 통과했는지
    # 화면에서 사라진다. 같으면 하나로, 다르면 런별로 그대로 낸다.
    states = {st for st, _ in per.values()}
    detail = dict(blocks)
    joined = " · ".join(f"{n} {st}" for n, (st, _) in per.items())
    if len(states) == 1:
        why = " · ".join(f"{n}: {w}" for n, (_, w) in per.items() if w)
        return states.pop(), detail, why
    return joined, detail, joined


def _iter_candidates(root: Path):
    """레코드 파일을 가진 디렉터리를 얕게 훑는다."""
    if any((root / f).is_file() for f in _RECORD_FILES):
        yield root
    for depth in range(1, _MAX_DEPTH + 1):
        for p in root.glob("/".join(["*"] * depth)):
            if p.is_dir() and any((p / f).is_file() for f in _RECORD_FILES):
                yield p


def recent_runs(limit: int = 10) -> List[Run]:
    """최근 `limit` 건. 없으면 빈 목록이다 — 만들어 내지 않는다.

    상한이 10 인 이유: 사용자가 화면에서 **직접 육안 판정**한다. 5 로 자르면
    한 웨이브만 밀려도 직전 대조군이 화면 밖으로 나가고, 대조군 없는 판정은
    "좋아졌다" 를 확인할 방법이 없다.
    """
    seen: Dict[Path, Run] = {}
    for root in scan_dirs():
        for d in _iter_candidates(root):
            if d in seen:
                continue
            rec: Dict[str, Dict[str, Any]] = {}
            newest = 0.0
            for fname in _RECORD_FILES:
                f = d / fname
                if f.is_file():
                    data = _load(f)
                    if data is not None:
                        rec[fname.removesuffix(".json")] = data
                        newest = max(newest, f.stat().st_mtime)
            if not rec:
                continue
            kind = _kind_of(rec, d)
            gate, gate_detail, gate_reason = _gate_of(rec, kind)
            man = rec.get("manifest") or {}
            j = rec.get("judgment") or {}
            try:
                rel = str(d.relative_to(root))
            except ValueError:
                rel = d.name
            seen[d] = Run(
                run_id=hashlib.sha1(str(d).encode()).hexdigest()[:12],
                path=d,
                rel=f"{root.name}/{rel}" if rel != "." else root.name,
                mtime=newest or d.stat().st_mtime,
                kind=kind,
                asset_id=man.get("asset_id") or j.get("asset_id"),
                headline=_fmt_headline(kind, rec),
                gate=gate,
                gate_reason=gate_reason,
                gate_detail=gate_detail,
                records=rec,
            )
    runs = sorted(seen.values(), key=lambda r: r.mtime, reverse=True)[:limit]

    if not a5000_dirs() and not any(r.delivered for r in runs):
        runs.append(
            Run(
                run_id="a5000-pending",
                path=Path("."),
                rel=PENDING_A5000,
                mtime=0.0,
                kind="edit",
                asset_id=None,
                headline=MISSING,
                gate=MISSING,
                gate_reason="A5000 산출물이 아직 3090 에 없다",
                pending_reason=(
                    "A5000 편집 산출물은 아직 3090 에 없다. 다음 웨이브에 "
                    "handoff/pack.py 가 밀어주면 A5000_RUNS_DIR 만 그쪽으로 열면 된다"
                ),
            )
        )
    return runs


def detail_kind(kind: str) -> Dict[str, Any]:
    """🔴 **종류별 정본 그림이 다르다.** 틀리면 판정이 무의미해진다.

        형태 변경  실루엣 4분할 + **앞면 깊이맵**이 정본 (D19)
                   실루엣만으로는 파낸 구멍·오목 디테일이 원리적으로 안 보인다
        색 변경    **색 렌더가 정본** (D19-a)
                   레벨1 은 기하 불변이라 실루엣도 깊이맵도 before/after 가 똑같다
        생성       실루엣 3뷰 + 깊이맵
    """
    if kind == "recolor":
        return {
            "canonical": "color_after",
            "images": ["color", "color_after"],
            "why": (
                "레벨1 은 <b>기하가 불변</b>이라 실루엣도 깊이맵도 before/after 가 "
                "똑같이 나온다 — 둘 다 색 채널을 안 본다. <b>색 렌더가 정본</b>이다 (D19-a)."
            ),
        }
    if kind == "edit":
        return {
            "canonical": "depth",
            "images": ["front", "side", "top", "depth"],
            "why": (
                "실루엣은 <b>투영</b>이라 깊이를 뭉갠다 — 파낸 구멍·오목 디테일이 "
                "뒷면으로 메워져 <b>원리적으로 안 보인다</b>. <b>앞면 깊이맵이 정본</b>이다 (D19)."
            ),
        }
    return {
        "canonical": "depth_side",
        "images": ["front", "side", "top", "depth", "depth_side"],
        "why": (
            "생성물은 실루엣 3뷰로 형태를, 깊이맵으로 오목 디테일을 본다 (D19). "
            "<b>옆면 깊이맵이 정본</b>이다 — 정면 하나로는 긴 축이 시선과 나란한 자산"
            "(오토바이 등)에서 부품이 전부 겹쳐 <b>구조를 못 가른다</b>."
        ),
    }
