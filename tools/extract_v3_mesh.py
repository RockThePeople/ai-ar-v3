#!/usr/bin/env python3
"""구 계약(v3) `.cbin` → 정점·면·색 `.npz`. **마이그레이션 전용 읽기 도구.**

    python3 tools/extract_v3_mesh.py <v3_contract_python_path> <src_chunk_dir> <out.npz>

🔴 왜 별도 프로세스인가. v4 디코더는 v3 바이트를 **거부한다** —
   `ChunkBinError: 계약 버전 불일치: file=3, local=4`. 그게 D75 의 안전장치이고
   옳게 동작하는 것이다. 옛 바이트를 읽으려면 **옛 리더**가 필요하다.

   그래서 계약을 고치지 않는다. 대신 D75 **직전 커밋의 워크트리**를 만들어 그
   `deltacontract` 로 디코드하고, 결과를 계약과 무관한 순수 numpy 배열로 넘긴다.
   재분할·인코딩은 현재(v4) 계약이 한다. 두 계약이 한 프로세스에서 만나지 않는다.
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np


def main() -> int:
    contract_path, src, out = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    sys.path.insert(0, contract_path)
    from deltacontract import CONTRACT_VERSION            # noqa: E402
    from deltacontract.chunkbin import decode             # noqa: E402

    files = sorted(src.glob("*.cbin"))
    if not files:
        raise SystemExit(f".cbin 이 없다: {src}")

    vs, fs, cs = [], [], []
    offset, have_color = 0, True
    for f in files:
        m = decode(f.read_bytes())
        v = np.asarray(m.positions, dtype=np.float64)
        vs.append(v)
        fs.append(np.asarray(m.indices, dtype=np.int64).reshape(-1, 3) + offset)
        offset += len(v)
        c = getattr(m, "colors", None)
        if c is None or len(c) != len(v):
            have_color = False
        else:
            cs.append(np.asarray(c, dtype=np.uint8))

    verts = np.concatenate(vs); faces = np.concatenate(fs)
    payload = {"vertices": verts, "faces": faces, "src_contract": np.int64(CONTRACT_VERSION)}
    if have_color and cs:
        payload["colors"] = np.concatenate(cs)
    np.savez_compressed(out, **payload)
    print(f"v{CONTRACT_VERSION} {src.parent.name}/{src.name}: 청크 {len(files)} · "
          f"정점 {len(verts):,} · 면 {len(faces):,} · 색 {'있음' if have_color else '없음'} → {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
