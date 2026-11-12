"""S13: 보정·분류 LLM 에 보낼 라쏘 이미지(numpy 만, Pillow 없음).

- (가) 겹친 이미지: 조건 렌더(흰 배경)에 마스크 영역을 반투명 빨강 + 외곽선으로 겹치고 물체 bbox 로 잘라 최대 768 px.
- (나) 확대: 마스크 bbox 를 2배 여유로 잘라 최대 512 px(겹침 없이 실제 모습 + 외곽선만).
- mask_ratio = 마스크 화소 / 물체(알파 > 0) 화소. 물체 화소는 마스크와 겹친 곳만이 아니라 렌더 전체 알파다.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .cropinpaint import decode_png, encode_png, mask_bool, render_rgb, resize_lanczos, resize_nearest

TINT = np.array([255, 0, 64], np.float32)   # 칠한 영역 색
ALPHA = 0.45
OVERLAY_MAX = 768
ZOOM_MAX = 512


def _bbox(m: np.ndarray) -> Optional[tuple]:
    ys, xs = np.where(m)
    if not len(ys):
        return None
    return int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1


def _edge(m: np.ndarray, w: int = 3) -> np.ndarray:
    """마스크 외곽선(안쪽 w px)."""
    inner = m.copy()
    for _ in range(w):
        e = inner.copy()
        e[1:] &= inner[:-1]
        e[:-1] &= inner[1:]
        e[:, 1:] &= inner[:, :-1]
        e[:, :-1] &= inner[:, 1:]
        inner = e
    return m & ~inner


def _fit(a: np.ndarray, m: Optional[np.ndarray], limit: int):
    h, w = a.shape[:2]
    s = min(1.0, limit / max(h, w))
    if s >= 1.0:
        return a, m
    hw = (max(1, round(h * s)), max(1, round(w * s)))
    return resize_lanczos(a, hw), (resize_nearest(m, hw) if m is not None else None)


def _pad_box(b: tuple, H: int, W: int, k: float, min_side: int = 0) -> tuple:
    y0, y1, x0, x1 = b
    cy, cx = (y0 + y1) / 2, (x0 + x1) / 2
    side = max((y1 - y0) * k, (x1 - x0) * k, min_side)
    hy, hx = side / 2, side / 2
    return (max(0, int(cy - hy)), min(H, int(cy + hy + 0.5)), max(0, int(cx - hx)), min(W, int(cx + hx + 0.5)))


def region_images(render_png: bytes, mask_png: bytes) -> dict:
    """{overlay_png, zoom_png, mask_ratio, mask_px, object_px, boxes}. 마스크가 비면 overlay 만, zoom None."""
    r = decode_png(render_png)
    m = mask_bool(decode_png(mask_png))
    H, W = m.shape
    obj = (r[:, :, 3] > 0) if r.shape[2] == 4 else (r.max(axis=2) > 8)
    rgb = render_rgb(r)
    mask_px, obj_px = int(m.sum()), int(obj.sum())
    ratio = round(mask_px / obj_px, 4) if obj_px else None

    tinted = rgb.astype(np.float32)
    tinted[m] = tinted[m] * (1 - ALPHA) + TINT * ALPHA
    tinted = tinted.astype(np.uint8)
    edge = _edge(m)
    tinted[edge] = TINT.astype(np.uint8)

    ob = _bbox(obj | m) or (0, H, 0, W)
    ob = _pad_box(ob, H, W, 1.08)
    ov, _ = _fit(tinted[ob[0]:ob[1], ob[2]:ob[3]], None, OVERLAY_MAX)

    zoom, zb = None, None
    mb = _bbox(m)
    if mb is not None:
        zb = _pad_box(mb, H, W, 2.0, min_side=96)
        z = rgb[zb[0]:zb[1], zb[2]:zb[3]].copy()
        z[edge[zb[0]:zb[1], zb[2]:zb[3]]] = TINT.astype(np.uint8)
        zoom, _ = _fit(z, None, ZOOM_MAX)
    return {"overlay_png": encode_png(ov), "zoom_png": encode_png(zoom) if zoom is not None else None,
            "mask_ratio": ratio, "mask_px": mask_px, "object_px": obj_px,
            "boxes": {"object": list(ob), "zoom": list(zb) if zb else None}}
