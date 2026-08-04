"""`slat.safetensors` → slat 좌표 `.npy`. **맥북 라쏘의 입력이다** (D58).

────────────────────────────────────────────────────────────────────────
왜 이 파일이 필요한가 — D34 의 정확한 반대편
────────────────────────────────────────────────────────────────────────
D34 가 확정한 것: `.cbin` 에서는 slat 좌표를 얻을 수 없다. 두 번 유도해 봤고 두 번
다 틀렸다 (표면 복셀화 10,264 vs slat 정본 9,591). 그래서 마스크 산출이 A5000 으로
넘어갔다.

그런데 **좌표 자체는 A5000 디스크에 파일로 있다** —
`assets/<asset_id>/staging/<job>/slat.safetensors`. 유도할 수 없을 뿐이지 없는 게
아니었다. 라쏘가 필요로 하는 것은 계산이 아니라 그 파일이고, 그건 옮기면 된다.

⚠️ 그러므로 이 모듈은 D34 를 뒤집지 않는다. D34 는 "유도하지 마라" 이고 이 모듈은
   "정본을 그대로 옮긴다" 이다. **여기서 좌표를 만들어 내는 코드는 한 줄도 없다.**

────────────────────────────────────────────────────────────────────────
🔴 총계 대조 없이는 slat 이라고 부르지 않는다 (D28)
────────────────────────────────────────────────────────────────────────
파일 이름이 `slat.safetensors` 라는 것은 **선언**이지 증거가 아니다. W8 이 정확히
그 자리에서 물렸다 — 라벨만 믿으면 한 줄로 방어를 우회한다. 그래서 뽑아낸 좌표
개수를 `manifest.json` 의 `voxel_count_total` 과 대조하고, 어긋나면 `NotSlatCoords`
를 던진다. `slatmask.build_head3_mask()` 가 매니페스트로 하는 검사와 같은 논리다.

────────────────────────────────────────────────────────────────────────
의존성을 늘리지 않는다
────────────────────────────────────────────────────────────────────────
safetensors 는 **8바이트 리틀엔디언 헤더 길이 + JSON 헤더 + 텐서 바이트**가 전부다.
`safetensors` 패키지를 새로 넣으면 GPU 서버 두 대의 환경 재구축을 부른다
(CLAUDE.md: 새 의존성은 사용자 확인). struct·json·numpy 로 충분하다.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

from .slatmask import SLAT, NotSlatCoords

__all__ = ["read_safetensors_header", "load_slat_coords", "export_slat_npy"]

#: safetensors 의 dtype 이름 → numpy dtype. 좌표에 쓰이는 정수형만 둔다 —
#: 모르는 dtype 을 추측해서 읽으면 조용히 다른 숫자가 나온다.
_DTYPES: Dict[str, np.dtype] = {
    "I8": np.dtype("<i1"), "I16": np.dtype("<i2"),
    "I32": np.dtype("<i4"), "I64": np.dtype("<i8"),
    "U8": np.dtype("<u1"), "U16": np.dtype("<u2"),
    "U32": np.dtype("<u4"), "U64": np.dtype("<u8"),
    "F32": np.dtype("<f4"), "F64": np.dtype("<f8"),
}


def read_safetensors_header(path: Path) -> Tuple[Dict[str, dict], int]:
    """(헤더 dict, 데이터 시작 오프셋). 텐서 바이트는 안 읽는다."""
    with open(path, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(n).decode("utf-8"))
    return {k: v for k, v in header.items() if k != "__metadata__"}, 8 + n


def _read_tensor(path: Path, spec: dict, data_start: int) -> np.ndarray:
    dt = _DTYPES.get(spec["dtype"])
    if dt is None:
        raise NotSlatCoords(f"모르는 dtype 이다: {spec['dtype']} — 추측해서 읽지 않는다")
    lo, hi = spec["data_offsets"]
    with open(path, "rb") as f:
        f.seek(data_start + lo)
        buf = f.read(hi - lo)
    return np.frombuffer(buf, dtype=dt).reshape(spec["shape"])


def load_slat_coords(
    path: Path, *, manifest: Optional[dict] = None, voxel_res: int = 64
) -> np.ndarray:
    """`slat.safetensors` → (N,3) int64 slat 좌표.

    Args:
        path: A5000 이 낸 `slat.safetensors` **원본**. 재포장본을 넣지 마라.
        manifest: 같은 자산의 `manifest.json`. 주면 `voxel_count_total` 과
            개수를 대조한다. **주는 것이 기본이어야 한다** — 안 주면 이 파일이
            정말 그 자산의 slat 인지 확인할 방법이 없다.

    Raises:
        NotSlatCoords: 좌표 텐서를 못 찾았거나, 격자 범위를 벗어났거나,
            매니페스트 총계와 어긋난다.
    """
    header, start = read_safetensors_header(Path(path))

    # 좌표 텐서 고르기 — (N,3) 또는 (N,4) 정수형. TRELLIS 는 배치 인덱스를 앞에
    # 붙여 (N,4) 로 내는 일이 있다. 이름으로 찍지 않고 **모양으로** 고른다.
    cand = [
        (k, v) for k, v in header.items()
        if len(v.get("shape", [])) == 2 and v["shape"][1] in (3, 4)
        and v.get("dtype", "").startswith(("I", "U"))
    ]
    if not cand:
        raise NotSlatCoords(
            f"좌표로 볼 텐서가 없다. 들어 있는 것: "
            + ", ".join(f"{k}{v.get('shape')}:{v.get('dtype')}" for k, v in header.items())
        )
    name, spec = max(cand, key=lambda kv: kv[1]["shape"][0])
    arr = _read_tensor(Path(path), spec, start).astype(np.int64)
    if arr.shape[1] == 4:
        # 앞 열이 배치 인덱스면 전부 0 이다. 아니면 무엇인지 모르는 것이므로 멈춘다.
        if not (arr[:, 0] == 0).all():
            raise NotSlatCoords(
                f"{name} 의 첫 열이 배치 인덱스가 아니다 (고유값 "
                f"{np.unique(arr[:, 0])[:5]}) — 어느 열이 좌표인지 추측하지 않는다"
            )
        arr = arr[:, 1:]

    if arr.min() < 0 or arr.max() >= voxel_res:
        raise NotSlatCoords(
            f"{name} 이 격자 범위를 벗어난다: [{arr.min()}, {arr.max()}] "
            f"vs [0, {voxel_res - 1}] — slat 좌표가 아니다"
        )

    # 🔴 라벨이 아니라 **총계**로 확인한다 (D28). 이 검사가 없으면 파일 이름만 믿는 것이다.
    if manifest is not None:
        total = manifest.get("voxel_count_total")
        if total is not None and int(total) != arr.shape[0]:
            raise NotSlatCoords(
                f"매니페스트와 어긋난다: slat {arr.shape[0]} vs "
                f"voxel_count_total {total} — 같은 자산·같은 잡이 맞는지 확인하라"
            )
    return arr


def export_slat_npy(asset_dir: Path, *, out: Optional[Path] = None) -> Tuple[Path, dict]:
    """자산 디렉터리의 `slat.safetensors` → `slat_coords.npy` + 옆에 붙일 스펙.

    스펙을 같이 내는 이유: 좌표만 건네면 받는 쪽이 **격자 출처를 모른다.** D28 이
    요구하는 것은 좌표가 아니라 "이 좌표가 slat 격자에서 왔다" 는 사실이고,
    그건 좌표 배열에 안 적힌다 (D28-a 가 구조로 강제한 그 자리다).
    """
    asset_dir = Path(asset_dir)
    manifest = json.loads((asset_dir / "manifest.json").read_text())
    coords = load_slat_coords(asset_dir / "slat.safetensors", manifest=manifest)
    out = out or (asset_dir / "slat_coords.npy")
    # 좌표는 0..63 이라 int16 로 충분하다 (int64 대비 4배 작다). `.npy` 는 dtype 을
    # 자기 안에 적으므로 받는 쪽은 `np.load()` 만 하면 되고 규약을 따로 안 맞춰도 된다.
    np.save(out, coords.astype(np.int16))

    spec = {
        "asset_id": manifest.get("asset_id"),
        "grid_source": SLAT,          # 🔴 slatmask 의 정본 상수. 문자열을 손으로 안 쓴다
        "voxel_res": manifest.get("contract", {}).get("voxel_res", 64),
        "n_coords": int(coords.shape[0]),
        "dtype": "int16",  # 0..63 이라 손실 없다
        "voxel_count_total": manifest.get("voxel_count_total"),
        "bbox_min": coords.min(axis=0).tolist(),
        "bbox_max": coords.max(axis=0).tolist(),
        "a5000_job_id": manifest.get("a5000_job_id"),
        "note": (
            "A5000 원본 slat.safetensors 에서 그대로 뽑은 좌표다. 3090 이 계산하지 "
            "않았다 (D34: .cbin 에서는 유도할 수 없다). 개수는 manifest 의 "
            "voxel_count_total 과 대조해 통과했다 (D28)."
        ),
    }
    (out.with_name(out.stem + "_spec.json")).write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out, spec
