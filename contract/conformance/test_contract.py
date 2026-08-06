"""
계약 적합성 테스트.

세 세션 어디서든 GPU 없이 `python -m pytest conformance/` 로 돌아간다.
여기 실패한 채로 서비스 코드를 짜면, 나중에 "가끔 청크가 안 맞음"이라는
가장 비싼 형태의 버그로 돌아온다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python"))
sys.path.insert(0, str(HERE))

from deltacontract import (  # noqa: E402
    CHUNK_GRID_RES,
    CHUNK_SIZE,
    DEFAULT_HALO_VOXELS,
    CONTRACT_CONSTANTS,
    VOXEL_RES,
    ChunkBinError,
    ContractMismatch,
    HEADER_SIZE,
    affected_chunks,
    albedo_to_rgba8,
    assert_contract_compatible,
    bbox_to_voxel_cells,
    blob_hash,
    buffer_views,
    canonical_sort,
    canonicalize,
    chunk_key,
    decode,
    diff_chunk_sets,
    dilate_cells,
    encode,
    normalized_to_chunk,
    normalized_to_voxel,
    parse_chunk_key,
    partition_mesh,
    split_trellis_vertex_attrs,
)
from fixture import apply_local_edit, voxel_cells_from_mesh, torus  # noqa: E402

# pydantic 이 없는 환경에서도 기하 계약 테스트는 전부 돌아야 한다.
try:
    from deltacontract.schemas import EditMask, PatchPackage  # noqa: E402

    HAS_SCHEMAS = True
except ImportError:
    HAS_SCHEMAS = False

needs_schemas = pytest.mark.skipif(not HAS_SCHEMAS, reason="pydantic 미설치")

GOLDEN = HERE / "golden"


# ══════════════════════════════════════════════════════ 픽스처


@pytest.fixture(scope="module")
def base_mesh():
    return torus()


def build_chunks(vertices, faces, attrs):
    albedo, normals = split_trellis_vertex_attrs(attrs)
    return partition_mesh(
        vertices,
        faces,
        normals=normals,
        colors_rgba8=albedo_to_rgba8(albedo),
        voxel_cells=voxel_cells_from_mesh(vertices),
    )


# ══════════════════════════════════════════════════════ 좌표계


def test_voxel_bounds_are_clamped_at_edges():
    lo = normalized_to_voxel(np.array([[-0.5, -0.5, -0.5]]))
    hi = normalized_to_voxel(np.array([[0.5, 0.5, 0.5]]))
    assert lo.tolist() == [[0, 0, 0]]
    # 상한은 안쪽으로 클램프된다 — 64 가 나오면 인덱스 범위를 벗어난다.
    assert hi.tolist() == [[VOXEL_RES - 1] * 3]


def test_chunk_grid_partitions_voxel_space_exactly():
    cells = np.stack(
        np.meshgrid(*[np.arange(VOXEL_RES)] * 3, indexing="ij"), axis=-1
    ).reshape(-1, 3)
    cids = np.unique(cells // CHUNK_SIZE, axis=0)
    assert cids.shape[0] == (VOXEL_RES // CHUNK_SIZE) ** 3 == 4096   # D75: 512 → 4,096


def test_chunk_key_roundtrip():
    for cid in [(0, 0, 0), (7, 3, 5), (1, 0, 7)]:
        assert parse_chunk_key(chunk_key(cid)) == cid


def test_canonical_order_is_permutation_invariant():
    rng = np.random.default_rng(7)
    cells = rng.integers(0, VOXEL_RES, size=(500, 3))
    a = canonical_sort(cells)
    b = canonical_sort(rng.permutation(cells))
    assert np.array_equal(a, b)


def test_canonical_order_rejects_negative():
    with pytest.raises(ValueError):
        canonical_sort(np.array([[-1, 0, 0]]))


# ══════════════════════════════════════════════════════ SparseTensor coords 정렬
#
# A5000 보고에서 확인된 함정 대응: SparseTensor 의 elementwise 연산이 coords 를
# 무시하므로, 순서가 다른 두 latent 를 그냥 더하면 조용히 틀린 결과가 나온다.


def test_strip_and_add_batch_roundtrip():
    from deltacontract import add_batch, strip_batch

    xyz = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.int64)
    c4 = add_batch(xyz)
    assert c4.shape == (2, 4) and np.all(c4[:, 0] == 0)
    assert np.array_equal(strip_batch(c4), xyz)
    assert np.array_equal(strip_batch(xyz), xyz)  # (N,3) 은 통과


def test_voxel_code_is_injective_and_range_checked():
    from deltacontract import voxel_code

    rng = np.random.default_rng(23)
    cells = np.unique(rng.integers(0, VOXEL_RES, size=(3000, 3)), axis=0)
    codes = voxel_code(cells)
    assert len(np.unique(codes)) == len(cells), "서로 다른 복셀이 같은 코드를 받았다"
    with pytest.raises(ValueError):
        voxel_code(np.array([[VOXEL_RES, 0, 0]]))
    with pytest.raises(ValueError):
        voxel_code(np.array([[-1, 0, 0]]))


def test_align_indices_matches_shuffled_coords():
    from deltacontract import align_indices

    rng = np.random.default_rng(29)
    cells = np.unique(rng.integers(0, VOXEL_RES, size=(2000, 3)), axis=0)
    ref = rng.permutation(cells)  # 같은 집합, 다른 순서 — 실제로 벌어지는 상황

    s_idx, r_idx = align_indices(cells, ref)
    assert len(s_idx) == len(cells), "공통 복셀을 전부 찾지 못했다"
    assert np.array_equal(cells[s_idx], ref[r_idx]), "매칭된 좌표가 서로 다르다"


def test_align_indices_handles_partial_overlap_and_batch_column():
    from deltacontract import add_batch, align_indices

    src = np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2]], dtype=np.int64)
    dst = np.array([[2, 2, 2], [9, 9, 9], [0, 0, 0]], dtype=np.int64)
    s_idx, d_idx = align_indices(src, dst)
    assert np.array_equal(src[s_idx], np.array([[0, 0, 0], [2, 2, 2]]))
    assert np.array_equal(dst[d_idx], np.array([[0, 0, 0], [2, 2, 2]]))

    # (N,4) 로 줘도 같은 결과여야 한다 (TRELLIS SparseTensor.coords 형태)
    s4, d4 = align_indices(add_batch(src), add_batch(dst))
    assert np.array_equal(s4, s_idx) and np.array_equal(d4, d_idx)


def test_align_indices_rejects_duplicates():
    from deltacontract import align_indices

    dup = np.array([[1, 1, 1], [1, 1, 1]], dtype=np.int64)
    with pytest.raises(ValueError):
        align_indices(dup, dup)


def test_assert_same_voxel_set():
    from deltacontract import assert_same_voxel_set

    a = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.int64)
    assert_same_voxel_set(a, a[::-1])  # 순서만 다른 건 정상
    with pytest.raises(ValueError):
        assert_same_voxel_set(a, a[:1])


def test_normal_map_encoding_is_decoded_by_default():
    """A5000 실측(2026-07-29)에서 드러난 버그의 회귀 테스트.

    TRELLIS vertex_attrs 의 뒤 3채널은 [0,1] 로 인코딩된 법선이다. 그대로 쓰면
    단위벡터 비율 0.00%. 이 변환이 호출부에 맡겨졌을 때 계약 문서 예제와 A5000
    probe **양쪽에서 동시에 빠졌다**. 예외가 안 나고 조명만 틀어지기 때문이다.
    """
    from deltacontract import normalize_normals

    rng = np.random.default_rng(41)
    unit = normalize_normals(rng.normal(size=(200, 3)))
    encoded = (unit + 1.0) * 0.5
    attrs = np.concatenate([rng.random((200, 3)), encoded], axis=1).astype(np.float32)

    _, decoded = split_trellis_vertex_attrs(attrs)  # 기본 "0_1"
    assert np.allclose(np.linalg.norm(decoded, axis=1), 1.0, atol=1e-5)
    assert np.allclose(decoded, unit, atol=1e-4), "복원된 법선이 원본과 다르다"
    assert decoded.min() < 0, "복원 후에도 음수가 없다 — 2n-1 이 안 걸렸다"

    # "raw" 는 아무것도 하지 않아야 한다 (진단용 경로)
    _, raw = split_trellis_vertex_attrs(attrs, normal_encoding="raw")
    assert np.array_equal(raw, encoded)
    assert raw.min() >= 0

    # "unit" 은 이미 [-1,1] 인 입력에 쓴다 — 길이만 맞춘다
    attrs_unit = np.concatenate([rng.random((200, 3)), unit * 3.0], axis=1).astype(np.float32)
    _, u = split_trellis_vertex_attrs(attrs_unit, normal_encoding="unit")
    assert np.allclose(np.linalg.norm(u, axis=1), 1.0, atol=1e-5)

    with pytest.raises(ValueError):
        split_trellis_vertex_attrs(attrs, normal_encoding="bogus")


def test_normalized_bounds_tolerate_flexicubes_overshoot():
    """`[-0.5,0.5]` 를 엄밀히 요구하면 실측 0.26% 의 정점에서 터진다.

    `get_defomed_verts` 의 deform 항은 결과를 클램프하지 않으므로 경계 정점이
    최대 1/(2*MESH_RES) 만큼 밖으로 나간다. 그건 버그가 아니다.
    반대로 좌표계를 통째로 잘못 넘긴 경우는 잡아야 한다.
    """
    from deltacontract import NORMALIZED_TOLERANCE, assert_in_normalized_bounds

    ok = np.array([[-0.5 - NORMALIZED_TOLERANCE * 0.5, 0.5, 0.0]], dtype=np.float32)
    assert_in_normalized_bounds(ok)  # 허용 범위 안
    assert_in_normalized_bounds(np.empty((0, 3), np.float32))  # 빈 배열도 통과

    with pytest.raises(ValueError):  # 복셀 인덱스를 그대로 넘긴 경우
        assert_in_normalized_bounds(np.array([[0.0, 32.0, 63.0]]))
    with pytest.raises(ValueError):  # 허용치 바로 바깥
        assert_in_normalized_bounds(np.array([[0.5 + NORMALIZED_TOLERANCE * 2, 0.0, 0.0]]))


def test_partition_rejects_wrong_coordinate_space():
    from deltacontract import VOXEL_RES

    v = np.array([[0, 0, 0], [VOXEL_RES - 1, 0, 0], [0, VOXEL_RES - 1, 0]], dtype=np.float32)
    with pytest.raises(ValueError):
        partition_mesh(v, np.array([[0, 1, 2]]))


def test_coord_order_and_determinism_are_contract_level():
    """3.4.0 설계 전환이 상수에 반영됐는지."""
    from deltacontract import COORD_ORDER, REQUIRES_DETERMINISTIC_ALGORITHMS

    # coord_order 는 계속 필요하다 — 근거가 "바이트 동일 판정"에서 "이음새 크기"로 바뀌었을 뿐.
    assert COORD_ORDER == "canonical"
    assert CONTRACT_CONSTANTS["coord_order"] == COORD_ORDER
    # 3.4.0: 부기가 델타를 정의하므로 해시 비교가 없다 → 결정성 플래그 불필요 (연산 -21%)
    assert REQUIRES_DETERMINISTIC_ALGORITHMS is False
    assert CONTRACT_CONSTANTS["requires_deterministic_algorithms"] is False


def test_canonical_order_is_exported_and_returns_permutation():
    """A5000 이 probe 를 짜다 걸린 지점 — 정렬 결과가 아니라 **순열 자체**가 필요하다
    (feats 재배열, native 순서 복원). 최상위에서 바로 꺼낼 수 있어야 한다."""
    from deltacontract import canonical_order

    rng = np.random.default_rng(43)
    cells = np.unique(rng.integers(0, VOXEL_RES, size=(300, 3)), axis=0)
    order = canonical_order(cells)
    assert sorted(order.tolist()) == list(range(len(cells))), "순열이 아니다"
    assert np.array_equal(cells[order], canonical_sort(cells))
    # 역순열로 원래 순서 복원 가능해야 한다
    inv = np.empty_like(order)
    inv[order] = np.arange(len(order))
    assert np.array_equal(cells[order][inv], cells)


def test_error_classes_map_1to1_to_wire_codes():
    """3090 지적 대응: 에러코드가 두 곳에서 정의되면 통합 시점에야 갈린 걸 안다.

    `schemas.ErrorBody.error_code` 의 Literal 목록과 `errors.py` 의 예외 클래스가
    정확히 같은 집합이어야 한다.
    """
    from deltacontract.errors import ERROR_CODE_TO_EXCEPTION

    codes = set(ERROR_CODE_TO_EXCEPTION)
    if HAS_SCHEMAS:
        from typing import get_args

        from deltacontract.schemas import ErrorBody

        literal = set(get_args(ErrorBody.model_fields["error_code"].annotation))
        assert codes == literal, f"errors.py 와 schemas.ErrorBody 가 어긋난다: {codes ^ literal}"

    # HTTP 상태와 재시도 정책이 실제로 붙어 있는지
    for code, cls in ERROR_CODE_TO_EXCEPTION.items():
        assert 400 <= cls.http_status < 600, code
        assert isinstance(cls.retriable, bool), code

    from deltacontract.errors import (
        BookkeepingMismatch,
        DeterminismViolation,
        UpstreamTimeout,
    )

    # BOOKKEEPING_MISMATCH 는 유일하게 재시도가 금지된 5xx 다.
    assert BookkeepingMismatch.retriable is False
    assert BookkeepingMismatch.http_status == 500
    assert UpstreamTimeout.retriable is True
    # 3.3.x 이하 호환 별칭
    assert DeterminismViolation is BookkeepingMismatch


def test_error_body_roundtrips_through_the_wire():
    from deltacontract.errors import (
        BookkeepingMismatch,
        DeltaContractError,
        InternalError,
        VersionConflict,
        raise_from_error_body,
    )

    original = VersionConflict("최신이 아니다", {"latest_version": "7"})
    body = original.to_dict()
    assert body == {
        "error_code": "VERSION_CONFLICT",
        "message": "최신이 아니다",
        "detail": {"latest_version": "7"},
    }

    # pytest 의 ExceptionInfo API 에 의존하지 않는다 — 폴백 러너에도 없다.
    try:
        raise_from_error_body(body)
    except VersionConflict as e:
        assert e.detail["latest_version"] == "7"
        assert e.message == "최신이 아니다"
    else:
        raise AssertionError("VersionConflict 가 나오지 않았다")

    with pytest.raises(BookkeepingMismatch):
        raise_from_error_body({"error_code": "BOOKKEEPING_MISMATCH", "message": "x"})

    # 모르는 코드를 조용히 삼키지 않는다 — 원본 코드를 detail 에 남긴다
    try:
        raise_from_error_body({"error_code": "WAT", "message": "y"})
    except InternalError as e:
        assert e.detail["unknown_error_code"] == "WAT"
    else:
        raise AssertionError("InternalError 가 나오지 않았다")

    # 하위 호환: ContractMismatch 는 이제 DeltaContractError 계열이다
    assert issubclass(ContractMismatch, DeltaContractError)


def test_chunk_uri_rule_is_unambiguous():
    """3090 지적 대응: `/v2` 접두사를 서버가 넣는지 클라이언트가 붙이는지가
    안 정해져 있으면 어긋났을 때 404 로만 보인다. 규칙을 함수로 고정한다."""
    from deltacontract import (
        API_PREFIX,
        chunk_uri,
        parse_chunk_uri,
        trellis_chunk_uri,
    )

    u = chunk_uri("a-77c0", "5_3_4", 7)
    assert u == "/v2/assets/a-77c0/chunks/5_3_4.v7.cbin"
    assert u.startswith(API_PREFIX + "/"), "선행 슬래시 포함 절대 경로여야 한다"
    assert parse_chunk_uri(u) == ("a-77c0", "5_3_4", 7)

    # 3090↔A5000 내부 경로는 별개 접두사
    assert trellis_chunk_uri("a-77c0", "5_3_4", 7).startswith("/v2/trellis/")

    for bad in [
        "assets/a/chunks/5_3_4.v7.cbin",          # 접두사 없음
        "/v2/assets/a/chunks/5_3_4.cbin",          # 버전 없음
        "/v2/assets/a/chunks/5-3-4.v7.cbin",       # 청크 키 문법 위반
        "https://h/v2/assets/a/chunks/5_3_4.v7.cbin",  # 호스트 포함
    ]:
        with pytest.raises(ValueError):
            parse_chunk_uri(bad)


def test_staging_uri_prefix_asymmetry_is_pinned():
    """🔴 3.11.0 이 잡 범위 staging 경로를 **문서에만** 정해서 3090 과 A5000 이
    각자 손으로 문자열을 만들었고 접두사가 갈렸다 (3090 실측 2026-07-31):

        /v2/trellis/assets/{id}/staging/... → 404   ← 유추한 쪽
        /v2/assets/{id}/staging/...         → 200   ← 구현된 쪽

    일반 청크는 내부 홉에 `/v2/trellis` 가 붙는데 staging 은 **양쪽 홉이 같다.**
    이 비대칭은 설계가 아니라 실측이다. 다시 유추하지 못하게 못 박는다.
    """
    from deltacontract import (
        is_staging_uri,
        parse_staging_chunk_uri,
        staging_chunk_uri,
        trellis_chunk_uri,
    )

    u = staging_chunk_uri("sample-snowman", "j-cc255e62d752", "2_2_4")
    assert u == "/v2/assets/sample-snowman/staging/j-cc255e62d752/chunks/2_2_4.cbin"

    # ★ 비대칭의 본체: 일반 청크에는 붙는 접두사가 staging 에는 없다
    assert trellis_chunk_uri("a", "2_2_4", 1).startswith("/v2/trellis/")
    assert not u.startswith("/v2/trellis/")

    assert parse_staging_chunk_uri(u) == ("sample-snowman", "j-cc255e62d752", "2_2_4")

    # 커밋본과 갈린다 — 클라이언트는 이걸로 캐시 정책을 나눈다 (staging 은 디스크 금지)
    from deltacontract import chunk_uri

    assert is_staging_uri(u)
    assert not is_staging_uri(chunk_uri("sample-snowman", "2_2_4", 1))

    # staging 에는 버전이 없다 — 커밋되지 않았으므로 v{n} 이 존재하지 않는다
    assert ".v" not in u.rsplit("/", 1)[-1]


def test_staging_job_id_rejects_traversal():
    """3090 실측: job_id 문자 집합에 `/` 가 없어도 `..` 자체는 통과하고,
    경로 정규화가 staging 을 한 단계 벗어나 자산 청크 경로를 가리킨다.

    출처(A5000)를 신뢰의 근거로 삼으면 그 가정이 바뀌었을 때 아무도 모른다.
    """
    from deltacontract import parse_staging_chunk_uri, staging_chunk_uri, validate_job_id

    assert validate_job_id("j-cc255e62d752") == "j-cc255e62d752"

    for bad in ["..", ".", "a/b", "a\\b", "", "../x", "/abs"]:
        with pytest.raises(ValueError):
            staging_chunk_uri("a", bad, "2_2_4")

    # 이미 만들어진 문자열로 들어오는 경로도 막는다
    for bad_uri in [
        "/v2/assets/a/staging/../chunks/2_2_4.cbin",
        "/v2/trellis/assets/a/staging/j-1/chunks/2_2_4.cbin",  # 있지도 않은 접두사
        "/v2/assets/a/staging/j-1/chunks/2_2_4.v1.cbin",       # staging 에 버전 금지
        "/v2/assets/a/staging/j-1/chunks/2-2-4.cbin",          # 청크 키 문법 위반
    ]:
        with pytest.raises(ValueError):
            parse_staging_chunk_uri(bad_uri)


def test_uri_violation_is_both_invalid_request_and_value_error():
    """3090 지적: 계약 함수가 순수 ValueError 를 던지면 요청 핸들러까지 새어
    **ErrorBody 없는 500** 이 된다. 3090 의 로컬 정의(InternalError)가 오히려
    나았고, 계약 것으로 교체하면서 나빠졌다.

    ValueError 를 떼면 기존 `except ValueError` 가 조용히 안 잡는다. 둘 다 상속한다.
    """
    from deltacontract import JOB_ID_MAX_LEN, UriRuleViolation, staging_chunk_uri
    from deltacontract.errors import DeltaContractError, InvalidRequest

    assert issubclass(UriRuleViolation, ValueError)
    assert issubclass(UriRuleViolation, InvalidRequest)
    assert issubclass(UriRuleViolation, DeltaContractError)

    try:
        staging_chunk_uri("a", "..", "2_2_4")
    except UriRuleViolation as e:
        assert e.error_code == "INVALID_REQUEST"
        assert e.http_status == 422  # 계약의 기존 선택 (FastAPI 검증 실패와 같은 코드)
        assert e.to_dict()["error_code"] == "INVALID_REQUEST"
    else:
        raise AssertionError("traversal 이 통과했다")

    # 길이 상한 — 계약에 없어서 200자가 통과했다 (3090 실측)
    with pytest.raises(UriRuleViolation):
        staging_chunk_uri("a", "j" * (JOB_ID_MAX_LEN + 1), "2_2_4")
    staging_chunk_uri("a", "j" * JOB_ID_MAX_LEN, "2_2_4")  # 경계는 통과


def test_mask_fingerprint_is_order_invariant_and_pinned():
    """🔴 3.13.0 이 지문을 넣으면서 **바이트 표현을 안 정했다.** 3090 이 후보
    5가지(int32/int64/csv/lines/json)를 실측으로 재서 역산해야 했다.

    양쪽이 각자 직렬화하면 어긋나도 "지문 불일치" 로만 보이고, 마스크가 달랐던
    것인지 인코딩이 달랐던 것인지 **구분이 안 된다** — 그 구분 불가가 이 필드를
    넣은 목적 자체를 없앤다. 그래서 함수 하나로 못 박는다.
    """
    import numpy as np

    from deltacontract import dilate_cells, mask_fingerprint

    cells = np.array([[1, 2, 3], [10, 0, 5], [1, 2, 4], [63, 63, 63]], dtype=np.int64)

    # 1) 입력 순서에 무관하다 (canonical_sort 를 거치므로)
    a = mask_fingerprint(cells)
    assert a == mask_fingerprint(cells[::-1]) == mask_fingerprint(cells[[2, 0, 3, 1]])

    # 2) dtype 에 무관하다 — 플랫폼 기본 정수 폭으로 지문이 갈리면 안 된다
    assert mask_fingerprint(cells.astype(np.int32)) == a
    assert mask_fingerprint(cells.astype(np.int16)) == a

    # 3) 셀이 하나만 달라도 갈린다
    other = cells.copy()
    other[0, 0] = 2
    assert mask_fingerprint(other) != a

    # 4) 형태가 sha256 hex 다 (와이어에 문자열로 나간다)
    assert len(a) == 64 and all(ch in "0123456789abcdef" for ch in a)

    # 5) 빈 마스크도 예외 없이 값을 낸다. 단 비어있지 않은 것과 절대 같지 않다
    empty = mask_fingerprint(np.zeros((0, 3), dtype=np.int64))
    assert len(empty) == 64 and empty != a

    # 6) raw 와 dilated 는 다른 값이다 (halo 가 0 이 아닌 한)
    assert mask_fingerprint(dilate_cells(cells, 1)) != a


@needs_schemas
def test_empty_patch_expresses_up_to_date():
    """3090 지적 대응: 이미 최신인 클라이언트에게 409 를 주면 정상 상태를
    에러로 배우고, 진짜 충돌과 구분이 안 된다."""
    p = PatchPackage.up_to_date("a-77c0", 6)
    assert p.from_version == p.to_version == 6
    assert p.is_empty

    with pytest.raises(ValueError):  # 역행은 여전히 금지
        PatchPackage(asset_id="a", from_version=5, to_version=4)
    with pytest.raises(ValueError):  # no-op 인데 내용이 있으면 모순
        PatchPackage(asset_id="a", from_version=5, to_version=5, removed_chunk_ids=["0_0_0"])

    real = PatchPackage(asset_id="a", from_version=5, to_version=6)
    assert real.is_empty  # 버전은 올랐지만 실제 변경이 없는 경우도 표현 가능


def test_mesh_grid_divides_cleanly():
    from deltacontract import MESH_CELLS_PER_VOXEL, MESH_RES

    # 마스크 복셀 1개가 메시 셀 경계와 정확히 맞아떨어져야 halo 계산이 흔들리지 않는다.
    assert MESH_RES % VOXEL_RES == 0
    assert MESH_CELLS_PER_VOXEL == 4
    assert VOXEL_RES % CHUNK_SIZE == 0
    assert (MESH_RES // CHUNK_GRID_RES) == 16  # D75: 청크 1개 = 메시 셀 16³ (전 32³)


# ══════════════════════════════════════════════════════ 마스크


def test_bbox_mask_covers_expected_cells():
    cells = bbox_to_voxel_cells((-0.5, -0.5, -0.5), (-0.5 + 4.0 / VOXEL_RES,) * 3)
    assert cells.shape == (64, 3)  # 4^3
    assert cells.min() == 0 and cells.max() == 3


def test_halo_dilation_grows_and_clamps():
    single = np.array([[0, 0, 0]])
    grown = dilate_cells(single, halo=1)
    # 코너라 27개가 아니라 2^3 = 8개만 유효
    assert grown.shape[0] == 8
    assert grown.min() >= 0

    middle = np.array([[16, 16, 16]])
    assert dilate_cells(middle, halo=1).shape[0] == 27
    assert dilate_cells(middle, halo=0).shape[0] == 1


def test_affected_chunks_folds_three_sets():
    added = np.array([[0, 0, 0]])
    removed = np.array([[CHUNK_SIZE * 2, 0, 0]])
    modified = np.array([[0, 0, 0], [VOXEL_RES - 1, VOXEL_RES - 1, VOXEL_RES - 1]])
    keys = affected_chunks(added, removed, modified)
    # 🔴 키를 손으로 적지 않는다 — 격자가 바뀌면 같이 바뀌어야 하는 값이다 (D75).
    last = CHUNK_GRID_RES - 1
    assert keys == ["0_0_0", "2_0_0", f"{last}_{last}_{last}"]
    assert affected_chunks(None, None, None) == []


@needs_schemas
def test_edit_mask_validation():
    with pytest.raises(ValueError):
        EditMask(mode="bbox")
    with pytest.raises(ValueError):
        EditMask(mode="bbox", bbox_min=(0.1, 0, 0), bbox_max=(0.0, 1, 1))
    with pytest.raises(ValueError):
        EditMask(mode="voxels", voxels=[])

    # 🔴 3.26.0 (D28-a) — mode="voxels" 는 grid_source 를 **생략할 수 없다.**
    #    기본값으로 메우면 잘못된 격자가 침묵으로 정본을 참칭한다.
    with pytest.raises(ValueError, match="grid_source"):
        EditMask(mode="voxels", voxels=[(1, 2, 3)])
    with pytest.raises(ValueError):
        EditMask(mode="voxels", voxels=[(1, 2, 64)], grid_source="slat_coords")
    with pytest.raises(ValueError):
        EditMask(mode="voxels", voxels=[(1, 2, 3)], grid_source="lasso")

    m = EditMask(mode="voxels", voxels=[(1, 2, 3)], grid_source="slat_coords")  # ok
    assert m.is_canonical_grid
    assert not EditMask(mode="voxels", voxels=[(1, 2, 3)],
                        grid_source="surface_voxelize").is_canonical_grid
    # bbox 는 격자가 없다 — 요구하지 않는다.
    EditMask(mode="bbox", bbox_min=(-0.1, -0.1, -0.1), bbox_max=(0.1, 0.1, 0.1))


@needs_schemas
def test_idempotency_key_is_required_on_work_creating_requests():
    """🔴 3.27.0 (결정 5) — 연산을 만드는 요청은 멱등 키를 **반드시** 싣는다.

    계약이 Optional 인데 구현이 필수면 어느 쪽이 진실인지 매번 확인해야 한다
    (`manifest.v99 → 200` 과 같은 계열). 그리고 **서버가 지어내면 안 된다** —
    이 필드는 연산을 식별하므로(3.7.1), 서버 발급은 같은 요청 두 번이 다른 슬롯을
    가져가게 만들고 그때 멱등성은 **예외 없이** 사라진다.

    ⚠️ 응답인 `JobStatus.idempotency_key` 는 Optional 그대로다 — 그건 에코이지 입력이 아니다.
    """
    from deltacontract.schemas import AssembleRequest, EditMask, EditRequest, JobStatus

    mask = EditMask(mode="voxels", voxels=[(1, 2, 3)], grid_source="slat_coords")
    with pytest.raises(ValueError, match="idempotency_key"):
        EditRequest(session_id="s", base_version=1, raw_prompt="x", mask=mask)

    ok = EditRequest(session_id="s", base_version=1, raw_prompt="x", mask=mask,
                     idempotency_key="deadbeefdeadbeef")
    assert ok.idempotency_key == "deadbeefdeadbeef"

    # 조립도 연산이다 — 같은 규칙을 받는다.
    assert "idempotency_key" in AssembleRequest.model_fields
    assert AssembleRequest.model_fields["idempotency_key"].is_required()

    # 응답은 그대로 Optional (에코).
    assert not JobStatus.model_fields["idempotency_key"].is_required()


@needs_schemas
def test_slat_coords_response():
    """`GET /v2/assets/{id}/slat_coords.v{n}.json` — 라쏘가 투영할 대상 (3.26.0)."""
    from deltacontract import mask_fingerprint, parse_slat_coords_uri, slat_coords_uri
    from deltacontract.schemas import SlatCoordsResponse

    cells = [(1, 2, 3), (10, 20, 30)]
    r = SlatCoordsResponse(
        asset_id="dragon-c", version=7, n_cells=len(cells), coords=cells,
        fingerprint=mask_fingerprint(cells),
    )
    assert r.grid_source == "slat_coords" and r.voxel_res == 64

    # 잘린 목록은 형태가 멀쩡해서 예외를 안 낸다 — 그래서 길이를 실어 대조한다.
    with pytest.raises(ValueError, match="n_cells"):
        SlatCoordsResponse(asset_id="a", version=1, n_cells=2, coords=[(1, 2, 3)],
                           fingerprint="x")
    with pytest.raises(ValueError):
        SlatCoordsResponse(asset_id="a", version=1, n_cells=1, coords=[(1, 2, 64)],
                           fingerprint="x")

    uri = slat_coords_uri("dragon-c", 7)
    assert uri == "/v2/assets/dragon-c/slat_coords.v7.json"
    assert parse_slat_coords_uri(uri) == ("dragon-c", 7)


def test_slat_coords_uri_rejects_malformed():
    from deltacontract import UriRuleViolation, parse_slat_coords_uri

    for bad in ("/v2/assets/a/slat_coords.json",
                "/v2/assets/a/slat_coords.v7.cbin",
                "/v2/trellis/assets/a/slat_coords.v7.json",
                "/v2/assets/a/b/slat_coords.v7.json"):
        with pytest.raises(UriRuleViolation):
            parse_slat_coords_uri(bad)


# ══════════════════════════════════════════════════════ .cbin 포맷


def test_encode_decode_roundtrip(base_mesh):
    chunks = build_chunks(*base_mesh)
    assert chunks, "청크가 하나도 안 나왔다 — 픽스처가 잘못됐다"
    for key, mesh in chunks.items():
        blob = encode(mesh)
        back = decode(blob)
        assert back.chunk_coord == mesh.chunk_coord
        assert np.array_equal(back.positions, mesh.positions)
        assert np.array_equal(back.normals, mesh.normals)
        assert np.array_equal(back.colors, mesh.colors)
        assert np.array_equal(back.indices, mesh.indices)
        assert back.voxel_count == mesh.voxel_count
        assert encode(back) == blob  # 재인코딩도 바이트 동일


def test_header_size_and_alignment(base_mesh):
    chunks = build_chunks(*base_mesh)
    mesh = next(iter(chunks.values()))
    blob = encode(mesh)
    assert HEADER_SIZE == 40 and HEADER_SIZE % 4 == 0
    views = buffer_views(blob)
    for name, (off, length) in views.items():
        assert off % 4 == 0, f"{name} bufferView 가 4바이트 정렬이 아니다"
        assert off + length <= len(blob)
    last = max(views.values(), key=lambda t: t[0])
    assert last[0] + last[1] == len(blob), "bufferView 합이 파일 길이와 안 맞는다"


def test_decode_rejects_wrong_magic_and_truncation(base_mesh):
    blob = encode(next(iter(build_chunks(*base_mesh).values())))
    with pytest.raises(ChunkBinError):
        decode(b"XXXX" + blob[4:])
    with pytest.raises(ChunkBinError):
        decode(blob[:-4])
    with pytest.raises(ChunkBinError):
        decode(blob + b"\x00\x00\x00\x00")


def test_encode_requires_canonicalize():
    from deltacontract.chunkbin import ChunkMesh

    raw = ChunkMesh(
        chunk_coord=(0, 0, 0),
        positions=np.zeros((3, 3), np.float32),
        indices=np.array([0, 1, 2], np.uint32),
    )
    with pytest.raises(ChunkBinError):
        encode(raw)


# ══════════════════════════════════════════════════════ canonicalize (§9-1)


def _tri_mesh():
    rng = np.random.default_rng(11)
    v = rng.random((40, 3)).astype(np.float32) - 0.5
    f = rng.integers(0, 40, size=(60, 3))
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
    return v, f.astype(np.uint32)


def test_canonicalize_is_invariant_to_vertex_permutation():
    v, f = _tri_mesh()
    a = encode(canonicalize((0, 0, 0), v, f.ravel()))

    rng = np.random.default_rng(3)
    perm = rng.permutation(v.shape[0])
    inv = np.empty_like(perm)
    inv[perm] = np.arange(perm.size)
    b = encode(canonicalize((0, 0, 0), v[perm], inv[f].ravel().astype(np.uint32)))
    assert a == b


def test_canonicalize_is_invariant_to_face_order():
    v, f = _tri_mesh()
    a = encode(canonicalize((0, 0, 0), v, f.ravel()))
    rng = np.random.default_rng(5)
    b = encode(canonicalize((0, 0, 0), v, rng.permutation(f).ravel().astype(np.uint32)))
    assert a == b


def test_canonicalize_welds_exact_duplicates():
    v, f = _tri_mesh()
    a = encode(canonicalize((0, 0, 0), v, f.ravel()))
    # 정점 배열 뒤에 완전 중복본을 붙이고, 일부 face 가 그걸 가리키게 한다.
    v2 = np.concatenate([v, v], axis=0)
    f2 = f.copy()
    f2[: len(f2) // 2] += v.shape[0]
    b = encode(canonicalize((0, 0, 0), v2, f2.ravel().astype(np.uint32)))
    assert a == b


def test_canonicalize_preserves_winding():
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32) * 0.1
    cw = canonicalize((0, 0, 0), v, np.array([0, 1, 2], np.uint32))
    ccw = canonicalize((0, 0, 0), v, np.array([0, 2, 1], np.uint32))
    assert encode(cw) != encode(ccw), "winding 이 정규화 과정에서 소실되면 안 된다"


# ══════════════════════════════════════════════════════ 분할 (partition)


def test_partition_covers_every_face_exactly_once(base_mesh):
    v, f, a = base_mesh
    chunks = build_chunks(v, f, a)
    total_tris = sum(m.index_count // 3 for m in chunks.values())
    assert total_tris == f.shape[0], "삼각형이 누락되거나 중복 배정됐다"


def test_partition_assigns_by_centroid(base_mesh):
    v, f, a = base_mesh
    chunks = build_chunks(v, f, a)
    centroids = v[f].mean(axis=1)
    expected = {chunk_key(c) for c in np.unique(normalized_to_chunk(centroids), axis=0)}
    assert set(chunks.keys()) == expected


def test_partition_is_stable_under_input_permutation(base_mesh):
    v, f, a = base_mesh
    ref = {k: blob_hash(encode(m)) for k, m in build_chunks(v, f, a).items()}

    rng = np.random.default_rng(17)
    perm = rng.permutation(v.shape[0])
    inv = np.empty_like(perm)
    inv[perm] = np.arange(perm.size)
    shuffled = {
        k: blob_hash(encode(m))
        for k, m in build_chunks(v[perm], rng.permutation(inv[f]), a[perm]).items()
    }
    assert ref == shuffled, "입력 순서가 바뀌었을 뿐인데 청크 해시가 달라졌다 (§9-1 위반)"


def test_empty_chunks_are_omitted(base_mesh):
    chunks = build_chunks(*base_mesh)
    assert len(chunks) < 512, "빈 청크까지 생성됐다 (§4.3 위반)"
    assert all(m.index_count > 0 for m in chunks.values())


# ══════════════════════════════════════════════════════ 델타 (핵심)


def test_local_edit_only_changes_local_chunks(base_mesh):
    v0, f0, a0 = base_mesh
    v1, f1, a1 = apply_local_edit(v0, f0, a0)

    before = {k: blob_hash(encode(m)) for k, m in build_chunks(v0, f0, a0).items()}
    after = {k: blob_hash(encode(m)) for k, m in build_chunks(v1, f1, a1).items()}

    changed, removed = diff_chunk_sets(before, after)
    unchanged = [k for k in after if k in before and before[k] == after[k]]

    assert changed, "편집했는데 바뀐 청크가 없다 — 픽스처나 분할이 잘못됐다"
    assert unchanged, "편집 영역 밖 청크가 전부 바뀌었다 — 델타의 전제가 무너졌다"

    # 편집 bbox 의 x 하한은 0.15 → voxel floor(0.65*64)=41.
    # 🔴 청크 하한을 **유도한다.** 예전에는 5 를 손으로 적어 뒀는데, D75 로 격자가
    #    바뀌자 그 5 가 실제 하한(10)보다 헐거워졌다 — 통과는 하지만 아무것도 안
    #    지키는 상태가 된다. 격자에서 계산하면 격자를 또 바꿔도 따라온다.
    cx_min = int(0.65 * VOXEL_RES) // CHUNK_SIZE
    for key in list(changed) + list(removed):
        cx = parse_chunk_key(key)[0]
        assert cx >= cx_min, f"편집 영역과 무관한 청크가 바뀌었다: {key} (하한 {cx_min})"

    # 🔴 D75 — 청크가 4복셀로 잘아지면서 이 편집이 **청크를 비우기 시작했다**(18개).
    #    예전에는 `assert not removed` 였다. 비워진 청크는 새 바이트가 없어서
    #    diff 로 만든 목록에서 사라지고, 클라이언트가 옛 기하를 들고 남는다 —
    #    부기를 배치에서 유도하는 이유 그 자체다 (§4.3). 이제 이 픽스처가 그 경로를
    #    실제로 밟는다는 사실을 잠근다.
    assert removed, "청크가 하나도 안 비워졌다 — removed 경로를 이 픽스처가 더는 안 밟는다"


def test_delta_is_a_small_fraction_of_full_payload(base_mesh):
    v0, f0, a0 = base_mesh
    v1, f1, a1 = apply_local_edit(v0, f0, a0)
    c0 = build_chunks(v0, f0, a0)
    c1 = build_chunks(v1, f1, a1)

    h0 = {k: blob_hash(encode(m)) for k, m in c0.items()}
    h1 = {k: blob_hash(encode(m)) for k, m in c1.items()}
    changed, _ = diff_chunk_sets(h0, h1)

    full = sum(len(encode(m)) for m in c1.values())
    delta = sum(len(encode(c1[k])) for k in changed)
    assert delta < full * 0.45, f"델타가 전체의 {delta / full:.0%} — 청크 크기 재검토 필요(§8.1)"


def _bookkeeping_chunks(v0, v1, halo: int) -> set[str]:
    """RePaint 부기가 내놓을 '영향받은 복셀' 집합을 흉내낸 뒤 halo 를 적용해 청크로 접는다."""
    moved = np.any(v0 != v1, axis=1)
    cells = normalized_to_voxel(np.concatenate([v0[moved], v1[moved]], axis=0))
    return set(affected_chunks(dilate_cells(cells, halo), None, None))


def test_halo_zero_is_insufficient(base_mesh):
    """§8.2 가 왜 존재하는지에 대한 실행 가능한 증거.

    halo 없이 '움직인 복셀이 속한 청크'만 갱신 대상으로 잡으면 실제로 놓치는 청크가
    나온다. 원인은 디코더 receptive field 이전에 더 단순한 데 있다: 정점 하나만
    움직인 삼각형의 **무게중심**이, 움직인 어떤 정점도 살지 않는 이웃 청크로
    넘어갈 수 있다. 그 청크는 바이트가 바뀌었는데 갱신 목록에 없다 → 클라이언트에
    찢어진 이음매가 남는다.
    """
    v0, f0, a0 = base_mesh
    v1, f1, a1 = apply_local_edit(v0, f0, a0)
    h0 = {k: blob_hash(encode(m)) for k, m in build_chunks(v0, f0, a0).items()}
    h1 = {k: blob_hash(encode(m)) for k, m in build_chunks(v1, f1, a1).items()}
    changed, removed = diff_chunk_sets(h0, h1)

    missed = (set(changed) | set(removed)) - _bookkeeping_chunks(v0, v1, halo=0)
    assert missed, "halo=0 인데 놓친 청크가 없다 — 픽스처가 경계를 안 건드리고 있다"


def test_bookkeeping_with_halo_covers_all_changed_chunks(base_mesh):
    """FINAL §0: diff 를 하지 않는다. 부기가 말한 영향 청크가 실제 해시 변경을 **덮어야** 한다.

    한 방향(부기 ⊇ 해시변경)만 요구한다. 부기가 더 넓게 잡는 건 안전하고(전송량만 손해),
    좁게 잡으면 갱신 누락이라 치명적이다.
    """
    v0, f0, a0 = base_mesh
    v1, f1, a1 = apply_local_edit(v0, f0, a0)
    h0 = {k: blob_hash(encode(m)) for k, m in build_chunks(v0, f0, a0).items()}
    h1 = {k: blob_hash(encode(m)) for k, m in build_chunks(v1, f1, a1).items()}
    changed, removed = diff_chunk_sets(h0, h1)

    # 🔴 D75 — 청크가 4복셀이 되면서 **halo=1 로는 부족해졌다.** 그 사실 자체를 잠근다:
    #    기본값을 되돌리면 이 단언이 먼저 깨진다.
    missed1 = (set(changed) | set(removed)) - _bookkeeping_chunks(v0, v1, halo=1)
    assert missed1, (
        "halo=1 이 다 덮는다 — DEFAULT_HALO_VOXELS 를 1 로 되돌려도 되는지 재검토하라"
    )

    missed = (set(changed) | set(removed)) - _bookkeeping_chunks(v0, v1, DEFAULT_HALO_VOXELS)
    assert not missed, (
        f"halo={DEFAULT_HALO_VOXELS} 로도 놓친 청크: {sorted(missed)} — 키워야 한다 (§8.2)"
    )


# ══════════════════════════════════════════════════════ 계약 상수 / 스키마


def test_contract_compat_fails_fast():
    assert_contract_compatible(dict(CONTRACT_CONSTANTS))
    bad = dict(CONTRACT_CONSTANTS)
    bad["chunk_size"] = CHUNK_SIZE * 2
    with pytest.raises(ContractMismatch):
        assert_contract_compatible(bad)
    with pytest.raises(ContractMismatch):
        assert_contract_compatible({"contract_version": 1})  # 키 누락


@needs_schemas
def test_patch_rejects_backwards_version():
    # to_version == from_version 은 3.2.0 부터 **허용**된다 ("따라올 것 없음").
    # 그 동작은 test_empty_patch_expresses_up_to_date 가 따로 검증한다.
    with pytest.raises(ValueError):
        PatchPackage(asset_id="a", from_version=3, to_version=2)
    PatchPackage(asset_id="a", from_version=3, to_version=4)


@needs_schemas
def test_schema_field_names_match_csharp_mirror():
    """C# 미러(unity/ChunkContracts.cs)의 JsonProperty 이름과 대조한다.

    필드 이름이 갈리는 건 이 프로젝트에서 가장 자주 나올 수 있는 사고라 자동화한다.
    """
    from deltacontract.schemas import (
        ChunkEntry,
        ChunkManifest,
        ContractInfo,
        EditMask,
        EditRequest,
        ErrorBody,
        GenerateRequest,
        JobStatus,
        PatchPackage,
        ServerHealth,
        SlatCoordsResponse,
        SpatialContext,
    )

    cs = (HERE.parent / "unity" / "ChunkContracts.cs").read_text(encoding="utf-8")
    models = (
        ContractInfo, ChunkEntry, ChunkManifest, PatchPackage, SpatialContext,
        GenerateRequest, SlatCoordsResponse, EditMask, EditRequest, JobStatus,
        ErrorBody, ServerHealth,
    )
    missing = [
        f"{m.__name__}.{name}"
        for m in models
        for name in m.model_fields
        # 닫는 괄호까지 보지 않는다 — C# 쪽은 NullValueHandling 같은 인자가 붙는다.
        if f'JsonProperty("{name}"' not in cs
    ]
    assert not missing, f"C# 미러에 없는 필드: {missing}"


# ══════════════════════════════════════════════════════ 골든 벡터


@pytest.mark.skipif(not (GOLDEN / "golden.json").exists(), reason="make_golden.py 를 먼저 실행할 것")
def test_matches_golden(base_mesh):
    golden = json.loads((GOLDEN / "golden.json").read_text(encoding="utf-8"))
    assert golden["contract"] == CONTRACT_CONSTANTS, "골든이 현재 계약 상수와 다르다 — 재생성 필요"

    v0, f0, a0 = base_mesh
    v1, f1, a1 = apply_local_edit(v0, f0, a0)
    for label, (v, f, a) in (("v1", (v0, f0, a0)), ("v2", (v1, f1, a1))):
        produced = {k: blob_hash(encode(m)) for k, m in build_chunks(v, f, a).items()}
        expected = {k: e["hash"] for k, e in golden["versions"][label].items()}
        assert produced == expected, f"{label} 해시가 골든과 다르다"


@pytest.mark.skipif(not (GOLDEN / "golden.json").exists(), reason="make_golden.py 를 먼저 실행할 것")
def test_golden_files_decode():
    for path in sorted(GOLDEN.glob("*.cbin")):
        mesh = decode(path.read_bytes())
        assert mesh.index_count % 3 == 0
        assert mesh.positions.shape[0] == mesh.vertex_count
