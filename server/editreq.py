"""라쏘 마스크 → 편집 요청 (W17 ② · 계약 3.26.0).

이 모듈은 **HTTP 를 하지 않는다.** 라우트는 3090 담당이다. 여기 있는 것은 그
라우트가 실어 보낼 **본문을 만드는 함수**다.

🔴 왜 함수로 주는가. 계약이 세 가지를 규칙으로만 적어 뒀고, 규칙만 적고 함수를
안 주면 그 규칙은 안 지켜진다 — 이 리포에서 세 번 반복됐다 (staging 경로 · 마스크
지문 · 멱등 키).

    ① 마스크 지문은 `mask_fingerprint()` 로만 만든다
       손으로 직렬화하면 클라이언트와 서버가 조용히 갈린다 (3.14.0 에서 실제로 갈렸다)
    ② 멱등 키는 **요청 내용에서 파생**시킨다 (3.15.5)
       고정 키면 다른 마스크를 보내도 서버가 옛 연산을 재생한다 — Unity 하네스가
       실제로 그 상태였고, 정상 경로를 영영 안 밟았다
    ③ `grid_source` 는 생략 불가 (D28-a)
       기본값으로 메우면 잘못된 격자가 침묵으로 정본을 참칭한다
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np

from deltacontract import mask_fingerprint, slat_coords_uri  # noqa: F401
from deltacontract.coords import VOXEL_RES

__all__ = [
    "GridSourceMissing",
    "build_slat_coords_payload",
    "build_edit_mask",
    "derive_idempotency_key",
    "build_edit_request",
]

#: 계약의 정본 격자 이름. 다른 값이면 편집에 쓰지 않는다 (D28-a).
SLAT_COORDS = "slat_coords"


class GridSourceMissing(ValueError):
    """마스크의 격자 출처가 없거나 정본이 아니다 (D28-a)."""


def _cells(coords) -> np.ndarray:
    a = np.asarray(coords, dtype=np.int64).reshape(-1, 3)
    if a.size and (a.min() < 0 or a.max() >= VOXEL_RES):
        raise ValueError(
            f"셀이 격자 [0,{VOXEL_RES}) 를 벗어난다: [{a.min()}, {a.max()}]"
        )
    return np.unique(a, axis=0)


def build_slat_coords_payload(asset_id: str, version: int, coords) -> dict:
    """`GET /v2/assets/{id}/slat_coords.v{n}.json` 응답 본문 (3090 이 서빙한다).

    `n_cells` 를 같이 싣는 이유는 **잘린 목록을 잡기 위해서**다. 잘린 좌표 목록은
    형태가 멀쩡해서 예외를 안 내고, 그걸로 만든 마스크도 멀쩡해 보인다.
    """
    cells = _cells(coords)
    return {
        "asset_id": asset_id,
        "version": int(version),
        "grid_source": SLAT_COORDS,
        "voxel_res": VOXEL_RES,
        "n_cells": int(cells.shape[0]),
        "coords": cells.tolist(),
        "fingerprint": mask_fingerprint(cells),
        "uri": slat_coords_uri(asset_id, version),
    }


def build_edit_mask(lasso_result: dict, *, halo_margin_voxels: int = 1) -> dict:
    """라쏘 산출물(`unity/Headless` 또는 Editor 창의 JSON) → `EditMask` 본문.

    ⚠️ `grid_source` 를 **여기서 채워 넣지 않는다.** 산출물에 없으면 거부한다 —
       채워 넣으면 그 순간 "무엇으로 만든 마스크인지" 를 서버가 지어낸 것이 된다.
    """
    gs = lasso_result.get("grid_source")
    if gs != SLAT_COORDS:
        raise GridSourceMissing(
            f"마스크의 격자 출처가 {gs!r} 다 (D28-a). 편집에 쓰려면 {SLAT_COORDS!r} 여야 한다. "
            "라쏘 산출물이면 SlatLassoPicker 가 이미 박아 준다 — 없다는 것은 다른 "
            "경로로 만든 마스크라는 뜻이고, 격자가 다르면 예외 없이 다른 물체를 편집한다."
        )
    cells = _cells(lasso_result["cells"])
    if cells.shape[0] == 0:
        raise ValueError("빈 마스크는 편집 요청이 될 수 없다")

    declared = lasso_result.get("mask_fingerprint")
    actual = mask_fingerprint(cells)
    if declared is not None and declared != actual:
        raise ValueError(
            f"산출물의 지문({declared})과 셀에서 다시 계산한 값({actual})이 다르다. "
            "전송 중 잘렸거나 직렬화가 갈렸다."
        )
    return {
        "mode": "voxels",
        "voxels": [tuple(int(v) for v in c) for c in cells.tolist()],
        "halo_margin_voxels": int(halo_margin_voxels),
        "grid_source": SLAT_COORDS,
    }


def derive_idempotency_key(
    asset_id: str, base_version: int, raw_prompt: str, mask_cells, seed: int = 42
) -> str:
    """계약 3.15.5 의 권장식. **내용 파생**이라 같은 편집은 재생, 다른 편집은 재계산."""
    fp = mask_fingerprint(_cells(mask_cells))
    blob = "|".join([asset_id, str(int(base_version)), raw_prompt, fp, str(int(seed))])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_edit_request(
    *,
    asset_id: str,
    session_id: str,
    base_version: int,
    raw_prompt: str,
    lasso_result: dict,
    seed: int = 42,
    halo_margin_voxels: int = 1,
) -> dict:
    """라쏘 마스크를 실은 `EditRequest` 본문.

    `asset_id` 는 요청 본문에 없고 **멱등 키에만** 들어간다 — 계약이 그렇게 정했다
    (경로가 자산을 식별한다). 키에는 들어가야 한다: 안 넣으면 서로 다른 자산의
    같은 프롬프트·같은 마스크가 같은 키를 갖고, 한쪽 결과가 다른 쪽으로 재생된다.
    """
    mask = build_edit_mask(lasso_result, halo_margin_voxels=halo_margin_voxels)
    return {
        "session_id": session_id,
        "base_version": int(base_version),
        "raw_prompt": raw_prompt,
        "mask": mask,
        "seed": int(seed),
        "idempotency_key": derive_idempotency_key(
            asset_id, base_version, raw_prompt, mask["voxels"], seed
        ),
    }
