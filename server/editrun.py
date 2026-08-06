"""편집 잡의 실체 — 마스크 + 자연어 → 델타 (`PatchPackage`).

지금 실제로 도는 것은 **레벨1(recolor)** 이다. GPU 가 필요 없고 초 단위이며,
기하 바이트가 100% 보존된다 (D24). 형태 편집(레벨2)은 A5000 경로라 여기 없다.

🔴 op 를 **지어내지 않는다.** 자연어에서 op 를 뽑는 것은 `server/llm.py` 이고,
   소비자가 처리 못 하는 op 는 `server/dispatch.py` 가 **거부**한다 (D26).
   여기서 recolor 로 강등하면 게이트가 "형태를 바꿨다" 고 적으면서 색만 바꾼 결과를
   재게 된다.
"""

from __future__ import annotations

import numpy as np

from deltacontract import mask_fingerprint  # type: ignore[import-not-found]
from deltacontract.schemas import (  # type: ignore[import-not-found]
    ChunkEntry,
    ContractInfo,
    EditRequest,
    PatchPackage,
)
from deltacontract import CONTRACT_CONSTANTS, chunk_uri  # type: ignore[import-not-found]

from .assetstore import STORE
from .dispatch import UnsupportedOp, check_supported
from .llm import EditSpec, LLMError, MaskSummary, plan_edit
from .pipeline.mask import build_mask
from .pipeline.recolor import recolor_asset

__all__ = ["run_edit"]

#: 색 어휘. **한국어는 활용한다** — "빨강 / 빨간 / 빨갛게 / 붉은" 이 다 같은 색이다.
#: 어간만 넣으면 "빨갛게" 를 놓친다 (W26f 실측에서 실제로 놓쳤다).
#:
#: ⚠️ 이건 **어휘표지 이해가 아니다.** 여기 없는 표현은 못 읽고, 그때는 거부한다.
#:    표를 넓히는 것으로 "자연어를 이해한다" 고 말하지 않는다.
#: ⚠️ 기본색이 **없다.** 못 읽으면 거부다 — 기본색을 칠하면 무엇을 요청하든 같은
#:    색이 되는 서버가 되고, 그 서버는 언제나 "성공" 을 보고한다.
_COLORS = {
    (220, 38, 38): ("빨강", "빨간", "빨갛", "붉은", "붉게", "적색", "red"),
    (255, 106, 0): ("주황", "주홍", "오렌지", "orange"),
    (250, 204, 21): ("노랑", "노란", "노랗", "누런", "황색", "yellow"),
    (34, 197, 94): ("초록", "녹색", "푸른색", "green"),
    (37, 99, 235): ("파랑", "파란", "파랗", "청색", "blue"),
    (147, 51, 234): ("보라", "자주", "purple", "violet"),
    (20, 20, 20): ("검정", "검은", "까만", "까맣", "흑색", "black"),
    (240, 240, 240): ("흰색", "하얀", "하얗", "백색", "white"),
}


class ColorNotUnderstood(ValueError):
    error_code = "COLOR_NOT_UNDERSTOOD"


def _color_of(prompt: str):
    """프롬프트 → RGBA. 못 읽으면 **거부한다.**

    가장 **뒤에** 나온 색을 쓴다 — "빨간 차의 바퀴를 파랑으로" 에서 요청된 색은
    뒤쪽이다. 앞을 쓰면 배경 묘사에 끌려간다.
    """
    low = prompt.lower()
    hits = [(low.rfind(w), rgb) for rgb, words in _COLORS.items()
            for w in words if w in low]
    if not hits:
        raise ColorNotUnderstood(
            f"프롬프트에서 색을 못 읽었다: {prompt!r}. "
            f"아는 표현: {sorted(w for ws in _COLORS.values() for w in ws)}. "
            "기본색으로 칠하지 않는다 — 요청과 다른 결과를 성공으로 보고하게 된다")
    return (*max(hits)[1], 255)


def run_edit(asset_id: str, req: EditRequest, progress) -> dict:
    progress(0.1, "mask", "마스크 검증")
    m = req.mask
    if m.mode != "voxels" or not m.voxels:
        raise ValueError("지금 도는 편집 경로는 mode='voxels' 만 받는다")
    cells = np.asarray(m.voxels, dtype=np.int64)

    # 🔴 격자 출처는 **요청이 밝힌 것**을 쓴다. 서버가 채우지 않는다 (D28-a).
    mask = build_mask(cells, halo=m.halo_margin_voxels, grid_source=m.grid_source)
    mask.require_slat_grid("편집 요청")

    progress(0.25, "op", "자연어 → op")
    # 🔴 op 를 여기서 **찍지 않는다.** `llm.py` 가 정본이고, 못 하는 op 는
    #    `dispatch.py` 가 거부한다 (D26). 자동 강등(add → recolor)은 특히 안 한다 —
    #    그러면 게이트가 "형태를 바꿨다" 고 적으면서 색만 바꾼 결과를 재게 된다.
    #
    #    이 배선이 진단을 **가른다**: "이 부분을 뾰족하게" 는 색을 못 읽은 것이
    #    아니라 이 소비자가 못 하는 op 다. 전에는 둘 다 COLOR_NOT_UNDERSTOOD 였다.
    try:
        # 마스크는 **요약만** 넘어간다 (셀 좌표는 안 넘긴다 — llm.py 의 규약).
        spec = plan_edit(req.raw_prompt, mask=MaskSummary.from_mask(mask))
    except LLMError:
        raise
    # 🔴 op 가 소비자를 정한다. 여기서 op 를 갈아끼우지 않는다 (D26) —
    #    자동 강등은 게이트가 "형태를 바꿨다" 고 적으면서 색만 바꾼 결과를 재게 만든다.
    consumer = "recolor" if spec.op == "recolor" else "voxhammer"
    check_supported(spec, consumer)           # 못 하는 op 는 여기서 **거부**된다

    if consumer == "voxhammer":
        # 형태 편집은 상류(<EDIT_HOST>)가 한다. 색은 안 읽는다 — 색 어휘가 없어도 된다.
        from .voxhammer import run_voxhammer_edit

        return run_voxhammer_edit(asset_id, req, spec, progress)

    progress(0.35, "color", "색 해석")
    color = _color_of(spec.target_prompt or req.raw_prompt)

    progress(0.45, "recolor", "정점 색 교체")
    parent = STORE.blobs(asset_id, req.base_version)
    r = recolor_asset(parent, mask, color, job_id=None)
    child = r.package.blobs

    changed = sorted(k for k in child if k in parent and child[k] != parent[k])
    added = sorted(set(child) - set(parent))
    removed = sorted(set(parent) - set(child))

    progress(0.8, "chunk", "새 판본 저장")
    to_version = req.base_version + 1
    STORE.put_version(asset_id, to_version, {k: child[k] for k in changed + added})

    entries = {}
    for k in changed + added:
        from deltacontract import decode  # noqa: PLC0415

        d = decode(child[k])
        import hashlib

        entries[k] = ChunkEntry(
            uri=chunk_uri(asset_id, k, to_version),
            hash=hashlib.sha256(child[k]).hexdigest(),
            byte_length=len(child[k]),
            vertex_count=int(len(d.positions)), index_count=int(len(d.indices)),
            voxel_count=int(getattr(d, "voxel_count", 0) or 0), version=to_version)

    patch = PatchPackage(
        asset_id=asset_id, from_version=req.base_version, to_version=to_version,
        contract=ContractInfo(**CONTRACT_CONSTANTS),
        changed_chunks=entries, removed_chunk_ids=removed,
        mask_fingerprint=mask_fingerprint(mask.cells),
        mask_voxels_used=int(mask.n_cells), op="edit")
    return {"patch": patch, "asset_id": asset_id,
            "manifest": STORE.manifest(asset_id, to_version)}
