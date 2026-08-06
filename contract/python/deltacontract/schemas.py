"""
JSON 스키마 정본. 3090 과 A5000 이 **같은 파일을 import** 한다.

스키마를 두 벌 유지하면 필드 이름이 갈리고, 그 순간 델타 계약이 조용히 깨진다.
Unity 쪽 대응은 unity/ChunkContracts.cs 에 있고 필드 이름이 1:1 로 일치해야 한다.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .coords import CONTRACT_CONSTANTS, CONTRACT_VERSION, DEFAULT_HALO_VOXELS

# URL 규칙은 uris.py 가 정본이다 (pydantic 없이도 쓸 수 있어야 하므로 분리).
from .uris import API_PREFIX, TRELLIS_PREFIX, chunk_uri, parse_chunk_uri  # noqa: F401

Vec3 = Tuple[float, float, float]


class ContractInfo(BaseModel):
    """모든 응답에 실려 나가는 계약 상수. 받는 쪽이 assert_contract_compatible() 로 검증."""

    contract_version: int = CONTRACT_VERSION
    normalized_min: float = CONTRACT_CONSTANTS["normalized_min"]
    normalized_max: float = CONTRACT_CONSTANTS["normalized_max"]
    voxel_res: int = CONTRACT_CONSTANTS["voxel_res"]
    chunk_size: int = CONTRACT_CONSTANTS["chunk_size"]
    chunk_grid_res: int = CONTRACT_CONSTANTS["chunk_grid_res"]
    position_quant_bits: int = CONTRACT_CONSTANTS["position_quant_bits"]
    slat_channels: int = CONTRACT_CONSTANTS["slat_channels"]
    mesh_res: int = CONTRACT_CONSTANTS["mesh_res"]
    coord_order: str = CONTRACT_CONSTANTS["coord_order"]
    requires_deterministic_algorithms: bool = CONTRACT_CONSTANTS[
        "requires_deterministic_algorithms"
    ]


# ════════════════════════════════════════════════════ 청크 / 매니페스트


class ChunkEntry(BaseModel):
    """매니페스트의 청크 1개.

    `uri` 는 반드시 `chunk_uri()` 로 만든다 (규칙은 그 docstring 참고).
    """

    uri: str  # 예: "/v2/assets/9f2.../chunks/3_1_5.v7.cbin"
    hash: str  # .cbin 전체 바이트의 sha256 (hex)
    byte_length: int
    vertex_count: int
    index_count: int
    voxel_count: int
    # 이 청크 바이트가 마지막으로 바뀐 버전. Unity 캐시 키로도 쓰인다.
    version: int


class ChunkManifest(BaseModel):
    asset_id: str
    version: int
    contract: ContractInfo = Field(default_factory=ContractInfo)
    chunks: Dict[str, ChunkEntry] = Field(default_factory=dict)

    #: 3.10.0 — 서버가 이 자산을 **샘플로 취급하는가** (실효 설정 기준).
    #:
    #: Unity 세션 지적(2026-07-30): 이전 판은 "서버가 정상 기동했으면 샘플 목록이
    #: 검증됐다" 고 했는데 그 추론이 무효다. `BLOCKEDIT_ALLOW_SAMPLE_DRIFT=1` 로
    #: 드리프트된 목록으로도 기동하고, 계약 문서의 예시 목록 자체가 틀린 상태였다.
    #:
    #: 그래서 클라이언트가 **편집할 자산 하나에 대해** 직접 확인한다. 목록 전체를
    #: 노출하는 엔드포인트는 여전히 만들지 않는다.
    #:
    #: `True` = 이 자산의 편집은 `ephemeral` 로 처리된다는 서버의 약속.
    #: **`False` 나 부재면 샘플 편집을 보내지 마라** — 정상 커밋되어 v1 을 영구히 벗어난다.
    is_sample: bool = False

    def hashes(self) -> Dict[str, str]:
        return {k: v.hash for k, v in self.chunks.items()}


class PatchPackage(BaseModel):
    """무엇이 바뀌었는지는 **부기(마스크+halo)가 정한다.** 해시를 비교하지 않는다.

    3.4.0 에서 확정. FINAL §0 의 원래 원칙으로 돌아온 것이다:

      "diff(사후 비교 계산)를 하지 않는다. 편집 마스크는 생성 시점에 이미 알고
       있으므로, '무엇이 바뀌었는가'를 파이프라인 전 구간에 그대로 전달한다."

    ── 왜 해시 비교를 버렸나 ──
    `SLatMeshDecoder` 는 수용영역이 전역이라(swin 12블록 ≈ 48복셀 > 오브젝트 26복셀),
    복셀의 2.4%만 편집해도 **모든 정점이 1e-6 단위로 움직인다.** 기하는 99.88%
    같은데 float32 바이트는 100% 달라진다. 그래서 해시로 판정하면 매번 전량 전송이다.

    ── 그래서 어떻게 하나 ──
    부기 밖 청크는 **새로 디코딩된 결과를 버리고 이전 버전 바이트를 그대로 유지**한다.
    `changed_chunks` 에는 부기가 지목한 청크만 들어간다. 나머지 청크의
    `ChunkEntry.version` 은 예전 값 그대로이므로 클라이언트가 재다운로드하지 않는다.

    ── 대가 ──
    옛 바이트(보존 영역)와 새 바이트(편집 영역)가 만나는 면에 이음새가 남는다.
    두 조각이 서로 다른 디코딩에서 나왔기 때문이다.

    A5000 실측(2026-07-29, top 25% 마스크, 대조군으로 지표 baseline 을 뺀 순수 신호):

        median 1.33e-3 (셀 0.33배) · 1셀 초과 정점 13.2% (baseline 9.5%)

    **육안으로는 안 보인다.** 오브젝트가 화면 높이의 25%/50%/100% 가 되는 세 거리
    전부에서 이음새가 식별되지 않았다 (스냅 없이도).

    ── 지표 주의 ──
    "이음새 max"는 읽지 마라. 대조군(편집 없이 재디코딩 → 합성물이 부모와 **바이트
    완전 동일**)에서도 max 가 3.98셀로 나온다. 상대 청크에 대응 정점이 없는 자리를
    세면서 생기는 잔여값이고, 실제 불연속이 아니다. 의미 있는 지표는 **median 과
    1셀 초과 비율**이며 baseline(median 0.00 / 9.5%)을 빼고 읽어야 한다.

    ── halo 는 이음새를 못 줄인다 ──
    3.4.0 에서 "halo 가 전송량 ↔ 이음새 품질의 손잡이"라고 적었는데 **틀렸다.**
    halo 0~3 에서 이음새가 반응하지 않는다 — 드리프트가 편집 지점에서 멀수록 줄어드는
    게 아니라 전역에 균일하게 깔리기 때문이다. halo 를 키우면 이음새 **위치만 바깥으로
    밀릴 뿐 크기는 그대로**다. 전송량도 실측상 halo 0~3 에서 동일했다(24 청크).

    그래서 `halo_margin_voxels` 의 의미는 **RePaint 경계 조화** 하나로 돌아간다.

    ── 결정성 플래그도 이음새를 못 줄인다 ──
    base·edit 을 모두 결정성 ON 으로 만든 조건이 OFF 조건과 3자리까지 같았다
    (median 0.34 vs 0.33 셀, 1셀 초과 13.19% vs 13.24%). 경계 정점은 비결정성에
    흔들리지 않는다 — OFF 재디코딩에서 청크 바이트가 0/23 동일인데도 정점 수는
    23/23 동일하고 경계 median 은 0.00 이었다. 흔들림이 표면 내부에만 머문다.

    ── 이음새를 없애고 싶으면: 경계 정점 스냅 (선택) ──
    부기 청크의 경계 정점을 부모 청크의 대응 정점으로 스냅한다 (임계 2셀).
    실측: 후보 965개 중 941개(97.5%)를 평균 0.39셀 이동 → median 0.33 → **0.00 셀**,
    1셀 초과 13.19% → 10.2% (baseline 9.63% 에 근접). 전송량은 불변.

    부모 청크는 이미 클라이언트가 갖고 있어 못 고치므로 **새 청크 쪽만** 건드린다.
    비용이 사실상 0 이라 켜두는 게 손해는 아니지만, 육안으로 차이가 없으므로 필수는 아니다.
    """

    asset_id: str
    from_version: int
    to_version: int
    contract: ContractInfo = Field(default_factory=ContractInfo)
    changed_chunks: Dict[str, ChunkEntry] = Field(default_factory=dict)
    removed_chunk_ids: List[str] = Field(default_factory=list)

    #: 3.9.0 — 이 패치는 **커밋되지 않았다.** 샘플 자산(개발/테스트 전용) 편집이다.
    #:
    #: 샘플은 버전이 영구히 1 에 머문다. 편집 결과는 계산해서 돌려주지만 2단계
    #: 커밋의 promote 를 하지 않으므로 staging 이 TTL 로 사라진다. 그래서 같은
    #: 샘플을 몇 번이든 같은 base_version=1 로 편집할 수 있다 — 리셋 엔드포인트도,
    #: VersionConflict 도 없다.
    #:
    #: `ephemeral=True` 일 때만 `to_version == from_version` 에 내용이 있을 수 있다.
    #: 클라이언트는 이걸 **메모리에만** 적용하고 디스크 캐시에 쓰지 않는다.
    ephemeral: bool = False

    # ── 마스크 반향 (3.21.0) ─────────────────────────────────────────────
    # 🔴 왜 넣나: 클라이언트가 "내가 보낸 마스크 == 서버가 쓴 마스크" 를 확인할
    #    경로가 **없었다**. 3.16.0 이 "PatchPackage 에 지문은 없는 게 맞다" 로
    #    정리했는데, 그 결과 Unity 가 voxels 탐침 4번(mask_rows ≠ 0)을 **원리적으로**
    #    답할 수 없었다. 간접 근거(청크 수가 작다)는 "마스크가 먹었다" 이지
    #    "서버가 내 마스크를 썼다" 가 아니다 — Unity 가 그 둘을 갈라 보고했다.
    #
    # ⚠️ 이건 A5000 내부 노출이 아니다. **클라이언트 자신이 보낸 것의 반향**이라
    #    도메인 분리(§1)를 안 깬다. 클라가 로컬로 같은 값을 계산해 대조하면 끝난다.
    #    A5000 의 job_id·mask_rows 원값은 여전히 안 싣는다.
    mask_fingerprint: Optional[str] = None      # coords.mask_fingerprint(halo 적용 전)
    mask_voxels_used: Optional[int] = None      # halo 적용 **후** 서버가 실제로 쓴 셀 수

    # 3.24.0 — 이 패치를 만든 연산.
    # 🔴 **봉쇄 판정이 연산마다 다르다** (§53). 편집은 마스크가 "바뀔 자리" 라 마스크 밖
    #    변경이 위반이지만, 조립은 마스크가 **비울 자리만** 정의하고 기증자 위치는 offset 이
    #    정하므로 마스크 밖 변경이 **정상**이다.
    #    이 필드가 없던 동안 클라이언트는 호출자 플래그에 의존해야 했고, 3090 의 debugview 는
    #    실제로 정상 조립을 빨간 경보로 찍었다. **응답만 보고 판별되어야 한다.**
    # 부재하면 "edit" 로 읽는다 (구 서버 호환).
    op: Literal["edit", "assemble"] = "edit"

    @model_validator(mode="after")
    def _check_versions(self) -> "PatchPackage":
        # to_version == from_version 은 **허용한다** — "따라올 것 없음"의 표현이다.
        #
        # 3090 자기보고(2026-07-29)의 지적:
        #   "이미 최신인 클라이언트가 GET /patch?from_version=N 을 부르면 돌려줄 값이
        #    없습니다. VersionConflict 로 처리했지만 의미상 맞지 않습니다 — 충돌이
        #    아니라 '따라올 것 없음'입니다."
        #
        # 맞다. 409 로 답하면 클라이언트가 정상 상태를 에러로 배우고, 그러면
        # 진짜 충돌과 구분이 안 된다. 304 도 쓰지 않는다 — ETag 협상이 필요해지고
        # UnityWebRequest 의 304 처리가 번거롭다.
        # 200 + 빈 패치가 가장 단순하고, `is_empty` 로 분기하면 된다.
        if self.to_version < self.from_version:
            raise ValueError(
                f"to_version({self.to_version}) 이 from_version({self.from_version}) 보다 작다. "
                "패치는 뒤로 갈 수 없다."
            )
        if (
            self.to_version == self.from_version
            and not self.ephemeral
            and (self.changed_chunks or self.removed_chunk_ids)
        ):
            raise ValueError(
                "to_version == from_version 인 패치는 비어 있어야 한다 "
                "(ephemeral=True 인 샘플 편집은 예외)."
            )
        return self

    @property
    def is_empty(self) -> bool:
        """따라올 것이 없다. 클라이언트는 아무것도 안 하면 된다."""
        return not self.changed_chunks and not self.removed_chunk_ids

    @classmethod
    def up_to_date(cls, asset_id: str, version: int) -> "PatchPackage":
        """이미 최신인 클라이언트에게 돌려줄 빈 패치."""
        return cls(asset_id=asset_id, from_version=version, to_version=version)


# ════════════════════════════════════════════════════ 편집 마스크
#
# FINAL 명세는 bbox 만 다뤘다. 라쏘 선택을 위해 voxels 모드를 같이 정의해 둔다.
# 🔴 (3.19.2 정정) 이전 주석이 "Unity 가 복셀 부분집합을 이미 보내고 있다" 였는데
#    **거짓이다** — 그건 별도 프로젝트(ai-ar-v2) 얘기를 옮겨 적은 것이고, 이 클라이언트는
#    voxels 모드를 **한 번도 와이어에 올린 적이 없다.** DTO 는 양쪽에 있으나 경로는 미검증이다.
#    ⚠️ "스키마에 있다" 와 "그 경로가 돈 적 있다" 는 다른 사실이다.


class SlatCoordsResponse(BaseModel):
    """`GET /v2/assets/{id}/slat_coords.v{n}.json` 응답 (3.26.0 · W17).

    🔴 **라쏘가 투영할 대상**이다. 정점이 아니라 **복셀**을 투영해야 결과가 곧바로
    SLat 마스크가 된다 (D58). 정점을 투영하면 메시 정점 집합이 나오고, 그걸 복셀로
    되돌리는 역산은 `.cbin` 에 slat coords 가 없어서(D34) **두 번 실패했다.**

    `fingerprint` 는 `coords.mask_fingerprint(coords)` 다. 클라이언트가 받은 좌표가
    서버가 보낸 것과 같은지 **한 값으로** 확인한다 — 각자 직렬화하면 어긋나도
    예외가 안 나고 "결과가 이상하다" 로만 보인다 (3.14.0 에서 실제로 갈렸다).
    """

    asset_id: str
    version: int
    #: 🔴 항상 "slat_coords" 다. 다른 값이면 **라쏘 결과를 편집에 쓰면 안 된다** (D28-a).
    grid_source: Literal["slat_coords"] = "slat_coords"
    voxel_res: int = 64
    n_cells: int
    coords: List[Tuple[int, int, int]]
    fingerprint: str

    @model_validator(mode="after")
    def _check(self) -> "SlatCoordsResponse":
        if self.n_cells != len(self.coords):
            raise ValueError(
                f"n_cells({self.n_cells}) 와 coords 길이({len(self.coords)}) 가 다르다. "
                "둘이 갈리면 클라이언트가 잘린 목록으로 마스크를 만들고, 그 마스크는 "
                "형태가 멀쩡해서 예외를 안 낸다."
            )
        for c in self.coords:
            if not all(0 <= v < self.voxel_res for v in c):
                raise ValueError(f"좌표가 격자 [0,{self.voxel_res}) 를 벗어난다: {c}")
        return self


class EditMask(BaseModel):
    mode: Literal["bbox", "voxels"]
    # mode == "bbox": NORMALIZED [-0.5,0.5] 공간의 AABB
    bbox_min: Optional[Vec3] = None
    bbox_max: Optional[Vec3] = None
    # mode == "voxels": VOXEL [0,64) SLat 공간의 셀 인덱스 목록 (라쏘 결과)
    voxels: Optional[List[Tuple[int, int, int]]] = None
    # §8.2. 마스크 경계 바깥 halo. 0 이면 확장 없음.
    # 🔴 D75 에서 1 → 2. 청크가 4복셀로 잘아지면서 1복셀 부풀림이 경계 넘김을
    #    못 덮게 됐다 (conformance 실측: halo=1 이 114청크 중 6개를 놓친다).
    halo_margin_voxels: int = DEFAULT_HALO_VOXELS

    # 🔴 3.26.0 (D28-a) — **이 셀들이 어느 격자에서 나왔는가.**
    #
    # 두 격자가 있다:  `slat_coords`(정본) 와 `surface_voxelize`(진단용).
    # 둘은 같은 자산에서도 **다른 셀 집합**이고, 섞으면 마스크도 조립도 지표도
    # 정상 동작하면서 전부 **다른 물체에 대한 숫자**가 된다. 예외가 안 난다.
    #
    # 그래서 `mode="voxels"` 에서는 **생략을 허용하지 않는다.** 기본값을
    # `slat_coords` 로 두면 잘못된 격자가 침묵으로 정본을 참칭한다 —
    # 이 리포가 D28 에서 실제로 물린 자리다.
    grid_source: Optional[Literal["slat_coords", "surface_voxelize"]] = None

    @model_validator(mode="after")
    def _check(self) -> "EditMask":
        if self.mode == "bbox":
            if self.bbox_min is None or self.bbox_max is None:
                raise ValueError("mode='bbox' 에는 bbox_min/bbox_max 가 필요하다.")
            if any(a >= b for a, b in zip(self.bbox_min, self.bbox_max)):
                raise ValueError("bbox_min 의 모든 성분이 bbox_max 보다 작아야 한다.")
        else:
            if not self.voxels:
                raise ValueError("mode='voxels' 에는 비어있지 않은 voxels 가 필요하다.")
            if self.grid_source is None:
                raise ValueError(
                    "mode='voxels' 에는 grid_source 가 필요하다 (D28-a). "
                    "라쏘 산출물이면 'slat_coords' 다. 생략을 기본값으로 메우면 "
                    "다른 격자가 침묵으로 정본을 참칭한다 — 그때는 예외가 안 나고 "
                    "마스크·조립·지표가 전부 다른 물체에 대해 정상 동작한다."
                )
            for v in self.voxels:
                if not all(0 <= c < 64 for c in v):
                    raise ValueError(f"복셀 셀이 격자 [0,64) 를 벗어난다: {v}")
        return self

    @property
    def is_canonical_grid(self) -> bool:
        """정본 격자인가. 아니면 판정 경로가 거부해야 한다 (D28-a)."""
        return self.grid_source == "slat_coords"


# ════════════════════════════════════════════════════ Unity -> 3090


class SpatialContext(BaseModel):
    """자산을 어디에 얼마만 하게 놓을 것인가.

    🔴 **`estimated_footprint_m` 는 클라이언트가 실제로 렌더링하는 값과 같아야 한다**
    (3.15.3). 기본값에 기대지 말고 **렌더 스케일을 그대로 실어라.**

    계약 작성자가 3.15.1 에서 "렌더 스케일 = 이 필드의 기본값(1.0)" 이라고 적었는데,
    근거가 **이 필드의 기본값**이었다. 순환이다 — 서버에 보내는 값을 렌더 기준으로
    삼고, 그 렌더 기준의 근거를 다시 이 값으로 댔다.
    Unity 실측(2026-07-31)에서 1.0 m 는 책상 위에서 나쁘게 보인다.

    두 값이 갈리면 서버는 1 m 짜리를 전제로 만들고 클라이언트는 0.5 m 로 그린다.
    자산이 NORMALIZED `[-0.5,0.5]³` 라 **예외가 안 나고**, 디테일 밀도만 조용히 어긋난다.

    ⚠️ 값은 `surface_type` 마다 다르다. 이 필드가 처음부터 그 축을 갖고 있었다.

    🔴🔴 **이 값을 "보기 좋은 크기" 로 튜닝하려던 것이 잘못된 매개변수화였다**
    (Unity 실측, 2026-07-31). 두 표면의 스윕이 그걸 드러냈다:

        table  ~0.3 m 거리   성립 0.24 ~ 0.5 m    1.0 m 는 깨진다
        floor  ~1.9 m 거리   성립 0.5  ~ 1.0 m    0.24 m 는 너무 작다

    같은 물체가 표면에 따라 반대 방향으로 갈린다. 그런데 **거리가 6배 다르다.**

    Unity 가 자기 데이터로 이유를 찾았다 — 판정 기준 셋 중 **하나만 갈랐다:**

        G1 접지선이 보이는가        다섯 값 전부 통과   ← 안 가른다
        G2 화면 면적 50% 미만인가    다섯 값 전부 통과   ← 안 가른다 (1.0 m 도 21.8%)
        G3 실물과 크기 관계가 서는가  ★ 이것만 갈랐다

    G2 가 1.9 m 거리에서 전부 통과한 것이 결정적이다. **화면 점유율은 스케일이 아니라
    거리의 함수다.** 책상에서 1.0 m 가 깨진 것도 스케일이 아니라 30 cm 거리 탓일
    가능성이 크다.

    ⇒ **G3 는 "물리적으로 그럴듯한 크기인가" 다.** 눈사람은 실제로 약 1 m 이고,
    그건 책상 위든 바닥이든 같다. 책상 위 1 m 눈사람이 깨져 보인 것은 렌더링 문제가
    아니라 **그런 물건이 실재하지 않기 때문**이다.

    ```
    이 필드는 "화면에서 보기 좋은 크기" 가 아니라
    **"이 물체가 현실에서 몇 미터인가"** 다.
    ```

    보기 좋음은 사용자가 어디에 서는지의 함수이고, 앱이 통제할 수 없다.
    물리적 타당성은 통제할 수 있고 표면·거리와 무관하다.

    ⚠️ **가설이다.** 근거는 표면 둘 × 스케일 다섯의 소프트 판정이고, 거리와 스케일이
    얽혀 있어 분리되지 않았다. 확정하려면 **스케일을 고정하고 거리만 바꿔라** —
    같은 물체가 거리만으로 깨지고 성립하면 가설이 맞다.

    ⚠️ 지금 코드의 `0.5` 는 사용자가 **검증 편의로 임의 설정한 값**이고 임의 후보 넷 중
    가장 나아 보였을 뿐이다. 후보 집합이 임의면 결과는 "최적" 이 아니라
    **"그 중 최선"** 이다. 둘을 같게 적지 마라.

    A4 절대 검증(실측 0.45 vs 선언 0.50, ±10%)이 답한 것은 **"렌더가 선언한 값과
    일치한다"** 이지 "0.5 가 좋다" 가 아니다. 두 질문을 뭉치지 마라.

    숫자보다 **이유가 이전 가능하다:**

        AR 로 보이느냐는 시차와 접지에서 온다. 큰 물체를 가까이 두면 둘 다 죽는다 —
        배경이 남아야 카메라가 움직일 때 물체와 배경의 상대 이동이 보이고,
        그게 "저기 있다" 는 감각이다.

    기본값 `1.0` 은 **바꾸지 않는다.** 물리적 타당성 기준에서 눈사람 1 m 는 맞는 값이고,
    바닥 스윕에서도 성립 범위 안이다. `wall` 은 여전히 미측정.
    """

    surface_type: Literal["floor", "table", "wall"] = "floor"
    estimated_footprint_m: float = 1.0


class GenerateRequest(BaseModel):
    session_id: str
    raw_prompt: str
    spatial_context: SpatialContext = Field(default_factory=SpatialContext)
    seed: int = 42


class EditRequest(BaseModel):
    """편집 요청.

    🔴 **`spatial_context` 가 없다. 의도적이다** (3.15.4 에서 명문화).

    Unity 가 이걸 찾다가 없어서 보고했다 (2026-07-31). 계약 작성자가 "SpatialContext
    를 명시해서 보내라" 고 지시했는데 **편집 경로에는 실을 자리가 없다** — 확인 없이
    지시한 내 오류다.

    없는 것이 맞다:

        마스크는 VOXEL `[0,64)` / NORMALIZED `[-0.5,0.5]³` 공간이라 **스케일이 없다.**
        편집은 기존 자산의 latent 에서 출발하므로 그 자산의 크기가 이미 정해져 있다.
        여기에 footprint 를 실으면 **같은 자산의 크기를 정하는 곳이 두 군데**가 되고,
        어긋나면 예외 없이 조용히 갈린다.

    자산의 실물 크기는 `GenerateRequest.spatial_context` 에서 한 번만 정한다.
    클라이언트가 렌더 스케일을 바꾸고 싶으면 그건 클라이언트 로컬 변환이지 편집이 아니다.
    """

    session_id: str
    base_version: int
    raw_prompt: str
    mask: EditMask
    seed: int = 42
    # §10-1 부분 실패 대비. 같은 키로 재요청하면 이미 커밋된 결과를 그대로 돌려준다.
    #
    # 🔴 **키는 요청 내용에서 파생시켜라. 고정하지도, 매번 새로 만들지도 마라** (3.15.5).
    #
    #     권장:  sha256(asset_id | base_version | raw_prompt | mask 지문 | seed)[:16]
    #
    # 이 필드는 **연산을 식별한다** (3.7.1). 그래서 갈래가 셋이고 둘이 함정이다:
    #
    #   고정 키      다른 마스크를 보내도 서버가 **옛 연산을 재생**한다.
    #                클라이언트는 자기가 안 보낸 마스크의 결과를 받고, 예외는 안 난다.
    #                Unity 하네스가 실제로 이 상태였다 (코드 상수 bbox 시절의 키를
    #                손으로 그린 마스크에 그대로 썼다).
    #                부작용 하나 더 — 하네스를 재실행할 때마다 재생 경로만 타므로
    #                **정상 경로를 영영 안 밟는다.**
    #   매번 새 키    멱등성이 사라진다. 응답 유실 재시도가 GPU 재계산이 된다.
    #                (그리고 그 코드는 나중에 지워야 한다)
    #   ✅ 내용 파생   같은 편집 재시도 → 같은 키 → 재생 (의도한 동작)
    #                다른 편집        → 다른 키 → 새 계산 (의도한 동작)
    #
    # 마스크 지문은 `coords.mask_fingerprint()` 를 쓴다 — 직렬화를 손으로 정하면
    # 클라이언트와 서버가 조용히 갈린다 (3.14.0 에서 실제로 갈렸다).
    #
    # 🔴 3.27.0 (결정 5) — **Optional 에서 필수로 올린다.**
    #
    # 계약은 Optional 인데 구현은 없으면 못 돈다 (슬롯·재생색인·job 기록이 전부 이 키다).
    # 그 어긋남이 `manifest.v99 → 200` 과 같은 계열이다 — 문서가 허용한 입력을 구현이
    # 거부하거나 그 반대이면, **어느 쪽이 진실인지 매번 확인해야 한다.**
    #
    # 그리고 서버가 지어내면 안 된다: 이 필드는 **연산을 식별한다**(3.7.1).
    # 서버가 발급하면 같은 요청 두 번이 **다른 슬롯**을 가져가고, 그때 멱등성은
    # 예외 없이 사라진다 — 재시도가 GPU 재계산이 되고 아무도 모른다.
    #
    # ⚠️ 필수로 올리면서 **함수를 같이 준다**: `server.editreq.derive_idempotency_key()`.
    #    규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다 (이 리포에서 세 번 반복됐다).
    idempotency_key: str


JobState = Literal["queued", "running", "succeeded", "failed"]


class AssembleRequest(BaseModel):
    """Unity → 3090 **조립** 요청 (3.24.0).

    🔴 **왜 늦게 생겼나 — 내 공백이다.** §55 에서 "승격한다" 고 적어놓고 라우트도 스키마도
    만들지 않아서, 조립이 3090 까지 왔는데 **Unity 가 띄울 방법이 없었다.**
    "받아라" 는 수신만 가능하게 했지 시작을 가능하게 하지 않았다.

    ⚠️ `BAssembleRequest`(3090→A5000)와 필드명이 같아도 **같은 타입이 아니다** —
    `asset_id` 의 의미(클라 자산 ↔ 서버 자산)와 인증 경계가 다르다.
    `EditRequest`/`BEditRequest` 가 이미 그렇게 갈려 있다.

    ⚠️ 스케일 인자가 **없다.** 좌표 확대가 인접성을 파괴한다(6-이웃 유지율 s=2.0 에서 0%).
    크기는 `donor_crop_fraction` 으로만 고른다.
    """

    session_id: str
    base_version: int
    donor_asset_id: str
    donor_crop_fraction: float = Field(default=0.4, gt=0.0, le=1.0)
    donor_crop_axis: int = 2                    # 0=x 1=y 2=z
    donor_crop_keep: Literal["top", "bottom"] = "top"
    # VOXEL 격자의 **정수** 평행이동. None 이면 서버가 assemble.fit_offset 으로 정한다.
    offset: Optional[Tuple[int, int, int]] = None
    # 대상에서 **비울** 영역. 기증자 위치를 정하는 것이 아니다 (§53).
    mask: EditMask
    # 🔴 3.27.0 — EditRequest 와 같은 이유로 필수다 (결정 5). 조립도 연산이다.
    idempotency_key: str


class JobStatus(BaseModel):
    """as-built 의 /blocks/jobs/{id} 폴링 패턴을 유지한다 — Unity 쪽이 이미 이걸 쓴다."""

    job_id: str
    state: JobState
    asset_id: Optional[str] = None
    progress: float = 0.0
    stage: Optional[str] = None  # "load"|"prompt"|"t2i"|"structure"|"slat"|"repaint"|"decode"|"chunk"|"render"
    # 어휘 밖의 세부. 분기하지 말고 표시만 해라.
    #
    # 3090 지적(2026-07-29): 계약이 "폴백 사실을 stage_detail 에 남겨라"를 요구했는데
    # 정작 JobStatus 에 그 필드가 없었다 (LAB_API 의 RunSummary 에만 있었다).
    # 실제로 이 필드가 사고를 하나 막았다 — anthropic 패키지가 없어 Claude 변환이
    # 조용히 폴백하고 있었고, 폴백 로그가 없었으면 "변환은 차이가 없다"는 틀린
    # 결론이 나올 뻔했다.
    stage_detail: Optional[str] = None
    # state == "succeeded" 일 때 정확히 하나가 채워진다.
    manifest: Optional[ChunkManifest] = None
    patch: Optional[PatchPackage] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    # 3090이 자동 발급한 값을 클라이언트에게 돌려준다.
    #
    # 3090 지적(2026-07-29): 클라이언트가 잡을 잃고 재요청할 때 같은 키를 못 쓰면
    # 자동 발급의 효과가 "같은 프로세스 안에서만" 유지된다. 키를 응답에 실어야
    # 재시도 멱등성이 프로세스 경계를 넘는다.
    idempotency_key: Optional[str] = None


class ServerHealth(BaseModel):
    """3090 -> Unity 의 `/v2/health`.

    3090 지적(2026-07-29): `BHealth` 는 A5000 용인데 Unity 용은 JSON 예시 한 덩이뿐이라
    클라이언트가 뭘 믿어야 할지 계약이 안 정해줬다.

    `ok` 는 **이 서버가 응답 가능한가**만 뜻한다. 업스트림이 죽어도 200 + ok=true 로
    답하고 `upstream_ok=false` 로 알린다 — health 가 500 이면 모니터링이 본문을 못 읽는다.
    """

    # 🔴 **진단 표면만 `extra="allow"` 다** (3.19.2).
    #
    # 이 프로젝트는 "무슨 코드가 떠 있나"를 이 응답으로 판정한다. 그런데 pydantic
    # 기본값(`extra="ignore"`)이 **모르는 키를 조용히 버린다.** 서버가 새 필드를
    # 먼저 보내면 침묵이 돌아오고, 그 침묵이 "안 보냈다"로 읽힌다.
    # 실측(2026-08-03): A5000 이 `build_untracked` 를 실었는데 스키마가 몰라서
    # 통째로 드롭됐고 응답만 보면 서버가 안 보낸 것과 구분되지 않았다.
    #
    # ⚠️ **페이로드 스키마는 `ignore` 로 둔다.** `forbid` 로 바꾸면 서버가 필드를
    # 더하는 순간 롤링 배포가 깨진다(구 클라이언트가 즉사). 진단 표면만 예외다 —
    # 모르는 것을 보여주는 게 그 표면의 일이기 때문이다.
    model_config = ConfigDict(extra="allow")

    # 3.23.0 — §29 가 "BHealth·ServerHealth 둘 다" 로 확정했는데 선언을 안 넣었다.
    # extra="allow" 로 통과하고 있었지만 **타입도 C# 미러도 못 만든다.**
    # 🔴 둘 다 Optional=None 이다. `int = 0` 으로 두면 필드를 안 보내는 서버가
    #    "untracked 없음" 이라고 말한 게 된다 — 3.16.1 이 `bool = False` 에서 잡은 거짓말.
    build_untracked: Optional[int] = None   # git ls-files --others --exclude-standard

    ok: bool = True
    contract: ContractInfo = Field(default_factory=ContractInfo)

    # ── 돌고 있는 코드가 무엇인가 (3.16.0) ────────────────────────────
    #
    # 🔴 **"테스트가 통과했다" 와 "그 코드가 떠 있다" 는 다른 사실이다.**
    #
    # 이 프로젝트가 이미 한 번 배웠다 — "빌드 산출물의 상태를 소스 파일로 추정하지
    # 마라. 씬 YAML(소스)이 맞다고 APK(배포물)도 맞다는 보장이 없다."
    # 서버에도 그대로 적용된다. 커밋은 됐는데 프로세스는 안 내렸다 재기동되면,
    # pytest 는 초록이고 와이어는 옛 동작이다. **예외가 안 난다.**
    #
    # 실제로 사용자가 이걸 물었다 (2026-08-01): "이 수정사항은 8083 에 떠야 하는 거
    # 아닌가?" 그때 확인할 방법이 계약에 없었다 — 세션의 자기보고뿐이었고,
    # 두 세션이 같은 시각에 A5000 재생 버그를 "고쳤다"/"미해결" 로 서로 다르게 적었다.
    #
    #   build          기동한 코드의 커밋 해시 (예: git rev-parse --short HEAD)
    #   build_dirty    커밋 안 된 변경 위에서 떴는가
    #   started_at     프로세스 기동 시각 (ISO8601). 커밋 시각과 비교하면
    #                  "고친 뒤에 떴는가" 가 한 줄로 판정된다
    #
    # 이 셋이 있으면 "재기동했나요?" 를 물을 필요가 없다. **값으로 판정된다.**
    # `BHealth` 에도 같은 필드가 있고, `/v2/health` 가 업스트림을 실어 보내므로
    # **curl 한 번이 3자 코드 버전 검사**가 된다 (§14 의 3자 상수 검사와 같은 형태).
    build: Optional[str] = None
    # 🔴 `Optional` 이고 기본값이 `None` 이다 — `False` 가 아니다 (3.16.1, 3090 지적).
    # `False` 를 기본값으로 두면 **필드를 안 보내는 서버가 "깨끗하다" 고 말한 것이 된다.**
    # 위험한 방향의 거짓말이다. `None`(모름)과 `False`(깨끗)를 갈라야 한다.
    # 3090 이 자기 buildinfo 에서 같은 판단을 이미 했다 — git 호출이 실패하면
    # 깨끗으로 접지 않고 모름/더러움으로 본다.
    build_dirty: Optional[bool] = None
    started_at: Optional[str] = None

    upstream_ok: bool = False
    upstream: Optional["BHealth"] = None
    upstream_error: Optional[str] = None
    # 잡 **개수만**. 목록을 덤프하면 잡이 쌓일수록 응답이 무한정 커진다 (as-built 결함).
    jobs: Dict[str, int] = Field(default_factory=dict)  # {"queued": 0, "running": 1, ...}


# ════════════════════════════════════════════════════ 3090 -> A5000
#
# A5000 은 glTF/매니페스트/버전관리를 몰라야 한다 (FINAL §1 도메인 분리).
# 아래 스키마에는 glTF 라는 단어가 등장하지 않는다.


class BGenerateRequest(BaseModel):
    asset_id: str  # 3090이 발급 (FINAL §7)
    seed: int = 42
    # 이미지는 멀티파트 파트로 전달. 여기엔 파트 이름만.
    image_part: str = "image"


class BEditRequest(BaseModel):
    """3090 → A5000 편집 요청.

    🔴🔴 **`prompt` 는 현재 구현에서 아무 일도 하지 않는다** (A5000 실측, 3.16.4).

    `edit()` 이 `op_edit(parent, d, req.mask, req.seed, ...)` 로만 넘겨서 이 문자열은
    **어디에도 전달되지 않는다.** 그리고 그건 버그가 아니라 백본의 성질이다 —
    `TRELLIS-image-large` 는 **이미지 조건**이고 텍스트 조건 자리가 없다.
    편집 조건은 `pipe.get_cond([parent/input.png])` 하나뿐이다.

    A5000 이 대조군으로 증명했다. 바이트 동일성으로는 못 잰다(3.4.0 이후 비결정적).
    마스크 안 latent 평균 L2 로 쟀다:

        같은 시드 · 다른 프롬프트   pumpkin ↔ devil horns ↔ 빈 문자열   0.006~0.007
        같은 시드 · 같은 프롬프트   같은 요청 재실행 (대조군)            0.006   ← 비결정성 바닥
        다른 시드                  seed 7 / 1234 / 777                2.13~2.42  ← 350배

    **프롬프트를 바꾼 효과가 같은 프롬프트를 다시 돌린 효과와 정확히 같다.**
    편집 전체 크기의 0.2% — 즉 0 이다. 재실행 대조군이 노이즈 바닥을 세워준 것이
    이 측정의 핵심이다. 그게 없으면 0.007 이 작은 효과인지 0 인지 못 가린다.

    ⚠️ **데모에서 프롬프트를 화면에 띄우면 그 자리는 거짓말이 된다.**
    "자연어로 편집한다" 는 지금 성립하지 않는다. 마스크와 시드만이 입력이다.

    계약이 이 필드를 정의해두고 구현이 쓰는지 아무도 확인하지 않아서 오래 지나갔다.
    **필드를 정의하면 그것이 실제로 소비되는지도 검증 대상이다** — 값이 안 쓰이는
    것은 예외를 안 내고, 스키마 테스트도 이름만 본다.

    복구 경로는 둘이고 **둘 다 계약 변경**이다 (데드라인 후 판단):
      1. 편집별 조건 이미지를 받는다 (텍스트 → 이미지 → 조건). 백본에 맞는 방향
      2. 스파스 구조 단계를 마스크 영역에 다시 태운다 (아래 Phase 1 제약 해제)
    """

    asset_id: str
    base_version: int
    prompt: str  # ⚠️ 현재 소비되지 않는다. 위 docstring 참조
    mask: EditMask
    seed: int = 42
    idempotency_key: Optional[str] = None


class BAssembleRequest(BaseModel):
    """3090 → A5000 **조립** 요청 (계약 3.19.0).

    편집과 왜 다른 엔드포인트인가 — 필수 필드가 다르다. `BEditRequest` 에 op 를
    얹으면 양쪽 필드가 전부 Optional 이 되어 **fail-fast 검증이 사라진다.**
    이 프로젝트의 주된 방어가 그거라 나눈다. 응답은 `BChunkResponse` 그대로다.

    그리고 `prompt` 를 받지 않는다. 3.17.0 이 그 필드가 A5000 에서 no-op 임을
    실측으로 박았는데, 새 연산에 다시 실으면 같은 거짓말을 반복하게 된다.

    ⚠️ 스케일 인자가 **없다.** 좌표 확대가 인접성을 파괴한다(6-이웃 유지율
    s=2.0 에서 0%). 크기는 `donor_crop_fraction` 으로만 고른다.
    `deltacontract.assemble` 의 함수를 쓰면 이 규칙이 코드로 강제된다.
    """

    asset_id: str                  # 대상(base). 결과가 커밋될 자산
    base_version: int
    donor_asset_id: str            # 기증자. 읽기만 한다 — 변경되지 않는다
    donor_crop_fraction: float = Field(gt=0.0, le=1.0)
    donor_crop_axis: int = 2       # 0=x 1=y 2=z. 기본 z(상단축)
    donor_crop_keep: Literal["top", "bottom"] = "top"
    # VOXEL 격자의 **정수** 평행이동. 소수는 거부된다 (assemble.place_cells).
    # None 이면 서버가 assemble.fit_offset 으로 정한다 (마스크 바닥에 앉힌다).
    offset: Optional[Tuple[int, int, int]] = None
    # 대상에서 **비울** 영역. 조립은 REMOVE 만 정의돼 있다 — 겹쳐 넣으면 좌표
    # 충돌 시 어느 latent 를 쓸지가 미정의다 (3.17.0 실측 806건).
    mask: EditMask
    idempotency_key: Optional[str] = None


class ChunkMeshPayload(BaseModel):
    """B->A 청크 1개의 **메타데이터**. 실제 바이트는 .cbin 으로 따로 전달된다
    (GET /v2/trellis/assets/{id}/chunks/...). FINAL §5 의 중립 포맷."""

    chunk_id: str  # "x_y_z"
    hash: str
    byte_length: int
    vertex_count: int
    index_count: int
    voxel_count: int
    # 이 청크 바이트가 속한 버전. 3090이 B 로부터 바이트를 다시 가져올 때 URL 에 쓴다.
    #
    # 3090 지적(2026-07-29): to_version 에서 유추하면 "바뀌지 않은 청크를 다시 받아야
    # 할 때"(디스크 손상 복구 등) 그 청크의 버전을 알 방법이 없다. 인터페이스 구멍이라
    # 명시 필드로 닫는다.
    #
    # 도메인 분리를 깨지 않는다 — B 는 이미 BEditRequest.base_version 을 받고
    # to_version 을 돌려준다. B 가 모르는 건 glTF·매니페스트이지 버전 번호가 아니다.
    version: int


class BChunkResponse(BaseModel):
    """B 가 재생성한 것을 보고한다. **무엇이 실제로 바뀌었는지는 A 가 판정한다.**

    3090 지적(2026-07-29)에 대한 확정: `chunks` 는 "해시가 바뀐 것"이 아니라
    "B 가 재생성한 것"이다. B 에게 "바뀐 것만 보내라"를 요구하면 B 가 이전 버전의
    해시 상태를 들고 있어야 하는데, 그건 A 의 도메인(버전 관리)을 B 로 새게 한다.
    같은 바이트를 다시 보내도 정상이며, A 는 그 경우 버전을 올리지 않는다.

    ────────────────────────────────────────────────────────────────────
    🔴 `chunks[]` 항목은 `ChunkEntry` 가 **아니다** — `uri` 가 없다
    ────────────────────────────────────────────────────────────────────
    3090 지적(2026-08-04, W3): `entry["uri"]` 로 읽다가 `KeyError` 로 죽었다.
    A5000 실측 항목은 이 모양이다:

        {"chunk_id": "0_3_1", "hash": "...", "byte_length": 1308,
         "vertex_count": 29, "index_count": 114, "voxel_count": 2, "version": 1}

    `ChunkEntry.uri` 는 **매니페스트**(Unity 가 받는 것)의 필드이고, 여기는
    `chunk_id` 다. 같은 것을 가리키는 두 모양이 공존하므로 한쪽을 다른 쪽에서
    유추하지 마라.

    그리고 이 응답이 가리키는 바이트는 **커밋 전이면 staging 에 있다.** 경로는
    `uris.staging_chunk_uri(asset_id, job_id, chunk_id)` 로만 만든다 — 일반 청크
    경로(`/v2/trellis/assets/...`)에서 유추하면 404 만 나온다. staging 만
    `/v2/trellis` 접두사가 붙지 않고 버전 번호도 없다. (W3 에서 이 유추로
    181개 청크를 전부 404 로 받았다. `contract/README.md` 참조.)
    """

    asset_id: str
    to_version: int
    contract: ContractInfo = Field(default_factory=ContractInfo)
    is_initial: bool = False

    # 이 응답이 **재계산이 아니라 재생**인가 (멱등 키로 저장된 결과를 돌려준 것).
    # A5000 지적(2026-07-29): 서버 내부 판정에만 쓰던 `_base_version` 을
    # 언더스코어 필드로 실어보내고 있었다. 정식 필드로 올린다.
    #
    # A 는 이걸로 두 가지를 한다:
    #   - 재생 비율을 로그에 남긴다 (재시도가 잦으면 네트워크나 클라이언트 문제다)
    #   - base_version 불일치를 경고한다 (아래)
    replayed: bool = False
    # 멱등 키와 함께 저장돼 있던 base_version. 요청의 base_version 과 다르면
    # 클라이언트가 혼동한 상태다 — 저장된 결과를 주되 **경고를 남겨라.**
    # 키는 요청 상태가 아니라 **연산**을 식별한다.
    replayed_base_version: Optional[int] = None

    # ── 마스크 지문 (3.13.0) ──────────────────────────────────────────
    #
    # B 가 **실제로 쓴** 마스크의 지문이다. A 가 보낸 마스크와 대조하라고 싣는다.
    #
    # 왜 필요한가: 지금까지 "A 가 보낸 마스크"와 "B 가 쓴 마스크"가 같은지를
    # 확인할 방법이 없었다. A5000 이 슬롯(`job.json`)에 지문을 남겼지만 그건
    # A5000 디스크에만 있어 3090 이 못 본다. 어긋나도 **조용히** 지나간다 —
    # 이 프로젝트가 반복해서 밟은 종류다.
    #
    # `stats` 에 못 싣는다: `Dict[str, float]` 이라 문자열이 안 들어간다
    # (A5000 지적, 2026-07-31). 그래서 별도 필드로 올린다.
    #
    #   mask_fingerprint          halo 적용 **전** 원본 셀의 지문
    #   mask_fingerprint_dilated  halo 적용 **후** 셀의 지문
    #
    # 원본을 정본으로 삼는 이유는 halo 가 서버가 붙이는 값이라 요청 동일성과
    # 무관하기 때문이다. dilated 를 같이 싣는 이유는 A 가 `dilate_cells()` 로
    # 독립 재계산해서 대조할 수 있기 때문이다 — 서버 숫자가 비교의 어느 쪽에도
    # 안 들어가는 검증이 이 프로젝트에서 반복해서 값을 했다 (3.9.7).
    #
    # 🔴 **직렬화는 `coords.mask_fingerprint()` 만 쓴다** (3.14.0).
    # 3.13.0 이 "canonical_sort 후 sha256" 까지만 적고 바이트 표현을 안 정했다.
    # 3090 이 후보 5가지를 실측으로 재서 역산해야 했다 —
    # `sha256(canonical_sort(cells).astype(int32).tobytes())`.
    # 그 형태를 계약 함수로 승격했으므로 이제 양쪽이 같은 코드를 부른다.
    # 두 서버가 각자 직렬화하면 어긋나도 "지문 불일치" 로만 보이고,
    # **마스크가 달랐던 것인지 인코딩이 달랐던 것인지 구분이 안 된다** —
    # 그 구분 불가가 이 필드를 넣은 목적 자체를 없앤다.
    #
    # 셀 순서에 무관하다. 마스크 없는 요청(최초 생성)에서는 `None` 이다.
    mask_fingerprint: Optional[str] = None
    mask_fingerprint_dilated: Optional[str] = None

    chunks: List[ChunkMeshPayload] = Field(default_factory=list)
    removed_chunk_ids: List[str] = Field(default_factory=list)
    # RePaint 부기에서 나온 영향 청크 집합 (§4.2). chunks 와 일치해야 하며,
    # 어긋나면 결정성 가정이 깨진 것이므로 3090이 거부한다.
    # 부기(마스크+halo → 청크)가 지목한 집합. **이게 "무엇이 바뀌었는가"의 정의다.**
    #
    # A 의 검증은 전칭이다:
    #     ∀ c ∈ bookkeeping_affected_chunk_ids : c ∈ chunks ∨ c ∈ removed_chunk_ids
    #
    # 부기는 복셀에서 계산되므로 그 청크에 실제로 삼각형이 생기는지 모른다.
    # **기하가 없으면 `removed_chunk_ids` 에 넣어라.** "아무 데도 안 넣기"는 금지 —
    # 그러면 A 가 "빠뜨렸다"와 "비었다고 알려줬다"를 구분할 수 없다.
    # (3090 지적, 2026-07-29: 실측 bookkeeping 24 vs chunks 23)
    #
    # 단, 응답에 싣기 전에 **실재하는 청크로 가지치기**한다:
    #     보고할 부기 = 원래 부기 ∩ (base 매니페스트 청크 ∪ 새 디코딩 청크)
    # 양쪽에 없는 청크는 보낼 것도 지울 것도 없는 증명 가능한 no-op 이다.
    # bbox 마스크가 빈 공간을 크게 물면 부기가 256 까지 부푸는데(A5000 실측)
    # 그중 226 이 이런 유령이었다. 가지치기는 디코딩 **후에** 한다.
    #
    # 빠뜨리면 그 청크는 낡은 바이트로 남아 편집이 반영되지 않은 조각이
    # 화면에 남는다 → BookkeepingMismatch.
    bookkeeping_affected_chunk_ids: List[str] = Field(default_factory=list)

    # 필수 키가 있다. 3090 지적(2026-07-29): 부기가 지나치게 넓으면 C-5 포함관계
    # 검증이 자동으로 통과해버려 **아무것도 못 잡는다.** 그리고 그때는 델타 이득도
    # 같이 사라진다. 그래서 효율 자체를 계측해서 실어보낸다.
    #
    #   bookkeeping_chunks : 부기가 지목한 청크 수
    #   regenerated_chunks : 실제로 재생성해 보낸 청크 수 (= len(chunks))
    #   total_chunks       : 이 자산의 전체 청크 수
    #   repaint_ms / decode_ms / chunk_ms : 단계별 소요
    #
    # 선택 키:
    #   masked_rows       (3.13.0) 편집이 실제로 건드린 latent 행 수.
    #                     마스크 지문과 짝이다 — 지문이 다른데 masked_rows 가 같으면
    #                     "추가분이 전부 빈 공간이었다" 는 뜻이고, 그 판정에
    #                     A5000 이 네 시간을 썼다(2026-07-31). 이제 두 필드로 즉답이다.
    #   seam_* / snapped_vertices : 이음새 계측 (3.7.0)
    #
    # ⚠️ 타입이 `Dict[str, float]` 이라 `null` 을 못 넣는다. **미측정은 키를 빼라.**
    # (`LAB_API.md` 의 "미측정은 null" 규칙은 그쪽 표면에만 적용된다.)
    #
    # A 는 `regenerated_chunks / total_chunks` 를 로그에 남긴다. 이 비율이 1.0 에
    # 가까우면 델타가 이름만 남은 것이고, 조용히 성능이 무너지는 걸 이 숫자로만 안다.
    stats: Dict[str, float] = Field(default_factory=dict)


class BHealth(BaseModel):
    # 🔴 **진단 표면만 `extra="allow"` 다** (3.19.2).
    #
    # 이 프로젝트는 "무슨 코드가 떠 있나"를 이 응답으로 판정한다. 그런데 pydantic
    # 기본값(`extra="ignore"`)이 **모르는 키를 조용히 버린다.** 서버가 새 필드를
    # 먼저 보내면 침묵이 돌아오고, 그 침묵이 "안 보냈다"로 읽힌다.
    # 실측(2026-08-03): A5000 이 `build_untracked` 를 실었는데 스키마가 몰라서
    # 통째로 드롭됐고 응답만 보면 서버가 안 보낸 것과 구분되지 않았다.
    #
    # ⚠️ **페이로드 스키마는 `ignore` 로 둔다.** `forbid` 로 바꾸면 서버가 필드를
    # 더하는 순간 롤링 배포가 깨진다(구 클라이언트가 즉사). 진단 표면만 예외다 —
    # 모르는 것을 보여주는 게 그 표면의 일이기 때문이다.
    model_config = ConfigDict(extra="allow")

    # 3.23.0 — §29 가 "BHealth·ServerHealth 둘 다" 로 확정했는데 선언을 안 넣었다.
    # extra="allow" 로 통과하고 있었지만 **타입도 C# 미러도 못 만든다.**
    # 🔴 둘 다 Optional=None 이다. `int = 0` 으로 두면 필드를 안 보내는 서버가
    #    "untracked 없음" 이라고 말한 게 된다 — 3.16.1 이 `bool = False` 에서 잡은 거짓말.
    build_untracked: Optional[int] = None   # git ls-files --others --exclude-standard

    ok: bool = True
    contract: ContractInfo = Field(default_factory=ContractInfo)
    # 돌고 있는 코드 (3.16.0). `ServerHealth.build` 주석 참조 —
    # `/v2/health` 가 이걸 업스트림으로 실어 보내므로 curl 한 번이 3자 검사가 된다.
    build: Optional[str] = None
    # 🔴 `Optional` 이고 기본값이 `None` 이다 — `False` 가 아니다 (3.16.1, 3090 지적).
    # `False` 를 기본값으로 두면 **필드를 안 보내는 서버가 "깨끗하다" 고 말한 것이 된다.**
    # 위험한 방향의 거짓말이다. `None`(모름)과 `False`(깨끗)를 갈라야 한다.
    # 3090 이 자기 buildinfo 에서 같은 판단을 이미 했다 — git 호출이 실패하면
    # 깨끗으로 접지 않고 모름/더러움으로 본다.
    build_dirty: Optional[bool] = None
    started_at: Optional[str] = None
    low_vram: bool = False
    gpu_mem_gb: Optional[float] = None
    # TRELLIS 1 (2-stage: Structure → SLat). TRELLIS.2 는 국소 편집이 되지 않아 폐기했다.
    # 2-stage 이므로 FINAL §9-3(단계 간 의존성) 결정성 항목은 적용되지 않는다.
    trellis_variant: str = "TRELLIS-image-large"
    stages: List[str] = Field(default_factory=lambda: ["sparse_structure", "slat"])
    slat_resolution: int = 64  # sample_sparse_structure() 가 내놓는 좌표 공간

    # ★ 3090은 이 값이 False 면 요청을 보내지 않고 CONTRACT_MISMATCH 로 실패시킨다.
    #
    # A5000 실측(2026-07-29): 결정성 플래그 없이 같은 시드로 두 번 돌리면 청크 해시가
    # 77/77 전부 다르다. 켜면 77/77 비트 동일. 즉 이 플래그가 꺼져 있으면 "안 바뀐
    # 청크는 바이트 동일"이라는 델타의 근거 자체가 사라지고, 모든 편집이 전체
    # 재전송으로 퇴화한다 — 그것도 조용히.
    #
    # 서버 기동 시 다음이 모두 적용됐을 때만 True 로 보고할 것:
    #   CUBLAS_WORKSPACE_CONFIG=:4096:8 (환경변수), use_deterministic_algorithms(True),
    #   cudnn.deterministic=True, cudnn.benchmark=False
    deterministic_algorithms: bool = False  # 3.4.0 부터 불필요. 참고 정보로만 싣는다
    # 저장된 SLat 을 어느 순서로 디코딩하는지. 계약값(canonical)과 달라야 할 이유가 없다.
    coord_order: str = "canonical"


# ════════════════════════════════════════════════════ 에러 규약


class ErrorBody(BaseModel):
    """모든 4xx/5xx 본문. code 는 클라이언트 분기용이라 문자열 상수로 고정한다."""

    error_code: Literal[
        "CONTRACT_MISMATCH",
        "UNAUTHORIZED",
        "NOT_FOUND",
        "ASSET_NOT_FOUND",
        "INVALID_REQUEST",
        "VERSION_CONFLICT",  # base_version 이 최신이 아님
        "MASK_EMPTY",  # 마스크가 활성 복셀을 하나도 안 덮음
        "UPSTREAM_TIMEOUT",  # 3090 -> A5000 타임아웃
        "UPSTREAM_OOM",
        "BOOKKEEPING_MISMATCH",  # 부기가 지목한 청크를 B 가 빠뜨렸다 (집합의 문제)
        "CHUNK_HASH_MISMATCH",  # 전송된 청크 바이트 해시가 보고와 다르다 (바이트의 문제, 재시도 가능)
        "STAGING_EXPIRED",  # 410. ephemeral 결과가 만료됐다 — 404(URI 오류)와 갈라야 한다
        "INTERNAL",
    ]
    message: str
    detail: Optional[Dict[str, str]] = None


# ServerHealth 가 뒤에 정의된 BHealth 를 참조하므로 지연 해석을 확정한다.
ServerHealth.model_rebuild()
