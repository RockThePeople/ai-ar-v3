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
from .llm import EditSpec
from .pipeline.mask import build_mask
from .pipeline.recolor import recolor_asset

__all__ = ["run_edit"]

#: 자연어에서 색을 못 뽑았을 때 쓰는 값이 **없다.** 못 뽑으면 거부한다 —
#: 기본색을 칠하면 "무엇을 요청했든 주황이 되는" 서버가 된다.
_COLORS = {
    "빨강": (220, 38, 38), "red": (220, 38, 38),
    "주황": (255, 106, 0), "orange": (255, 106, 0),
    "노랑": (250, 204, 21), "yellow": (250, 204, 21),
    "초록": (34, 197, 94), "green": (34, 197, 94),
    "파랑": (37, 99, 235), "blue": (37, 99, 235),
    "보라": (147, 51, 234), "purple": (147, 51, 234),
    "검정": (20, 20, 20), "black": (20, 20, 20),
    "흰색": (240, 240, 240), "white": (240, 240, 240),
}


class ColorNotUnderstood(ValueError):
    error_code = "COLOR_NOT_UNDERSTOOD"


def _color_of(prompt: str):
    low = prompt.lower()
    for word, rgb in _COLORS.items():
        if word in low:
            return (*rgb, 255)
    raise ColorNotUnderstood(
        f"프롬프트에서 색을 못 읽었다: {prompt!r}. 아는 것: {sorted(set(_COLORS))}. "
        "기본색으로 칠하지 않는다 — 요청과 다른 결과를 성공으로 보고하게 된다")


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
    spec = EditSpec(op="recolor", target_prompt=req.raw_prompt,
                    source="server", raw=req.raw_prompt)
    check_supported(spec, "recolor")          # 못 하는 op 는 여기서 **거부**된다
    color = _color_of(req.raw_prompt)

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
