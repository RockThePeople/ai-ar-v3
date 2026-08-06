"""편집 전 **판본 동기화** — 상류에 base 판본이 없으면 밀어 넣고 검사한다.

────────────────────────────────────────────────────────────────────────
왜 자동화인가
────────────────────────────────────────────────────────────────────────
W27 에서 `base_version=1` 이 `<EDIT_HOST>` 에 커밋돼 있지 않아 **409
VERSION_CONFLICT** 가 났고, 손으로 파일 둘(`slat.safetensors`·`input.png`)을 밀어
넣어 풀었다. 손으로 하면 **"커밋을 잊어서 409" 가 반드시 재발한다.**

더 나쁜 경우가 있다. `recolor` 는 3090 **로컬**이라 그 결과 판본이 상류에 아예 없다.
recolor 로 만든 v2 위에 형태 편집을 걸면 그 순간 터진다 — 지금 구조가 그 씨앗이다.

────────────────────────────────────────────────────────────────────────
🔴 커밋 **전에** 필수 파일을 검사한다 (A5000 `V1-REQUIRED-FILES.md`)
────────────────────────────────────────────────────────────────────────
셋은 라우트가 **202 를 준 뒤 워커에서** 죽는다:

    slat.safetensors   `_load_slat` → job INTERNAL FileNotFoundError
    input.png          `pipe.get_cond` → job INTERNAL FileNotFoundError
    slat 메타 norm_*   `op_edit` KeyError — **다음 편집이 불가능한 막다른 버전**

즉 "요청은 성공했는데 결과가 없다" 로 보인다. 그래서 **보내기 전에** 막는다 —
없는 채로 밀어 넣으면 상류에 막다른 판본이 생기고, 그건 지우기 전까지 계속 실패한다.

⚠️ `slat_space` 는 `"denormalized"` 여야 한다. 정규화 상태로 넣으면 **조용히 틀린
   기하**가 나온다 — 예외가 안 난다.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Dict, List, Optional

__all__ = ["RequiredFilesMissing", "check_v1_payload", "REQUIRED_SLAT_META"]

#: 없으면 **다음 편집이 불가능한 막다른 판본**이 된다 (ops.py:265-266 KeyError).
REQUIRED_SLAT_META = ("norm_mean", "norm_std", "slat_space")


class RequiredFilesMissing(RuntimeError):
    """상류 판본에 필요한 것이 없다. **밀어 넣기 전에** 멈춘다."""

    error_code = "UPSTREAM_PAYLOAD_INCOMPLETE"


def _slat_metadata(path: Path) -> Dict[str, str]:
    """safetensors `__metadata__`. 패키지를 새로 안 넣는다 — 헤더만 읽는다."""
    with open(path, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(n).decode("utf-8"))
    return header.get("__metadata__") or {}


def check_v1_payload(asset_dir: Path, *, n_chunks: Optional[int] = None) -> Dict[str, object]:
    """상류로 밀 판본 재료를 검사한다. 부족하면 `RequiredFilesMissing`.

    Args:
        asset_dir: `slat.safetensors` · `input.png` 를 담은 3090 쪽 자산 디렉터리.
        n_chunks: 같이 보낼 청크 수 (0 이면 거부 — 빈 판본을 만들지 않는다).

    Returns:
        검사 요약. 통과했을 때만 돌아온다.
    """
    asset_dir = Path(asset_dir)
    missing: List[str] = []
    slat = asset_dir / "slat.safetensors"
    img = asset_dir / "input.png"
    if not slat.is_file():
        # 대체 이름을 찾아 준다 — 생성 경로는 `source.png`/`slat.safetensors` 로 남긴다.
        missing.append("slat.safetensors")
    if not img.is_file() and not (asset_dir / "source.png").is_file():
        missing.append("input.png (또는 source.png)")
    if n_chunks is not None and n_chunks <= 0:
        missing.append("chunks (0개 — 빈 판본을 만들지 않는다)")

    meta: Dict[str, str] = {}
    if slat.is_file():
        meta = _slat_metadata(slat)
        missing += [f"slat 메타 {k}" for k in REQUIRED_SLAT_META if k not in meta]
        space = meta.get("slat_space")
        if space is not None and space != "denormalized":
            raise RequiredFilesMissing(
                f"slat_space 가 {space!r} 다. 'denormalized' 여야 한다 — 정규화 상태로 "
                "넣으면 **조용히 틀린 기하**가 나온다 (예외가 안 난다)")

    if missing:
        raise RequiredFilesMissing(
            f"상류 판본 재료가 없다: {', '.join(missing)}. "
            "이대로 밀면 라우트는 202 를 주고 **워커에서** 죽는다 — "
            "'요청은 성공, 결과는 없음' 이 되어 원인을 찾기 어렵다. 보내기 전에 멈춘다")

    return {"slat": str(slat.name), "image": img.name if img.is_file() else "source.png",
            "slat_meta_keys": sorted(meta), "n_chunks": n_chunks}
