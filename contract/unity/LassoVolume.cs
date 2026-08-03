// LassoVolume — 화면 자유곡선(2DOF) → 3D 부피 마스크
//
// 근거: Yu, Efstathiou, Isenberg, Isenberg,
//   "Efficient Structure-Aware Selection Techniques for 3D Point Cloud
//    Visualizations with 2DOF Input" (CloudLasso / TeddySelection), IEEE TVCG 2012
//   https://hal.science/hal-00718310
//
// 제목의 **2DOF** 가 핵심이다 — 폰 터치는 x,y 뿐이고 깊이가 없다.
// 논문이 상정한 입력 조건이 그것과 정확히 같다.
//
// ⚠️ 여기 구현한 것은 논문의 밀도 기반 곡면 산출이 **아니라**, 논문이 비교군으로
//    삼아 이긴 "카메라 방향 압출" 쪽이다. 점유 셀과 교집합(호출부 책임)해서
//    압출의 최악(빈 공간까지 선택)을 막는 것을 전제로 한다.
//
// 사용 순서:
//   1) 오브젝트 정점/점유 셀을 화면에 투영해 PointInPolygon 으로 후보를 고른다
//      (카메라 뒤 sp.z <= 0 은 버린다. 앞뒤 둘 다 잡는 "관통"이 의도다)
//   2) SolidifyAlongAxis 로 앞뒤 껍질 사이를 채운다        ← 이게 "부피"다
//   3) 점유 셀과 교집합 + 격자 범위 [0,RES) 로 클램프
//   4) halo 팽창은 **서버가 한다** (deltacontract.coords.dilate_cells)
//      클라가 미리 팽창시키면 "서버가 무엇을 건드릴지" 를 클라가 예측해서
//      검증하는 봉쇄 검사가 깨진다.
//
// ⚠️ 실측 주의: SLat 같은 **껍질 표현**(두께 중앙값 3복셀)에서는 "빈 내부"에
//    데이터가 없어서, 2)가 넣은 셀을 3)의 교집합이 도로 지운다. 버그가 아니다.
//    2)가 실제로 일하는 곳은 **오목한 껍질**뿐이다. 교집합이 몇 개를 지웠는지
//    로그로 찍으면 이 단계가 밥값을 하는지 스스로 증명한다.
//
// 전부 static 이고 카메라를 안 받는다 — 테스트에서 엔진 없이 부를 수 있다.

using System.Collections.Generic;
using UnityEngine;

namespace DeltaContract
{
    public static class LassoVolume
    {
        /// <summary>주어진 방향(이미 대상 프레임으로 변환됨)에서 가장 정렬된 축(0=x,1=y,2=z).</summary>
        public static int DominantAxis(Vector3 dir)
        {
            var ax = Mathf.Abs(dir.x);
            var ay = Mathf.Abs(dir.y);
            var az = Mathf.Abs(dir.z);
            if (ax >= ay && ax >= az) return 0;
            return ay >= az ? 1 : 2;
        }

        /// <summary>스크린 폴리곤 내부 판정(even-odd ray casting).</summary>
        public static bool PointInPolygon(Vector2 p, List<Vector2> poly)
        {
            if (poly == null || poly.Count < 3) return false;
            bool inside = false;
            for (int i = 0, j = poly.Count - 1; i < poly.Count; j = i++)
            {
                var a = poly[i];
                var b = poly[j];
                if (((a.y > p.y) != (b.y > p.y)) &&
                    (p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x))
                    inside = !inside;
            }
            return inside;
        }

        /// <summary>
        /// 같은 (다른 두 축) 열에서 depthAxis 값의 min..max 를 전부 채운다.
        /// 표면 셀만 든 후보를 앞뒤 껍질 사이가 꽉 찬 부피로 만든다 — 빈 내부도 포함.
        /// </summary>
        public static List<Vector3Int> SolidifyAlongAxis(IEnumerable<Vector3Int> cells, int axis)
        {
            var min = new Dictionary<Vector2Int, int>();
            var max = new Dictionary<Vector2Int, int>();
            foreach (var c in cells)
            {
                var key = OtherTwo(c, axis);
                var d = Component(c, axis);
                if (!min.TryGetValue(key, out var lo)) { min[key] = d; max[key] = d; }
                else
                {
                    if (d < lo) min[key] = d;
                    if (d > max[key]) max[key] = d;
                }
            }

            var result = new List<Vector3Int>();
            foreach (var kv in min)
            {
                var hi = max[kv.Key];
                for (var d = kv.Value; d <= hi; d++) result.Add(Compose(kv.Key, axis, d));
            }
            return result;
        }

        /// <summary>26-연결 형태학적 팽창 반경 r (Chebyshev ≤ r → (2r+1)³ 이웃).
        /// ⚠️ 보통은 쓰지 마라 — halo 는 서버가 한다(위 4번). 참고용으로만 남긴다.
        /// 그리고 이 함수는 **격자 범위를 안 본다.** 경계 셀에서 범위 밖 좌표가 나오고,
        /// 실측에서 그게 서버의 쓰기 경로를 터뜨린 적이 있다. 호출 후 반드시 클램프해라.</summary>
        public static HashSet<Vector3Int> Dilate(IEnumerable<Vector3Int> cells, int r)
        {
            var result = new HashSet<Vector3Int>();
            foreach (var c in cells)
                for (var dx = -r; dx <= r; dx++)
                for (var dy = -r; dy <= r; dy++)
                for (var dz = -r; dz <= r; dz++)
                    result.Add(new Vector3Int(c.x + dx, c.y + dy, c.z + dz));
            return result;
        }

        static Vector2Int OtherTwo(Vector3Int c, int axis) =>
            axis == 0 ? new Vector2Int(c.y, c.z) :
            axis == 1 ? new Vector2Int(c.x, c.z) :
                        new Vector2Int(c.x, c.y);

        static int Component(Vector3Int c, int axis) => axis == 0 ? c.x : axis == 1 ? c.y : c.z;

        static Vector3Int Compose(Vector2Int key, int axis, int d) =>
            axis == 0 ? new Vector3Int(d, key.x, key.y) :
            axis == 1 ? new Vector3Int(key.x, d, key.y) :
                        new Vector3Int(key.x, key.y, d);
    }
}
