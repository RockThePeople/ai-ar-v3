// VoxelFrame — 복셀 프레임 ↔ Unity 프레임. **이 변환의 유일한 정의다** (D9).
//
// 🔴 W22 에서 화면과 판정이 다른 프레임이라 오토바이가 90° 누워 보였다. 그때
//    변환이 두 곳(적용기·컨트롤러)에 있었다. 세 번째가 생기려는 순간 여기로 모은다 —
//    "매직넘버를 흩뿌리지 말고 이 상수를 참조한다" (CLAUDE.md D9).
//
//     GLB_TO_VOXEL : voxel = (x, −z, y)      (server/pipeline/frames.py 가 정본)
//     역변환       : unity = (vx, vz, −vy)
//
// 판정용 slat coords 도, 표시용 `.cbin` 정점도 **이 함수만** 탄다.

using UnityEngine;

namespace DeltaContract
{
    public static class VoxelFrame
    {
        public static Vector3 ToUnity(Vector3 v) => new Vector3(v.x, v.z, -v.y);
        public static Vector3 ToVoxel(Vector3 v) => new Vector3(v.x, -v.z, v.y);
    }
}
