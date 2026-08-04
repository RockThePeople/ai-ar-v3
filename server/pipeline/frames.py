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

# 격자 해상도는 **계약 상수**다. 여기서 복제하지 않는다 — 두 곳에 있으면 갈라진다.
from deltacontract.coords import VOXEL_RES  # type: ignore[import-not-found]

__all__ = [
    "SURFACE_VOXELIZATION_ROLE",
    "SURFACE_VOXELIZATION_SOURCE",
    "VOXEL_GRID_SOURCE",
    "GridSourceMismatch",
    "assert_slat_grid",
    "is_x_symmetric",
    "mirror_x",
    "symmetrize_x",
    "x_symmetry_cost",
    "assert_x_symmetric",
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

#: 자체 표면 복셀화로 만든 좌표의 출처 이름. `MaskResult.grid_source` 기본값이다 (D28-a).
#: 기본값을 정본으로 두지 않는다 — 아무 생각 없이 만든 마스크가 판정을 통과하면
#: D28-a 가 아무것도 막지 못한다.
SURFACE_VOXELIZATION_SOURCE = "surface_voxelize"


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


# ══════════════════════════════ D35 — 다섯 번째 함정: **좌우 대칭은 X 를 못 가린다**
#
# 🔴 전수 탐색(D9)이 답을 고를 수 있었던 것은 **소년상이 비대칭**이었기 때문이다.
#    좌우 대칭 자산에서는 그 방법이 통하지 않는다.
#
# W8 실측 (dragon-c, 날개 둘 — 좌우 대칭):
#     전수 탐색 1위 0.8231 vs 2위 0.8146 — 격차 **1.01배**.
#     2위는 같은 순열의 **X 반전**이다. 소년상은 4.8배였다 — 그땐 안 보였다.
#     VoxHammer 자체 복셀화로 교차 확인해도 identity 0.7486 vs mirror-X 0.7463.
#     Y·Z 는 명확하다 (0.052 / 0.044 로 확실히 오답) — **X 만** 못 가린다.
#
# ⇒ IoU 로는 원리적으로 못 가린다. 대칭 자산에서 X 반전은 자기 자신과 거의 같기 때문이다.
#
# ★ 해법 (최소): **마스크를 X 대칭으로 만든다.**
#   그러면 X 반전이 마스크를 바꾸지 않으므로 **모호성이 무해해진다.** 고를 필요가 없다.
#   D22② 의 "좌우로 머리 폭만큼 넓힌다" 를 그대로 따르면 자연히 대칭이 된다.
#
# ⚠️ 비대칭 마스크가 필요해지면 X 를 특정할 **fiducial**(비대칭 표식)을 자산과 함께
#    보내야 한다. 그때 다시 정한다 — 지금 없는 것을 있는 척하지 않는다.


def mirror_x(cells: np.ndarray) -> np.ndarray:
    """VOXEL 셀을 X 축으로 반전한다: `x → VOXEL_RES-1-x` (D35).

    대칭성 검사와 대조군에 쓴다. 파이프라인이 이걸 부를 일은 없다 —
    부를 일이 생겼다면 X 방향을 특정해야 하는 상황이고, 그건 fiducial 문제다.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return a.copy()
    out = a.copy()
    out[:, 0] = (VOXEL_RES - 1) - out[:, 0]
    return out


def is_x_symmetric(cells: np.ndarray) -> bool:
    """이 셀 집합이 **격자 중심** X 반전에 대해 불변인가 (D35).

    True 면 X 반전 모호성이 **무해하다** — 어느 쪽을 골라도 같은 마스크다.

    🔴 D35-a — "중심" 은 **격자 중심**(x + x' = VOXEL_RES-1)이지 자산 중심이 아니다.
       W10 에서 이 성질이 **우연히** 성립했다: 머리가 격자 중앙 근처에 있어서
       `[xlo-w, xhi+w]` 를 클램프한 결과가 마침 합 63 이 됐을 뿐이다.
       자산이 한쪽으로 치우치면 그대로 깨진다 — 그때는 X 반전이 다른 마스크를
       만들고 D35 의 "무해" 가 성립하지 않는다.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return True
    lhs = np.unique(a, axis=0)
    rhs = np.unique(mirror_x(a), axis=0)
    return lhs.shape == rhs.shape and bool(np.array_equal(lhs, rhs))


def symmetrize_x(cells: np.ndarray) -> np.ndarray:
    """마스크를 X 대칭으로 만든다 — 자기 자신과 X 반전의 **합집합** (D35).

    이것이 대칭 자산에서 X 모호성을 다루는 방법이다. 축을 고르지 않는다 —
    **고를 필요가 없게 만든다.**

    ⚠️ 마스크가 넓어진다. 그 대가로 "X 를 잘못 골랐다" 는 실패 모드가 사라진다.
       IoU 로 못 가리는 것을 억지로 고르는 것보다 낫다.
    ⚠️ D35-a — 자산이 격자 한쪽으로 치우쳐 있으면 대칭화가 마스크를 **크게** 넓힌다
       (반대편 허공까지 덮는다). `x_symmetry_cost()` 로 미리 재라.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return a.copy()
    return np.unique(np.concatenate([a, mirror_x(a)], axis=0), axis=0)


def x_symmetry_cost(cells: np.ndarray) -> float:
    """대칭화가 마스크를 몇 배로 넓히는가 (D35-a). 1.0 이면 이미 대칭이다.

    W10 은 1.0 이었지만 그건 **우연이다** — 머리가 격자 중앙 근처에 있어서
    `[xlo-w, xhi+w]` 클램프 결과가 마침 합 63 이 됐을 뿐이다. 치우친 자산에서는
    2.0 에 가까워지고, 그만큼 마스크가 허공을 덮는다. 비용이 크면 대칭화 대신
    fiducial 을 쓸지 다시 판단해야 한다.
    """
    a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return 1.0
    return symmetrize_x(a).shape[0] / float(np.unique(a, axis=0).shape[0])


def assert_x_symmetric(cells: np.ndarray, where: str = "") -> None:
    """마스크가 **격자 중심** X 대칭인지 강제한다 (D35-a).

    🔴 D35 는 "마스크를 X 대칭으로 만든다" 로 모호성을 무해화했다. 그런데 그 성질을
    **아무도 검사하지 않으면 우연히 성립하다가 조용히 깨진다** — W10 이 정확히 그
    상태였다. 모듈이 `[xlo-w, xhi+w]` 를 클램프할 뿐 격자 중심 대칭을 강제하지
    않았고, 머리가 중앙에 있어 합이 63 이 됐을 뿐이다.
    """
    if not is_x_symmetric(cells):
        a = np.asarray(cells, dtype=np.int64).reshape(-1, 3)
        lo, hi = int(a[:, 0].min()), int(a[:, 0].max())
        raise ValueError(
            f"{where or '마스크'} 가 격자 중심 X 대칭이 아니다 (D35-a): "
            f"x∈[{lo},{hi}] 이고 lo+hi={lo + hi} 인데 {VOXEL_RES - 1} 이어야 한다. "
            "좌우 대칭 자산에서는 X 반전을 IoU 로 못 가리므로(dragon-c 1·2위 격차 "
            "1.01배) 마스크를 대칭으로 만들어 모호성을 무해화한다. "
            "`symmetrize_x()` 를 쓰거나, 비대칭이 꼭 필요하면 X 를 특정할 "
            "fiducial 을 함께 보내라."
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
