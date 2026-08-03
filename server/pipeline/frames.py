"""D9 — 좌표 프레임 규약. **축 순열을 상수로 못박는다.**

────────────────────────────────────────────────────────────────────────
🔴 이 파일이 없으면 전부 조용히 무의미해진다
────────────────────────────────────────────────────────────────────────
`docs/PROGRESS.md` §2 D9:

    GLB       Y-up   (±0.50)
    복셀 격자  Z-up   (±0.49)
    변환      voxel = (x, -z, y)      즉 perm=(0,2,1) sign=(1,-1,1)

    근거   A5000 이 가정하지 않고 48개 부호付 순열을 **전수 탐색**해 확정.
           정답 IoU 0.9365 vs 차점 0.1943 — 4.8배 격차로 모호하지 않다

**항등 변환을 쓰면 IoU 0.19 대가 나온다.** 예외는 안 난다. 마스크는 여전히
"위쪽 35%" 를 잡고, 조립도 돌고, 지표도 숫자를 낸다 — 다만 그 숫자가 전부
다른 물체에 대한 것이다. 눈으로 보기 전까지 아무도 모른다.

그래서 문서가 아니라 **함수와 상수**로 둔다. 방법론 5조 4번:

    규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다

이 프로젝트는 같은 방식으로 세 번 물렸다 (3.11.0 staging 경로 · 3.13.0 마스크
지문 · 배치/부기). 축 순열이 네 번째가 될 자리였다.

────────────────────────────────────────────────────────────────────────
방향 대응 (물리적 서술 ↔ 두 프레임)
────────────────────────────────────────────────────────────────────────
    물리       GLB(Y-up)      VOXEL(Z-up)
    위         +Y             +Z
    앞         +Z             **-Y**      ← 부호가 뒤집히는 유일한 축
    오른쪽     +X             +X

`GLB_TO_VOXEL.apply()` 를 거치지 않은 GLB 좌표를 복셀 격자에 넣지 마라.
GLB 를 읽는 경로는 `pipeline/voxelize.py` 의 `load_mesh()` 하나이고, 그 함수가
기본값으로 이 변환을 적용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations, product
from typing import List, Tuple

import numpy as np

__all__ = [
    "GLB_TO_VOXEL",
    "IDENTITY",
    "VOXEL_TO_GLB",
    "AxisTransform",
    "all_signed_permutations",
    "assert_not_identity",
    "to_voxel_frame",
]


@dataclass(frozen=True)
class AxisTransform:
    """부호付 축 순열. `out[i] = sign[i] * pts[:, perm[i]]`.

    스케일도 평행이동도 없다 — 순열과 부호뿐이다. 그래서 역변환이 항상 존재하고
    왕복이 정확히 항등이다 (부동소수 오차 없음).
    """

    perm: Tuple[int, int, int]
    sign: Tuple[int, int, int]
    note: str = ""

    def __post_init__(self) -> None:
        if sorted(self.perm) != [0, 1, 2]:
            raise ValueError(f"perm 은 (0,1,2) 의 순열이어야 한다: {self.perm}")
        if any(s not in (-1, 1) for s in self.sign):
            raise ValueError(f"sign 은 ±1 이어야 한다: {self.sign}")

    @property
    def is_identity(self) -> bool:
        return self.perm == (0, 1, 2) and self.sign == (1, 1, 1)

    def apply(self, pts: np.ndarray) -> np.ndarray:
        """(N,3) 좌표를 변환한다. dtype 을 보존한다 (정수 좌표는 정수로 남는다)."""
        a = np.asarray(pts)
        if a.ndim != 2 or a.shape[1] != 3:
            raise ValueError(f"(N,3) 이 필요하다. got {a.shape}")
        out = a[:, list(self.perm)].copy()
        for i, s in enumerate(self.sign):
            if s < 0:
                out[:, i] = -out[:, i]
        return out

    def inverse(self) -> "AxisTransform":
        inv_perm = [0, 0, 0]
        inv_sign = [1, 1, 1]
        for i, p in enumerate(self.perm):
            inv_perm[p] = i
            inv_sign[p] = self.sign[i]
        return AxisTransform(
            perm=tuple(inv_perm), sign=tuple(inv_sign), note=f"inverse of {self.note}"
        )

    def __str__(self) -> str:  # pragma: no cover - 진단 출력
        axes = "xyz"
        parts = [
            f"{'-' if self.sign[i] < 0 else ''}{axes[self.perm[i]]}" for i in range(3)
        ]
        return f"({', '.join(parts)})"


# ══════════════════════════════════════════════════════════════ 정본 상수
#
# 🔴 이 값을 바꾸려면 A5000 의 전수 탐색을 다시 돌리고 docs/adr/ 에 근거를 남겨라.
#    server/tests/test_frames.py 가 48개 전수 탐색으로 이 값을 재확인한다.

GLB_TO_VOXEL = AxisTransform(
    perm=(0, 2, 1), sign=(1, -1, 1), note="D9: voxel = (x, -z, y)"
)
VOXEL_TO_GLB = GLB_TO_VOXEL.inverse()

# 대조군 전용. **파이프라인에서 쓰지 마라** — 이걸 쓰면 IoU 0.19 대가 나온다.
IDENTITY = AxisTransform(perm=(0, 1, 2), sign=(1, 1, 1), note="항등 — 오답")


def to_voxel_frame(pts: np.ndarray) -> np.ndarray:
    """GLB(Y-up) 좌표 → 복셀 격자(Z-up) 좌표. GLB 를 읽는 모든 경로가 통과한다."""
    return GLB_TO_VOXEL.apply(pts)


def assert_not_identity(transform: AxisTransform, where: str = "") -> None:
    """항등 변환이 파이프라인에 섞여 들어오는 것을 막는다.

    항등은 예외를 안 내고 그럴듯한 숫자를 낸다. 그래서 **명시적으로** 거부한다.
    대조군에서 의도적으로 항등을 쓰려면 이 함수를 부르지 않으면 된다.
    """
    if transform.is_identity:
        raise ValueError(
            f"{where or '프레임 변환'}에 항등 변환이 들어왔다. GLB 는 Y-up, 복셀 격자는 "
            "Z-up 이므로 항등은 오답이다 (D9: A5000 전수 탐색 결과 IoU 0.9365 vs "
            "항등 계열 0.19 대). 예외 없이 조용히 틀린 물체를 계측하게 된다."
        )


def all_signed_permutations() -> List[AxisTransform]:
    """48개(6 순열 × 8 부호) 전부. A5000 의 전수 탐색을 테스트에서 재현하기 위한 것."""
    return [
        AxisTransform(perm=p, sign=s)
        for p in permutations((0, 1, 2))
        for s in product((1, -1), repeat=3)
    ]
