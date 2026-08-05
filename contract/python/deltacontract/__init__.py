"""deltacontract — 3D 메시의 공간 청크 델타 전송 계약.

한 오브젝트를 64³ 복셀 격자 → 8³ 청크(512슬롯)로 자르고, 편집이 건드린 청크만
전송·교체하기 위한 순수 함수 모음이다. **네트워크·GPU·모델 의존이 없다.**

  coords     좌표계 변환 · Morton 정렬 · 마스크 팽창 · 마스크 지문
  chunkbin   .cbin 인코딩/디코딩 (40바이트 헤더 · magic CBN1)
  partition  메시를 청크로 분할
  assemble   다른 자산의 일부를 마스크 자리에 끼워넣기 (크롭·정수배치·부기)
  schemas    와이어 스키마 (pydantic, 선택 의존)
  errors     오류 코드 13종

배경과 실측 근거는 README.md · FINDINGS.md.
"""

from .coords import (  # noqa: F401
    DEFAULT_HALO_VOXELS,
    CHUNK_GRID_RES,
    CHUNK_SIZE,
    CONTRACT_CONSTANTS,
    CONTRACT_VERSION,
    MESH_CELLS_PER_VOXEL,
    MESH_RES,
    NORMALIZED_MAX,
    NORMALIZED_MIN,
    SLAT_CHANNELS,
    VOXEL_RES,
    COORD_ORDER,
    NORMALIZED_TOLERANCE,
    REQUIRES_DETERMINISTIC_ALGORITHMS,
    ContractMismatch,
    add_batch,
    affected_chunks,
    align_indices,
    assert_same_voxel_set,
    strip_batch,
    voxel_code,
    assert_contract_compatible,
    assert_in_normalized_bounds,
    bbox_to_voxel_cells,
    canonical_order,
    canonical_sort,
    chunk_bounds_normalized,
    chunk_key,
    dilate_cells,
    mask_fingerprint,
    normalized_to_chunk,
    normalized_to_voxel,
    voxel_to_chunk,
    parse_chunk_key,
)
from .uris import (  # noqa: F401
    API_PREFIX,
    JOB_ID_MAX_LEN,
    TRELLIS_PREFIX,
    UriRuleViolation,
    chunk_uri,
    is_staging_uri,
    parse_chunk_uri,
    parse_slat_coords_uri,
    parse_staging_chunk_uri,
    slat_coords_uri,
    staging_chunk_uri,
    trellis_chunk_uri,
    validate_job_id,
)
from .errors import (  # noqa: F401
    ERROR_CODE_TO_EXCEPTION,
    AssetNotFound,
    DeltaContractError,
    BookkeepingMismatch,
    DeterminismViolation,
    InternalError,
    InvalidRequest,
    MaskEmpty,
    NotFound,
    Unauthorized,
    UpstreamOOM,
    UpstreamTimeout,
    VersionConflict,
    raise_from_error_body,
)
from .chunkbin import (  # noqa: F401
    HEADER_SIZE,
    ChunkBinError,
    ChunkMesh,
    blob_hash,
    buffer_views,
    canonicalize,
    decode,
    encode,
)
from .partition import (  # noqa: F401
    albedo_to_rgba8,
    normalize_normals,
    diff_chunk_sets,
    partition_mesh,
    split_trellis_vertex_attrs,
)
# schemas 는 pydantic 에 의존한다. coords/chunkbin/partition 은 numpy 만 있으면 되므로,
# pydantic 이 없는 환경(예: 순수 계산만 하는 A5000 서브프로세스)에서도 기하 계약은
# 그대로 쓸 수 있게 선택적 import 로 둔다. HAS_SCHEMAS 로 확인 가능.
try:
    from .schemas import (  # noqa: F401
        BChunkResponse,
        BEditRequest,
        BGenerateRequest,
        BHealth,
        ChunkEntry,
        ChunkManifest,
        ChunkMeshPayload,
        ContractInfo,
        EditMask,
        EditRequest,
        ErrorBody,
        GenerateRequest,
        JobStatus,
        PatchPackage,
        SpatialContext,
    )

    HAS_SCHEMAS = True
except ImportError as _e:  # pragma: no cover
    HAS_SCHEMAS = False
    _SCHEMAS_IMPORT_ERROR = _e

__version__ = "3.25.0"
