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


# ══════════════════════════════════════════════════════════ S20e 사용자 라쏘 볼륨 다듬기
# 사용자 결정(2026-11-14 21:09): 자동 마스킹 대신 사용자 라쏘 볼륨 U 를 다듬는다. 상수는 물체 크기 L 에 비례(21:20).
DEPTH_RATIO = 0.10      # T = clamp(round(0.10·L), 3, 16)
MERGE_RATIO = 0.13      # M = clamp(round(0.13·L), 4, 16)
SIDE_PAD = 30           # 옆 자르기·빈 셀 화면 여유 3%(0–1000 단위)
MERGE_PAD = 50          # 합치기 화면 여유 5%
ZRES = 256              # 깊이 버퍼 해상도


def size_consts(occupied, depth_ratio: float = DEPTH_RATIO, merge_ratio: float = MERGE_RATIO,
                fixed: Optional[tuple] = None) -> dict:
    """L = 점유 셀 경계 상자 최장 변(셀), T·M. fixed=(T, M) 이면 고정 상수(비교용)."""
    c = np.asarray(occupied, int).reshape(-1, 3)
    L = int((c.max(0) - c.min(0) + 1).max()) if len(c) else 0
    if fixed:
        return {"L": L, "T": int(fixed[0]), "M": int(fixed[1]), "fixed": True}
    T = int(min(16, max(3, round(depth_ratio * L))))
    M = int(min(16, max(4, round(merge_ratio * L))))
    return {"L": L, "T": T, "M": M, "fixed": False}


def _grid_xy(points: np.ndarray, cam: dict) -> tuple:
    """복셀 점 → (x, y, z), x·y 는 0–1000 화면 단위(렌더 정사각 전체 = 0–1000)."""
    px, py, z = project(points, cam)
    s = 1000.0 / RES
    return px * s, py * s, z


def lasso_cells(cam: dict, polygon: Sequence[Sequence[float]]) -> list:
    """시험·실측 도구: 카메라 시선 방향 관통 볼륨 — 중심이 다각형(0–1000 화면 단위) 안에 투영되는 64³ 셀 전부(빈 셀 포함)."""
    cells = _all_cells()
    x, y, z = _grid_xy(centers(cells), cam)
    poly = np.asarray(polygon, float)
    inside = np.zeros(len(cells), bool)
    j = len(poly) - 1
    for i in range(len(poly)):                     # 짝홀 규칙
        xi, yi = poly[i]
        xj, yj = poly[j]
        cross = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / np.where(yj - yi == 0, 1e-9, yj - yi) + xi)
        inside ^= cross
        j = i
    return cells[inside & (z > 0)].tolist()


def depth_buffer(occupied, cam: dict, res: int = ZRES) -> tuple:
    """사용자 카메라에서 화소마다 첫 점유 셀 깊이 d₀(복셀 단위 = z·64). 셀은 화면에 비친 크기만큼(반경 ≥ 1, ≤ 12 화소) 찍어
    셀 사이 틈이 생기지 않게 한다. → (zbuf, fn(셀들)→d₀)."""
    import math
    p = centers(occupied)
    x, y, z = _grid_xy(p, cam)
    fy = math.radians(float(cam["fov_y_deg"]))
    fx = 2.0 * math.atan(float(cam.get("aspect", 1.0)) * math.tan(fy / 2))
    t = math.tan(max(fy, fx) / 2)
    zs = np.where(z > 1e-6, z, 1e-6)
    half_px = (0.5 / GRID) / (zs * t) * 0.5 * res                 # 셀 반 크기(화소)
    rad = np.clip(np.ceil(half_px * 1.5), 1, 12).astype(int)      # 대각선 여유
    ix = np.clip((x / 1000 * res).astype(int), 0, res - 1)
    iy = np.clip((y / 1000 * res).astype(int), 0, res - 1)
    zb = np.full((res, res), np.inf)
    zc = z * GRID
    R = int(rad.max()) if len(rad) else 1
    for dx in range(-R, R + 1):
        for dy in range(-R, R + 1):
            sel = rad >= max(abs(dx), abs(dy))
            if sel.any():
                np.minimum.at(zb, (np.clip(iy[sel] + dy, 0, res - 1), np.clip(ix[sel] + dx, 0, res - 1)), zc[sel])

    def d0_of(cells) -> np.ndarray:
        q = centers(cells)
        qx, qy, _ = _grid_xy(q, cam)
        jx = np.clip((qx / 1000 * res).astype(int), 0, res - 1)
        jy = np.clip((qy / 1000 * res).astype(int), 0, res - 1)
        return zb[jy, jx]
    return zb, d0_of


def _in_box(x, y, box, pad: float) -> np.ndarray:
    if not box:
        return np.zeros(len(x), bool)
    return (x >= box[0] - pad) & (x <= box[2] + pad) & (y >= box[1] - pad) & (y <= box[3] + pad)


def refine_volume(user_cells, occupied, cam: dict, plan: dict, consts: dict) -> dict:
    """S20e 2.2 잘라내기 + 2.3 합치기. plan = {op, keep_box, old_object_box, new_space_box}(0–1000 화면 단위).
    → {cells, n_user, cut_depth, cut_side, cut_empty, merged, n_final, fallback, ...}."""
    T, M = consts["T"], consts["M"]
    op = plan.get("op") or "add"
    keep, old, new = plan.get("keep_box"), plan.get("old_object_box"), plan.get("new_space_box")
    if op not in ("replace", "remove"):
        old = None
    if op not in ("add", "replace"):
        new = None
    U = np.unique(np.asarray(user_cells, int).reshape(-1, 3), axis=0)
    occ_grid = np.zeros((GRID,) * 3, bool)
    O = np.asarray(occupied, int).reshape(-1, 3)
    occ_grid[O[:, 0], O[:, 1], O[:, 2]] = True
    is_occ = occ_grid[U[:, 0], U[:, 1], U[:, 2]]
    x, y, z = _grid_xy(centers(U), cam)
    zc = z * GRID
    _, d0_of = depth_buffer(O, cam)
    d0 = d0_of(U)
    in_old = _in_box(x, y, old, 0)
    # 1 깊이 자르기(점유 셀만): d₀ + T 보다 먼 셀 제외, 원래 물건 상자 안은 2T
    lim = d0 + np.where(in_old, 2 * T, T)
    depth_ok = zc <= lim + 0.5
    # 2 옆 자르기(점유 셀만): keep ∪ old (+3%) 밖 제외
    side_ok = _in_box(x, y, keep, SIDE_PAD) | _in_box(x, y, old, SIDE_PAD)
    # 3 빈 셀: new ∪ keep (+3%) 안만, recolor 는 전부 제외
    empty_ok = np.zeros(len(U), bool) if op == "recolor" else (_in_box(x, y, new, SIDE_PAD) | _in_box(x, y, keep, SIDE_PAD))
    keep_occ = is_occ & depth_ok & side_ok
    keep_emp = (~is_occ) & empty_ok
    kept = U[keep_occ | keep_emp]
    out = {"n_user": int(len(U)), "n_user_occupied": int(is_occ.sum()),
           "cut_depth": int((is_occ & ~depth_ok).sum()), "cut_side": int((is_occ & depth_ok & ~side_ok).sum()),
           "cut_empty": int(((~is_occ) & ~empty_ok).sum()), "merged": 0, "fallback": False, "dropped_far": 0}
    merged = np.zeros((0, 3), int)
    if old is not None and len(kept):
        # 2.3 합치기: 씨앗 = 다듬은 셀 중 old 상자 안 점유 셀, 26-이웃으로 점유 셀을 넓힘(세 제한 모두)
        kx, ky, _ = _grid_xy(centers(kept), cam)
        seed_mask = occ_grid[kept[:, 0], kept[:, 1], kept[:, 2]] & _in_box(kx, ky, old, 0)
        seeds = kept[seed_mask]
        lo, hi = U.min(0) - M, U.max(0) + M
        allowed = np.zeros((GRID,) * 3, bool)
        ax, ay, az = _grid_xy(centers(O), cam)
        a_ok = (_in_box(ax, ay, old, MERGE_PAD) & (az * GRID <= d0_of(O) + 2 * T + 0.5)
                & np.all((O >= lo) & (O <= hi), axis=1))
        allowed[O[a_ok, 0], O[a_ok, 1], O[a_ok, 2]] = True
        have = np.zeros((GRID,) * 3, bool)
        have[kept[:, 0], kept[:, 1], kept[:, 2]] = True
        frontier = np.zeros((GRID,) * 3, bool)
        if len(seeds):
            frontier[seeds[:, 0], seeds[:, 1], seeds[:, 2]] = True
        reach = frontier.copy()
        while frontier.any():
            grown = _shift_or(frontier) & allowed & ~reach
            reach |= grown
            frontier = grown
        add = reach & ~have
        merged = np.argwhere(add)
        out["merged"] = int(len(merged))
        if len(merged):
            kept = np.concatenate([kept, merged])
        if len(kept) > MAX_CELLS:                     # 4 넘으면 합친 부분부터 먼 것을 뺀다
            c0 = centers(seeds).mean(0) if len(seeds) else centers(kept).mean(0)
            mset = {tuple(m) for m in merged.tolist()}
            base = [c for c in kept.tolist() if tuple(c) not in mset]
            room = max(0, MAX_CELLS - len(base))
            far = np.argsort(np.linalg.norm(centers(merged) - c0, axis=1))
            keep_m = merged[far[:room]]
            out["dropped_far"] = int(len(merged) - len(keep_m))
            out["merged"] = int(len(keep_m))
            kept = np.concatenate([np.asarray(base, int).reshape(-1, 3), keep_m])
    if not len(kept):                                  # 4 결과가 비면 사용자 볼륨 그대로
        kept, out["fallback"] = U, True
    kept = np.unique(kept, axis=0)
    if len(kept) > MAX_CELLS:
        kept = kept[:MAX_CELLS]
    out["cells"] = kept.tolist()
    out["n_final"] = int(len(kept))
    return out


def grid_full(rgb: np.ndarray, label: str = "") -> np.ndarray:
    """정사각 렌더 전체에 0–1000 격자(자르지 않음 — 상자 좌표가 렌더 화소와 맞게)."""
    return grid_image(rgb, label)


def tint_mask(rgb: np.ndarray, mask: np.ndarray, color=(255, 40, 90), alpha: float = 0.45) -> np.ndarray:
    """렌더 전체 크기 그대로 마스크를 칠한다(자르지 않음)."""
    out = rgb.astype(np.float32).copy()
    out[mask] = out[mask] * (1 - alpha) + np.asarray(color, np.float32) * alpha
    return out.astype(np.uint8)


# ── S20f 물건이 가장 잘 보이는 시점 고르기 ──────────────────────────
def visible_count(cells, occupied, cam: dict, tol: float = 1.0) -> int:
    """cells 중 점유 셀(cells∩occupied)이 그 카메라에서 실제로 보이는(첫 표면에서 tol 셀 안) 수. z-버퍼."""
    O = np.asarray(occupied, int).reshape(-1, 3)
    occ = np.zeros((GRID,) * 3, bool)
    occ[O[:, 0], O[:, 1], O[:, 2]] = True
    C = np.asarray(cells, int).reshape(-1, 3)
    C = C[occ[C[:, 0], C[:, 1], C[:, 2]]] if len(C) else C
    if not len(C):
        return 0
    _, d0_of = depth_buffer(O, cam)
    _, _, z = _grid_xy(centers(C), cam)
    return int(((z * GRID) <= d0_of(C) + tol).sum())


def pick_view(cells, occupied, cams: dict, user_cam: dict) -> dict:
    """후보 = 사용자 카메라 + 4시점. 보이는 라쏘 점유 셀 수 최대, 같으면 사용자 → az000 → … 순."""
    order = [("user", user_cam)] + [(v, cams[v]) for v in VIEW_AZ]
    counts = {name: visible_count(cells, occupied, cam) for name, cam in order}
    best = max(order, key=lambda it: (counts[it[0]], -[n for n, _ in order].index(it[0])))
    return {"view": best[0], "camera": best[1], "counts": counts}
