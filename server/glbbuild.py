"""`.cbin` 청크 세트 → 뷰어용 GLB. **3D 로 정말 바뀌었는가**에 답하는 그림이다.

────────────────────────────────────────────────────────────────────────
🔴 W12 의 내 진술을 정정한다 — `.cbin` 에는 색이 **있다**
────────────────────────────────────────────────────────────────────────
W12 에 `dragon-c_before.glb` 를 만들고 화면에 이렇게 적었다:

    "편집 전이 흰색인 것은 편집 결과가 아니다. .cbin 은 정점·면만 담고
     색 채널이 없다."

**틀렸다.** 실측: dragon-c 124/124 청크, moto-b 89/89 청크가 `colors` 를 담고 있다.
흰색이었던 이유는 내가 `realasset.cbin_dir_to_mesh()` 를 썼기 때문이다 — 그 함수는
(vertices, faces) 만 반환하고 색을 **버린다.** 자산의 성질이 아니라 내 빌더의 성질이었다.

그 오진이 화면 경고로 나갔고 그대로 다음 웨이브 브리핑에까지 실렸다. 방법론 5조 2번
("예외가 안 났다 ≠ 안전하다")의 변종이다 — 흰 모델을 보고 원인을 자산에 돌렸는데,
그 경로가 색을 안 실어 나른 것뿐이었다. 여기서는 색을 **싣는다.**

────────────────────────────────────────────────────────────────────────
🔴 좌표 프레임 (D9)
────────────────────────────────────────────────────────────────────────
`.cbin` 정점은 복셀 프레임(Z-up)이고 GLB 는 Y-up 이다. `frames.VOXEL_TO_GLB` 를
건다 — 매직 회전을 쓰지 않는다. 안 걸면 이 GLB 만 90° 누워 보이고, A5000 이 낸
GLB 와 나란히 걸었을 때 좌우 비교가 통째로 뜻을 잃는다 (W12 에서 실제로 겪었다).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from deltacontract import decode  # type: ignore[import-not-found]

from .pipeline.frames import VOXEL_TO_GLB

__all__ = ["GLB_CACHE", "cbin_dir_to_colored_mesh", "build_glb", "cached_glb"]

#: 만든 GLB 를 두는 곳. **자산 디렉터리를 더럽히지 않는다** — 스캔 대상이라
#: 산출물이 그 안에 쌓이면 "A5000 이 준 것" 과 "내가 만든 것" 이 섞인다.
GLB_CACHE = Path(os.environ.get(
    "GLB_CACHE_DIR", str(Path.home() / "ai-ar-v3-assets" / "_glbcache")))


def cbin_dir_to_colored_mesh(
    chunk_dir: Path,
) -> Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """`.cbin` 디렉터리 → (vertices, faces, colors_rgba8|None).

    `realasset.cbin_dir_to_mesh()` 와 같은 이음 방식이되 **색을 버리지 않는다.**
    색이 한 청크라도 없으면 전체를 None 으로 낸다 — 일부만 색이 있으면 나머지가
    검게 나와서 "색이 없다" 와 "색이 검다" 가 화면에서 같아 보인다.
    """
    files = sorted(Path(chunk_dir).glob("*.cbin"))
    if not files:
        raise FileNotFoundError(f".cbin 이 없다: {chunk_dir}")

    vs: List[np.ndarray] = []
    fs: List[np.ndarray] = []
    cs: List[np.ndarray] = []
    offset = 0
    have_color = True
    for f in files:
        mesh = decode(f.read_bytes())
        v = np.asarray(mesh.positions, dtype=np.float64)
        vs.append(v)
        fs.append(np.asarray(mesh.indices, dtype=np.int64).reshape(-1, 3) + offset)
        offset += v.shape[0]
        c = getattr(mesh, "colors", None)
        if c is None or len(c) != len(v):
            have_color = False
        else:
            cs.append(np.asarray(c, dtype=np.uint8))

    verts = np.concatenate(vs, axis=0)
    faces = np.concatenate(fs, axis=0)
    colors = np.concatenate(cs, axis=0) if (have_color and cs) else None
    return verts, faces, colors


def build_glb(chunk_dir: Path, out: Path) -> Path:
    """청크를 이어 붙여 GLB 를 만든다. **A5000 에 요청하지 않는다.**

    `.cbin` 은 디코더가 낸 실제 표면 메시라 대용물이 아니다 — 원본 그대로다.
    """
    import trimesh  # 이미 선언된 의존성이다 (새로 추가하지 않는다)

    verts, faces, colors = cbin_dir_to_colored_mesh(chunk_dir)
    # 🔴 D9 — 복셀 프레임(Z-up) → GLB(Y-up). 정본 상수를 건다.
    mesh = trimesh.Trimesh(
        vertices=VOXEL_TO_GLB.apply(verts), faces=faces, process=False,
        vertex_colors=colors if colors is not None else None,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(out)
    return out


def cached_glb(chunk_dir: Path, tag: str) -> Path:
    """만들어 두고 재사용한다. 캐시 키는 **청크 파일들의 내용**에서 낸다.

    mtime 을 쓰지 않는 이유: 같은 내용을 다시 쓴 것과 실제로 바뀐 것이 구분되지
    않고, 그러면 화면이 옛 모델을 계속 보여주면서 아무 증상도 안 낸다.
    """
    files = sorted(Path(chunk_dir).glob("*.cbin"))
    h = hashlib.sha1()
    for f in files:
        st = f.stat()
        h.update(f.name.encode())
        h.update(str(st.st_size).encode())
    out = GLB_CACHE / f"{tag}-{h.hexdigest()[:12]}.glb"
    if not out.is_file():
        build_glb(Path(chunk_dir), out)
    return out
