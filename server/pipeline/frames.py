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
    "SURFACE_VOXELIZATION_ROLE",
    "VOXEL_GRID_SOURCE",
    "GridSourceMismatch",
    "assert_slat_grid",
    "DECODER_NATIVE_TO_GLB",
    "DECODER_NATIVE_TO_VOXEL",
    "GLB_TO_VOXEL",
    "IDENTITY",
    "TO_GLB_ROTATION",
    "VOXEL_TO_GLB",
    "AxisTransform",
    "all_signed_permutations",
    "assert_not_identity",
    "decoder_native_to_voxel_frame",
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


# ══════════════════════════════════════════ D9-b — 두 번째 좌표 함정
#
# 🔴 **같은 파이프라인에 좌표 함정이 둘이고, 둘은 서로 반대 방향이다.**
#
#     D9    GLB 파일에서 읽은 메시   → 항등을 쓰면 **틀린다** (IoU 0.19)
#     D9-b  디코더 native 메시       → 항등이 **정답이다**
#
# 디코더의 native 프레임은 z-up 이고 **그것이 복셀 격자 프레임과 같다.** TRELLIS 의
# `to_glb` 는 마지막에 아래 회전을 걸어 z-up 을 y-up 으로 바꾼 뒤 파일로 내보낸다:
#
#     vertices @ [[1, 0, 0],
#                 [0, 0, -1],
#                 [0, 1, 0]]        # = (x, y, z) → (x, z, -y)
#
# 이 행렬은 `VOXEL_TO_GLB` 와 **같은 변환**이다 (테스트가 확인한다).
#
# ⇒ `to_glb` 를 **거친** 것(=GLB 파일)을 복셀과 비교할 때는 `GLB_TO_VOXEL` 을 건다.
# ⇒ `to_glb` 를 **거치지 않은** 것(=기하 전용 export, 디버그 덤프)은 이미 복셀
#    프레임이므로 **아무것도 걸지 않는다.** 여기에 `GLB_TO_VOXEL` 을 걸면 틀린다.
#
# 근거: A5000 이 기하 전용 export 에서 이 회전을 빠뜨려 왕복 메시만 z-up 이었다.
#       소스를 읽고 잡았다 — 놓쳤으면 **잡음 바닥값이 통째로 허수가 됐다** (D9-b).

# `to_glb` 말미의 회전 행렬 원문. `v @ TO_GLB_ROTATION` 형태로 쓴다.
TO_GLB_ROTATION = np.array(
    [[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.int64
)

# 디코더 native(z-up) → GLB(y-up). 위 행렬과 같다.
DECODER_NATIVE_TO_GLB = VOXEL_TO_GLB

# 🔴 디코더 native → 복셀 격자. **항등이다.** 두 프레임이 같기 때문이다.
#    D9 에서 항등이 오답이었던 것과 정반대다 — 그래서 이름을 따로 준다.
#    `assert_not_identity` 를 이 경로에 쓰지 마라.
DECODER_NATIVE_TO_VOXEL = AxisTransform(
    perm=(0, 1, 2), sign=(1, 1, 1), note="D9-b: 디코더 native 는 이미 복셀 프레임이다"
)


def to_voxel_frame(pts: np.ndarray) -> np.ndarray:
    """GLB(Y-up) 좌표 → 복셀 격자(Z-up) 좌표. GLB **파일**을 읽는 경로가 통과한다.

    ⚠️ 디코더 native 메시(`to_glb` 를 안 거친 것)에는 쓰지 마라 — 그건 이미 복셀
       프레임이다 (D9-b). `decoder_native_to_voxel_frame()` 을 써라.
    """
    return GLB_TO_VOXEL.apply(pts)


def decoder_native_to_voxel_frame(pts: np.ndarray) -> np.ndarray:
    """디코더 native(z-up) 좌표 → 복셀 격자 좌표. **항등이다** (D9-b).

    함수로 두는 이유는 호출부가 "변환을 생각했다" 는 것을 코드에 남기기 위해서다.
    항등이라 없어도 되지만, 없으면 다음 세션이 `to_voxel_frame` 을 잘못 건다.
    """
    return DECODER_NATIVE_TO_VOXEL.apply(pts)


# ══════════════════════════════════════ D28 — 세 번째 좌표 함정: **격자 정본**
#
# 🔴 앞의 둘은 "축을 어떻게 도느냐" 였다. 이건 **"어느 격자를 쓰느냐"** 다.
#    같은 자산을 두 세션이 각자 복셀화하면 축이 맞아도 인덱스가 한 칸 어긋난다.
#
# W7 실측 (같은 dragon-c):
#     3090    복셀 10,264 · 목 극소 **z=45**:32            (자기 표면 복셀화)
#     A5000   복셀  9,591 · 목 극소 **z=44**:32, z=45:28   (manifest·slat coords)
#     청크 수 124 로 일치. 형상도 일치. **인덱스만 한 칸 밀렸다.**
#
# 원인은 하나다 — 두 세션이 서로 다른 복셀화를 썼다. 격자가 다르면 z 인덱스가 밀리고,
# 그 오프바이원이 "마스크가 목 극소점 **위**" 인지 "극소점 **에서**" 인지를 갈랐다.
# 이번 게이트의 "자연스럽게" = 목 연결부 품질에 직결된다.
#
# ⇒ **정본은 slat coords / manifest 다.** VoxHammer 가 그 공간에서 동작하기 때문이다.
#   우리 `surface_voxelize()` 는 **진단용**이고 **마스크 좌표의 근거가 될 수 없다.**
#   마스크도 마스크 지문도 slat 격자에서 만든다.
#
# 이 사실을 문서에만 두면 안 지켜진다 — D9·D9-b 와 같은 이유로 상수와 함수로 둔다.

#: 복셀 격자의 정본이 무엇인지. 매니페스트/인계물에 그대로 싣는다 (D27 ③).
VOXEL_GRID_SOURCE = "slat_coords"

#: 우리 표면 복셀화의 지위. 정본이 아니다.
SURFACE_VOXELIZATION_ROLE = "diagnostic_only"


class GridSourceMismatch(ValueError):
    """마스크가 정본이 아닌 격자에서 만들어졌다 (D28).

    축이 맞아도 인덱스가 밀린다. 실측 오프바이원 하나가 "목 극소점 위" 와
    "목 극소점에서" 를 갈랐다 — 예외는 안 나고 결과만 달라진다.
    """


def assert_slat_grid(grid_source: str, where: str = "") -> None:
    """마스크 좌표의 근거가 slat 격자인지 확인한다 (D28).

    호출부가 자기 격자 출처를 **명시적으로 말하게** 만드는 것이 목적이다.
    `surface_voxelize()` 결과로 만든 마스크를 그대로 넘기면 여기서 걸린다.

    Args:
        grid_source: 이 좌표가 어느 격자에서 왔는지. 정본은 `VOXEL_GRID_SOURCE`.
    """
    if grid_source != VOXEL_GRID_SOURCE:
        raise GridSourceMismatch(
            f"{where or '마스크'} 의 격자 출처가 {grid_source!r} 다. 정본은 "
            f"{VOXEL_GRID_SOURCE!r}(manifest) 이다 — VoxHammer 가 그 공간에서 "
            "동작하기 때문이다 (D28). 자체 표면 복셀화는 진단용이고 마스크 좌표의 "
            "근거가 될 수 없다. 실측: 같은 자산에서 3090 z=45 vs A5000 z=44 로 "
            "한 칸 밀렸고, 그 한 칸이 '목 극소점 위' 와 '극소점에서' 를 갈랐다."
        )


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
