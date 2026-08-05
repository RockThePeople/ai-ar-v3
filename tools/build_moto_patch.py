#!/usr/bin/env python3
"""moto-b 의 `.cbin` 부모 청크 + recolor 델타를 만든다 (W21).

Unity 가 **실제 계약 바이트**로 in-place 교체를 시험할 재료다.

    python3 tools/build_moto_patch.py <out_dir>

만드는 것:
    parent/<key>.cbin      부모 청크 전체
    patch/<key>.cbin       바뀐 청크만 (recolor)
    patch.json             changed / added / removed  ← **셋으로 적는다** (D72)
    patch-removed.json     🔴 removed 를 일부러 만든 변형 (DESIGN_INTENT §3-E 시험용)

⚠️ 기하는 합성 디코더(`occupancy_to_mesh`)로 만든다. A5000 의 TRELLIS 디코더가 아니다.
   **형식과 부기는 실물**이고 기하만 합성이다. 이 웨이브의 질문이 "Unity 가 청크
   Mesh 만 갈아끼우는가" 라서 그 구분으로 충분하다 — 다만 보고에 그대로 적는다.
"""

from __future__ import annotations

import json
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "contract" / "python"))

from deltacontract import canonicalize, encode  # noqa: E402
from deltacontract.partition import partition_mesh  # noqa: E402

from server.pipeline.mask import build_mask  # noqa: E402
from server.pipeline.recolor import recolor_asset  # noqa: E402
from server.pipeline.voxelize import occupancy_to_mesh  # noqa: E402

NPY = ROOT / "handoff/lasso/moto-b.slat_coords.npy"
MASK = ROOT / "handoff/lasso/moto-b.rear-wheel.mask.json"
COLOR = (255, 106, 0, 255)          # 3090 이 W20 에 쓴 색 그대로


def main() -> None:
    out = pathlib.Path(sys.argv[1])
    (out / "parent").mkdir(parents=True, exist_ok=True)
    (out / "patch").mkdir(parents=True, exist_ok=True)

    cells = np.unique(np.load(NPY).astype(np.int64), axis=0)
    verts, faces = occupancy_to_mesh(cells)
    meshes = partition_mesh(verts, faces, voxel_cells=cells)

    # 색을 입힌다 — recolor 경로는 COLOR 채널이 있는 자산에만 쓴다.
    # 높이(z)에 따라 회색조를 주어 **교체 전후를 눈으로도 구분**할 수 있게 한다.
    parent = {}
    for k, m in meshes.items():
        pos = np.asarray(m.positions)
        t = np.clip((pos[:, 2] + 0.5), 0.0, 1.0)
        base = (60 + t * 150).astype(np.uint8)
        colors = np.stack([base, base, base, np.full(len(pos), 255, np.uint8)], axis=1)
        parent[k] = encode(canonicalize(
            chunk_coord=m.chunk_coord, positions=pos, indices=np.asarray(m.indices),
            colors=colors, voxel_count=m.voxel_count,
        ))
    print(f"부모 청크 {len(parent)}개 · {sum(len(b) for b in parent.values()):,} 바이트")

    mask_doc = json.loads(MASK.read_text())["mask"]
    assert mask_doc["grid_source"] == "slat_coords", "격자 출처가 정본이 아니다 (D28-a)"
    mask = build_mask(np.array(mask_doc["voxels"], dtype=np.int64), halo=0,
                      grid_source=mask_doc["grid_source"])

    r = recolor_asset(parent, mask, COLOR)
    changed = sorted(r.bookkeeping.changed)
    removed = sorted(r.bookkeeping.removed)
    print(f"recolor 변경 {len(changed)}/{len(parent)} 청크 · 제거 {len(removed)} · "
          f"정점 {r.n_vertices_recolored:,} · 절감(참고) {r.transfer_saving:.1%}")

    for k, b in parent.items():
        (out / "parent" / f"{k}.cbin").write_bytes(b)
    for k in changed:
        (out / "patch" / f"{k}.cbin").write_bytes(r.blobs[k])

    # 🔴 changed / added / removed 를 **셋으로** 적는다. 쌍이 아니다 (D72).
    #    recolor 는 청크 집합을 바꾸지 않으므로 added = removed = 0 이어야 한다 —
    #    아니면 경로가 깨진 것이다.
    (out / "patch.json").write_text(json.dumps({
        "asset_id": "v3-moto-b",
        "op": "recolor",
        "n_chunks_total": len(parent),
        "changed": changed,
        "added": [],
        "removed": removed,
        "saving_reference_only": round(r.transfer_saving, 4),
        "mask_fingerprint": mask_doc.get("mask_fingerprint"),
        "note": "기하는 합성 디코더 산출. 형식·부기는 실물이다",
    }, indent=1))

    # ── §3-E 시험용 변형: 청크 하나를 **없앤다.**
    #    removed 는 GameObject 를 파괴하므로 사전에서도 지워야 한다. 안 지우면
    #    다음 패치가 **파괴된 MeshFilter 에 ApplyTo** 를 걸고 예외가 안 난다.
    victim = sorted(set(parent) - set(changed))[0]
    (out / "patch-removed.json").write_text(json.dumps({
        "asset_id": "v3-moto-b",
        "op": "synthetic-removal",
        "n_chunks_total": len(parent),
        "changed": changed[:3],
        "added": [],
        "removed": [victim],
        "note": "removed 경로 시험용. 실제 recolor 산출이 아니다",
    }, indent=1))
    print(f"removed 시험용 희생 청크 {victim}")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
