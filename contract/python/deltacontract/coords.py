"""
좌표계 · 청크 그리드 · canonical ordering.

이 모듈이 정의하는 것은 "세 세션이 같은 말을 하는가"의 전부다. 여기 있는 상수 중
하나라도 세션마다 다르면 델타는 조용히 깨지고, 증상은 "가끔 청크가 안 맞음"으로만
나타나서 디버깅이 매우 비싸다. 그래서 모든 상수는 CONTRACT_VERSION 에 묶여 있고,
매니페스트/응답 JSON에 그대로 실려 나간다 — 받는 쪽이 자기 상수와 비교해서 다르면
즉시 거부하도록 되어 있다 (assert_contract_compatible 참고).

────────────────────────────────────────────────────────────────────────
좌표계 3층 구조
────────────────────────────────────────────────────────────────────────

  1) NORMALIZED   float [-0.5, +0.5]^3
     TRELLIS 메시가 나오는 공간. 오브젝트 로컬. 모든 기하 데이터의 기준.

  2) VOXEL        int [0, 64)^3
     TRELLIS 1 의 SLat 좌표 공간. `sample_sparse_structure()` 가
     `torch.argwhere(decoder(z_s) > 0)` 로 내놓는 활성 복셀 인덱스가 여기 산다.
     편집 마스크, 부기(added/removed/modified), 청크 분류가 전부 이 공간을 쓴다.

  3) CHUNK        int [0, 8)^3
     VOXEL 을 CHUNK_SIZE(=8) 로 나눈 것. 512개 슬롯.

Unity 월드 좌표는 이 계약에 등장하지 않는다. anchor pose ↔ NORMALIZED 변환은
전적으로 클라이언트 책임이다 (서버가 앵커를 알면 안 된다).

────────────────────────────────────────────────────────────────────────
왜 64³ / CHUNK_SIZE = 8 인가  (contract_version 2 에서 32³/4 에서 변경)
────────────────────────────────────────────────────────────────────────
v1 은 TRELLIS.2 전제로 occupancy 32³ 위에 청크를 얹었다. TRELLIS.2 를 버리고
TRELLIS 1 + SLat RePaint 로 가면서 정수 공간을 SLat 그리드(64³)에 맞춘다.

이유는 편의가 아니라 필연이다: 마스크 복셀 하나가 **모델이 실제로 재생성할 수 있는
최소 단위**와 1:1 로 대응해야 한다. 마스크 공간이 SLat 그리드보다 성기면 "이 복셀만
다시 만들어줘"라는 요청을 모델 좌표로 옮길 때 반올림이 생기고, 그 반올림은
경계에서 비결정적으로 흔들려 §9-2 를 직접 위반한다.

결과적으로 FINAL 명세 §8.1 의 원래 숫자(64³, chunk_size 8, 512 슬롯)로 돌아온다.
슬롯 수(=매니페스트 오버헤드)와 청크당 부피(=국소 편집 1회의 재전송량)의
트레이드오프는 명세와 동일하다. 튜닝은 §13-2 실측 후.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Set, Tuple

import numpy as np

# ══════════════════════════════════════════════════════════ 계약 상수

CONTRACT_VERSION = 3

NORMALIZED_MIN = -0.5
NORMALIZED_MAX = 0.5
NORMALIZED_SPAN = NORMALIZED_MAX - NORMALIZED_MIN  # 1.0

# A5000 실측 확인 완료 (2026-07-29):
#   ss_flow_img_dit_L_16l8.resolution = 16, SparseStructureDecoder 가 UpsampleBlock3d
#   2개로 ×4 → coords ∈ [0, 64). slat_flow.in_channels = 8, coords shape (N,4)[b,x,y,z].
VOXEL_RES = 64
CHUNK_SIZE = 8
CHUNK_GRID_RES = VOXEL_RES // CHUNK_SIZE  # 8  → 8³ = 512 슬롯

# SLat 채널 수. RePaint 주입 시 feats shape 검증에 쓴다.
SLAT_CHANNELS = 8

# 메시 디코더 내부 격자 해상도. SLatMeshDecoder 가 SparseSubdivideBlock3d 2개로
# 64 → 128 → 256 으로 올린 뒤 FlexiCubes 를 돌린다 (`SparseFeatures2Mesh(res=resolution*4)`).
#
# 이 값은 **청크 분류에 쓰이지 않는다.** 분할은 NORMALIZED 공간에서 무게중심으로만
# 하므로 메시 격자가 몇이든 무관하다. 그럼에도 상수로 박아두는 이유는 두 가지다:
#   1) 정수 정합성 — MESH_RES 가 VOXEL_RES 의 배수가 아니면 "마스크 복셀 1개"가
#      메시 셀 경계와 어긋나 halo 계산이 흔들린다. 아래 assert 로 강제한다.
#   2) 해석 — 마스크 복셀 1개 = 메시 셀 4³ 개, 청크 1개 = 메시 셀 32³ 개.
#      halo_margin_voxels=1 은 실제로는 메시 셀 4칸짜리 완충대라는 뜻이다.
MESH_RES = 256
MESH_CELLS_PER_VOXEL = MESH_RES // VOXEL_RES  # 4

assert VOXEL_RES % CHUNK_SIZE == 0, "CHUNK_SIZE 는 VOXEL_RES 를 나누어떨어져야 한다"
assert MESH_RES % VOXEL_RES == 0, "MESH_RES 는 VOXEL_RES 의 배수여야 한다"

# ─────────────────────────────────────────────────────────────────────
# coords 정본 순서 — contract_version 3에서 계약으로 승격
# ─────────────────────────────────────────────────────────────────────
# A5000 실측(2026-07-29, 실험 C): SLat 을 native 순서로 디코딩한 결과와 canonical
# 순서로 디코딩한 결과가 **다른 메시**다. 반올림 흔들림이 아니라 정점 수 자체가
# 다르다 (190714 vs 190694, faces 381344 vs 381300). 결정성 플래그를 켜도 사라지지
# 않는다 — spconv 의 reduction 이 입력 행 순서에 의존하고, 그 차이가 SDF 부호를
# 뒤집어 FlexiCubes 가 다른 정점을 뱉기 때문이다.
#
# 따라서 "어느 순서가 정본인가"를 계약이 정해야 한다. canonical 을 택한다:
#   - FINAL §9-1 이 이미 요구하는 바다.
#   - native 순서는 `sample_sparse_structure()` 의 `torch.argwhere` 출력에 의존한다.
#     우리 통제 밖이고, 업스트림이 바뀌면 조용히 따라 바뀐다.
#   - 실측상 native coords 는 Morton 순이 **아니다** (canonical_order 가 항등순열이
#     아님이 확인됨). 즉 이 선택은 실질적인 차이를 만든다.
#
# 결정성 플래그와 달리 **이건 계속 필요하다.** 근거가 바뀌었을 뿐이다:
# 이제는 "바이트 동일 판정" 때문이 아니라 **이음새 크기** 때문이다.
# base 를 canonical 로 디코딩했는데 edit 을 native 로 디코딩하면, 보존 영역의
# 기하가 1e-6 이 아니라 위상 단위로 어긋난다(V 190714 vs 190694). 그러면
# 옛 바이트와 새 바이트가 만나는 면의 이음새가 눈에 보이게 커진다.
#
# 저장된 SLat 을 재디코딩할 때는 반드시 저장 시점과 같은 순서로 넣어야 한다.
# 저장 포맷의 coord_order 메타데이터를 로드 시점에 검증할 것. 비용은 0 이다.
COORD_ORDER = "canonical"

# ─────────────────────────────────────────────────────────────────────
# 결정성 플래그 — 더 이상 필요하지 않다 (3.4.0에서 True → False)
# ─────────────────────────────────────────────────────────────────────
# 3.3.x 까지는 전제였다. "안 바뀐 청크는 바이트 동일"을 **해시 비교로 판정**했기
# 때문이다. 그 판정이 성립하려면 디코딩이 결정적이어야 했다.
#
# 3.4.0 에서 판정 방식을 바꿨다 — 무엇이 바뀌었는지는 **부기(마스크+halo)가 정한다.**
# 해시를 비교하지 않으므로 디코딩이 결정적일 필요가 없다. FINAL §0 의 원래 원칙
# ("diff 를 하지 않는다. 편집 마스크는 생성 시점에 이미 알고 있다")으로 돌아온 것이다.
#
# 얻는 것:
#   - 연산 -21% (요청당 약 1.7초)
#   - A5000 이 발견한 "프로세스 내 반복 디코딩이 결정성을 깬다" 문제가 **소멸**한다.
#     상주 서버·상주 셸을 그냥 짜면 된다
#
# 이음새 때문에 되살려야 하는 것 아니냐는 의심이 있었고, A5000 이 X/Y/Z/W 대조로
# 기각했다 (2026-07-29): base·edit 을 모두 ON 으로 만들어도 이음새가 3자리까지 같다.
# 경계 정점은 비결정성에 흔들리지 않는다.
#
# 켜도 해롭지는 않다. 다만 대가를 치를 이유가 없다.
REQUIRES_DETERMINISTIC_ALGORITHMS = False

# ─────────────────────────────────────────────────────────────────────
# NORMALIZED 공간 허용 오차
# ─────────────────────────────────────────────────────────────────────
# 1차 보고의 "(1-1e-8) 계수 덕에 [-0.5,0.5] 를 엄밀히 넘지 않는다"는 **틀렸다**
# (A5000 자기정정, 2026-07-29). `get_defomed_verts` 의
#     v_pos / res - 0.5 + (1 - 1e-8) / (res * 2) * tanh(deform)
# 는 deform 을 반 셀로 제한할 뿐 결과를 클램프하지 않는다. v_pos=0 인 경계 정점은
# 밖으로 밀려난다. 실측 이탈률 0.26%, 최대 초과 4.14e-4 (이론 상한 1/512 안).
#
# 클램프하지 않는다 — 기하를 왜곡시키고, 얻는 게 "불변식이 예뻐 보이는 것"뿐이다.
# 대신 허용치를 계약에 명시하고, 검증은 이 여유를 반영한다.
NORMALIZED_TOLERANCE = 1.0 / (2 * MESH_RES)  # 1/512 ≈ 1.953e-3

# canonical 정점 정렬에 쓰는 위치 양자화. 값 자체를 바꾸지 않고 **정렬 키**로만 쓴다.
# 2^20 → NORMALIZED 공간에서 약 1e-6 해상도. float32 유효자릿수(~7자리) 안쪽이라
# "같은 float32 는 같은 키"가 보장되고, 그보다 미세한 차이는 애초에 float32로 구분 불가.
POSITION_QUANT_BITS = 20
POSITION_QUANT_SCALE = float(1 << POSITION_QUANT_BITS)

Coord = Tuple[int, int, int]


CONTRACT_CONSTANTS = {
    "contract_version": CONTRACT_VERSION,
    "normalized_min": NORMALIZED_MIN,
    "normalized_max": NORMALIZED_MAX,
    "voxel_res": VOXEL_RES,
    "chunk_size": CHUNK_SIZE,
    "chunk_grid_res": CHUNK_GRID_RES,
    "position_quant_bits": POSITION_QUANT_BITS,
    "slat_channels": SLAT_CHANNELS,
    "mesh_res": MESH_RES,
    "coord_order": COORD_ORDER,
    "requires_deterministic_algorithms": REQUIRES_DETERMINISTIC_ALGORITHMS,
}


# 예외 계층은 errors.py 가 정본이다 (에러코드·HTTP 상태·재시도 여부가 한 곳에 묶여 있다).
# 여기서는 하위 호환을 위해 이름만 재수출한다.
from .errors import ContractMismatch  # noqa: E402,F401


def assert_contract_compatible(remote: dict) -> None:
    """상대 응답에 실려온 계약 상수를 검증한다.

    3090은 A5000 응답에 대해, Unity는 3090 응답에 대해 반드시 이걸 호출한다.
    "일단 돌려보고 이상하면 고친다"가 가장 비싼 실패 모드인 영역이라 fail-fast 한다.
    """
    diff = {
        k: (v, remote.get(k))
        for k, v in CONTRACT_CONSTANTS.items()
        if k in remote and remote[k] != v
    }
    missing = [k for k in CONTRACT_CONSTANTS if k not in remote]
    if diff or missing:
        raise ContractMismatch(
            f"계약 상수 불일치 (local vs remote): {diff}; 누락된 키: {missing}"
        )


# ══════════════════════════════════════════════════════════ canonical order
#
# FINAL 명세 §9-1 [필수]: "활성 복셀을 방문하는 순서가 고정된 canonical order 여야 한다."
# 업스트림 구현이 우연히 결정적인 것에 기대지 않고, 파이프라인에 들어오는 모든 좌표
# 배열을 여기 통과시켜 순서를 우리가 소유한다.


def morton3(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
    """3D Morton(Z-order) 코드. 각 성분은 0..2^21-1 정수."""

    def _spread(v: np.ndarray) -> np.ndarray:
        v = v.astype(np.uint64) & np.uint64(0x1FFFFF)
        v = (v | (v << np.uint64(32))) & np.uint64(0x1F00000000FFFF)
        v = (v | (v << np.uint64(16))) & np.uint64(0x1F0000FF0000FF)
        v = (v | (v << np.uint64(8))) & np.uint64(0x100F00F00F00F00F)
        v = (v | (v << np.uint64(4))) & np.uint64(0x10C30C30C30C30C3)
        v = (v | (v << np.uint64(2))) & np.uint64(0x1249249249249249)
        return v

    return _spread(x) | (_spread(y) << np.uint64(1)) | (_spread(z) << np.uint64(2))


def canonical_order(coords: np.ndarray) -> np.ndarray:
    """(N,3) 비음수 정수 좌표 -> Morton 순 정렬 인덱스.

    같은 좌표 **집합**이면 입력 순서와 무관하게 항상 같은 순열이 나온다.
    """
    coords = np.asarray(coords)
    if coords.size == 0:
        return np.empty((0,), dtype=np.int64)
    if coords.ndim != 2 or coords.shape[1] != 3:
        raise ValueError(f"(N,3) 정수 좌표가 필요하다. got {coords.shape}")
    if coords.min() < 0:
        raise ValueError("Morton 코드는 음수 좌표를 지원하지 않는다. 그리드 좌표로 변환 후 호출할 것.")
    keys = morton3(coords[:, 0], coords[:, 1], coords[:, 2])
    return np.argsort(keys, kind="stable")


def canonical_sort(coords: np.ndarray) -> np.ndarray:
    coords = np.asarray(coords)
    if coords.size == 0:
        return coords.reshape(0, 3)
    return coords[canonical_order(coords)]


# ══════════════════════════════════════════════════════════════════════════
# 마스크 지문 — 3.14.0
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 **또 규칙만 적고 함수를 안 줬다.** 3.13.0 이 `BChunkResponse.mask_fingerprint`
# 를 넣으면서 "canonical_sort 후 sha256" 까지만 적고 **바이트 표현을 안 정했다.**
#
# 3090 이 후보 5가지(int32 / int64 / csv / lines / json)를 실측으로 재서 역산했다
# (2026-07-31). 셀 수가 다른 두 지문이 동시에 맞아 우연은 아니지만, **명세가
# 아니라 실측에 기댄 가정**이라고 스스로 적었다. 그 가정을 없앤다.
#
# 이게 세 번째다: 3.11.0 staging 경로(문서만) → 3.13.0 staging 함수 추가,
# 3.13.0 지문(문서만) → 여기. **계약에 새 식별자를 정의하면 같은 드롭에서
# 함수도 같이 낸다.** 규율은 지켜지지 않고 함수는 지켜진다.


def mask_fingerprint(cells: np.ndarray) -> str:
    """마스크 셀 집합의 지문. **양쪽이 이 함수만 쓴다.**

    두 서버가 각자 직렬화하면 어긋나도 예외가 안 나고 "지문 불일치" 로만 보인다 —
    그러면 마스크가 실제로 달랐던 것인지 인코딩이 달랐던 것인지 구분이 안 된다.
    그 구분 불가가 이 필드를 넣은 목적 자체를 없앤다.

    정의 (3090 실측 형태를 계약으로 승격):

        sha256( canonical_sort(cells).astype(int32).tobytes() )   # C-contiguous

    `canonical_sort` 를 거치므로 **입력 셀 순서에 무관**하다.
    `int32` 인 이유는 VOXEL 좌표가 `[0, 64)` 이라 폭이 남고, 플랫폼별 기본 정수
    폭(Windows int32 / Linux int64)에 따라 지문이 갈리는 걸 막기 위해서다.

    halo 적용 **전** 원본 셀에 쓰면 `mask_fingerprint`,
    `dilate_cells()` 결과에 쓰면 `mask_fingerprint_dilated` 다.
    """
    import hashlib

    cells = np.asarray(cells)
    if cells.size == 0:
        canon = np.zeros((0, 3), dtype=np.int32)
    else:
        canon = canonical_sort(cells).astype(np.int32)
    return hashlib.sha256(np.ascontiguousarray(canon).tobytes()).hexdigest()


# ══════════════════════════════════════════════════════════ 좌표 변환


def normalized_to_voxel(pts: np.ndarray) -> np.ndarray:
    """NORMALIZED float -> VOXEL 정수 셀 인덱스 [0, 64). 경계는 안쪽으로 클램프."""
    pts = np.asarray(pts, dtype=np.float64)
    cell = np.floor((pts - NORMALIZED_MIN) / NORMALIZED_SPAN * VOXEL_RES)
    return np.clip(cell, 0, VOXEL_RES - 1).astype(np.int64)


def voxel_to_normalized_center(cells: np.ndarray) -> np.ndarray:
    """VOXEL 셀 -> 그 셀 중심의 NORMALIZED 좌표."""
    cells = np.asarray(cells, dtype=np.float64)
    return (cells + 0.5) / VOXEL_RES * NORMALIZED_SPAN + NORMALIZED_MIN


def voxel_to_chunk(cells: np.ndarray) -> np.ndarray:
    """VOXEL 셀 -> CHUNK 좌표. FINAL §4.2 chunk_id() 의 벡터화 버전."""
    return np.asarray(cells, dtype=np.int64) // CHUNK_SIZE


def normalized_to_chunk(pts: np.ndarray) -> np.ndarray:
    return voxel_to_chunk(normalized_to_voxel(pts))


def chunk_bounds_normalized(cid: Coord) -> Tuple[np.ndarray, np.ndarray]:
    """청크의 NORMALIZED AABB."""
    c = np.asarray(cid, dtype=np.float64)
    lo = c * CHUNK_SIZE / VOXEL_RES * NORMALIZED_SPAN + NORMALIZED_MIN
    hi = (c + 1) * CHUNK_SIZE / VOXEL_RES * NORMALIZED_SPAN + NORMALIZED_MIN
    return lo, hi


# ══════════════════════════════════════════════════════════ 청크 키 (JSON 경계)
#
# chunk_id 는 네트워크/JSON 에서 **항상 "x_y_z" 문자열**이다.
# 튜플로 다니다가 JSON 직렬화에서 리스트가 되어 dict 키로 못 쓰는 사고를 원천 차단.


def chunk_key(cid: Sequence[int]) -> str:
    return f"{int(cid[0])}_{int(cid[1])}_{int(cid[2])}"


def parse_chunk_key(key: str) -> Coord:
    x, y, z = key.split("_")
    return int(x), int(y), int(z)


def chunk_keys_sorted(cids: Iterable[Sequence[int]]) -> List[str]:
    """canonical(Morton) 순으로 정렬된 청크 키 목록."""
    arr = np.asarray([tuple(int(v) for v in c) for c in cids], dtype=np.int64)
    if arr.size == 0:
        return []
    uniq = np.unique(arr, axis=0)
    return [chunk_key(c) for c in canonical_sort(uniq)]


# ══════════════════════════════════════════════════════════ 편집 마스크
#
# FINAL 명세는 bbox 만 다뤘지만, Unity 라쏘 선택(Direction A)은 복셀 부분집합을
# 보낸다. 계약은 두 모드를 모두 받는다.
#   - "bbox"   : NORMALIZED AABB 두 점
#   - "voxels" : VOXEL 셀 인덱스의 명시적 목록 (라쏘 결과)
# 어느 쪽이든 아래에서 동일한 (N,3) VOXEL 셀 집합으로 정규화된다.


def bbox_to_voxel_cells(bbox_min: Sequence[float], bbox_max: Sequence[float]) -> np.ndarray:
    lo = np.floor((np.asarray(bbox_min, np.float64) - NORMALIZED_MIN) / NORMALIZED_SPAN * VOXEL_RES)
    hi = np.ceil((np.asarray(bbox_max, np.float64) - NORMALIZED_MIN) / NORMALIZED_SPAN * VOXEL_RES)
    lo = np.clip(lo, 0, VOXEL_RES).astype(np.int64)
    hi = np.clip(hi, 0, VOXEL_RES).astype(np.int64)
    return dense_cells(lo, hi)


def dense_cells(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """[lo, hi) 범위의 모든 셀을 canonical order 로 나열."""
    lo = np.asarray(lo, np.int64)
    hi = np.asarray(hi, np.int64)
    if np.any(hi <= lo):
        return np.empty((0, 3), dtype=np.int64)
    axes = [np.arange(lo[i], hi[i], dtype=np.int64) for i in range(3)]
    grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    return canonical_sort(grid)


def dilate_cells(cells: np.ndarray, halo: int) -> np.ndarray:
    """FINAL §8.2 halo margin.

    마스크 경계 바로 바깥 복셀은 디코더의 넓은 receptive field 때문에 latent 가
    같아도 출력이 미세하게 달라질 수 있다. 26-이웃으로 halo 만큼 팽창시켜
    재생성 대상에 포함한다. halo=0 이면 그대로 반환.
    """
    cells = np.asarray(cells, np.int64).reshape(-1, 3)
    if halo <= 0 or cells.size == 0:
        return canonical_sort(cells)
    rng = np.arange(-halo, halo + 1, dtype=np.int64)
    offsets = np.stack(np.meshgrid(rng, rng, rng, indexing="ij"), axis=-1).reshape(-1, 3)
    expanded = (cells[:, None, :] + offsets[None, :, :]).reshape(-1, 3)
    expanded = expanded[np.all((expanded >= 0) & (expanded < VOXEL_RES), axis=1)]
    return canonical_sort(np.unique(expanded, axis=0))


def affected_chunks(
    added: np.ndarray | None,
    removed: np.ndarray | None,
    modified: np.ndarray | None,
) -> List[str]:
    """FINAL §4.2. added/removed/modified VOXEL 셀 -> 영향받은 청크 키 목록.

    이건 diff 계산이 아니다. 세 집합은 RePaint 부기(bookkeeping)에서 그대로
    넘어온 것이고(§0 핵심 설계 원칙), 여기서는 청크로 접기만 한다.
    """
    parts = [np.asarray(a, np.int64).reshape(-1, 3) for a in (added, removed, modified) if a is not None and np.asarray(a).size > 0]
    if not parts:
        return []
    return chunk_keys_sorted(voxel_to_chunk(np.concatenate(parts, axis=0)))


def cell_set(cells: np.ndarray) -> Set[Coord]:
    return {(int(a), int(b), int(c)) for a, b, c in np.asarray(cells).reshape(-1, 3)}


def assert_in_normalized_bounds(pts: np.ndarray, label: str = "vertices") -> None:
    """NORMALIZED 공간 범위 검증. NORMALIZED_TOLERANCE 만큼의 이탈은 허용한다.

    `[-0.5, 0.5]` 를 엄밀히 요구하면 실측상 0.26% 의 정점에서 터진다 — 그건 버그가
    아니라 FlexiCubes 의 경계 정점 변형이 원래 그렇게 동작하는 것이다.
    여기서 잡고 싶은 건 그게 아니라 **좌표계를 통째로 잘못 넘긴 경우**(예: 복셀
    인덱스를 그대로 넘김, 미터 단위 월드 좌표를 넘김)다. 그건 이 허용치를 몇 자릿수
    단위로 넘어간다.
    """
    a = np.asarray(pts, dtype=np.float64)
    if a.size == 0:
        return
    lo = NORMALIZED_MIN - NORMALIZED_TOLERANCE
    hi = NORMALIZED_MAX + NORMALIZED_TOLERANCE
    if a.min() < lo or a.max() > hi:
        raise ValueError(
            f"{label} 가 NORMALIZED 범위를 벗어난다: "
            f"min={a.min():.6g}, max={a.max():.6g}, "
            f"허용=[{lo:.6g}, {hi:.6g}] (tolerance={NORMALIZED_TOLERANCE:.6g})"
        )


# ══════════════════════════════════════════════════════════ SparseTensor coords 정렬
#
# ⚠️ A5000 실측 보고(2026-07-29)에서 확인된 함정:
#
#   `SparseTensor.__elemwise__` 는 두 텐서를 더하거나 곱할 때 **coords 를 전혀 보지
#   않고 feats 만 elementwise** 로 연산한다. 순서가 다르면 예외 없이 조용히 틀린
#   결과가 나온다. `DEBUG=False` 가 기본이라 내장 검증도 돌지 않는다.
#
# RePaint 는 "원본 latent 를 마스크 밖에 강제 대입"하는 연산이므로 이 함정을 정면으로
# 밟는다. 그래서 좌표 매칭 primitive 를 계약에 둔다 — 양쪽이 각자 구현하면 한쪽이
# 틀렸을 때 알아낼 방법이 없다.
#
# VoxHammer 참고 구현은 (N,1,3) vs (1,M,3) 브로드캐스트 비교라 N~2만에서 메모리가
# 터진다. 아래는 정수 코드 + searchsorted 로 O(N log N).


def strip_batch(coords: np.ndarray) -> np.ndarray:
    """TRELLIS SparseTensor coords (N,4)[b,x,y,z] -> (N,3)[x,y,z].

    (N,3) 이 들어오면 그대로 돌려준다. 배치 열을 실수로 좌표로 취급하는 사고가
    이 파이프라인에서 가장 조용히 번지는 버그라 진입점을 하나로 모은다.
    """
    a = np.asarray(coords)
    if a.ndim != 2 or a.shape[1] not in (3, 4):
        raise ValueError(f"(N,3) 또는 (N,4) 가 필요하다. got {a.shape}")
    return a[:, 1:] if a.shape[1] == 4 else a


def add_batch(coords: np.ndarray, batch: int = 0) -> np.ndarray:
    """(N,3) -> (N,4)[b,x,y,z]. TRELLIS SparseTensor 생성용."""
    a = strip_batch(coords).astype(np.int64)
    return np.concatenate([np.full((a.shape[0], 1), batch, dtype=np.int64), a], axis=1)


def voxel_code(coords: np.ndarray) -> np.ndarray:
    """복셀 좌표 -> 유일한 int64 코드. (N,4) 면 배치까지 포함해 인코딩한다.

    Morton 이 아니라 raster 코드를 쓰는 이유: 여기서는 정렬 순서가 아니라
    **동일성 판정**이 목적이고, raster 쪽이 역변환이 자명해서 디버깅이 쉽다.
    canonical 정렬이 필요하면 canonical_sort() 를 따로 쓴다.
    """
    a = np.asarray(coords, dtype=np.int64)
    if a.ndim != 2 or a.shape[1] not in (3, 4):
        raise ValueError(f"(N,3) 또는 (N,4) 가 필요하다. got {a.shape}")
    xyz = a[:, 1:] if a.shape[1] == 4 else a
    if xyz.min() < 0 or xyz.max() >= VOXEL_RES:
        raise ValueError(f"좌표가 [0,{VOXEL_RES}) 범위를 벗어난다: min={xyz.min()}, max={xyz.max()}")
    code = (xyz[:, 0] * VOXEL_RES + xyz[:, 1]) * VOXEL_RES + xyz[:, 2]
    if a.shape[1] == 4:
        code = a[:, 0] * (VOXEL_RES**3) + code
    return code


def align_indices(src: np.ndarray, dst: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """두 coords 배열의 **공통 복셀**에 대한 인덱스 쌍을 돌려준다.

    Returns:
        (src_idx, dst_idx) — `src[src_idx]` 와 `dst[dst_idx]` 가 같은 복셀을 가리킨다.
        순서는 복셀 코드 오름차순으로 결정적이다.

    사용 예 (RePaint 마스크 밖 강제 대입):

        s_idx, r_idx = align_indices(sample_coords, ref_coords)
        feats = sample.feats.clone()
        feats[torch.as_tensor(s_idx)] = ref_feats[torch.as_tensor(r_idx)]
        sample = sample.replace(feats)

    torch 텐서는 `.cpu().numpy()` 로 넘기고, 반환된 인덱스는 다시 텐서에 그대로 쓰면 된다.
    """
    sc = voxel_code(src)
    dc = voxel_code(dst)
    if len(np.unique(sc)) != len(sc):
        raise ValueError("src coords 에 중복 복셀이 있다")
    if len(np.unique(dc)) != len(dc):
        raise ValueError("dst coords 에 중복 복셀이 있다")

    d_order = np.argsort(dc, kind="stable")
    d_sorted = dc[d_order]
    pos = np.searchsorted(d_sorted, sc)
    pos_clipped = np.clip(pos, 0, len(d_sorted) - 1) if len(d_sorted) else pos
    hit = (len(d_sorted) > 0) & (pos < len(d_sorted)) & (d_sorted[pos_clipped] == sc)

    src_idx = np.flatnonzero(hit)
    dst_idx = d_order[pos[src_idx]]
    order = np.argsort(sc[src_idx], kind="stable")
    return src_idx[order], dst_idx[order]


def assert_same_voxel_set(a: np.ndarray, b: np.ndarray, label: str = "") -> None:
    """두 coords 가 같은 복셀 집합인지 확인. RePaint 주입 직전에 부른다.

    같은 집합이지만 **순서가 다를 수 있다** — 그건 정상이고, 그래서 align_indices()
    가 필요하다. 여기서 잡는 건 "집합 자체가 다른" 경우다.
    """
    ac, bc = set(voxel_code(a).tolist()), set(voxel_code(b).tolist())
    if ac != bc:
        raise ValueError(
            f"{label or 'coords'} 복셀 집합 불일치: "
            f"src에만 {len(ac - bc)}개, dst에만 {len(bc - ac)}개"
        )
