"""S27 13.2: 판본 전체 glb — `/clients/glb` 와 같은 조립·캐시(data/glb_clients), NORMAL 추가.

`glbbuild.build_glb` 는 고치지 않는다(같은 정점·색·축 변환 `VOXEL_TO_GLB`, 면 수 축소 없음). 여기서는 같은 메시에
정점 법선을 계산해 넣는다. `VOXEL_TO_GLB` 는 회전(행렬식 +1)이라 감김·법선 방향이 그대로다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Dict


def build_glb_normals(chunk_dir: Path, out: Path) -> Path:
    import numpy as np
    import trimesh
    from ..glbbuild import VOXEL_TO_GLB, cbin_dir_to_colored_mesh
    verts, faces, colors = cbin_dir_to_colored_mesh(chunk_dir)
    v = np.asarray(VOXEL_TO_GLB.apply(verts), dtype=np.float64)
    vn = vertex_normals(v, np.asarray(faces, dtype=np.int64))
    mesh = trimesh.Trimesh(vertices=v, faces=faces, process=False,
                           vertex_colors=colors if colors is not None else None)
    mesh.vertex_normals = vn                              # 메시를 다 만든 뒤 넣어야 export 가 다시 계산하지 않는다
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.glb")
    mesh.export(tmp, file_type="glb", include_normals=True)
    tmp.replace(out)                                      # 동시 요청이 반쯤 쓴 파일을 받지 않게
    return out


def vertex_normals(v, f):
    """면적 가중 정점 법선(numpy). trimesh 기본 계산은 scipy 가 없으면 매우 느린 대체 경로로 간다(실측: 수십만 정점에서 2분 넘김)."""
    import numpy as np
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    vn = np.zeros_like(v)
    for k in range(3):
        np.add.at(vn, f[:, k], fn)
    ln = np.linalg.norm(vn, axis=1, keepdims=True)
    return np.where(ln > 0, vn / np.where(ln > 0, ln, 1), np.array([0.0, 1.0, 0.0]))


def cached(data_dir: Path, key: str, blobs: Dict[str, bytes]) -> Path:
    """data/glb_clients/<key>-n.glb (-n = 법선 포함판. 법선 없는 옛 캐시와 이름이 겹치지 않게)."""
    out = Path(data_dir) / "glb_clients" / f"{key}-n.glb"
    if not out.is_file():
        with tempfile.TemporaryDirectory() as td:
            for k, b in blobs.items():
                (Path(td) / f"{k}.cbin").write_bytes(b)
            build_glb_normals(Path(td), out)
    return out


def version_glb(data_dir: Path, store, asset_id: str, version: int) -> Path:
    blobs = store.blobs(asset_id, int(version))
    return cached(data_dir, f"{store._slug(asset_id)}-v{int(version)}", blobs)


def model_uri(asset_id: str, version: int) -> str:
    return f"/v2/assets/{asset_id}/model.v{int(version)}.glb"
