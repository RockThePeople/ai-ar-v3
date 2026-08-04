"""인계본이 **리포 정본과 같은가** — 버전 검사 (D27-b 제안).

────────────────────────────────────────────────────────────────────────
🔴 왜 필요한가 — W11 에서 드러난 것
────────────────────────────────────────────────────────────────────────
A5000 이 받은 `slatmask` 인계본에 **D28-a 구조 강제가 없었다.**

    grid_source=          부재
    require_slat_grid()   부재
    is_x_symmetric()      부재

실효 방어는 런타임 2건(`source != SLAT` 거부, 매니페스트 총계 대조)뿐이었다.
D28-a 를 **구조로 강제**했다고 적었는데, 실행된 코드에는 그 구조가 없었다.

그리고 `slatmask.py` 는 **리포에 존재하지 않는다.** 즉 인계본이 정본에서
파생됐다는 근거가 처음부터 없었고, 아무도 그것을 확인할 방법이 없었다.

D27 은 "받는 세션이 **받았다**를 보고해야 인계 완료" 라고 했다. 그런데 "받았다"
는 **파일이 도착했다**는 뜻이지 **맞는 버전이 도착했다**는 뜻이 아니었다.
sha256 대조(D27②)는 보낸 것과 받은 것이 같은지를 보지, 보낸 것이 **정본인지**는
보지 않는다. 그 틈이 이번 실패다.

────────────────────────────────────────────────────────────────────────
두 겹으로 검사한다
────────────────────────────────────────────────────────────────────────
① **바이트 동일성** — 인계본 sha256 == 리포 정본 sha256.
   드리프트를 전부 잡지만, 리포에 없는 파일(`slatmask.py`)에는 쓸 수 없다.

② **필수 API 존재** — 그 모듈이 약속한 심볼을 실제로 갖고 있는가.
   ①이 불가능한 경우에도 돌고, **이번 실패를 정확히 잡는다** —
   `grid_source` 가 없는 인계본은 여기서 걸린다.

②가 더 중요하다. ①은 "같은 파일인가" 를 묻고 ②는 **"약속한 일을 하는가"** 를 묻는다.
버전이 달라도 API 가 있으면 대개 괜찮고, 버전이 같아 보여도 API 가 없으면 무조건 사고다.

⚠️ 이 모듈은 **검사만** 한다. 인계 자체(전송·수령 확인)는 D27 규약이고 사람이 한다.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = [
    "REQUIRED_API",
    "HandoffMismatch",
    "ProvenanceReport",
    "check_required_api",
    "file_digest",
    "repo_manifest",
    "verify_handoff",
]


class HandoffMismatch(RuntimeError):
    """인계본이 리포 정본과 다르거나, 약속한 API 를 갖고 있지 않다."""


# ══════════════════════════════════════════════════════════ 필수 API
#
# 🔴 여기 적힌 심볼이 없으면 그 모듈은 **약속한 일을 하지 않는다.**
#    조항 번호를 같이 적는 이유: 검사가 실패했을 때 "왜 필요한가" 를 다시
#    조사하지 않아도 되게 하기 위해서다.
REQUIRED_API: Dict[str, Dict[str, str]] = {
    # 마스크를 만드는 쪽이 반드시 갖춰야 하는 것 (D28-a · D35-a).
    "slatmask": {
        "grid_source": "D28-a — 좌표가 어느 격자에서 왔는지 구조로 강제한다",
        "require_slat_grid": "D28-a — 정본 격자가 아니면 판정을 거부한다",
        "is_x_symmetric": "D35-a — X 반전 모호성이 무해한지 검사한다",
    },
    "server.pipeline.mask": {
        "MaskResult": "마스크 상태",
        "build_mask": "클램프 → 팽창 순서를 한 곳에 고정한다",
    },
    "server.pipeline.frames": {
        "GLB_TO_VOXEL": "D9 — 축 순열",
        "DECODER_NATIVE_TO_VOXEL": "D9-b — 디코더 native 는 이미 복셀 프레임",
        "VOXEL_GRID_SOURCE": "D28 — 격자 정본",
        "assert_slat_grid": "D28-a — 격자 출처 강제",
        "is_x_symmetric": "D35 — X 대칭 검사",
        "assert_x_symmetric": "D35-a — X 대칭 강제",
    },
    "server.metrics": {
        "NoiseFloor": "D33 — 자산 id 없는 바닥값을 거부한다",
        "HaloBandResult": "D37 — 원시 개수가 1급 시민",
        "check_direction": "D38 — op 별 방향 조건",
        "AnchorRetention": "D39 — 앵커 잔존율 (문턱 없음)",
    },
}


def file_digest(path: Path) -> str:
    """파일 바이트의 sha256. 인계 대조(D27②)와 같은 값이다."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def repo_manifest(paths: Iterable[str], root: Optional[Path] = None) -> Dict[str, Dict]:
    """리포 정본의 지문 목록. 인계물에 **동봉**한다 (D27③ 과 짝).

    자산·마스크만이 아니라 **코드도** 인계 대상이라는 것이 이번 교훈이다.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    out: Dict[str, Dict] = {}
    for rel in paths:
        p = base / rel
        if not p.is_file():
            raise HandoffMismatch(f"정본에 없는 파일이다: {rel}")
        out[rel] = {"sha256": file_digest(p), "bytes": p.stat().st_size}
    return out


def verify_handoff(
    manifest: Mapping[str, Mapping], received_root: Path
) -> List[str]:
    """받은 쪽이 부르는 검사 ① — 바이트 동일성.

    Returns:
        어긋난 파일 목록 (빈 리스트면 전부 일치).
    """
    base = Path(received_root)
    bad: List[str] = []
    for rel, meta in manifest.items():
        p = base / rel
        if not p.is_file():
            bad.append(f"{rel}: 없다")
            continue
        got = file_digest(p)
        if got != meta["sha256"]:
            bad.append(f"{rel}: sha256 {got[:12]}… ≠ 정본 {meta['sha256'][:12]}…")
    return bad


@dataclass(frozen=True)
class ProvenanceReport:
    """API 검사 결과. **없는 심볼과 그 이유**를 함께 낸다."""

    module: str
    missing: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        if self.ok:
            return f"{self.module}: 필수 API 전부 있음"
        lines = [f"{self.module}: 필수 API {len(self.missing)}개 부재"]
        lines += [f"  · {name} — {why}" for name, why in sorted(self.missing.items())]
        return "\n".join(lines)


def check_required_api(
    module_name: str,
    *,
    required: Optional[Mapping[str, str]] = None,
    obj: object = None,
) -> ProvenanceReport:
    """받은 쪽이 부르는 검사 ② — **약속한 API 를 실제로 갖고 있는가.**

    ①(바이트 동일성)이 불가능한 경우에도 돈다. 이번 실패를 정확히 잡는 검사다 —
    `grid_source` 가 없는 `slatmask` 인계본은 여기서 걸린다.

    Args:
        module_name: `REQUIRED_API` 의 키. 없으면 `required` 를 직접 준다.
        obj: 이미 import 한 모듈. None 이면 `module_name` 으로 import 한다.

    ⚠️ 심볼 이름은 모듈의 최상위 속성뿐 아니라 **클래스 필드·메서드**도 본다 —
       `grid_source` 는 `MaskResult` 의 필드이지 모듈 속성이 아니다. 이름만 보고
       "없다" 고 하면 오탐이 난다.
    """
    spec = dict(required) if required is not None else REQUIRED_API.get(module_name)
    if spec is None:
        raise HandoffMismatch(
            f"{module_name!r} 의 필수 API 목록이 없다. 검사할 것을 먼저 적어라 — "
            "목록 없는 모듈을 통과시키면 이 검사가 아무것도 막지 못한다."
        )

    target = obj if obj is not None else importlib.import_module(module_name)
    names = set(dir(target))
    # 클래스 안쪽까지 한 겹 들여다본다 (dataclass 필드·메서드).
    for attr in list(names):
        member = getattr(target, attr, None)
        if inspect.isclass(member):
            names.update(dir(member))
            names.update(getattr(member, "__annotations__", {}).keys())

    missing = {name: why for name, why in spec.items() if name not in names}
    return ProvenanceReport(module=module_name, missing=missing)


def assert_required_api(module_name: str, **kw) -> None:
    """`check_required_api` 의 예외판. 게이트 경로가 부른다.

    ⚠️ bool 을 돌려주지 않는다 — 검사하고도 무시할 수 있으면 그게 곧 조용한 실패다
       (`dispatch.check_supported` · `metrics.check_direction` 과 같은 이유).
    """
    report = check_required_api(module_name, **kw)
    if not report.ok:
        raise HandoffMismatch(
            report.describe()
            + "\n⇒ 인계본이 리포 정본보다 오래됐을 수 있다. 정본을 다시 보내고 "
            "받는 쪽이 이 검사를 통과했다고 보고해야 인계 완료다 (D27④)."
        )
