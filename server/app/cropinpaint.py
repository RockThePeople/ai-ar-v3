"""S7 — 마스크 주변만 잘라 인페인팅. 표준 라이브러리 zlib 와 numpy 만 쓴다(새 의존성 없음).

조건 렌더는 계약 2.2 대로 정사각 1024², 화각 max(fov_y, fov_x) 라 세로 폰이면 물체가 작게 들어가고 마스크 영역의
픽셀이 적다. 렌더 틀(카메라·화각)은 A5000 3D 편집의 조건이기도 해 바꾸지 않고, 인페인팅 단계만 바꾼다:

1. 마스크(흰색 > 127) 경계 상자 → 한 변 = max(긴 변 × 2.0, 384) 정사각(중심 = 상자 중심, 이미지 안으로 밀고,
   이미지보다 크면 전체).
2. 렌더(흰 배경 RGB)·마스크를 그 정사각으로 잘라 1024² 로 키워(렌더 Lanczos-3, 마스크 최근접 후 0/255) 워커에 보낸다.
3. 결과를 잘라 낸 크기로 줄여(Lanczos-3, 축소 시 영역 확장) 원래 자리에 붙인다. 마스크 안만 바꾸고 경계 4 px 는
   안쪽으로 부드럽게 섞는다. 마스크 밖은 워커 합성(`composite.py`)과 같은 흰 배경 RGB 그대로다.

"흰 배경 RGB" 는 워커(포크 `utils/inpaint.py`)가 PIL `paste` 로 만드는 값과 비트 동일하게 계산한다
(Paste.c `BLEND`/`DIV255` 정수식).
"""

from __future__ import annotations

import struct
import time
import zlib
from typing import Callable, Optional

import numpy as np

MASK_THRESHOLD = 127
SCALE = 2.0
MIN_SIDE = 384
WORK = 1024
FEATHER = 4

_CH = {0: 1, 2: 3, 4: 2, 6: 4}


# ── PNG 읽기·쓰기 ──────────────────────────────────────────────────
def _chunks(b: bytes):
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 가 아니다")
    i = 8
    while i < len(b):
        n, = struct.unpack(">I", b[i:i + 4])
        yield b[i + 4:i + 8], b[i + 8:i + 8 + n]
        i += 12 + n


def _unfilter(raw: bytes, h: int, stride: int, bpp: int) -> np.ndarray:
    out = np.zeros((h, stride), np.uint8)
    prev = np.zeros(stride, np.uint8)
    p = 0
    for r in range(h):
        f = raw[p]
        line = np.frombuffer(raw, np.uint8, stride, p + 1)
        p += stride + 1
        if f == 0:
            cur = line.copy()
        elif f == 2:                                       # Up
            cur = line + prev
        elif f == 1:                                       # Sub: 채널별 누적합(mod 256)
            cur = np.cumsum(line.reshape(-1, bpp), axis=0, dtype=np.uint8).reshape(-1)
        else:                                              # Avg(3)·Paeth(4): 왼쪽 의존이라 순차
            cur = bytearray(line.tobytes())
            up = prev.tobytes()
            if f == 3:
                for x in range(stride):
                    a = cur[x - bpp] if x >= bpp else 0
                    cur[x] = (cur[x] + ((a + up[x]) >> 1)) & 255
            elif f == 4:
                for x in range(stride):
                    a = cur[x - bpp] if x >= bpp else 0
                    b = up[x]
                    c = up[x - bpp] if x >= bpp else 0
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    cur[x] = (cur[x] + pr) & 255
            else:
                raise ValueError(f"모르는 필터 {f}")
            cur = np.frombuffer(bytes(cur), np.uint8)
        out[r] = cur
        prev = out[r]
    return out


def decode_png(data: bytes) -> np.ndarray:
    """8비트 비인터레이스 회색·RGB·회색+알파·RGBA PNG → (H, W, C) uint8."""
    hdr, idat = None, b""
    for t, d in _chunks(data):
        if t == b"IHDR":
            hdr = d
        elif t == b"IDAT":
            idat += d
    w, h, bd, ct, _cm, _fm, il = struct.unpack(">IIBBBBB", hdr)
    if bd != 8 or il != 0 or ct not in _CH:
        raise ValueError(f"지원하지 않는 PNG: bit_depth={bd} color_type={ct} interlace={il}")
    c = _CH[ct]
    return _unfilter(zlib.decompress(idat), h, w * c, c).reshape(h, w, c)


def encode_png(a: np.ndarray, level: int = 6) -> bytes:
    a = np.ascontiguousarray(a, np.uint8)
    if a.ndim == 2:
        a = a[:, :, None]
    h, w, c = a.shape
    ct = {1: 0, 2: 4, 3: 2, 4: 6}[c]
    raw = np.concatenate([np.zeros((h, 1), np.uint8), a.reshape(h, w * c)], axis=1).tobytes()

    def ch(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0)) + \
        ch(b"IDAT", zlib.compress(raw, level)) + ch(b"IEND", b"")


# ── 픽셀 연산 ──────────────────────────────────────────────────────
def render_rgb(a: np.ndarray) -> np.ndarray:
    """워커 `composite.render_rgb` 와 비트 동일: RGBA 는 흰 배경에 PIL paste(Paste.c BLEND·DIV255), 회색은 3채널로."""
    c = a.shape[2]
    if c == 4:
        al = a[:, :, 3:4].astype(np.uint32)
        v = 255 * (255 - al) + a[:, :, :3].astype(np.uint32) * al + 128
        return (((v >> 8) + v) >> 8).astype(np.uint8)
    if c == 3:
        return a
    if c == 2:                                             # 회색+알파: PIL 도 RGBA 로 바꾼 뒤 같은 식
        return render_rgb(np.concatenate([np.repeat(a[:, :, :1], 3, 2), a[:, :, 1:2]], 2))
    return np.repeat(a, 3, 2)


def mask_bool(a: np.ndarray) -> np.ndarray:
    """True = 다시 그릴 영역. 회색이면 그대로, RGB(A)면 PIL convert("L") 과 같은 ITU-R 601 정수식."""
    if a.shape[2] in (1, 2):
        g = a[:, :, 0]
    else:
        r, g_, b = (a[:, :, i].astype(np.uint32) for i in range(3))
        g = ((r * 19595 + g_ * 38470 + b * 7471 + 0x8000) >> 16).astype(np.uint8)
    return g > MASK_THRESHOLD


def crop_box(m: np.ndarray, scale: float = SCALE, min_side: int = MIN_SIDE) -> Optional[tuple]:
    """마스크 경계 상자 → (x0, y0, side). 마스크가 비면 None. side 가 이미지 짧은 변 이상이면 전체(정사각 이미지 가정)."""
    ys, xs = np.nonzero(m)
    if ys.size == 0:
        return None
    h, w = m.shape
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    side = int(round(max(max(y1 - y0, x1 - x0) * scale, min_side)))
    if side >= min(h, w):
        return (0, 0, min(h, w)) if h == w else None
    cy, cx = (y0 + y1) / 2, (x0 + x1) / 2
    ty = int(round(cy - side / 2))
    tx = int(round(cx - side / 2))
    ty = min(max(ty, 0), h - side)
    tx = min(max(tx, 0), w - side)
    return tx, ty, side


def _lanczos(x: np.ndarray) -> np.ndarray:
    x = np.abs(x)
    out = np.sinc(x) * np.sinc(x / 3.0)
    out[x >= 3.0] = 0.0
    return out


def _weights(n_in: int, n_out: int) -> np.ndarray:
    """PIL Resample.c precompute_coeffs 와 같은 구조의 Lanczos-3 가중치 행렬 (n_out, n_in). 축소면 지지 폭을 늘린다."""
    scale = n_in / n_out
    fs = max(scale, 1.0)
    support = 3.0 * fs
    W = np.zeros((n_out, n_in), np.float64)
    for o in range(n_out):
        center = (o + 0.5) * scale
        lo = max(int(center - support + 0.5), 0)
        hi = min(int(center + support + 0.5), n_in)
        xs = np.arange(lo, hi)
        k = _lanczos((xs - center + 0.5) / fs)
        s = k.sum()
        W[o, lo:hi] = k / s if s else 0
    return W


def resize_lanczos(a: np.ndarray, out_hw: tuple) -> np.ndarray:
    h, w, c = a.shape
    oh, ow = out_hw
    if (h, w) == (oh, ow):
        return a.copy()
    Wy, Wx = _weights(h, oh).astype(np.float32), _weights(w, ow).astype(np.float32)
    f = a.astype(np.float32).reshape(h, w * c)
    t = (Wy @ f).reshape(oh, w, c).transpose(0, 2, 1)          # (oh, c, w) — 세로 먼저(행렬곱 = BLAS)
    out = (t @ Wx.T).transpose(0, 2, 1)                        # (oh, ow, c)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def resize_nearest(m: np.ndarray, out_hw: tuple) -> np.ndarray:
    h, w = m.shape
    oh, ow = out_hw
    yi = np.minimum(((np.arange(oh) + 0.5) * h / oh).astype(np.int64), h - 1)
    xi = np.minimum(((np.arange(ow) + 0.5) * w / ow).astype(np.int64), w - 1)
    return m[yi][:, xi]


def feather_alpha(m: np.ndarray, px: int = FEATHER) -> np.ndarray:
    """마스크 안쪽으로 px 만큼 0→1 로 올라가는 섞기 비율(마스크 밖은 0). 4-이웃 침식 횟수 = 경계까지 거리."""
    cur = m.copy()
    acc = np.zeros(m.shape, np.float32)
    for _ in range(px):
        acc += cur
        p = np.pad(cur, 1, constant_values=False)
        cur = cur & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return acc / px


# ── 한 번의 잘라 인페인팅 ───────────────────────────────────────────
def crop_inpaint(render_png: bytes, mask_png: bytes, call: Callable[[bytes, bytes], bytes]) -> tuple:
    """call(render_png, mask_png) → edit_png 를 잘라 부른다. 반환: (전체 edit PNG, 기록 dict, 자른 이미지 dict|None).

    마스크가 비면 자르지 않고 그대로 부른다(기록 box=None)."""
    t0 = time.perf_counter()
    r = render_rgb(decode_png(render_png))
    mk = mask_bool(decode_png(mask_png))
    if mk.shape != r.shape[:2]:
        raise ValueError(f"크기 불일치: render {r.shape}, mask {mk.shape}")
    box = crop_box(mk)
    if box is None:
        return call(render_png, mask_png), {"box": None, "reason": "빈 마스크 또는 정사각 아님 — 자르지 않음"}, None
    x0, y0, s = box
    rc = r[y0:y0 + s, x0:x0 + s]
    mc = mk[y0:y0 + s, x0:x0 + s]
    up_r = resize_lanczos(rc, (WORK, WORK)) if s != WORK else rc
    up_m = (resize_nearest(mc, (WORK, WORK)) if s != WORK else mc).astype(np.uint8) * 255
    send_r, send_m = encode_png(up_r, 1), encode_png(up_m, 1)
    t1 = time.perf_counter()
    got_png = call(send_r, send_m)
    t2 = time.perf_counter()
    got = render_rgb(decode_png(got_png))
    back = resize_lanczos(got, (s, s)) if got.shape[:2] != (s, s) else got
    a = feather_alpha(mk)[y0:y0 + s, x0:x0 + s, None]
    out = r.copy()
    region = out[y0:y0 + s, x0:x0 + s]
    blend = np.clip(np.rint(region.astype(np.float32) * (1 - a) + back.astype(np.float32) * a), 0, 255).astype(np.uint8)
    out[y0:y0 + s, x0:x0 + s] = np.where(a > 0, blend, region)
    edit_png = encode_png(out)
    t3 = time.perf_counter()
    ys, xs = np.nonzero(mk)
    rec = {"box": [x0, y0, s], "mask_bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
           "scale": round(WORK / s, 3), "mask_px": int(mk.sum()), "mask_px_sent": int((up_m > 0).sum()),
           "feather_px": FEATHER, "pre_s": round(t1 - t0, 3), "post_s": round(t3 - t2, 3)}
    return edit_png, rec, {"crop_render.png": send_r, "crop_mask.png": send_m, "crop_edit.png": got_png}
