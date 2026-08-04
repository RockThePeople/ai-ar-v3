// SlatLassoPicker — 화면 라쏘 → **SLat 복셀 마스크** (D57 · D58)
//
// 🔴 D58: **정점이 아니라 복셀을 투영한다.**
//    LassoVolume.cs 머리말의 1) 단계는 "정점/점유 셀" 을 투영하라고 적혀 있지만,
//    정점을 투영하면 결과가 **메시 정점 집합**이라 SLat 마스크가 아니다. 그걸
//    복셀로 되돌리려면 격자 역산이 필요하고, 그 역산은 이 프로젝트에서 두 번
//    실패했다 (D34 — `.cbin` 에는 slat coords 가 없다).
//    ⇒ 입력을 **처음부터 slat coords** 로 받는다. 그러면 역산이 아예 없다.
//
// ⚠️ LassoVolume 은 **재구현하지 않는다.** 이 파일은 그 머리말이 "호출부 책임" 이라고
//    적어 둔 1)투영 · 3)교집합·클램프 만 한다. 판정 자체(PointInPolygon ·
//    DominantAxis · SolidifyAlongAxis)는 전부 계약의 코드를 그대로 부른다.
//
// ⚠️ halo 는 **서버가 한다** (LassoVolume 머리말 4번). 여기서 팽창시키지 않는다.
//
// 카메라 타입을 받지 않고 **투영 델리게이트**를 받는다:
//   · Unity      : local => cam.WorldToScreenPoint(tr.TransformPoint(local))
//   · 헤드리스    : local => 명시적 MVP 행렬
// 그래서 엔진 없이 같은 코드를 검증할 수 있다.

using System;
using System.Collections.Generic;
using System.Security.Cryptography;

namespace DeltaContract
{
    /// <summary>라쏘 판정 결과. **셀 수를 단계별로 들고 다닌다** — 어느 단계가
    /// 밥값을 했는지 스스로 증명하게 하려는 것이다 (LassoVolume 머리말의 요구).</summary>
    public sealed class LassoMaskResult
    {
        public List<UnityEngine.Vector3Int> Cells = new List<UnityEngine.Vector3Int>();
        public string GridSource = SlatLassoPicker.SlatCoords;   // 🔴 D28-a
        public int Projected;        // 투영한 slat 복셀 수
        public int BehindCamera;     // 카메라 뒤라 버린 수
        public int InPolygon;        // 폴리곤 안에 든 후보 수
        public int AfterSolidify;    // 압출 후 (빈 내부 포함)
        public int SolidifyAdded;    // 압출이 새로 넣은 수
        public int IntersectRemoved; // 점유 교집합이 도로 지운 수  ← 껍질 표현이면 크다
        public int DominantAxis;
        public string Fingerprint = "";
    }

    public static class SlatLassoPicker
    {
        public const string SlatCoords = "slat_coords";

        // 🔴 계약 상수의 **복사본**이다 (contract/unity/ChunkContracts.cs DeltaConstants).
        //    직접 참조하지 않는 이유는 그쪽이 Newtonsoft 에 의존해서 헤드리스 검증이
        //    막히기 때문이다. 두 곳에 있는 것은 위험하므로 **드리프트는 테스트가 막는다**
        //    (server/tests/test_lasso.py::test_picker_constants_match_the_contract).
        public const int VoxelRes = 64;
        public const float NormalizedMin = -0.5f;
        public const float NormalizedMax = 0.5f;

        /// <summary>VOXEL 셀 → 그 셀 **중심**의 NORMALIZED 로컬 좌표.</summary>
        public static UnityEngine.Vector3 VoxelCenter(UnityEngine.Vector3Int c)
        {
            float span = NormalizedMax - NormalizedMin;
            return new UnityEngine.Vector3(
                (c.x + 0.5f) / VoxelRes * span + NormalizedMin,
                (c.y + 0.5f) / VoxelRes * span + NormalizedMin,
                (c.z + 0.5f) / VoxelRes * span + NormalizedMin);
        }

        /// <summary>화면 폴리곤 → slat 복셀 마스크.</summary>
        /// <param name="slatCoords">🔴 자산의 **slat coords**. 메시 정점이 아니다 (D58).</param>
        /// <param name="polygon">화면 좌표 폴리곤 (드래그 궤적).</param>
        /// <param name="localToScreen">로컬(NORMALIZED) → 화면. z ≤ 0 이면 카메라 뒤.</param>
        /// <param name="viewDirLocal">카메라 전방을 **오브젝트 프레임으로** 옮긴 벡터.</param>
        public static LassoMaskResult Pick(
            IEnumerable<UnityEngine.Vector3Int> slatCoords,
            List<UnityEngine.Vector2> polygon,
            Func<UnityEngine.Vector3, UnityEngine.Vector3> localToScreen,
            UnityEngine.Vector3 viewDirLocal)
        {
            if (slatCoords == null) throw new ArgumentNullException(nameof(slatCoords));
            if (localToScreen == null) throw new ArgumentNullException(nameof(localToScreen));

            var occupied = new HashSet<UnityEngine.Vector3Int>();
            foreach (var c in slatCoords) occupied.Add(c);

            var r = new LassoMaskResult { Projected = occupied.Count };

            // ── 1) 투영 + 폴리곤 판정. **계약의 PointInPolygon 을 그대로 쓴다.**
            var candidates = new List<UnityEngine.Vector3Int>();
            foreach (var c in occupied)
            {
                var sp = localToScreen(VoxelCenter(c));
                if (sp.z <= 0f) { r.BehindCamera++; continue; }   // 카메라 뒤는 버린다
                if (LassoVolume.PointInPolygon(new UnityEngine.Vector2(sp.x, sp.y), polygon))
                    candidates.Add(c);
            }
            r.InPolygon = candidates.Count;

            // ── 2) 압출. 앞뒤 껍질 사이를 채운다 = "부피".
            r.DominantAxis = LassoVolume.DominantAxis(viewDirLocal);
            var solid = LassoVolume.SolidifyAlongAxis(candidates, r.DominantAxis);
            r.AfterSolidify = solid.Count;
            r.SolidifyAdded = solid.Count - candidates.Count;

            // ── 3) 점유 교집합 + 격자 클램프. 압출의 최악(빈 공간 선택)을 막는다.
            var final = new HashSet<UnityEngine.Vector3Int>();
            foreach (var c in solid)
            {
                if (c.x < 0 || c.y < 0 || c.z < 0) continue;
                if (c.x >= VoxelRes || c.y >= VoxelRes || c.z >= VoxelRes) continue;
                if (occupied.Contains(c)) final.Add(c);
            }
            r.IntersectRemoved = solid.Count - final.Count;

            // ── 4) halo 는 서버가 한다. 여기서 팽창시키지 않는다.
            r.Cells = new List<UnityEngine.Vector3Int>(final);
            r.Cells.Sort((a, b) => Morton(a).CompareTo(Morton(b)));
            r.Fingerprint = MaskFingerprint(r.Cells);
            return r;
        }

        /// <summary>계약의 `deltacontract.coords.mask_fingerprint` 와 **같은 값**이어야 한다:
        /// sha256( canonical_sort(cells).astype(int32).tobytes() ).
        /// 양쪽이 각자 직렬화하면 어긋나도 예외가 안 나고 "지문 불일치" 로만 보인다.</summary>
        public static string MaskFingerprint(IEnumerable<UnityEngine.Vector3Int> cells)
        {
            var list = new List<UnityEngine.Vector3Int>(cells);
            list.Sort((a, b) => Morton(a).CompareTo(Morton(b)));
            var bytes = new byte[list.Count * 12];
            for (int i = 0; i < list.Count; i++)
            {
                Buffer.BlockCopy(BitConverter.GetBytes(list[i].x), 0, bytes, i * 12, 4);
                Buffer.BlockCopy(BitConverter.GetBytes(list[i].y), 0, bytes, i * 12 + 4, 4);
                Buffer.BlockCopy(BitConverter.GetBytes(list[i].z), 0, bytes, i * 12 + 8, 4);
            }
            using (var sha = SHA256.Create())
            {
                var h = sha.ComputeHash(bytes);
                var sb = new System.Text.StringBuilder(h.Length * 2);
                foreach (var b in h) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }

        /// <summary>Morton 코드 (계약의 `morton3` 와 동일한 21비트 인터리브).</summary>
        public static ulong Morton(UnityEngine.Vector3Int c) =>
            Spread((ulong)c.x) | (Spread((ulong)c.y) << 1) | (Spread((ulong)c.z) << 2);

        static ulong Spread(ulong v)
        {
            v &= 0x1FFFFFUL;
            v = (v | (v << 32)) & 0x1F00000000FFFFUL;
            v = (v | (v << 16)) & 0x1F0000FF0000FFUL;
            v = (v | (v << 8))  & 0x100F00F00F00F00FUL;
            v = (v | (v << 4))  & 0x10C30C30C30C30C3UL;
            v = (v | (v << 2))  & 0x1249249249249249UL;
            return v;
        }
    }
}
