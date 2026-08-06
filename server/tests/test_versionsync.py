"""커밋 전 필수 파일 검사 — **202 뒤 워커에서 죽는 것**을 앞당겨 막는가.

A5000 `V1-REQUIRED-FILES.md` 실측: `slat.safetensors` · `input.png` · slat 메타
`norm_*` 이 없으면 라우트는 202 를 주고 워커에서 죽는다. "요청은 성공, 결과는 없음"
이라 원인을 찾기 어렵다 — 3090 이 두 번 연속 그 자리에서 멈췄다.
"""

from __future__ import annotations

import json
import struct

import pytest

from server.versionsync import (
    REQUIRED_SLAT_META,
    RequiredFilesMissing,
    check_v1_payload,
)


def _slat(path, meta: dict):
    header = {"coords": {"dtype": "I32", "shape": [1, 4], "data_offsets": [0, 16]}}
    if meta is not None:
        header["__metadata__"] = meta
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * 16)


def _asset(tmp_path, *, slat_meta=None, image=True, slat=True):
    d = tmp_path / "a"
    d.mkdir(exist_ok=True)
    if slat:
        _slat(d / "slat.safetensors", slat_meta if slat_meta is not None else {
            "norm_mean": "[0]", "norm_std": "[1]", "slat_space": "denormalized"})
    if image:
        (d / "input.png").write_bytes(b"\x89PNG")
    return d


def test_complete_payload_passes(tmp_path):
    out = check_v1_payload(_asset(tmp_path), n_chunks=376)
    assert out["n_chunks"] == 376
    assert set(REQUIRED_SLAT_META) <= set(out["slat_meta_keys"])


def test_missing_slat_is_caught_before_sending(tmp_path):
    with pytest.raises(RequiredFilesMissing, match="slat.safetensors"):
        check_v1_payload(_asset(tmp_path, slat=False), n_chunks=376)


def test_missing_image_is_caught(tmp_path):
    with pytest.raises(RequiredFilesMissing, match="input.png"):
        check_v1_payload(_asset(tmp_path, image=False), n_chunks=376)


def test_source_png_counts_as_the_image(tmp_path):
    """생성 경로는 RGBA 를 `source.png` 로 남긴다 — 그것도 받는다."""
    d = _asset(tmp_path, image=False)
    (d / "source.png").write_bytes(b"\x89PNG")
    assert check_v1_payload(d, n_chunks=1)["image"] == "source.png"


@pytest.mark.parametrize("drop", REQUIRED_SLAT_META)
def test_each_required_meta_key_is_checked(tmp_path, drop):
    """🔴 `norm_*` 이 없으면 **다음 편집이 불가능한 막다른 판본**이 된다."""
    meta = {"norm_mean": "[0]", "norm_std": "[1]", "slat_space": "denormalized"}
    meta.pop(drop)
    with pytest.raises(RequiredFilesMissing, match=drop):
        check_v1_payload(_asset(tmp_path, slat_meta=meta), n_chunks=1)


def test_normalized_slat_space_is_refused_loudly(tmp_path):
    """정규화 상태로 넣으면 **조용히 틀린 기하**가 나온다 — 예외가 안 난다."""
    meta = {"norm_mean": "[0]", "norm_std": "[1]", "slat_space": "normalized"}
    with pytest.raises(RequiredFilesMissing, match="denormalized"):
        check_v1_payload(_asset(tmp_path, slat_meta=meta), n_chunks=1)


def test_zero_chunks_is_refused(tmp_path):
    with pytest.raises(RequiredFilesMissing, match="빈 판본"):
        check_v1_payload(_asset(tmp_path), n_chunks=0)
