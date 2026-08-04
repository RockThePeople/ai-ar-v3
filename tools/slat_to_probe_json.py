#!/usr/bin/env python3
"""자산의 slat coords(.npy/.json) → 라쏘 하네스 입력 JSON (W17 ①).

3090 이 dragon-c 의 slat coords 를 갖고 있다. 그걸 이 스크립트로 넘기면
`unity/Headless` 하네스나 Unity Editor 창이 그대로 읽는다.

    python3 tools/slat_to_probe_json.py coords.npy out.json [--polygon poly.json]

🔴 입력은 **slat coords** 여야 한다 (D28-a). `.cbin` 에서 역산하지 마라 —
   `.cbin` 에는 slat coords 가 없다 (D34). 두 번 실패했다.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "contract" / "python"))
from deltacontract import mask_fingerprint  # noqa: E402
from deltacontract.coords import VOXEL_RES  # noqa: E402

DEFAULT_CAMERA = {
    "position": [0.0, -20.0, 0.0], "target": [0.0, 0.0, 0.0], "up": [0.0, 0.0, 1.0],
    "fov_deg": 6.0, "width": 1080.0, "height": 1920.0,
}


def load_coords(path: pathlib.Path) -> np.ndarray:
    if path.suffix == ".npy":
        a = np.load(path)
    else:
        raw = json.loads(path.read_text())
        a = np.asarray(raw["slat_coords"] if isinstance(raw, dict) else raw)
    a = np.asarray(a, dtype=np.int64).reshape(-1, 3)
    if a.size and (a.min() < 0 or a.max() >= VOXEL_RES):
        raise SystemExit(
            f"좌표가 격자 [0,{VOXEL_RES}) 를 벗어난다: [{a.min()}, {a.max()}]. "
            "SLat 격자가 맞는지 확인해라 — 다른 격자면 판정 전체가 다른 물체에 대한 숫자다 (D28)."
        )
    return np.unique(a, axis=0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("coords")
    ap.add_argument("out")
    ap.add_argument("--polygon", help="화면 폴리곤 JSON ([[x,y], …]). 없으면 빈 목록")
    ap.add_argument("--camera", help="카메라 JSON. 없으면 측면 기본값")
    args = ap.parse_args()

    coords = load_coords(pathlib.Path(args.coords))
    poly = json.loads(pathlib.Path(args.polygon).read_text()) if args.polygon else []
    cam = json.loads(pathlib.Path(args.camera).read_text()) if args.camera else DEFAULT_CAMERA

    pathlib.Path(args.out).write_text(json.dumps(
        {"slat_coords": coords.tolist(), "polygon": poly, "camera": cam}))
    print(f"복셀 {len(coords)} · 전체 지문 {mask_fingerprint(coords)} → {args.out}")


if __name__ == "__main__":
    main()
