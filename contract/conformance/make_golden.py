"""
골든 벡터 생성기.

세 세션이 각자 구현해도 **같은 바이트**가 나오는지 스스로 검증할 수 있게 만드는 장치다.
Unity/C# 구현은 golden/*.cbin 을 읽어서 정점/인덱스가 golden.json 의 값과 맞는지만
확인하면 되고, Python 구현은 픽스처로부터 다시 만들어 해시가 같은지 확인한다.

사용:
    python conformance/make_golden.py          # 생성/갱신
    python -m pytest conformance/              # 검증

계약(coords.CONTRACT_VERSION 또는 .cbin 레이아웃)을 바꾸면 반드시 재생성하고,
세 세션이 모두 갱신되기 전에는 배포하지 않는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python"))
sys.path.insert(0, str(HERE))

from deltacontract import (  # noqa: E402
    CONTRACT_CONSTANTS,
    albedo_to_rgba8,
    blob_hash,
    encode,
    partition_mesh,
    split_trellis_vertex_attrs,
)
from fixture import apply_local_edit, voxel_cells_from_mesh, torus  # noqa: E402

GOLDEN_DIR = HERE / "golden"


def build(vertices, faces, attrs):
    albedo, normals = split_trellis_vertex_attrs(attrs)
    return partition_mesh(
        vertices,
        faces,
        normals=normals,
        colors_rgba8=albedo_to_rgba8(albedo),
        voxel_cells=voxel_cells_from_mesh(vertices),
    )


def main() -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for stale in GOLDEN_DIR.glob("*.cbin"):
        stale.unlink()

    v0, f0, a0 = torus()
    v1, f1, a1 = apply_local_edit(v0, f0, a0)

    chunks_v1 = build(v0, f0, a0)
    chunks_v2 = build(v1, f1, a1)

    manifest = {"contract": CONTRACT_CONSTANTS, "versions": {}}

    for label, chunks in (("v1", chunks_v1), ("v2", chunks_v2)):
        entries = {}
        for key, mesh in sorted(chunks.items()):
            blob = encode(mesh)
            (GOLDEN_DIR / f"{label}_{key}.cbin").write_bytes(blob)
            entries[key] = {
                "hash": blob_hash(blob),
                "byte_length": len(blob),
                "vertex_count": mesh.vertex_count,
                "index_count": mesh.index_count,
                "voxel_count": mesh.voxel_count,
            }
        manifest["versions"][label] = entries

    h1 = {k: e["hash"] for k, e in manifest["versions"]["v1"].items()}
    h2 = {k: e["hash"] for k, e in manifest["versions"]["v2"].items()}
    changed = sorted(k for k in h2 if h1.get(k) != h2[k])
    manifest["delta_v1_to_v2"] = {
        "changed": changed,
        "removed": sorted(k for k in h1 if k not in h2),
        "unchanged": sorted(k for k in h2 if k in h1 and h1[k] == h2[k]),
    }

    (GOLDEN_DIR / "golden.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    total = sum(e["byte_length"] for e in manifest["versions"]["v2"].values())
    delta = sum(
        manifest["versions"]["v2"][k]["byte_length"] for k in changed
    )
    print(f"청크 수      : v1={len(h1)}  v2={len(h2)}")
    print(f"변경된 청크  : {len(changed)} / {len(h2)}")
    print(f"전체 재전송  : {total:,} bytes")
    print(f"델타 전송    : {delta:,} bytes  ({delta / total * 100:.1f}%)")
    print(f"골든 기록    : {GOLDEN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
