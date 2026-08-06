// AssetScale — 64³ 오브젝트가 **실세계에서 몇 미터인가**. 이 값의 유일한 정의다.
//
// 🔴 ai-ar-v2 §3 불변식이 생긴 이유가 이것이다: 스케일이 두 곳에 있으면 한쪽만 고쳐도
//    **예외가 안 나고** 서버는 1 m 를 전제로 만들고 클라이언트는 0.5 m 로 그린다.
//    자산이 NORMALIZED [-0.5,0.5]³ 라 형태는 멀쩡해 보이고 디테일 밀도만 조용히 어긋난다.
//
// 값의 근거: v2 실측에서 **0.24 m** 가 책상 위 손바닥 크기로 적당했다. 그보다 크면
// 책상에서 넘치고, 작으면 편집 대상을 손가락으로 못 고른다.
//
// ⚠️ 서버에 보내는 `SpatialContext.estimated_footprint_m` 와 **같은 값**이어야 한다
//    (계약 3.15.3: "클라이언트가 실제로 렌더링하는 값을 그대로 실어라").

using UnityEngine;

namespace DeltaContract
{
    public static class AssetScale
    {
        /// <summary>자산 한 변의 월드 크기(m). NORMALIZED 1.0 이 이 길이가 된다.</summary>
        public const float FootprintMeters = 0.24f;

        /// <summary>자산 루트에 넣을 스케일.</summary>
        public static Vector3 RootScale => Vector3.one * FootprintMeters;

        /// <summary>복셀 한 칸의 월드 크기(m). 브러시·허용오차를 미터로 말할 때 쓴다.</summary>
        public static float VoxelMeters => FootprintMeters / DeltaConstants.VoxelRes;
    }
}
