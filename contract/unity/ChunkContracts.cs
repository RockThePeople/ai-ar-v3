// deltacontract — JSON DTO (Unity/C# 구현)
//
// python/deltacontract/schemas.py 와 필드 이름이 1:1 로 일치해야 한다.
// UnityEngine.JsonUtility 는 Dictionary 를 처리하지 못하므로 Newtonsoft 를 쓴다:
//   Package Manager > com.unity.nuget.newtonsoft-json
//
// 배치 위치 제안: Assets/Scripts/DeltaContract/ChunkContracts.cs

using System;
using System.Collections.Generic;
using Newtonsoft.Json;

namespace DeltaContract
{
    [Serializable]
    public class ContractInfo
    {
        [JsonProperty("contract_version")]     public int ContractVersion;
        [JsonProperty("normalized_min")]       public float NormalizedMin;
        [JsonProperty("normalized_max")]       public float NormalizedMax;
        [JsonProperty("voxel_res")]        public int VoxelRes;
        [JsonProperty("chunk_size")]           public int ChunkSize;
        [JsonProperty("chunk_grid_res")]       public int ChunkGridRes;
        [JsonProperty("position_quant_bits")]  public int PositionQuantBits;
        [JsonProperty("slat_channels")]        public int SlatChannels;
        [JsonProperty("mesh_res")]             public int MeshRes;
        [JsonProperty("coord_order")]          public string CoordOrder;
        [JsonProperty("requires_deterministic_algorithms")] public bool RequiresDeterministicAlgorithms;

        /// <summary>
        /// 서버가 다른 계약 상수를 쓰고 있으면 즉시 던진다.
        /// "일단 돌려보고 이상하면 고친다"가 이 영역에서 가장 비싼 실패 모드다.
        /// </summary>
        public void AssertCompatible()
        {
            if (ContractVersion != (int)ChunkBin.ContractVersion)
                throw new ChunkBinException(
                    $"계약 버전 불일치: server={ContractVersion}, client={ChunkBin.ContractVersion}");
            if (VoxelRes != DeltaConstants.VoxelRes ||
                ChunkSize != DeltaConstants.ChunkSize ||
                ChunkGridRes != DeltaConstants.ChunkGridRes ||
                PositionQuantBits != DeltaConstants.PositionQuantBits ||
                SlatChannels != DeltaConstants.SlatChannels ||
                MeshRes != DeltaConstants.MeshRes ||
                CoordOrder != DeltaConstants.CoordOrder ||
                Math.Abs(NormalizedMin - DeltaConstants.NormalizedMin) > 1e-6f ||
                Math.Abs(NormalizedMax - DeltaConstants.NormalizedMax) > 1e-6f)
            {
                throw new ChunkBinException(
                    $"계약 상수 불일치: server(voxel={VoxelRes}, chunk={ChunkSize}, grid={ChunkGridRes}, " +
                    $"quant={PositionQuantBits}, range=[{NormalizedMin},{NormalizedMax}])");
            }
        }
    }

    /// <summary>python/deltacontract/coords.py 의 상수 미러. 바꿀 때 양쪽 같이.</summary>
    public static class DeltaConstants
    {
        public const float NormalizedMin = -0.5f;
        public const float NormalizedMax = 0.5f;
        public const int VoxelRes = 64;
        // 🔴 D75 — 8 → 4. **둘 다 리터럴이다** (주석은 나눗셈처럼 보이지만 계산이 아니다).
        //    하나만 고치면 어긋나고 런타임에만 드러나므로, conformance 의
        //    `mirror_check` 가 이제 **값까지** 파이썬 CONTRACT_CONSTANTS 와 대조한다.
        public const int ChunkSize = 4;
        public const int ChunkGridRes = 16;  // VoxelRes / ChunkSize (계산이 아니라 리터럴)
        public const int PositionQuantBits = 20;

        /// <summary>
        /// API 경로 접두사. ChunkEntry.Uri 에 **이미 포함되어** 온다 —
        /// 클라이언트가 덧붙이면 안 된다. 서버는 deltacontract.chunk_uri() 로만 만든다.
        /// </summary>
        public const string ApiPrefix = "/v2";

        /// <summary>
        /// 최종 URL 조합. Uri 는 선행 슬래시를 포함한 절대 경로이므로 그냥 이어붙인다.
        /// baseUrl 끝의 슬래시만 정리한다.
        /// </summary>
        public static string ResolveUri(string baseUrl, string uri)
        {
            if (string.IsNullOrEmpty(uri)) throw new ChunkBinException("빈 uri");
            if (!uri.StartsWith("/"))
                throw new ChunkBinException(
                    $"uri 는 선행 슬래시를 포함한 절대 경로여야 한다: {uri}");
            return baseUrl.TrimEnd('/') + uri;
        }
        public const int SlatChannels = 8;
        public const int MeshRes = 256;             // FlexiCubes 내부 격자 (VoxelRes x 4)
        public const string CoordOrder = "canonical";

        /// <summary>
        /// NORMALIZED 공간 허용 오차 = 1/(2*MeshRes).
        /// FlexiCubes 경계 정점은 [-0.5,0.5] 를 이만큼 벗어난다 (실측 0.26%, 최대 4.14e-4).
        /// 버그가 아니므로 클라이언트에서 클램프하거나 검증 실패시키지 마라.
        /// </summary>
        public const float NormalizedTolerance = 1.0f / (2 * MeshRes);

        /// <summary>오브젝트 로컬(NORMALIZED) 좌표 -> VOXEL(SLat 64³) 셀 인덱스.</summary>
        public static UnityEngine.Vector3Int ToVoxel(UnityEngine.Vector3 local)
        {
            float span = NormalizedMax - NormalizedMin;
            int Cell(float c) => UnityEngine.Mathf.Clamp(
                UnityEngine.Mathf.FloorToInt((c - NormalizedMin) / span * VoxelRes), 0, VoxelRes - 1);
            return new UnityEngine.Vector3Int(Cell(local.x), Cell(local.y), Cell(local.z));
        }
    }

    [Serializable]
    public class ChunkEntry
    {
        [JsonProperty("uri")]          public string Uri;           // 서버 base URL 에 상대적
        [JsonProperty("hash")]         public string Hash;          // .cbin 전체 sha256
        [JsonProperty("byte_length")]  public int ByteLength;
        [JsonProperty("vertex_count")] public int VertexCount;
        [JsonProperty("index_count")]  public int IndexCount;
        [JsonProperty("voxel_count")]  public int VoxelCount;
        [JsonProperty("version")]      public int Version;          // 캐시 키
    }

    [Serializable]
    public class ChunkManifest
    {
        [JsonProperty("asset_id")] public string AssetId;
        [JsonProperty("version")]  public int Version;
        [JsonProperty("contract")] public ContractInfo Contract;
        [JsonProperty("chunks")]   public Dictionary<string, ChunkEntry> Chunks = new Dictionary<string, ChunkEntry>();

        /// <summary>3.10.0 — 서버가 이 자산을 샘플로 취급하는가 (실효 설정 기준).
        /// false 나 부재면 <b>편집을 보내지 마라</b> — 정상 커밋되어 v1 을 영구히 벗어난다.
        /// "서버가 떴으니 목록이 맞다" 는 추론은 무효다 (드리프트 탈출구가 존재한다).</summary>
        [JsonProperty("is_sample")]
        public bool IsSample = false;
    }

    [Serializable]
    public class PatchPackage
    {
        [JsonProperty("asset_id")]           public string AssetId;
        [JsonProperty("from_version")]       public int FromVersion;
        [JsonProperty("to_version")]         public int ToVersion;
        [JsonProperty("contract")]           public ContractInfo Contract;
        [JsonProperty("changed_chunks")]     public Dictionary<string, ChunkEntry> ChangedChunks = new Dictionary<string, ChunkEntry>();
        [JsonProperty("removed_chunk_ids")]  public List<string> RemovedChunkIds = new List<string>();

        /// <summary>3.9.0 — 샘플 자산 편집. 커밋되지 않았으므로 디스크 캐시에 쓰지 마라.
        /// 메모리에만 적용한다. to_version == from_version 이어도 내용이 있을 수 있다.</summary>
        [JsonProperty("ephemeral")]
        public bool Ephemeral = false;

        /// <summary>3.21.0 — 서버가 실제로 쓴 마스크의 반향. **halo 적용 전** 셀의 지문
        /// (coords.mask_fingerprint). 클라이언트가 보낸 것의 반향이라 도메인 분리를 안 깬다.
        ///
        /// ⚠️ 이 필드가 없던 동안 클라이언트는 "내가 보낸 마스크 == 서버가 쓴 마스크" 를
        ///    확인할 경로가 없었다. voxels 탐침(§32)에서도 없어서 A5000 구간을
        ///    "통과" 가 아니라 "관측 불가" 로 적어야 했다.
        ///    로컬로 같은 값을 계산해 대조하면 그 공백이 닫힌다.</summary>
        [JsonProperty("mask_fingerprint")]  public string MaskFingerprint;

        /// <summary>3.21.0 — halo 적용 **후** 서버가 실제로 마스킹한 셀 수.
        /// A5000 의 mask_rows 원값이 아니라 3090 이 접어 낸 수다 (도메인 분리 유지).</summary>
        [JsonProperty("mask_voxels_used")]  public int? MaskVoxelsUsed;

        /// <summary>3.24.0 — 이 패치를 만든 연산. "edit" | "assemble".
        ///
        /// 🔴 **봉쇄 판정이 연산마다 다르다** (§53). 편집은 마스크가 바뀔 자리를 정의하므로
        ///    마스크 밖 변경이 위반이지만, 조립은 마스크가 **비울 자리만** 정의하고
        ///    기증자 위치는 offset 이 정하므로 마스크 밖 변경이 **정상**이다.
        ///    이 필드가 없던 동안 클라이언트는 호출자 플래그에 의존해야 했다 —
        ///    응답만 보고 판별할 수 없으면 하네스가 조용히 틀린 판정을 낸다(Unity 실측).
        ///
        /// 부재하면 "edit" 로 읽는다 (구 서버 호환).</summary>
        [JsonProperty("op")]  public string Op = "edit";
    }

    [Serializable]
    public class SpatialContext
    {
        [JsonProperty("surface_type")]           public string SurfaceType = "floor"; // floor|table|wall
        [JsonProperty("estimated_footprint_m")]  public float EstimatedFootprintM = 1.0f;
    }

    [Serializable]
    public class GenerateRequest
    {
        [JsonProperty("session_id")]      public string SessionId;
        [JsonProperty("raw_prompt")]      public string RawPrompt;
        [JsonProperty("spatial_context")] public SpatialContext SpatialContext = new SpatialContext();
        [JsonProperty("seed")]            public int Seed = 42;
    }

    /// <summary>
    /// 편집 마스크. as-built 라쏘(Direction A)는 mode="voxels" 로 보낸다.
    /// bbox 는 단순 박스 선택용 대체 경로.
    /// </summary>
    /// <summary>`GET /v2/assets/{id}/slat_coords.v{n}.json` 응답 (3.26.0 · W17).
    ///
    /// 🔴 <b>라쏘가 투영할 대상.</b> 정점이 아니라 <b>복셀</b>을 투영해야 결과가 곧바로
    /// SLat 마스크가 된다 (D58). `.cbin` 에는 slat coords 가 없다 (D34).</summary>
    [Serializable]
    public class SlatCoordsResponse
    {
        [JsonProperty("asset_id")]    public string AssetId;
        [JsonProperty("version")]     public int Version;
        [JsonProperty("grid_source")] public string GridSource = "slat_coords";
        [JsonProperty("voxel_res")]   public int VoxelRes = DeltaConstants.VoxelRes;
        [JsonProperty("n_cells")]     public int NCells;
        [JsonProperty("coords")]      public List<int[]> Coords = new List<int[]>();
        [JsonProperty("fingerprint")] public string Fingerprint;

        /// <summary>받은 목록이 잘리지 않았는지. 잘린 목록도 형태는 멀쩡해서 예외를 안 낸다.</summary>
        public void Validate()
        {
            if (Coords == null || Coords.Count != NCells)
                throw new InvalidOperationException(
                    $"n_cells({NCells}) 와 coords 길이({Coords?.Count ?? 0}) 가 다르다.");
            if (GridSource != "slat_coords")
                throw new InvalidOperationException(
                    $"격자 출처가 정본이 아니다: {GridSource} (D28-a). 편집에 쓰지 마라.");
        }

        public List<UnityEngine.Vector3Int> ToCells()
        {
            var outp = new List<UnityEngine.Vector3Int>(Coords.Count);
            foreach (var c in Coords) outp.Add(new UnityEngine.Vector3Int(c[0], c[1], c[2]));
            return outp;
        }
    }

    [Serializable]
    public class EditMask
    {
        [JsonProperty("mode")]                public string Mode;      // "bbox" | "voxels"
        [JsonProperty("bbox_min", NullValueHandling = NullValueHandling.Ignore)] public float[] BboxMin;
        [JsonProperty("bbox_max", NullValueHandling = NullValueHandling.Ignore)] public float[] BboxMax;
        [JsonProperty("voxels",   NullValueHandling = NullValueHandling.Ignore)] public List<int[]> Voxels;
        // 🔴 D75 에서 1 → 2 (청크 4복셀). 실측: halo=1 이 바뀐 청크를 놓친다.
        [JsonProperty("halo_margin_voxels")]  public int HaloMarginVoxels = 2;

        /// <summary>🔴 3.26.0 (D28-a) — 이 셀들이 어느 격자에서 나왔는가.
        /// "slat_coords"(정본) 또는 "surface_voxelize"(진단용). <b>mode="voxels" 에서는
        /// 생략할 수 없다</b> — 기본값으로 메우면 잘못된 격자가 침묵으로 정본을 참칭하고,
        /// 그때는 예외가 안 나면서 마스크·조립·지표가 전부 다른 물체에 대해 동작한다.</summary>
        [JsonProperty("grid_source", NullValueHandling = NullValueHandling.Ignore)]
        public string GridSource;

        /// <param name="gridSource">라쏘(SlatLassoPicker) 산출물이면 "slat_coords".
        /// 기본값을 두지 않는 것이 요점이다 — 부르는 쪽이 매번 답하게 한다.</param>
        public static EditMask FromVoxels(
            IEnumerable<UnityEngine.Vector3Int> cells, string gridSource, int halo = 2)
        {
            if (string.IsNullOrEmpty(gridSource))
                throw new ArgumentException(
                    "grid_source 가 필요하다 (D28-a). 라쏘 산출물이면 \"slat_coords\" 다.",
                    nameof(gridSource));
            var list = new List<int[]>();
            foreach (var c in cells) list.Add(new[] { c.x, c.y, c.z });
            return new EditMask
            {
                Mode = "voxels", Voxels = list, HaloMarginVoxels = halo, GridSource = gridSource,
            };
        }
    }

    [Serializable]
    public class EditRequest
    {
        [JsonProperty("session_id")]      public string SessionId;
        [JsonProperty("base_version")]    public int BaseVersion;
        [JsonProperty("raw_prompt")]      public string RawPrompt;
        [JsonProperty("mask")]            public EditMask Mask;
        [JsonProperty("seed")]            public int Seed = 42;
        // 재시도 시 같은 값을 유지해야 서버가 중복 커밋을 막을 수 있다 (FINAL §10-1).
        [JsonProperty("idempotency_key")] public string IdempotencyKey;
    }

    /// <summary>
    /// 3.24.0 — Unity → 3090 **조립** 요청. `BAssembleRequest`(3090→A5000)와 필드명이
    /// 같지만 **같은 타입이 아니다** — `asset_id` 의 의미와 인증 경계가 다르다
    /// (`EditRequest`/`BEditRequest` 가 이미 그렇게 갈려 있다).
    ///
    /// ⚠️ 스케일 인자가 **없다.** 좌표 확대가 인접성을 파괴한다(6-이웃 유지율 s=2.0 에서 0%).
    ///    크기는 `donor_crop_fraction` 으로만 고른다.
    /// </summary>
    [Serializable]
    public class AssembleRequest
    {
        [JsonProperty("session_id")]           public string SessionId;
        [JsonProperty("base_version")]         public int BaseVersion;
        [JsonProperty("donor_asset_id")]       public string DonorAssetId;
        [JsonProperty("donor_crop_fraction")]  public float DonorCropFraction = 0.4f;
        [JsonProperty("donor_crop_axis")]      public int DonorCropAxis = 2;      // 0=x 1=y 2=z
        [JsonProperty("donor_crop_keep")]      public string DonorCropKeep = "top";
        // VOXEL 격자의 **정수** 평행이동. null 이면 서버가 fit_offset 으로 정한다.
        [JsonProperty("offset", NullValueHandling = NullValueHandling.Ignore)] public int[] Offset;
        // 대상에서 **비울** 영역. 기증자 위치를 정하는 것이 아니다 (§53).
        [JsonProperty("mask")]                 public EditMask Mask;
        [JsonProperty("idempotency_key")]      public string IdempotencyKey;
    }

    [Serializable]
    public class JobStatus
    {
        [JsonProperty("job_id")]     public string JobId;
        [JsonProperty("state")]      public string State;     // queued|running|succeeded|failed
        [JsonProperty("asset_id")]   public string AssetId;
        [JsonProperty("progress")]   public float Progress;
        [JsonProperty("stage")]        public string Stage;
        // 어휘 밖 세부. 분기하지 말고 표시만.
        [JsonProperty("stage_detail")] public string StageDetail;
        [JsonProperty("manifest")]   public ChunkManifest Manifest;  // 최초 생성 성공 시
        [JsonProperty("patch")]      public PatchPackage Patch;      // 편집 성공 시
        [JsonProperty("error")]      public string Error;
        [JsonProperty("error_code")] public string ErrorCode;
        // 서버가 자동 발급한 값. 잡을 잃고 재요청할 때 **같은 값을 다시 보내야**
        // 중복 커밋이 안 난다. 저장해 두었다가 재시도에 실어라.
        [JsonProperty("idempotency_key")] public string IdempotencyKey;

        public bool IsTerminal => State == "succeeded" || State == "failed";
    }

    /// <summary>
    /// GET /v2/health. `Ok` 는 **서버가 응답 가능한가**만 뜻한다 —
    /// 업스트림(A5000)이 죽어도 200 + Ok=true 로 오고 UpstreamOk 가 false 다.
    /// </summary>
    [Serializable]
    public class ServerHealth
    {
        [JsonProperty("ok")]             public bool Ok;
        [JsonProperty("contract")]       public ContractInfo Contract;

        // 돌고 있는 코드가 무엇인가 (3.16.0).
        // "테스트가 통과했다" 와 "그 코드가 떠 있다" 는 다른 사실이다 —
        // 커밋만 하고 프로세스를 재기동 안 하면 와이어는 옛 동작인데 예외가 안 난다.
        // StartedAt 을 커밋 시각과 비교하면 "고친 뒤에 떴는가" 가 한 줄로 판정된다.
        [JsonProperty("build")]          public string Build;
        [JsonProperty("build_dirty")]    public bool? BuildDirty;   // null=모름 / false=깨끗
        // 3.23.0 — untracked 파일 수. build_dirty 와 **다른 사실**이다:
        // 스크래치 파일 하나가 dirty 를 영구히 켜두면 그 경보는 의미를 잃는다(§25).
        [JsonProperty("build_untracked")] public int? BuildUntracked;
        [JsonProperty("started_at")]     public string StartedAt;

        [JsonProperty("upstream_ok")]    public bool UpstreamOk;
        // A5000 의 BHealth 원문. Unity 는 진단 표시 외에 쓸 일이 없어 느슨하게 받는다.
        [JsonProperty("upstream")]       public Dictionary<string, object> Upstream;
        [JsonProperty("upstream_error")] public string UpstreamError;
        [JsonProperty("jobs")]           public Dictionary<string, int> Jobs;
    }

    /// <summary>
    /// 서버가 내는 모든 4xx/5xx 본문. 401·404·422 포함해 **한 가지 모양**이다 —
    /// 파서를 두 벌 들지 마라.
    /// </summary>
    [Serializable]
    public class ErrorBody
    {
        [JsonProperty("error_code")] public string ErrorCode;
        [JsonProperty("message")]    public string Message;
        [JsonProperty("detail")]     public Dictionary<string, string> Detail;
    }
}
