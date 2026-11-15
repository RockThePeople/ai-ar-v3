"""S21 체인용 순수 파이썬 암호·ABI(새 의존성 없음 — 앱 venv 에 cryptography·eth-abi 설치가 막혀서).

- keccak256(Ethereum Keccak-256, SHA-3 이전 패딩 0x01)
- P-256(secp256r1) ECDSA: 서명(RFC 6979 결정적 k)·검증, 공개키 → (x, y)
- ABI 인코딩: 정적(uint256·address·bytes32·bool)·동적(bytes·string) 인자, 함수 선택자
정확성은 서버 시험(알려진 벡터)과 Sepolia P256VERIFY·컨트랙트 호출로 확인한다(보고서).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import List, Sequence, Tuple

# ── keccak256 ────────────────────────────────────────────────────
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _f(a: List[List[int]]) -> None:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data) + b"\x01"
    while len(msg) % rate:
        msg += b"\x00"
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        blk = msg[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(blk[8 * i:8 * i + 8], "little")
        _f(a)
    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


def selector(sig: str) -> bytes:
    return keccak256(sig.encode())[:4]


def to_checksum(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    h = keccak256(a.encode()).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(a))


# ── P-256 ────────────────────────────────────────────────────────
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and (p1[1] + p2[1]) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * p1[0] * p1[0] + A) * pow(2 * p1[1], -1, P) % P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], -1, P) % P
    x = (lam * lam - p1[0] - p2[0]) % P
    return x, (lam * (p1[0] - x) - p1[1]) % P


def _mul(k: int, pt=G):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def on_curve(x: int, y: int) -> bool:
    return 0 < x < P and 0 < y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def pub_from_priv(d: int) -> Tuple[int, int]:
    return _mul(d)


def parse_pubkey(hexkey: str) -> Tuple[int, int]:
    """'0x04' + x(64) + y(64) → (x, y). 형식·곡선 위가 아니면 ValueError."""
    h = hexkey.lower().removeprefix("0x")
    if len(h) != 130 or not h.startswith("04"):
        raise ValueError("signer_pubkey 는 0x04 + x 64자 + y 64자(비압축 65바이트)")
    x, y = int(h[2:66], 16), int(h[66:], 16)
    if not on_curve(x, y):
        raise ValueError("signer_pubkey 가 P-256 곡선 위의 점이 아니다")
    return x, y


def _rfc6979_k(d: int, h1: bytes) -> int:
    x = d.to_bytes(32, "big")
    hm = (int.from_bytes(h1, "big") % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + hm, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + hm, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        t = int.from_bytes(v, "big")
        if 0 < t < N:
            return t
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_sha256(d: int, msg: bytes, low_s: bool = True) -> Tuple[int, int]:
    """SHA256withECDSA(msg) — 앱(Keystore)과 같은 방식. 검증 해시 = sha256(msg)."""
    h = hashlib.sha256(msg).digest()
    e = int.from_bytes(h, "big")
    k = _rfc6979_k(d, h)
    r = _mul(k)[0] % N
    s = pow(k, -1, N) * (e + r * d) % N
    if low_s and s > N // 2:
        s = N - s
    return r, s


def verify_hash(h: bytes, r: int, s: int, x: int, y: int) -> bool:
    """EIP-7951 과 같은 검증: 해시 h 에 대한 (r, s). high-s·low-s 모두 받는다(0 < r, s < n)."""
    if not (0 < r < N and 0 < s < N) or not on_curve(x, y):
        return False
    e = int.from_bytes(h, "big")
    w = pow(s, -1, N)
    pt = _add(_mul(e * w % N), _mul(r * w % N, (x, y)))
    return pt is not None and pt[0] % N == r


def verify_sha256(msg: bytes, sig_hex: str, x: int, y: int) -> bool:
    """서명 '0x' + r(64) + s(64) 를 sha256(msg) 에 대해 검증."""
    h = sig_hex.lower().removeprefix("0x")
    if len(h) != 128:
        return False
    return verify_hash(hashlib.sha256(msg).digest(), int(h[:64], 16), int(h[64:], 16), x, y)


# ── ABI ──────────────────────────────────────────────────────────
def _word(v: int) -> bytes:
    return (v % (1 << 256)).to_bytes(32, "big")


def _pad(b: bytes) -> bytes:
    return b + b"\x00" * ((32 - len(b) % 32) % 32)


def encode(types: Sequence[str], values: Sequence) -> bytes:
    """uint256·int128·uint64·address·bytes32·bool(정적), bytes·string(동적)만."""
    head, tail = [], b""
    dyn = {"bytes", "string"}
    hsize = 32 * len(types)
    for t, v in zip(types, values):
        if t in dyn:
            b = v.encode() if t == "string" else (bytes.fromhex(v.removeprefix("0x")) if isinstance(v, str) else bytes(v))
            head.append(_word(hsize + len(tail)))
            tail += _word(len(b)) + _pad(b)
        elif t == "address":
            head.append(_word(int(v, 16)))
        elif t == "bytes32":
            b = bytes.fromhex(v.removeprefix("0x")) if isinstance(v, str) else bytes(v)
            head.append(b.ljust(32, b"\x00"))
        elif t == "bool":
            head.append(_word(1 if v else 0))
        else:                                              # uint/int
            head.append(_word(int(v)))
    return b"".join(head) + tail


def call_data(sig: str, types: Sequence[str], values: Sequence) -> str:
    return "0x" + (selector(sig) + encode(types, values)).hex()


def decode_words(data: str) -> List[int]:
    h = data.removeprefix("0x")
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]
