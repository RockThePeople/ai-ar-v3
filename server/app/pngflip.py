"""PNG 좌우 반전(S5, V1). 표준 라이브러리 zlib 와 numpy 만 쓴다(새 의존성 없음).

앱 화면(Unity, 왼손 좌표계)은 서버 렌더(Blender, 오른손)의 좌우 거울상이다. 서버 내부 이미지는 그대로 두고,
앱에 **보낼 때만** 뒤집는다(`APP_PREVIEW_MIRROR`, 기본 1). 지원: 8비트, 비인터레이스, 회색·RGB·회색+알파·RGBA·팔레트.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

_CH = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


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


def _chunk(t: bytes, d: bytes) -> bytes:
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)


def mirror_png(data: bytes) -> bytes:
    """PNG 바이트 → 좌우 반전한 PNG 바이트."""
    hdr, idat, keep = None, b"", []
    for t, d in _chunks(data):
        if t == b"IHDR":
            hdr = d
        elif t == b"IDAT":
            idat += d
        elif t in (b"PLTE", b"tRNS"):
            keep.append((t, d))
    w, h, bd, ct, _cm, _fm, il = struct.unpack(">IIBBBBB", hdr)
    if bd != 8 or il != 0 or ct not in _CH:
        raise ValueError(f"지원하지 않는 PNG: bit_depth={bd} interlace={il} color_type={ct}")
    ch = _CH[ct]
    px = _unfilter(zlib.decompress(idat), h, w * ch, ch).reshape(h, w, ch)[:, ::-1, :]
    rows = np.concatenate([np.zeros((h, 1), np.uint8), np.ascontiguousarray(px).reshape(h, w * ch)], axis=1)
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", hdr)
    for t, d in keep:
        out += _chunk(t, d)
    return out + _chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)) + _chunk(b"IEND", b"")


def decode_png(data: bytes) -> np.ndarray:
    """시험·비교용: PNG → (h, w, ch) uint8."""
    hdr, idat = None, b""
    for t, d in _chunks(data):
        if t == b"IHDR":
            hdr = d
        elif t == b"IDAT":
            idat += d
    w, h, bd, ct, _cm, _fm, il = struct.unpack(">IIBBBBB", hdr)
    ch = _CH[ct]
    return _unfilter(zlib.decompress(idat), h, w * ch, ch).reshape(h, w, ch)


def mirrored_file(src: Path, cache_root: Path, key: str) -> Path:
    """반전본을 한 번 만들어 캐시한다. 저장 원본(src)은 건드리지 않는다."""
    dst = cache_root / key
    if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(mirror_png(src.read_bytes()))
        tmp.replace(dst)
    return dst
