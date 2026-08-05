"""`slatexport` — 파일 이름이 `slat` 이라고 slat 으로 믿지 않는가 (D28).

이 모듈의 방어는 **총계 대조** 하나다. 그게 없으면 "A5000 이 준 파일이니까 맞겠지"
가 되고, W8 이 정확히 그 자리에서 물렸다 (표면 복셀화 10,264 를 slat 이라고 부름).

합성으로 잠근다 — 실자산 `slat.safetensors` 는 리포에 없고, 있어도 그건 A5000 이
만든 것이라 **이 코드가 무엇을 거부하는지**는 증명하지 못한다.
"""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from server.slatexport import export_slat_npy, load_slat_coords, read_safetensors_header
from server.slatmask import SLAT, NotSlatCoords


def _write_safetensors(path, tensors):
    """최소 safetensors 라이터. 8바이트 길이 + JSON 헤더 + 텐서 바이트가 전부다."""
    _NAMES = {np.dtype("<i4"): "I32", np.dtype("<i8"): "I64", np.dtype("<f4"): "F32"}
    header, blob, off = {}, b"", 0
    for name, arr in tensors.items():
        b = arr.tobytes()
        header[name] = {
            "dtype": _NAMES[arr.dtype],
            "shape": list(arr.shape),
            "data_offsets": [off, off + len(b)],
        }
        blob += b
        off += len(b)
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + blob)


def _coords(n=50, seed=0):
    rng = np.random.default_rng(seed)
    c = np.unique(rng.integers(0, 64, size=(n * 3, 3)), axis=0)[:n]
    return np.concatenate([np.zeros((len(c), 1), np.int32), c.astype(np.int32)], axis=1)


def _asset(tmp_path, coords, *, total=None, name="v3-test"):
    d = tmp_path / "slot"
    d.mkdir(exist_ok=True)
    _write_safetensors(d / "slat.safetensors", {
        "feats": np.zeros((len(coords), 8), dtype=np.dtype("<f4")),
        "coords": coords.astype(np.dtype("<i4")),
    })
    (d / "manifest.json").write_text(json.dumps({
        "asset_id": name,
        "voxel_count_total": len(coords) if total is None else total,
        "a5000_job_id": "j-test",
        "contract": {"voxel_res": 64},
    }))
    return d


# ══════════════════════════════════════════════ 🔴 총계 대조 (D28)
def test_manifest_mismatch_is_rejected(tmp_path):
    """개수가 어긋나면 거부한다. **이 검사가 이 모듈의 존재 이유다.**"""
    c = _coords()
    d = _asset(tmp_path, c, total=len(c) + 673)   # W8 실측이 딱 이 모양이었다
    with pytest.raises(NotSlatCoords) as e:
        load_slat_coords(d / "slat.safetensors", manifest=json.loads((d / "manifest.json").read_text()))
    assert str(len(c)) in str(e.value)


def test_matching_manifest_passes(tmp_path):
    c = _coords()
    d = _asset(tmp_path, c)
    got = load_slat_coords(d / "slat.safetensors",
                           manifest=json.loads((d / "manifest.json").read_text()))
    assert got.shape == (len(c), 3)
    assert np.array_equal(got, c[:, 1:])          # 배치 열이 떨어져 나갔다


# ══════════════════════════════════════════════ 좌표 텐서 고르기
def test_batch_column_must_be_zero(tmp_path):
    """(N,4) 의 앞 열이 배치 인덱스가 아니면 **추측하지 않고 멈춘다.**

    어느 열이 좌표인지 찍어서 맞히면, 틀렸을 때 예외가 안 나고 그냥 다른 물체의
    좌표가 나온다 — 이 프로젝트가 여섯 번 물린 모양 그대로다.
    """
    c = _coords()
    c[3, 0] = 7
    d = _asset(tmp_path, c)
    with pytest.raises(NotSlatCoords, match="배치 인덱스"):
        load_slat_coords(d / "slat.safetensors")


def test_out_of_grid_is_rejected(tmp_path):
    c = _coords()
    c[0, 1] = 200
    d = _asset(tmp_path, c)
    with pytest.raises(NotSlatCoords, match="격자 범위"):
        load_slat_coords(d / "slat.safetensors")


def test_no_integer_2d_tensor_is_rejected(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    _write_safetensors(d / "slat.safetensors", {"feats": np.zeros((9, 8), dtype=np.dtype("<f4"))})
    with pytest.raises(NotSlatCoords, match="좌표로 볼 텐서가 없다"):
        load_slat_coords(d / "slat.safetensors")


def test_header_parses_without_safetensors_package(tmp_path):
    """의존성을 늘리지 않았다는 것 자체를 잠근다 (CLAUDE.md)."""
    d = _asset(tmp_path, _coords())
    header, off = read_safetensors_header(d / "slat.safetensors")
    assert set(header) == {"feats", "coords"}
    assert off > 8


# ══════════════════════════════════════════════ 인계물
def test_export_carries_grid_source_and_is_lossless(tmp_path):
    """좌표만 건네면 받는 쪽이 **격자 출처를 모른다** (D28-a). 스펙이 같이 가야 한다."""
    c = _coords()
    d = _asset(tmp_path, c)
    out, spec = export_slat_npy(d)

    assert spec["grid_source"] == SLAT
    assert spec["n_coords"] == spec["voxel_count_total"] == len(c)
    loaded = np.load(out)
    assert loaded.dtype == np.int16               # 0..63 이라 int16 로 손실이 없다
    assert np.array_equal(loaded.astype(np.int64), c[:, 1:])
    assert json.loads(out.with_name(out.stem + "_spec.json").read_text())["grid_source"] == SLAT
