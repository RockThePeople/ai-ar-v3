"""라쏘 검증용 합성 자산 — **바퀴 대역이 z 로 안 갈리게** 만든 것이 요점이다 (D57).

작업 2 의 관문은 "바퀴를 빼고 몸체만" 이다. 세션이 손으로 만든 z 밴드 마스크는
지금까지 다섯 번 정정됐다 (D25-a/b/c/d · D50 · D53). 그래서 이 픽스처는
**바퀴와 몸체의 z 범위를 일부러 겹쳐 놓는다** — 어떤 z 밴드로도 못 가른다.
가르려면 화면에서 둘러싸는 수밖에 없고, 그것이 라쏘다.

좌표계는 VOXEL 격자 [0,64) · Z-up (D9). 카메라는 -Y 에서 본다 = 측면도.
"""

from __future__ import annotations

import numpy as np

#: 몸체 x 범위와 바퀴 x 범위 사이의 간격. 원근으로 뭉개지지 않을 만큼 띄운다.
BODY_X = (24, 40)
BODY_Y = (24, 40)
BODY_Z = (26, 48)
WHEEL_CENTERS = ((14, 32, 30), (50, 32, 30))
WHEEL_R = 5


def build_car() -> dict:
    """몸체 껍질 + 바퀴 둘. 반환값은 slat coords 와 부위별 셀 집합."""
    body = []
    x0, x1 = BODY_X
    y0, y1 = BODY_Y
    z0, z1 = BODY_Z
    t = 2
    for x in range(x0, x1):
        for y in range(y0, y1):
            for z in range(z0, z1):
                on_shell = (
                    x < x0 + t or x >= x1 - t
                    or y < y0 + t or y >= y1 - t
                    or z < z0 + t or z >= z1 - t
                )
                if on_shell:
                    body.append((x, y, z))

    wheels = []
    for cx, cy, cz in WHEEL_CENTERS:
        for x in range(cx - WHEEL_R, cx + WHEEL_R + 1):
            for y in range(cy - WHEEL_R, cy + WHEEL_R + 1):
                for z in range(cz - WHEEL_R, cz + WHEEL_R + 1):
                    d2 = (x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2
                    if (WHEEL_R - 2) ** 2 <= d2 <= WHEEL_R**2:
                        wheels.append((x, y, z))

    body_a = np.array(sorted(set(body)), dtype=np.int64)
    wheel_a = np.array(sorted(set(wheels)), dtype=np.int64)
    allc = np.array(sorted(set(map(tuple, body_a.tolist())) | set(map(tuple, wheel_a.tolist()))),
                    dtype=np.int64)
    return {"slat_coords": allc, "body": body_a, "wheels": wheel_a}


#: 측면 카메라. **멀리서 좁은 화각**으로 본다 — 원근으로 몸체 앞면이 바퀴보다
#  넓게 투영되면 x 로도 안 갈린다. 그건 라쏘의 문제가 아니라 관측 조건의 문제다.
CAMERA = {
    "position": [0.0, -20.0, 0.0],
    "target": [0.0, 0.0, 0.0],
    "up": [0.0, 0.0, 1.0],
    "fov_deg": 6.0,
    "width": 1080.0,
    "height": 1920.0,
}

#: 몸체만 둘러 그린 궤적. 손으로 그린 것처럼 들쭉날쭉하되 ±150px 안에 머문다.
BODY_POLYGON = [
    [408.0, 300.0], [430.0, 1150.0], [470.0, 1420.0], [530.0, 1560.0],
    [540.0, 1620.0], [600.0, 1660.0], [672.0, 1640.0], [700.0, 1520.0],
    [672.0, 1300.0], [660.0, 900.0], [640.0, 480.0], [600.0, 320.0],
    [520.0, 280.0], [450.0, 285.0],
]

#: ★ **손잡이(handedness) 잠금 케이스** (W18). 한쪽 바퀴만 둘러싼다.
#
#  자산이 x 대칭이라 몸체 라쏘로는 좌우 뒤집힘이 **셀 수로도 계수로도** 안 잡힌다 —
#  W18 에서 실제로 그랬다(모든 단계 계수 일치, 지문만 불일치). 한쪽 바퀴만 잡으면
#  좌우가 뒤집히는 순간 **다른 바퀴**가 잡히므로 소속으로 잡힌다.
ONE_WHEEL_POLYGON = [
    [180.0, 820.0], [380.0, 820.0], [380.0, 1060.0], [180.0, 1060.0],
]

#: 양성 대조 — 전부 감싼다. 이걸 안 두면 "아무것도 안 잡는 구현" 이 통과한다.
WIDE_POLYGON = [[10.0, 10.0], [1070.0, 10.0], [1070.0, 1910.0], [10.0, 1910.0]]


def probe_input(polygon) -> dict:
    car = build_car()
    return {
        "slat_coords": car["slat_coords"].tolist(),
        "polygon": polygon,
        "camera": CAMERA,
    }
