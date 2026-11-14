"""S20 자동 볼륨 마스킹 — 시점 카메라·투영·역투영·격자 이미지(순수 함수, numpy 만. 파일·네트워크 없음).

좌표: NORMALIZED(복셀 축, 계약 2.2). 셀 (i, j, k) 중심 = (idx + 0.5) / 64 − 0.5, z 가 위.
A5000 `/render` 는 NORMALIZED → GLB (x, z, −y) → 판본 정규화 (p − c)·s → Blender (x, −z, y) 로 바꾼다. 축 변환 둘을
합치면 항등이고 정규화는 균일 배율 + 평행이동이라, 같은 카메라로 복셀 프레임에서 핀홀 투영한 화소가 렌더 화소와 같다
(Voxhammer-light `editcore/render.py` camera_to_blender "full" 모드, 정사각 1024², 화각 max(fov_y, fov_x)).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np

GRID = 64
RES = 1024
VIEW_AZ = {"az000": 0.0, "az090": 90.0, "az180": 180.0, "az270": 270.0}   # 방위 0° = 복셀 +y 쪽에서 원점을 봄, 90° = +x
VIEW_KO = {"az000": "0°", "az090": "90°", "az180": "180°", "az270": "270°"}
ELEV_DEG = 15.0
FOV_Y_DEG = 40.0
MAX_CELLS = 65536


def centers(cells) -> np.ndarray:
    c = np.asarray(cells, np.float64).reshape(-1, 3)
    return (c + 0.5) / GRID - 0.5


def view_cameras(occupied) -> Dict[str, dict]:
    """S20 2.1: 점유 셀 경계 상자 중심 c, 반경 r(c 에서 가장 먼 셀 중심 + 반 셀). 고도 15°, fov_y 40°,
    거리 = r / sin(20°) × 1.1(원점 거리 0.6 아래면 0.6), forward → c, up ≈ +z. 계약 2.2 모양."""
    p = centers(occupied)
    lo, hi = p.min(0), p.max(0)
    c = (lo + hi) / 2
    r = float(np.linalg.norm(p - c, axis=1).max()) + 0.5 / GRID
    dist = max(r / math.sin(math.radians(FOV_Y_DEG / 2)) * 1.1, 0.6)
    e = math.radians(ELEV_DEG)
    out = {}
    for name, az in VIEW_AZ.items():
        a = math.radians(az)
        d = np.array([math.cos(e) * math.sin(a), math.cos(e) * math.cos(a), math.sin(e)])
        pos = c + dist * d
        if np.linalg.norm(pos) <= 0.5:                     # 계약 2.2: 정육면체 밖
            pos = c + (dist + 0.6) * d
        f = -d
        up = np.array([-math.sin(e) * math.sin(a), -math.sin(e) * math.cos(a), math.cos(e)])
        out[name] = {"frame": "normalized_voxel", "position": [round(float(x), 6) for x in pos],
                     "forward": [round(float(x), 6) for x in f], "up": [round(float(x), 6) for x in up],
                     "fov_y_deg": FOV_Y_DEG, "aspect": 1.0, "screen_px": [RES, RES]}
    return out


def project(points: np.ndarray, cam: dict, res: int = RES) -> tuple:
    """복셀 프레임 점 → (px, py, z). px 오른쪽, py 아래로. z ≤ 0 은 카메라 뒤."""
    f = np.asarray(cam["forward"], np.float64)
    f = f / np.linalg.norm(f)
    u = np.asarray(cam["up"], np.float64)
    r = np.cross(f, u)
    r = r / np.linalg.norm(r)
    u2 = np.cross(r, f)
    fy = math.radians(float(cam["fov_y_deg"]))
    fx = 2.0 * math.atan(float(cam.get("aspect", 1.0)) * math.tan(fy / 2))
    t = math.tan(max(fy, fx) / 2)
    d = points - np.asarray(cam["position"], np.float64)
    z = d @ f
    zs = np.where(z > 1e-9, z, 1e-9)
    px = (d @ r / zs / t * 0.5 + 0.5) * res
    py = (0.5 - d @ u2 / zs / t * 0.5) * res
    return px, py, z


def _all_cells() -> np.ndarray:
    g = np.arange(GRID)
    return np.stack(np.meshgrid(g, g, g, indexing="ij"), -1).reshape(-1, 3)


def _shift_or(a: np.ndarray) -> np.ndarray:
    o = a.copy()
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == dy == dz == 0:
                    continue
                s = np.zeros_like(a)
                xs = slice(max(dx, 0), GRID + min(dx, 0))
                xd = slice(max(-dx, 0), GRID + min(-dx, 0))
                ys = slice(max(dy, 0), GRID + min(dy, 0))
                yd = slice(max(-dy, 0), GRID + min(-dy, 0))
                zs = slice(max(dz, 0), GRID + min(dz, 0))
                zd = slice(max(-dz, 0), GRID + min(-dz, 0))
                s[xd, yd, zd] = a[xs, ys, zs]
                o |= s
    return o


def dilate(occupied, k: int = 2) -> np.ndarray:
    occ = np.zeros((GRID,) * 3, bool)
    c = np.asarray(occupied, int).reshape(-1, 3)
    if len(c):
        occ[c[:, 0], c[:, 1], c[:, 2]] = True
    for _ in range(k):
        occ = _shift_or(occ)
    return occ


def shrink_box(b: Sequence[float], k: float) -> list:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    hw, hh = (b[2] - b[0]) / 2 * k, (b[3] - b[1]) / 2 * k
    return [cx - hw, cy - hh, cx + hw, cy + hh]


def region_cells(boxes: Dict[str, Optional[Sequence[float]]], cams: Dict[str, dict], scale: float = 1.0) -> np.ndarray:
    """상자를 준 모든 시점의 상자 안에 셀 중심이 투영되는 셀(빈 공간 포함). 상자 좌표 0–1000 = 이미지 전체."""
    cells = _all_cells()
    p = centers(cells)
    keep = np.ones(len(cells), bool)
    used = 0
    for v, b in boxes.items():
        if not b or v not in cams:
            continue
        b = shrink_box(b, scale) if scale != 1.0 else b
        px, py, z = project(p, cams[v])
        s = RES / 1000.0
        keep &= (z > 0) & (px >= b[0] * s) & (px <= b[2] * s) & (py >= b[1] * s) & (py <= b[3] * s)
        used += 1
    if used == 0:
        return np.zeros((0, 3), int)
    return cells[keep]


def backproject(regions: List[dict], cams: Dict[str, dict], occupied, op: str) -> dict:
    """S20 2.2 역투영. replace·remove·recolor = 점유 2셀 팽창과 겹치는 셀만, add = 전부. region 합집합.
    N > 65,536 이면 모든 상자를 중심 기준 0.9배씩 줄여 다시(최대 3번). → {cells, n, shrink, too_large, per_region}."""
    near = dilate(occupied, 2) if op in ("replace", "remove", "recolor") else None
    scale = 1.0
    for shrink in range(4):
        sets, per = [], []
        for rg in regions:
            c = region_cells(rg.get("boxes") or {}, cams, scale)
            if near is not None and len(c):
                c = c[near[c[:, 0], c[:, 1], c[:, 2]]]
            sets.append(c)
            per.append({"name": rg.get("name"), "n": int(len(c))})
        allc = np.unique(np.concatenate(sets), axis=0) if sets and any(len(s) for s in sets) else np.zeros((0, 3), int)
        if len(allc) <= MAX_CELLS:
            return {"cells": allc.tolist(), "n": int(len(allc)), "shrink": shrink, "scale": round(scale, 4),
                    "too_large": False, "per_region": per}
        scale *= 0.9
    return {"cells": [], "n": int(len(allc)), "shrink": 3, "scale": round(scale / 0.9, 4), "too_large": True,
            "per_region": per}


# ── 이미지(격자·라벨·2×2) ─────────────────────────────────────────
_FONT = {   # 5×7 비트맵, 행마다 5비트
    "0": [14, 17, 19, 21, 25, 17, 14], "1": [4, 12, 4, 4, 4, 4, 14], "2": [14, 17, 1, 2, 4, 8, 31],
    "3": [31, 2, 4, 2, 1, 17, 14], "4": [2, 6, 10, 18, 31, 2, 2], "5": [31, 16, 30, 1, 1, 17, 14],
    "6": [6, 8, 16, 30, 17, 17, 14], "7": [31, 1, 2, 4, 8, 8, 8], "8": [14, 17, 17, 14, 17, 17, 14],
    "9": [14, 17, 17, 15, 1, 2, 12], "a": [0, 0, 14, 1, 15, 17, 15], "z": [0, 0, 31, 2, 4, 8, 31],
    "x": [0, 0, 17, 10, 4, 10, 17], "y": [0, 0, 17, 17, 15, 1, 14], " ": [0] * 7, "-": [0, 0, 0, 31, 0, 0, 0],
}


def draw_text(img: np.ndarray, text: str, x: int, y: int, scale: int = 3, color=(0, 0, 0), bg=(255, 255, 255)) -> None:
    """img (H, W, 3) uint8 에 글자를 그린다(배경 상자 포함). 없는 글자는 빈칸."""
    H, W = img.shape[:2]
    w = (6 * len(text) + 1) * scale
    h = 9 * scale
    x0, y0 = max(0, x), max(0, y)
    img[y0:min(H, y + h), x0:min(W, x + w)] = bg
    for n, ch in enumerate(text):
        rows = _FONT.get(ch, _FONT[" "])
        for ry, bits in enumerate(rows):
            for rx in range(5):
                if bits >> (4 - rx) & 1:
                    yy, xx = y + (ry + 1) * scale, x + (1 + n * 6 + rx) * scale
                    img[max(0, yy):max(0, min(H, yy + scale)), max(0, xx):max(0, min(W, xx + scale))] = color


def to_rgb(a: np.ndarray) -> np.ndarray:
    from .cropinpaint import render_rgb
    return render_rgb(a)


def grid_image(rgb: np.ndarray, label: str) -> np.ndarray:
    """0–1000 좌표 격자(100 간격 선 + 위·왼쪽 축 숫자) + 왼쪽 위에 시점 이름. 격자선은 옅은 파랑, 500 은 진하게."""
    img = rgb.copy()
    H, W = img.shape[:2]
    for v in range(0, 1001, 100):
        x = min(W - 1, round(v / 1000 * W))
        y = min(H - 1, round(v / 1000 * H))
        col = np.array([40, 90, 230] if v == 500 else [120, 160, 255], np.uint8)
        img[:, max(0, x - 1):x + 1] = (img[:, max(0, x - 1):x + 1] * 0.4 + col * 0.6).astype(np.uint8)
        img[max(0, y - 1):y + 1, :] = (img[max(0, y - 1):y + 1, :] * 0.4 + col * 0.6).astype(np.uint8)
    for v in range(100, 1000, 100):
        p = round(v / 1000 * W)
        draw_text(img, str(v), p - 25, 2, scale=2, color=(30, 60, 200))
        draw_text(img, str(v), 2, round(v / 1000 * H) - 9, scale=2, color=(30, 60, 200))
    draw_text(img, label, W - 6 * len(label) * 4 - 12, H - 44, scale=4)
    return img


def half(a: np.ndarray) -> np.ndarray:
    """2×2 평균으로 반 크기."""
    h, w = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
    a = a[:h, :w].astype(np.uint16)
    return ((a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2] + a[1::2, 1::2] + 2) // 4).astype(np.uint8)


def mosaic(imgs: Dict[str, np.ndarray], labels: bool = True) -> np.ndarray:
    """4장(az000·az090·az180·az270 순서) → 2×2, 각 칸 반 크기, 칸마다 시점 이름."""
    tiles = []
    for v in VIEW_AZ:
        t = half(imgs[v])
        if labels:
            t = t.copy()
            draw_text(t, v, 8, 8, scale=3)
        tiles.append(t)
    top = np.concatenate(tiles[:2], 1)
    bot = np.concatenate(tiles[2:], 1)
    return np.concatenate([top, bot], 0)


# ── S20d 좌우 규칙(사용자 결정 2026-11-14) ─────────────────────────────
# "오른쪽·왼쪽"(위치) = 보는 사람 기준: 앞 시점 이미지의 그 쪽.
# "오른손·왼손"처럼 캐릭터 몸 부위 = 캐릭터 기준: 캐릭터를 마주 보므로 앞 시점 이미지에서 반대쪽.
import re as _re

_BODY_KO = "손목|손등|손바닥|손|발목|발등|발|팔꿈치|팔|다리|무릎|허벅지|눈썹|눈|귀|어깨|뺨|볼|옆구리"
_BODY_EN = "hands?|feet|foot|arms?|legs?|eyes?|ears?|shoulders?|knees?|wrists?|ankles?|elbows?|thighs?|cheeks?|hips?"
_SIDE_RULES = (   # (패턴, 기준) — 몸 부위가 먼저(“오른쪽 손”도 캐릭터 기준)
    (_re.compile(rf"(오른|왼)\s*(?:쪽\s*)?(?:{_BODY_KO})"), "character"),
    (_re.compile(rf"\b(right|left)[\s-]+(?:{_BODY_EN})\b", _re.I), "character"),
    (_re.compile(r"(오른|왼)\s*(?:쪽|편)"), "viewer"),
    (_re.compile(r"\b(?:on|to|at)\s+the\s+(right|left)\b|\b(right|left)\s+side\b", _re.I), "viewer"),
)


def parse_side(text: str) -> Optional[dict]:
    """지시문 → {"side": "right"|"left", "frame": "character"|"viewer", "word": 찾은 말}. 없거나 서로 다르면 None(애매)."""
    found = []
    for pat, frame in _SIDE_RULES:
        for m in pat.finditer(text or ""):
            w = next(g for g in m.groups() if g)
            side = "right" if w.lower() in ("오른", "right") else "left"
            if not any(m.start() < e and s < m.end() for s, e, *_ in found):   # 이미 잡은 구간과 겹치면 건너뜀
                found.append((m.start(), m.end(), side, frame, m.group(0)))
    kinds = {(f[2], f[3]) for f in found}
    if len(kinds) != 1:
        return None
    s, e, side, frame, word = found[0]
    return {"side": side, "frame": frame, "word": word.strip()}


def expected_image_side(side: str, frame: str) -> str:
    """앞 시점 이미지에서 칠해져야 할 쪽. 캐릭터 기준이면 뒤집힌다."""
    if frame == "viewer":
        return side
    return "left" if side == "right" else "right"


def _right_vec(cam: dict) -> np.ndarray:
    f = np.asarray(cam["forward"], np.float64)
    f = f / np.linalg.norm(f)
    r = np.cross(f, np.asarray(cam["up"], np.float64))
    return r / np.linalg.norm(r)


def image_side_of(cells, occupied, cam: dict, margin_px: float = 8.0) -> dict:
    """앞 시점 카메라에서 몸(점유 셀) 중심과 칠한 셀 중심의 이미지 x 비교 → {"side": left|right|center, "dx"}."""
    body = centers(occupied).mean(0)
    mask = centers(cells).mean(0)
    bx = project(body[None], cam)[0][0]
    mx = project(mask[None], cam)[0][0]
    dx = float(mx - bx)
    side = "center" if abs(dx) < margin_px else ("right" if dx > 0 else "left")
    return {"side": side, "dx": round(dx, 1), "body_x": round(float(bx), 1), "mask_x": round(float(mx), 1)}


def mirror_cells(cells, occupied, cam: dict) -> list:
    """몸 중심을 지나고 앞 시점 카메라 오른쪽 방향에 수직인 면으로 셀을 뒤집는다(정수 셀로 반올림, 중복 제거)."""
    c = centers(occupied).mean(0)
    r = _right_vec(cam)
    p = centers(cells)
    q = p - 2.0 * ((p - c) @ r)[:, None] * r[None, :]
    idx = np.clip(np.rint((q + 0.5) * GRID - 0.5), 0, GRID - 1).astype(int)
    return np.unique(idx, axis=0).tolist()
