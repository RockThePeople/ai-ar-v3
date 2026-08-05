#!/usr/bin/env python3
"""moto-b 뒷바퀴 라쏘 케이스 생성 (W19).

⚠️ 여기 있는 투영식은 **폴리곤을 설계하기 위한 것**이지 판정이 아니다.
   판정은 C# 하네스와 Unity 가 한다 (D65: 엔진 밖 모형은 엔진 안 결과의 증거가 아니다).
   설계용 식조차 Unity 와 같은 **왼손** 기저를 쓴다 (W18).
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
NPY = ROOT / "handoff/lasso/moto-b.slat_coords.npy"

# 측면 카메라 — x 축을 따라 본다. 화면 x ∝ 월드 y(길이), 화면 y ∝ 월드 z(높이).
CAMERA = {"position": [-20.0, 0.0, 0.0], "target": [0.0, 0.0, 0.0], "up": [0.0, 0.0, 1.0],
          "fov_deg": 6.0, "width": 1080.0, "height": 1920.0}
FOCAL = (CAMERA["height"] / 2) / math.tan(math.radians(CAMERA["fov_deg"]) / 2)
DEPTH = 20.0     # 중앙 깊이. 실제는 19.5~20.5 라 ±2.5% 흔들린다 → 여유를 준다


def to_screen(vy: float, vz: float, margin: float = 0.0):
    """복셀 (y,z) → 화면. margin 은 중심에서 바깥으로 미는 여유(px)."""
    ly, lz = (vy + 0.5) / 64 - 0.5, (vz + 0.5) / 64 - 0.5
    sx, sy = ly / DEPTH * FOCAL + 540.0, lz / DEPTH * FOCAL + 960.0
    if margin:
        dx, dy = sx - 540.0, sy - 960.0
        n = math.hypot(dx, dy) or 1.0
        sx, sy = sx + dx / n * margin, sy + dy / n * margin
    return [round(sx, 3), round(sy, 3)]


#: 뒷바퀴 중심·반지름 (실루엣에서 읽었다). 바퀴는 y 44~63 · z 8~35 에 걸친다.
WHEEL_C = (50.5, 21.5)
WHEEL_R = 13.5


def arc(y_min=None, n=48, r=WHEEL_R):
    """바퀴 둘레를 따르는 폴리곤. `y_min` 이 있으면 그 왼쪽을 **잘라낸다.**"""
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        vy = WHEEL_C[0] + r * math.cos(t)
        vz = WHEEL_C[1] + r * math.sin(t)
        if y_min is not None and vy < y_min:
            vy = y_min                      # 왼쪽을 수직선으로 자른다
        pts.append((vy, vz))
    # 중복(잘린 변) 정리
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return [to_screen(vy, vz, margin=4.0) for vy, vz in out]


#: 25° 사면 카메라. **압출이 일을 하는 조건**을 찾으려고 넣었다 (W19).
#  채움 축이 시선 축과 같으면 압출은 구조적으로 순증 0이다 — 시차를 만들어 본 것이다.
OBLIQUE_DEG = 25.0
OBLIQUE_CAMERA = dict(
    CAMERA,
    position=[-20 * math.cos(math.radians(OBLIQUE_DEG)),
              -20 * math.sin(math.radians(OBLIQUE_DEG)), 0.0],
)

CASES = {
    # ★ 다리·스윙암 접촉부(y 40~47)를 피해 왼쪽을 y=46 에서 자른다
    "moto-rear-wheel": arc(y_min=46.0),
    # 대조 — 바퀴 원판 전체. 왼쪽 호가 다리와 같은 화면 위치라 무엇이 딸려 오는지 본다
    "moto-rear-wheel-full": arc(y_min=None),
    # 같은 폴리곤을 **사면 카메라**로. 카메라만 다르다.
    "moto-oblique": arc(y_min=46.0),
}

#: 케이스별 카메라. 없으면 정측면.
CASE_CAMERA = {"moto-oblique": OBLIQUE_CAMERA}


def main() -> None:
    out_dir = pathlib.Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    coords = np.unique(np.load(NPY).astype(np.int64), axis=0)
    for name, poly in CASES.items():
        (out_dir / f"{name}.json").write_text(json.dumps(
            {"slat_coords": coords.tolist(), "polygon": poly,
             "camera": CASE_CAMERA.get(name, CAMERA)}))
        print(f"{name}: 폴리곤 {len(poly)}점 · 복셀 {len(coords)}")


if __name__ == "__main__":
    main()
