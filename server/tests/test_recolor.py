"""D24 — 색 편집 경로. **이 경로의 강점은 기하 바이트가 보존된다는 것이다.**

S2 관통은 `.cbin → 복셀화 → occupancy_to_mesh → 재인코딩` 이라 색이 사라지고
(정점·면만 낸다) 기하도 재메싱을 거친다. 색 편집은 복셀 격자를 아예 안 거치므로
`positions` 와 `indices` 가 **바이트 단위로** 원본과 같다.

그 바이트 동일성이 이 파일이 못박는 것이다. 깨지면 D24 의 근거가 사라진다.

픽스처는 `contract/conformance/golden/` 의 실제 `.cbin` 을 쓴다 — 200개 전부
COLOR 채널을 갖고 있어서 합성 자산을 따로 만들 필요가 없다. 골든은 읽기만 한다.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pytest

from server import metrics
from server.pipeline import build_mask
from server.pipeline.recolor import (
    RecolorError,
    recolor_asset,
    recolor_chunk,
    sample_colors,
)

from deltacontract.chunkbin import buffer_views, decode, encode
from deltacontract.coords import chunk_key, normalized_to_voxel, voxel_to_chunk

_GOLDEN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "contract", "conformance", "golden",
)

ORANGE = (209, 148, 71)      # W6/3090 실측 호박 색
SNOW = (148, 147, 147)       # W6/3090 실측 눈사람 색 — 거의 무채색이다


def _golden_blobs(limit: int = 24):
    """골든 `.cbin` → {chunk_key: bytes}. 실제 계약 바이트다."""
    out = {}
    for path in sorted(glob.glob(os.path.join(_GOLDEN_DIR, "*.cbin")))[:limit]:
        blob = open(path, "rb").read()
        out[decode(blob).key] = blob
    return out


def _mask_covering(blobs, keys):
    """주어진 청크들의 정점이 떨어지는 복셀 셀 전부를 마스크로 만든다."""
    cells = []
    for key in keys:
        mesh = decode(blobs[key])
        cells.append(normalized_to_voxel(np.asarray(mesh.positions, dtype=np.float64)))
    return build_mask(np.unique(np.concatenate(cells, axis=0), axis=0), halo=0)


@pytest.fixture(scope="module")
def blobs():
    b = _golden_blobs()
    assert b, "골든 .cbin 을 못 읽었다"
    return b


# ══════════════════════════ 1. ★★ 기하 바이트 보존 — D24 의 근거
def test_recolor_preserves_geometry_bytes_exactly(blobs):
    """★★ **이 경로의 유일한 강점.** positions·indices 가 바이트 동일해야 한다.

    복셀화·재메싱을 안 거치므로 기하가 손대지지 않는다. W6/3090 실측 53/53.
    이게 깨지면 D24 는 "색도 되는 다른 경로" 일 뿐이고, 별도 경로로 둘 이유가 없다.
    """
    checked = 0
    for key, blob in blobs.items():
        new, n = recolor_chunk(blob, ORANGE)
        if n == 0:
            continue
        assert len(new) == len(blob), f"{key}: 길이가 바뀌었다"
        bv = buffer_views(decode(blob))
        for field in ("POSITION", "INDICES"):
            off, ln = bv[field]
            assert new[off : off + ln] == blob[off : off + ln], (
                f"{key}: {field} 바이트가 바뀌었다 — 기하가 보존되지 않는다"
            )
        # 헤더도 그대로여야 한다 (정점 수·인덱스 수·flags·좌표).
        assert new[:40] == blob[:40], f"{key}: 헤더가 바뀌었다"
        checked += 1
    assert checked > 0, "색이 바뀐 청크가 하나도 없다 — 픽스처가 이상하다"


def test_recolor_changes_only_the_color_channel(blobs):
    """바뀌는 것은 COLOR_0 구간뿐이다."""
    key = next(iter(blobs))
    blob = blobs[key]
    new, n = recolor_chunk(blob, ORANGE)
    assert n > 0

    bv = buffer_views(decode(blob))
    off, ln = bv["COLOR_0"]
    assert new[off : off + ln] != blob[off : off + ln]
    # COLOR_0 을 제외한 모든 바이트가 동일하다.
    assert new[:off] == blob[:off]
    assert new[off + ln :] == blob[off + ln :]


def test_normals_survive_recolor(blobs):
    """NORMAL 도 기하다 — 조명이 틀어지면 육안 판정이 무너진다."""
    key = next(iter(blobs))
    before, after = decode(blobs[key]), decode(recolor_chunk(blobs[key], ORANGE)[0])
    assert before.normals is not None
    assert np.array_equal(before.normals, after.normals)
    assert np.array_equal(before.positions, after.positions)
    assert np.array_equal(before.indices, after.indices)


def _crowded_colored_chunk(n_vertices: int = 48, seed: int = 7):
    """위치가 겹치는 정점이 많은 색 있는 청크.

    `canonicalize` 의 정렬 키는 (양자화 위치, **속성 raw 바이트**)다. 위치가 같은
    정점이 있어야 **색이 tie-break 를 좌우**하고, 그래야 색 변경이 정점 순서를
    바꾼다. 골든 청크는 정점이 3~35개뿐이라 이 상황이 잘 안 나온다.
    """
    from deltacontract.chunkbin import canonicalize

    rng = np.random.default_rng(seed)
    pos = rng.integers(0, 4, size=(n_vertices, 3)).astype(np.float32) / 64.0 - 0.4
    colors = np.concatenate(
        [rng.integers(0, 256, size=(n_vertices, 3), dtype=np.uint8),
         np.full((n_vertices, 1), 255, np.uint8)], axis=1)
    idx = np.arange(n_vertices // 3 * 3, dtype=np.uint32)
    return canonicalize(chunk_coord=(1, 1, 1), positions=pos, indices=idx, colors=colors)


def test_recolor_does_not_recanonicalize():
    """🔴 **함정 기록.** `canonicalize()` 를 다시 부르면 정점 순서가 바뀐다.

    정렬 키에 속성 raw 바이트가 tie-break 로 들어가 있어서, 색을 바꾼 뒤
    재정규화하면 positions·indices 가 통째로 재배열된다 — 이 경로의 유일한
    강점(기하 바이트 보존)이 사라진다. 그래서 `recolor.py` 는 `canonicalize` 를
    부르지 않는다. 여기서 두 결과를 나란히 놓아 그 차이를 숫자로 남긴다.
    """
    from deltacontract.chunkbin import canonicalize

    mesh = _crowded_colored_chunk()
    blob = encode(mesh)

    recolored, n = recolor_chunk(blob, ORANGE)
    assert n > 0

    colors = np.array(mesh.colors, copy=True)
    colors[:, :3] = ORANGE
    recanon = encode(
        canonicalize(
            chunk_coord=mesh.chunk_coord,
            positions=mesh.positions,
            indices=mesh.indices,
            colors=colors,
            voxel_count=mesh.voxel_count,
        )
    )

    off, ln = buffer_views(mesh)["POSITION"]
    # 우리 경로: 기하가 원본 그대로.
    assert recolored[off : off + ln] == blob[off : off + ln]
    # 재정규화 경로: 기하가 재배열된다. 이게 우리가 피한 것이다.
    assert recanon[off : off + ln] != blob[off : off + ln], (
        "픽스처가 재정규화 함정을 재현하지 못했다"
    )
    assert recolored != recanon


# ══════════════════════════ 2. 마스크 한정 색칠
def test_only_masked_vertices_are_recolored(blobs):
    """마스크 밖 정점은 색도 안 바뀐다 — 국소성은 색에서도 성립해야 한다."""
    keys = sorted(blobs)[:3]
    mask = _mask_covering(blobs, keys[:1])
    result = recolor_asset(blobs, mask, ORANGE)

    assert result.n_chunks_recolored >= 1
    assert result.n_vertices_recolored > 0

    # 마스크에 안 걸린 청크는 부모 바이트 그대로다.
    for key in blobs:
        if key not in result.bookkeeping.book:
            assert result.blobs[key] is blobs[key], f"{key} 가 재인코딩됐다"


def test_empty_mask_is_rejected(blobs):
    from deltacontract.errors import MaskEmpty

    with pytest.raises(MaskEmpty):
        build_mask(np.zeros((0, 3), dtype=np.int64), halo=0)


def test_chunk_without_color_is_rejected():
    """색이 없는 자산에 색 편집을 걸면 거부한다 — 조용히 통과시키지 않는다."""
    from server.pipeline import occupancy_to_mesh
    from deltacontract.partition import partition_mesh

    cells = np.array([[8, 8, 8], [9, 8, 8]], dtype=np.int64)
    meshes = partition_mesh(*occupancy_to_mesh(cells), voxel_cells=cells)
    blob = encode(next(iter(meshes.values())))
    with pytest.raises(RecolorError, match="COLOR"):
        recolor_chunk(blob, ORANGE)


# ══════════════════════════ 3. 부기 · 패키징
def test_recolor_never_adds_or_removes_chunks(blobs):
    """색 편집은 기하를 안 건드린다 — 청크가 생기지도 사라지지도 않는다.

    그래서 assemble 을 물었던 "비워진 청크가 diff 에서 사라지는" 함정이
    이 경로에는 **존재하지 않는다** (recolor.py 모듈 docstring).
    """
    mask = _mask_covering(blobs, sorted(blobs)[:2])
    result = recolor_asset(blobs, mask, ORANGE)

    assert result.bookkeeping.removed == []
    assert set(result.blobs) == set(blobs)
    for key in blobs:
        assert decode(result.blobs[key]).vertex_count == decode(blobs[key]).vertex_count


def test_bookkeeping_universal_rule_holds(blobs):
    mask = _mask_covering(blobs, sorted(blobs)[:2])
    bk = recolor_asset(blobs, mask, ORANGE).bookkeeping
    assert set(bk.changed) | set(bk.removed) == set(bk.book)
    assert not (set(bk.changed) & set(bk.removed))


def test_transfer_saving(blobs):
    """마스크가 자산의 일부만 덮으면 절감이 난다 (W6/3090 실측 70.46%)."""
    keys = sorted(blobs)[: max(1, len(blobs) // 4)]
    mask = _mask_covering(blobs, keys)
    result = recolor_asset(blobs, mask, ORANGE)
    assert 0.0 < result.transfer_saving < 1.0
    assert result.package.delta_bytes < result.package.full_bytes


# ══════════════════════════ 4. hue 지표 (D5 레벨1 · 문턱 30°)
def test_hue_shift_reaches_the_level1_threshold(blobs):
    """★ D5 레벨1 — 마스크 안 평균 hue 이동 > 30°."""
    keys = sorted(blobs)[:4]
    mask = _mask_covering(blobs, keys)

    # 먼저 마스크 안을 눈사람 색(거의 무채색)으로 칠해 before 를 만든다.
    base = recolor_asset(blobs, mask, SNOW).blobs
    after = recolor_asset(base, mask, ORANGE).blobs

    before_rgb = sample_colors(base, mask)
    after_rgb = sample_colors(after, mask)
    shift = metrics.hue_shift_degrees(before_rgb, after_rgb)

    assert before_rgb.shape[0] > 0 and after_rgb.shape[0] > 0
    assert shift > 30.0, f"hue 이동 {shift:.1f}° — 레벨1 문턱(30°) 미달"


def test_hue_stats_flags_achromatic_before():
    """🔴 회색의 hue 는 잡음이다 — 그 사실을 지표가 표면에 올리는지.

    흰 눈사람 (148,147,147) 의 "hue 58.3°" 는 실측치처럼 보이지만 신뢰할 수 없다.
    숨기면 다음 세션이 그 숫자를 근거로 인용한다.
    """
    snow = np.tile(np.array(SNOW, dtype=np.uint8), (64, 1))
    orange = np.tile(np.array(ORANGE, dtype=np.uint8), (64, 1))

    s, o = metrics.hue_stats(snow), metrics.hue_stats(orange)
    assert s.is_achromatic, f"채도 {s.mean_saturation:.4f} — 무채색으로 안 잡혔다"
    assert not o.is_achromatic
    assert metrics.hue_shift_degrees(snow, orange) > 30.0


def test_hue_mean_is_circular():
    """산술 평균을 쓰면 0°/359° 가 180° 로 나온다. 원형 평균이어야 한다."""
    red_a = np.tile(np.array([255, 4, 0], dtype=np.uint8), (32, 1))    # hue ≈ 1°
    red_b = np.tile(np.array([255, 0, 4], dtype=np.uint8), (32, 1))    # hue ≈ 359°
    stats = metrics.hue_stats(np.concatenate([red_a, red_b], axis=0))
    assert stats.mean_hue_deg < 5.0 or stats.mean_hue_deg > 355.0, stats.mean_hue_deg


def test_hue_shift_is_the_shorter_arc():
    a = np.tile(np.array([255, 0, 4], dtype=np.uint8), (8, 1))   # ≈359°
    b = np.tile(np.array([255, 40, 0], dtype=np.uint8), (8, 1))  # ≈9°
    assert metrics.hue_shift_degrees(a, b) < 30.0     # 350° 가 아니라 10° 대


def test_hue_shift_is_zero_for_identical_colors():
    c = np.tile(np.array(ORANGE, dtype=np.uint8), (16, 1))
    assert metrics.hue_shift_degrees(c, c) == pytest.approx(0.0, abs=1e-9)


def test_hue_metric_is_in_the_gate_list():
    assert "hue_shift_degrees" in metrics.GATE_METRICS
