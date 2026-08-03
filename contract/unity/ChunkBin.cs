// deltacontract — .cbin 파서 (Unity/C# 구현)
//
// python/deltacontract/chunkbin.py 가 정본이고 이 파일은 두 번째 구현이다.
// 둘은 conformance/golden/ 의 골든 벡터로 바이트 단위 대조된다.
// 포맷을 바꾸면 **반드시 두 파일을 같이** 바꾸고 골든을 재생성할 것.
//
// 배치 위치 제안: Assets/Scripts/DeltaContract/ChunkBin.cs
// 의존성: 없음 (Newtonsoft 는 ChunkContracts.cs 쪽에서만 필요)

using System;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using UnityEngine;

namespace DeltaContract
{
    public sealed class ChunkBinException : Exception
    {
        public ChunkBinException(string message) : base(message) { }
    }

    /// <summary>.cbin 한 개를 디코딩한 결과. 배열들은 이미 canonical 순서다.</summary>
    public sealed class ChunkMeshData
    {
        public Vector3Int ChunkCoord;
        public Vector3[] Positions;
        public Vector3[] Normals;   // null 가능
        public Color32[] Colors;    // null 가능
        public Vector2[] Uvs;       // null 가능
        public int[] Indices;
        public int VoxelCount;

        public string Key => $"{ChunkCoord.x}_{ChunkCoord.y}_{ChunkCoord.z}";
        public int VertexCount => Positions.Length;
    }

    public static class ChunkBin
    {
        // python 쪽 coords.CONTRACT_VERSION 과 반드시 같아야 한다.
        public const uint ContractVersion = 3;

        public const int HeaderSize = 40;
        private const uint Magic = 0x314E4243; // "CBN1" little-endian

        private const uint FlagNormal = 1u << 0;
        private const uint FlagColor  = 1u << 1;
        private const uint FlagUv     = 1u << 2;

        public static ChunkMeshData Decode(byte[] blob)
        {
            if (blob == null || blob.Length < HeaderSize)
                throw new ChunkBinException($"헤더보다 짧다: {(blob == null ? 0 : blob.Length)} bytes");
            if (!BitConverter.IsLittleEndian)
                throw new ChunkBinException("빅엔디언 플랫폼은 지원하지 않는다. 포맷은 리틀엔디언 고정이다.");

            uint magic = BitConverter.ToUInt32(blob, 0);
            if (magic != Magic)
                throw new ChunkBinException($"magic 불일치: 0x{magic:X8}");

            uint version = BitConverter.ToUInt32(blob, 4);
            if (version != ContractVersion)
                throw new ChunkBinException($"계약 버전 불일치: file={version}, client={ContractVersion}");

            uint flags = BitConverter.ToUInt32(blob, 8);
            int cx = BitConverter.ToInt32(blob, 12);
            int cy = BitConverter.ToInt32(blob, 16);
            int cz = BitConverter.ToInt32(blob, 20);
            int v  = (int)BitConverter.ToUInt32(blob, 24);
            int i  = (int)BitConverter.ToUInt32(blob, 28);
            int voxelCount = (int)BitConverter.ToUInt32(blob, 32);

            if (v < 0 || i < 0 || i % 3 != 0)
                throw new ChunkBinException($"헤더 카운트가 부정합: V={v}, I={i}");

            long expected = HeaderSize
                          + (long)v * 12
                          + ((flags & FlagNormal) != 0 ? (long)v * 12 : 0)
                          + ((flags & FlagColor)  != 0 ? (long)v * 4  : 0)
                          + ((flags & FlagUv)     != 0 ? (long)v * 8  : 0)
                          + (long)i * 4;
            if (expected != blob.Length)
                throw new ChunkBinException($"길이 불일치: expected={expected}, actual={blob.Length}");

            int off = HeaderSize;
            var data = new ChunkMeshData
            {
                ChunkCoord = new Vector3Int(cx, cy, cz),
                VoxelCount = voxelCount,
            };

            data.Positions = ReadVector3(blob, ref off, v);
            if ((flags & FlagNormal) != 0) data.Normals = ReadVector3(blob, ref off, v);
            if ((flags & FlagColor)  != 0) data.Colors  = ReadColor32(blob, ref off, v);
            if ((flags & FlagUv)     != 0) data.Uvs     = ReadVector2(blob, ref off, v);
            data.Indices = ReadInt32(blob, ref off, i);

            return data;
        }

        // Blittable 복사. Buffer.BlockCopy 는 배열 간 바이트 복사라 Vector3[] 로 바로 못 간다.
        // Marshal.Copy 로 float[] 을 거치는 대신 unsafe 없이 안전하게 처리한다.
        private static Vector3[] ReadVector3(byte[] blob, ref int off, int count)
        {
            var tmp = new float[count * 3];
            Buffer.BlockCopy(blob, off, tmp, 0, count * 12);
            off += count * 12;
            var outArr = new Vector3[count];
            for (int k = 0; k < count; k++)
                outArr[k] = new Vector3(tmp[k * 3], tmp[k * 3 + 1], tmp[k * 3 + 2]);
            return outArr;
        }

        private static Vector2[] ReadVector2(byte[] blob, ref int off, int count)
        {
            var tmp = new float[count * 2];
            Buffer.BlockCopy(blob, off, tmp, 0, count * 8);
            off += count * 8;
            var outArr = new Vector2[count];
            for (int k = 0; k < count; k++)
                outArr[k] = new Vector2(tmp[k * 2], tmp[k * 2 + 1]);
            return outArr;
        }

        private static Color32[] ReadColor32(byte[] blob, ref int off, int count)
        {
            var outArr = new Color32[count];
            for (int k = 0; k < count; k++)
            {
                int b = off + k * 4;
                outArr[k] = new Color32(blob[b], blob[b + 1], blob[b + 2], blob[b + 3]);
            }
            off += count * 4;
            return outArr;
        }

        private static int[] ReadInt32(byte[] blob, ref int off, int count)
        {
            var outArr = new int[count];
            Buffer.BlockCopy(blob, off, outArr, 0, count * 4);
            off += count * 4;
            return outArr;
        }

        /// <summary>다운로드한 바이트가 매니페스트의 hash 와 맞는지 검증.</summary>
        public static string Sha256Hex(byte[] blob)
        {
            using (var sha = SHA256.Create())
            {
                var h = sha.ComputeHash(blob);
                var sb = new StringBuilder(h.Length * 2);
                foreach (var b in h) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }

        /// <summary>
        /// FINAL §4.4 patch-in-place 의 핵심 한 줄. GameObject 를 파괴하지 않고
        /// 기존 Mesh 의 내용만 갈아끼운다 — 앵커링·물리·스크립트 참조가 유지된다.
        ///
        /// 주의: 정점 수가 줄어드는 패치에서 SetVertices 를 먼저 부르면 기존
        /// triangles 가 범위를 벗어나 예외가 난다. 그래서 Clear() 가 먼저다.
        /// </summary>
        public static void ApplyTo(Mesh mesh, ChunkMeshData data, bool markNoLongerReadable = false)
        {
            if (mesh == null) throw new ArgumentNullException(nameof(mesh));

            mesh.Clear(keepVertexLayout: false);
            mesh.indexFormat = data.VertexCount > 65535
                ? UnityEngine.Rendering.IndexFormat.UInt32
                : UnityEngine.Rendering.IndexFormat.UInt16;

            mesh.SetVertices(data.Positions);
            if (data.Normals != null) mesh.SetNormals(data.Normals);
            if (data.Colors  != null) mesh.SetColors(data.Colors);
            if (data.Uvs     != null) mesh.SetUVs(0, data.Uvs);
            mesh.SetTriangles(data.Indices, 0, calculateBounds: false);

            if (data.Normals == null) mesh.RecalculateNormals();
            mesh.RecalculateBounds();
            mesh.UploadMeshData(markNoLongerReadable);
        }
    }
}
