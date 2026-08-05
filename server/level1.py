"""레벨1(색 변경) 판정 기록기 — **D72 형식**.

────────────────────────────────────────────────────────────────────────
왜 별도 모듈인가
────────────────────────────────────────────────────────────────────────
`gate_g2()` 는 **레벨2(형태 변경)** 게이트다. 레벨1 은 기하가 불변이라 그 게이트의
효능 지표(신규 복셀 · 연결성분)가 전부 0 이 되고, 그러면 "아무것도 안 했다" 와
"색만 바꿨다" 가 같은 숫자로 나온다. 다른 판정이 필요하다.

⚠️ 이 모듈은 **산출자**다. DebugView 는 여전히 읽기만 한다 — 화면이 문턱을 다시
   적으면 게이트와 화면이 갈라지고, 갈라진 줄 아무도 모른다.

────────────────────────────────────────────────────────────────────────
🔴 `is_achromatic` 이 왜 문턱과 **같이** 가야 하는가
────────────────────────────────────────────────────────────────────────
"hue 이동 > 30°" 는 원본에 hue 가 **있을 때만** 뜻이 있다. 무채색(검정·회색)은
HSV 에서 hue 가 정의되지 않고, 채도가 0 에 가까우면 hue 는 잡음 한 톨에 180° 씩
튄다. moto-b 뒷바퀴가 정확히 그 경우다 — 검은 오토바이의 검은 타이어다.

그래서 `is_achromatic` 이 참이면 hue 문턱은 **적용하지 않고**, 효능은 육안(색 렌더
쌍)으로만 판정한다. 이걸 안 갈라 두면 무채색 자산에서 hue 이동이 큰 수로 나와
**아무 근거 없이 통과**하거나, 반대로 0 이 나와 **바꿨는데도 탈락**한다. 둘 다
예외가 안 난다.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np

from deltacontract import decode  # type: ignore[import-not-found]

__all__ = ["ACHROMATIC_SAT", "HUE_SHIFT_DEG", "ColorStat", "color_stat",
           "hue_shift_deg", "geometry_byte_identity", "build_judgment"]

#: 채도가 이 아래면 hue 가 정의되지 않는다고 본다 (0..1).
ACHROMATIC_SAT = 0.15
#: D72 효능 문턱. 🔴 무채색 원본에는 **적용하지 않는다** (위 docstring).
HUE_SHIFT_DEG = 30.0


@dataclass(frozen=True)
class ColorStat:
    n: int
    mean_rgb: Tuple[float, float, float]
    mean_hue_deg: float
    mean_sat: float

    @property
    def is_achromatic(self) -> bool:
        return self.mean_sat < ACHROMATIC_SAT


def _mask_vertex_colors(
    blobs: Mapping[str, bytes], mask_cells: np.ndarray
) -> np.ndarray:
    """마스크에 든 정점의 색만 (N,3) uint8. 청크 밖은 안 본다."""
    from deltacontract.coords import normalized_to_voxel  # type: ignore[import-not-found]

    want = set(
        (int(a) * 4096 + int(b) * 64 + int(c))
        for a, b, c in np.asarray(mask_cells, dtype=np.int64).reshape(-1, 3)
    )
    out = []
    for b in blobs.values():
        mesh = decode(b)
        if mesh.colors is None or len(mesh.colors) == 0:
            continue
        cells = normalized_to_voxel(mesh.positions)
        codes = (cells[:, 0].astype(np.int64) * 4096
                 + cells[:, 1].astype(np.int64) * 64 + cells[:, 2].astype(np.int64))
        keep = np.array([int(c) in want for c in codes], dtype=bool)
        if keep.any():
            out.append(np.asarray(mesh.colors, dtype=np.uint8)[keep][:, :3])
    return np.concatenate(out, axis=0) if out else np.zeros((0, 3), dtype=np.uint8)


def color_stat(blobs: Mapping[str, bytes], mask_cells: np.ndarray) -> ColorStat:
    """마스크 안 색 요약. 평균 RGB 를 HSV 로 옮겨 hue·채도를 낸다."""
    rgb = _mask_vertex_colors(blobs, mask_cells)
    if rgb.shape[0] == 0:
        return ColorStat(0, (0.0, 0.0, 0.0), 0.0, 0.0)
    mean = rgb.mean(axis=0)
    h, _l, s = colorsys.rgb_to_hls(*(mean / 255.0))
    # HLS 의 s 는 밝기에 따라 부풀 수 있다. 무채색 판정은 **원본 채널 편차**로도
    # 확인한다 — 회색은 R≈G≈B 다.
    spread = float(mean.max() - mean.min()) / 255.0
    return ColorStat(int(rgb.shape[0]), tuple(float(v) for v in mean),
                     float(h * 360.0), min(float(s), spread))


def hue_shift_deg(a: ColorStat, b: ColorStat) -> float:
    """원형 hue 차이 [0,180]. 색상환에서 어느 쪽으로 돌든 짧은 쪽."""
    d = abs(a.mean_hue_deg - b.mean_hue_deg) % 360.0
    return d if d <= 180.0 else 360.0 - d


def geometry_byte_identity(
    parent: Mapping[str, bytes], child: Mapping[str, bytes]
) -> Tuple[float, int, int]:
    """(비율, 같은 청크 수, 전체). **전 청크를 직접 디코드해 대조한다.**

    바이트 전체 비교가 아니라 `positions`·`indices` 비교다 — 색이 바뀐 청크는
    바이트가 다르지만 기하는 같아야 하고, 그 구분이 레벨1 의 정의다 (D24).
    """
    keys = sorted(set(parent) & set(child))
    same = 0
    for k in keys:
        a, b = decode(parent[k]), decode(child[k])
        if (np.array_equal(np.asarray(a.positions), np.asarray(b.positions))
                and np.array_equal(np.asarray(a.indices), np.asarray(b.indices))):
            same += 1
    return (same / len(keys) if keys else 0.0), same, len(keys)


def build_judgment(
    *,
    asset_id: str,
    op: str,
    parent: Mapping[str, bytes],
    child: Mapping[str, bytes],
    mask_cells: np.ndarray,
    mask_fingerprint: str,
    grid_source: str,
    changed: Sequence[str],
    inherited_identity: float,
    saving: float,
    visual_confirmed: Optional[bool] = None,
    color_renders: Optional[Dict[str, str]] = None,
    note: str = "",
) -> dict:
    """D72 형식 `judgment.json` 본문.

    `visual_confirmed` 기본값은 **None(미결)** 이다 — 사람이 색 렌더 쌍을 보고
    채우는 자리다. 코드가 True 로 채우면 "육안 확인" 이 자동 통과가 되고,
    그 순간 육안 조건이 아무것도 막지 못한다.
    """
    before = color_stat(parent, mask_cells)
    after = color_stat(child, mask_cells)
    shift = hue_shift_deg(before, after)
    geo_ratio, geo_same, geo_total = geometry_byte_identity(parent, child)

    added = sorted(set(child) - set(parent))
    removed = sorted(set(parent) - set(child))

    # ── 효능. 무채색 원본이면 hue 문턱을 **적용하지 않는다** (근거는 위 docstring).
    hue_ok: Optional[bool] = None if before.is_achromatic else shift > HUE_SHIFT_DEG
    if before.is_achromatic:
        efficacy = visual_confirmed          # 육안만이 근거다 — 미확인이면 미결
    elif hue_ok and visual_confirmed:
        efficacy = True
    elif hue_ok is False:
        efficacy = False
    else:
        efficacy = None

    preservation = bool(geo_ratio == 1.0 and inherited_identity == 1.0)
    # in-place: 색 편집은 청크를 **만들지도 없애지도 않는다.**
    in_place = bool(not added and not removed)

    return {
        "asset_id": asset_id,
        "op": op,
        "format": "D72",
        "gate_level1": {
            "efficacy": efficacy,
            "preservation": preservation,
            "in_place": in_place,
        },
        "efficacy_detail": {
            "hue_shift_deg": round(shift, 1),
            "hue_threshold_deg": HUE_SHIFT_DEG,
            "hue_ok": hue_ok,
            "is_achromatic": before.is_achromatic,
            "before": {"mean_rgb": [round(v, 1) for v in before.mean_rgb],
                       "mean_hue_deg": round(before.mean_hue_deg, 1),
                       "mean_sat": round(before.mean_sat, 3), "n_vertices": before.n},
            "after": {"mean_rgb": [round(v, 1) for v in after.mean_rgb],
                      "mean_hue_deg": round(after.mean_hue_deg, 1),
                      "mean_sat": round(after.mean_sat, 3), "n_vertices": after.n},
            # 🔴 무채색 원본에서 hue 가 왜 못 쓰는지를 **숫자로** 남긴다.
            #    moto-b 실측: 검정→주황이라는 명백한 변화가 hue 이동 3.6° 다
            #    (문턱 30°). 원본이 회색이라 명목 hue 가 이미 주황 근처였기 때문이다.
            #    이 두 값은 **참고**다 — 문턱이 아니다. 문턱은 맥북이 정한다.
            "rgb_distance": round(float(np.linalg.norm(
                np.asarray(after.mean_rgb) - np.asarray(before.mean_rgb))), 1),
            "sat_delta": round(after.mean_sat - before.mean_sat, 3),
            "visual_confirmed": visual_confirmed,
            "color_renders": color_renders or {},
        },
        "preservation_detail": {
            "geometry_byte_identity": geo_ratio,
            "geometry_chunks_same": geo_same,
            "geometry_chunks_total": geo_total,
            "inherited_byte_identity": inherited_identity,
        },
        "in_place_detail": {
            "changed": len(changed), "added": len(added), "removed": len(removed),
            "changed_keys": sorted(changed),
        },
        # 🔴 참고. **문턱이 아니다** (D70). 절감률만 보면 아무것도 안 보내는
        #    구현이 100% 를 받는다 (방법론 5조 3번).
        "reference": {"transfer_saving": saving, "note": "참고값 — 게이트 문턱이 아니다 (D70)"},
        "mask": {"n_cells": int(np.asarray(mask_cells).reshape(-1, 3).shape[0]),
                 "fingerprint": mask_fingerprint, "grid_source": grid_source},
        "gate_notes": {"note": note} if note else {},
    }
