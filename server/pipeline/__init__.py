"""S2 관통 파이프라인 — **순수 로직**. 네트워크도 GPU도 쓰지 않는다.

`docs/PROGRESS.md` §5 S2 의 1~5·7 을 담는다. HTTP·DebugView(6)·실자산은 3090 담당이다.
이 패키지가 GPU·네트워크를 쓰지 않는 것은 편의가 아니라 **일정 제약**이다 —
3090 이 눈사람/호박 자산을 확보하는 동안 맥북이 병렬로 완성해야 한다 (rev5 W2).

  voxelize   GLB/glTF → 64³ occupancy · occupancy → 합성 메시
  mask       bbox 마스크 산출 + halo 팽창 (🔴 클램프 **뒤에** 팽창)
  splice     contract 의 assemble 을 감싼다. 스케일 없음. 정수 평행이동만
  delta      부기를 **배치에서** 유도 (diff 금지)
  package    .cbin 세트 + manifest. 마스크 밖은 **부모 바이트 승계**

기하·인코딩·부기 규칙의 정본은 전부 `contract/python/deltacontract/` 다.
이 패키지는 그것을 **호출**할 뿐 재구현하지 않는다 (CLAUDE.md 계약 변경 규칙 3).
"""

from .voxelize import (  # noqa: F401
    load_mesh,
    normalize_to_normalized,
    occupancy_to_mesh,
    surface_voxelize,
    voxelize_asset,
)
from .mask import (  # noqa: F401
    HALO_DEFAULT,
    MaskResult,
    build_mask,
    clamp_cells,
    top_region_cells,
)
from .splice import SpliceResult, splice  # noqa: F401
from .delta import (  # noqa: F401
    Bookkeeping,
    derive_bookkeeping,
    diff_would_have_missed,
    verify_bookkeeping,
)
from .package import DeltaPackage, encode_chunks, package_delta  # noqa: F401

__all__ = [
    "load_mesh",
    "normalize_to_normalized",
    "occupancy_to_mesh",
    "surface_voxelize",
    "voxelize_asset",
    "HALO_DEFAULT",
    "MaskResult",
    "build_mask",
    "clamp_cells",
    "top_region_cells",
    "SpliceResult",
    "splice",
    "Bookkeeping",
    "derive_bookkeeping",
    "diff_would_have_missed",
    "verify_bookkeeping",
    "DeltaPackage",
    "encode_chunks",
    "package_delta",
]
