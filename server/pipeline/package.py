"""`.cbin` 세트 + manifest 포장.

────────────────────────────────────────────────────────────────────────
🔴 마스크 밖은 **부모 바이트를 승계**한다. 재디코딩본을 쓰지 않는다
────────────────────────────────────────────────────────────────────────
`contract/python/deltacontract/assemble.py` 서두:

    조립은 전체를 재디코딩한다. 그런데 디코딩은 프로세스를 새로 띄우면 바이트가
    재현되지 않는다 — 기증자 없이 base 만 다시 디코딩한 대조군에서 **152/152 청크가
    전부 다른 해시**를 냈다. 기하 변화는 중앙값 0.0002 메시셀이었다. 부동소수 잡음이다.

즉 "마스크 밖 바이트 100% 동일" 은 디코딩이 재현적이어서 성립하는 게 아니라
**부모 바이트를 그대로 물려주기 때문에** 성립한다. 이 모듈이 그 승계를 한 곳에서
수행하고, 그 밖의 경로를 만들지 않는다.

승계 규칙:

    c ∈ book        → 새로 인코딩한 바이트    (이번 연산이 책임지는 청크)
    c ∉ book        → **부모 바이트 그대로**   (재인코딩 결과를 쓰지 않는다)

전송량(C)은 `book` 에 속한 청크만 센다. 나머지는 클라이언트가 이미 갖고 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping

from deltacontract.chunkbin import (  # type: ignore[import-not-found]
    ChunkMesh,
    blob_hash,
    encode,
)
from deltacontract.coords import (  # type: ignore[import-not-found]
    CONTRACT_CONSTANTS,
    CONTRACT_VERSION,
)
from deltacontract.errors import BookkeepingMismatch  # type: ignore[import-not-found]

from .delta import Bookkeeping
from .mask import MaskResult

__all__ = ["DeltaPackage", "encode_chunks", "package_delta"]


@dataclass(frozen=True)
class DeltaPackage:
    """포장 결과.

    `blobs` 는 클라이언트가 **적용 후** 갖게 되는 전체 세트,
    `delta_blobs` 는 실제로 **선을 타고 가는** 부분집합이다. 둘을 구분하지 않으면
    절감률이 조용히 100% 로 나온다 (아무것도 안 보내면 절감은 완벽하다).
    """

    blobs: Dict[str, bytes]
    delta_blobs: Dict[str, bytes]
    removed: List[str]
    inherited_keys: List[str]
    manifest: dict = field(default_factory=dict)

    @property
    def full_bytes(self) -> int:
        """전체 재전송이라면 보냈을 바이트."""
        return sum(len(b) for b in self.blobs.values())

    @property
    def delta_bytes(self) -> int:
        """실제로 보내는 바이트."""
        return sum(len(b) for b in self.delta_blobs.values())


def encode_chunks(chunk_meshes: Mapping[str, ChunkMesh]) -> Dict[str, bytes]:
    """{chunk_key: ChunkMesh} → {chunk_key: .cbin 바이트}."""
    return {k: encode(m) for k, m in chunk_meshes.items()}


def package_delta(
    parent_blobs: Mapping[str, bytes],
    child_blobs: Mapping[str, bytes],
    bk: Bookkeeping,
    *,
    mask: MaskResult | None = None,
    job_id: str | None = None,
) -> DeltaPackage:
    """부모 세트 + 자식 세트 + 부기 → 승계가 적용된 델타 패키지.

    Args:
        parent_blobs: 편집 전 `.cbin` 세트.
        child_blobs:  결과 메시를 그대로 분할·인코딩한 세트 (승계 **전**).
        bk:           `derive_bookkeeping()` 결과.

    Raises:
        BookkeepingMismatch: 부기 밖에서 청크가 새로 생겼거나 사라졌을 때.
            그건 부기가 실제 변화를 못 따라갔다는 뜻이고, 조용히 넘어가면
            클라이언트가 옛 기하를 들고 남는다.
    """
    book = set(bk.book)
    removed = set(bk.removed)

    # 부기 밖에서 생겨나거나 사라진 청크는 사고다.
    born_outside = sorted(set(child_blobs) - set(parent_blobs) - book)
    if born_outside:
        raise BookkeepingMismatch(
            f"부기 밖에서 새 청크 {len(born_outside)}개가 생겼다: {born_outside[:8]}"
        )
    vanished_outside = sorted(set(parent_blobs) - set(child_blobs) - book)
    if vanished_outside:
        raise BookkeepingMismatch(
            f"부기 밖에서 청크 {len(vanished_outside)}개가 사라졌다: "
            f"{vanished_outside[:8]}"
        )

    blobs: Dict[str, bytes] = {}
    delta_blobs: Dict[str, bytes] = {}
    inherited: List[str] = []

    # 1) 부기 밖 — 부모 바이트 승계. child_blobs 의 재인코딩본은 **쓰지 않는다**.
    for key, blob in parent_blobs.items():
        if key in book:
            continue
        blobs[key] = blob
        inherited.append(key)

    # 2) 부기 안 — 새 바이트. removed 는 바이트가 없다(비었다고 알려주는 것이 계약).
    for key in bk.changed:
        if key not in child_blobs:
            raise BookkeepingMismatch(
                f"부기가 changed 라고 한 청크 {key} 의 바이트가 없다"
            )
        blobs[key] = child_blobs[key]
        delta_blobs[key] = child_blobs[key]

    manifest = {
        "contract": dict(CONTRACT_CONSTANTS),
        "contract_version": CONTRACT_VERSION,
        "job_id": job_id,
        "chunks": [
            {"chunk_id": k, "hash": blob_hash(b), "bytes": len(b)}
            for k, b in sorted(delta_blobs.items())
        ],
        "removed_chunk_ids": sorted(removed),
        "inherited_chunk_ids": sorted(inherited),
        "bookkeeping": {
            "zone1": bk.zone1,
            "zone2": bk.zone2,
            "book": bk.book,
        },
    }
    if mask is not None:
        manifest["mask_fingerprint"] = mask.fingerprint
        manifest["mask_fingerprint_dilated"] = mask.fingerprint_dilated
        manifest["halo_margin_voxels"] = mask.halo

    return DeltaPackage(
        blobs=blobs,
        delta_blobs=delta_blobs,
        removed=sorted(removed),
        inherited_keys=sorted(inherited),
        manifest=manifest,
    )
