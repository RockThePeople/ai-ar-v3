"""W18 델타 실험 재현 러너. `python -m handoff.w18-delta.run_experiment` 이 아니라
리포 루트에서 `python handoff/w18-delta/run_experiment.py` 로 부른다.

산출은 `RESULT.md` 에 적힌 숫자와 같아야 한다. 안 같으면 부기 규칙이 바뀐 것이다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]      # 홈 경로를 박지 않는다 (§7)
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "contract" / "python"))

import trimesh  # noqa: E402

from deltacontract import decode  # noqa: E402
from server.delta import assemble_delta, build_bookkeeping, component_sizes  # noqa: E402
from server.metrics import components  # noqa: E402
from server.pipeline.frames import GLB_TO_VOXEL  # noqa: E402
from server.pipeline.voxelize import surface_voxelize  # noqa: E402
from server.realasset import cbin_dir_to_mesh  # noqa: E402

W = _REPO / "w15-out"                                    # A5000 인계분 (gitignore)
BASE = Path.home() / "ai-ar-v3-assets" / "dragon-c" / "chunks"


def main() -> int:
    bv, bf = cbin_dir_to_mesh(BASE)
    base_cells = surface_voxelize(bv, bf, oversample=2.0)

    m = trimesh.load(W / "runG.glb", force="mesh")
    # 🔴 D9 — GLB 는 Y-up, 복셀 격자는 Z-up. 매직 회전이 아니라 정본 상수를 건다.
    rv = GLB_TO_VOXEL.apply(np.asarray(m.vertices, dtype=np.float64))
    rf = np.asarray(m.faces, dtype=np.int64)
    res_cells = surface_voxelize(rv, rf, oversample=2.0)
    mask = np.load(W / "w14_mask.npy").astype(np.int64)
    base_blobs = {p.name.split(".")[0]: p.read_bytes() for p in sorted(BASE.glob("*.cbin"))}
    print(f"base {len(base_cells):,} · runG {len(res_cells):,} · mask {len(mask):,}")

    # ── 문턱은 **관측 분포의 가장 큰 배수 간극**에서 고른다. 숫자를 먼저 정하지 않는다.
    probe = build_bookkeeping(base_cells, res_cells, mask, halo=1, min_component=1)
    sizes = sorted(set(component_sizes(probe.new_outside_cells)), reverse=True)
    ratio, lo, hi = max((sizes[i] / sizes[i + 1], sizes[i + 1], sizes[i])
                        for i in range(len(sizes) - 1))
    T = int(round((lo * hi) ** 0.5))
    print(f"성분 크기 상위 {sizes[:8]} · 최대 간극 {lo}→{hi} ({ratio:.1f}배) · 문턱 {T}")

    bk = build_bookkeeping(base_cells, res_cells, mask, halo=1, min_component=T)
    d = assemble_delta(base_blobs, rv, rf, res_cells, bk.book)
    print(f"부기 {len(bk.book)}청크 (마스크·halo {len(bk.mask_keys)} ∪ overflow "
          f"{len(bk.overflow_keys)}) · 살린 성분 {bk.kept_components} · 버린 {bk.dropped_voxels}복셀")
    print(f"델타 {d.n_delta_chunks}청크 {d.delta_bytes:,} B / 전체재전송 {d.full_bytes:,} B "
          f"→ 절감 {d.saving * 100:.1f}%")
    print(f"승계 동일률 {d.identity * 100:.1f}% · 승계 없이 재인코딩하면 "
          f"{d.reencoded_identity * 100:.1f}%")

    # ── 조립해서 **측면 머리가 살아남았는지** 본다. 절감률만 보면 아무것도 안 보내는
    #    구현이 100% 를 받는다 (방법론 5조 3번).
    verts, faces, off = [], [], 0
    for k in sorted(d.child_blobs):
        mesh = decode(d.child_blobs[k])
        v = np.asarray(mesh.positions, dtype=np.float64)
        verts.append(v)
        faces.append(np.asarray(mesh.indices, dtype=np.int64).reshape(-1, 3) + off)
        off += len(v)
    final = surface_voxelize(np.concatenate(verts), np.concatenate(faces), oversample=2.0)
    heads = components(final[final[:, 2] >= 45])[:3]
    ref = [c.n_cells for c in components(res_cells[res_cells[:, 2] >= 45])[:3]]
    print(f"머리 {[c.n_cells for c in heads]} · 델타 전 {ref}")

    print(json.dumps({"threshold": T, "book_chunks": len(bk.book),
                      "delta_chunks": d.n_delta_chunks, "delta_bytes": d.delta_bytes,
                      "full_bytes": d.full_bytes, "saving": d.saving,
                      "identity": d.identity, "reencoded_identity": d.reencoded_identity,
                      "heads": [c.n_cells for c in heads], "heads_before_delta": ref},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
