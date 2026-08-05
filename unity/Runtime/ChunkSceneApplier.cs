// ChunkSceneApplier — `.cbin` 델타를 **오브젝트를 내리지 않고** 씬에 반영한다 (W21 · D70).
//
// 🔴 사용자가 목표를 재정의했다 (D70): **전송 절감은 문턱이 아니다.**
//    "in-place 에서 오브젝트를 내릴 필요 없이 부분 교체가 가능하면 된다" 가 목표다.
//    ⇒ 이 파일이 재는 것은 절감률이 아니라 **무엇이 살아남았는가**다.
//
//    changed → Mesh 만 교체, GameObject 유지          ✅
//    added   → GameObject 신규 생성                    ⚠️
//    removed → GameObject **파괴**                     🔴 in-place 품질을 가르는 자리
//
// ⚠️ DESIGN_INTENT §3-E — **removed 는 파괴 + 사전에서 제거**다. 사전에 남겨 두면
//    다음 패치가 이미 파괴된 MeshFilter 에 `ApplyTo` 를 걸고, Unity 의 가짜 null
//    때문에 **예외가 안 나면서** 아무 일도 일어나지 않는다. 그 조용한 실패를 막으려고
//    파괴 직후 사전에서 지우고, 없는 키에 대한 changed 는 **세어서 표면에 올린다.**
//
// ⚠️ ChunkBin 은 재구현하지 않는다. 디코딩·ApplyTo 는 전부 계약 코드가 한다.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using UnityEngine;

namespace DeltaContract
{
    /// <summary>씬에 있는 청크 하나. **EntityId 가 유지되는지**가 이 웨이브의 지표다.</summary>
    public sealed class ChunkNode
    {
        public string Key;
        public GameObject Go;
        public MeshFilter Filter;
        public Mesh Mesh;
        // 🔴 Unity 6.5 는 `GetInstanceID()` 를 폐기하고 `GetEntityId()` 를 준다.
        //    int 로 내려받는 암묵 변환도 폐기 예정이라 **EntityId 타입 그대로** 들고 다닌다 —
        //    int 로 좁히면 미래에 다른 두 오브젝트가 같은 값으로 보일 수 있고,
        //    그때 "유지됐다" 는 판정이 조용히 거짓이 된다.
        public EntityId EntityId;
        public int ContentHash;       // 현재 Mesh 내용의 해시 — "정말 바뀌었나" 를 잰다
    }

    /// <summary>패치 1회의 결과. **셋으로 적는다** (D72) — 쌍이 아니다.</summary>
    public sealed class ApplyStats
    {
        public int Changed, Added, Removed;
        public int MeshesReplaced;      // 🔴 음성 대조 — 실제로 내용이 바뀐 GameObject 수
        public int EntitiesKept;        // 패치 전후로 EntityId 가 같은 청크 수
        public int Recreated;           // 유지됐어야 하는데 EntityId 가 바뀐 수
        public int Destroyed, Created;
        public int UnexpectedChanged;   // changed 인데 씬에 없던 키 (판본 어긋남)
        public double ElapsedMs;
        public int NodesBefore, NodesAfter;

        public string Describe() =>
            $"changed {Changed} / added {Added} / removed {Removed} · " +
            $"Mesh 실제교체 {MeshesReplaced} · EntityId 유지 {EntitiesKept} · " +
            $"재생성 {Recreated} · 생성 {Created} · 파괴 {Destroyed} · " +
            $"예상밖 changed {UnexpectedChanged} · {ElapsedMs:F2} ms · " +
            $"노드 {NodesBefore}→{NodesAfter}";
    }

    public sealed class ChunkSceneApplier
    {
        readonly Dictionary<string, ChunkNode> _nodes = new Dictionary<string, ChunkNode>();
        readonly Transform _root;
        readonly bool _addCollider;

        /// <summary>🔴 D9 — `.cbin` 정점은 **복셀 프레임(Z-up)** 이다. Unity 는 Y-up.
        ///
        ///     GLB_TO_VOXEL : voxel = (x, −z, y)        (server/pipeline/frames.py)
        ///     역변환       : unity = (vx, vz, −vy)     ← 여기서 쓰는 것
        ///
        /// 매직넘버를 흩뿌리지 않는다. 이 함수 하나만 고친다.</summary>
        public static Vector3 VoxelToUnity(Vector3 v) => new Vector3(v.x, v.z, -v.y);

        /// <summary>그 역. 라쏘의 지배축은 **복셀 인덱스 공간**에서 정해지므로
        /// 시선 벡터를 이쪽으로 되돌려서 넘겨야 한다.</summary>
        public static Vector3 UnityToVoxel(Vector3 v) => new Vector3(v.x, -v.z, v.y);

        public ChunkSceneApplier(Transform root, bool addCollider = true)
        {
            _root = root;
            _addCollider = addCollider;
        }

        public IReadOnlyDictionary<string, ChunkNode> Nodes => _nodes;
        public bool Has(string key) => _nodes.ContainsKey(key);

        /// <summary>부모 자산을 씬에 세운다. **청크 1개 = GameObject 1개.**</summary>
        public void Load(IReadOnlyDictionary<string, byte[]> blobs)
        {
            foreach (var kv in blobs) CreateNode(kv.Key, kv.Value);
        }

        /// <summary>델타를 반영한다. `noOp` 는 **음성 대조용** — 아무것도 안 한다.</summary>
        public ApplyStats Apply(
            IReadOnlyDictionary<string, byte[]> changed,
            IReadOnlyDictionary<string, byte[]> added,
            IReadOnlyCollection<string> removed,
            bool noOp = false)
        {
            var st = new ApplyStats
            {
                Changed = changed?.Count ?? 0,
                Added = added?.Count ?? 0,
                Removed = removed?.Count ?? 0,
                NodesBefore = _nodes.Count,
            };

            // 패치 전 EntityId 를 기억한다 — 유지율은 **전후 비교**로만 말할 수 있다.
            var before = new Dictionary<string, EntityId>(_nodes.Count);
            var hashBefore = new Dictionary<string, int>(_nodes.Count);
            foreach (var kv in _nodes)
            {
                before[kv.Key] = kv.Value.EntityId;
                hashBefore[kv.Key] = kv.Value.ContentHash;
            }

            var sw = Stopwatch.StartNew();
            if (!noOp)
            {
                // ── ① removed 먼저. 파괴 + **사전에서 제거** (§3-E)
                if (removed != null)
                    foreach (var key in removed)
                    {
                        if (!_nodes.TryGetValue(key, out var node)) continue;
                        DestroyNode(node);
                        _nodes.Remove(key);          // 🔴 이 한 줄이 조용한 실패를 막는다
                        st.Destroyed++;
                    }

                // ── ② changed. **Mesh 만 갈아끼운다.** GameObject·콜라이더·EntityId 유지
                if (changed != null)
                    foreach (var kv in changed)
                    {
                        if (_nodes.TryGetValue(kv.Key, out var node) && node.Filter != null)
                        {
                            ReplaceMesh(node, kv.Value);
                        }
                        else
                        {
                            // 씬에 없는 키의 changed = 클라와 서버의 판본이 어긋났다.
                            // 씬은 맞게 만들되 **숫자로 표면에 올린다.**
                            _nodes.Remove(kv.Key);
                            CreateNode(kv.Key, kv.Value);
                            st.UnexpectedChanged++;
                            st.Created++;
                        }
                    }

                // ── ③ added
                if (added != null)
                    foreach (var kv in added)
                    {
                        if (_nodes.ContainsKey(kv.Key)) { ReplaceMesh(_nodes[kv.Key], kv.Value); continue; }
                        CreateNode(kv.Key, kv.Value);
                        st.Created++;
                    }
            }
            sw.Stop();
            st.ElapsedMs = sw.Elapsed.TotalMilliseconds;
            st.NodesAfter = _nodes.Count;

            foreach (var kv in _nodes)
            {
                if (!before.TryGetValue(kv.Key, out var oldId)) continue;   // 새로 생긴 것
                if (oldId.Equals(kv.Value.EntityId)) st.EntitiesKept++;
                else st.Recreated++;
                // 🔴 음성 대조의 핵심: **내용이 실제로 바뀐 것**만 센다.
                if (hashBefore[kv.Key] != kv.Value.ContentHash) st.MeshesReplaced++;
            }
            return st;
        }

        void ReplaceMesh(ChunkNode node, byte[] blob)
        {
            var data = ChunkBin.Decode(blob);          // 계약 코드. 재구현하지 않는다
            ToUnityFrame(data);
            ChunkBin.ApplyTo(node.Mesh, data);         // 같은 Mesh 인스턴스에 덮어쓴다
            if (data.Normals == null) node.Mesh.RecalculateNormals();   // 없으면 음영이 안 생긴다
            node.ContentHash = ContentHashOf(data);
            // MeshCollider 는 같은 Mesh 를 참조하므로 갱신을 알려 준다.
            if (node.Go != null)
            {
                var mc = node.Go.GetComponent<MeshCollider>();
                if (mc != null) { mc.sharedMesh = null; mc.sharedMesh = node.Mesh; }
            }
        }

        void CreateNode(string key, byte[] blob)
        {
            var data = ChunkBin.Decode(blob);
            ToUnityFrame(data);
            var go = new GameObject($"chunk_{key}");
            go.transform.SetParent(_root, false);
            var mesh = new Mesh { name = $"cbin_{key}" };
            ChunkBin.ApplyTo(mesh, data);
            // `.cbin` 에 법선이 없으면 셰이더의 음영 항이 상수가 되어 **평면으로 보인다.**
            // 실기에서 "2D 인지 3D 인지 모르겠다" 는 보고가 나온 원인 중 하나다.
            if (data.Normals == null) mesh.RecalculateNormals();
            var mf = go.AddComponent<MeshFilter>();
            mf.sharedMesh = mesh;
            go.AddComponent<MeshRenderer>();
            if (_addCollider) go.AddComponent<MeshCollider>().sharedMesh = mesh;

            _nodes[key] = new ChunkNode
            {
                Key = key, Go = go, Filter = mf, Mesh = mesh,
                EntityId = go.GetEntityId(), ContentHash = ContentHashOf(data),
            };
        }

        static void DestroyNode(ChunkNode node)
        {
            if (node.Go == null) return;
#if UNITY_EDITOR
            if (!Application.isPlaying) { UnityEngine.Object.DestroyImmediate(node.Go); return; }
#endif
            UnityEngine.Object.Destroy(node.Go);
        }

        static void ToUnityFrame(ChunkMeshData data)
        {
            for (int i = 0; i < data.Positions.Length; i++)
                data.Positions[i] = VoxelToUnity(data.Positions[i]);
            if (data.Normals != null)
                for (int i = 0; i < data.Normals.Length; i++)
                    data.Normals[i] = VoxelToUnity(data.Normals[i]);
        }

        /// <summary>Mesh 내용의 해시. 위치와 색을 **둘 다** 본다 —
        /// recolor 는 위치를 안 바꾸므로 위치만 보면 "안 바뀌었다" 가 나온다.</summary>
        public static int ContentHashOf(ChunkMeshData d)
        {
            unchecked
            {
                int h = 17;
                h = h * 31 + d.Positions.Length;
                for (int i = 0; i < d.Positions.Length; i++)
                {
                    var p = d.Positions[i];
                    h = h * 31 + p.x.GetHashCode();
                    h = h * 31 + p.y.GetHashCode();
                    h = h * 31 + p.z.GetHashCode();
                }
                if (d.Colors != null)
                    for (int i = 0; i < d.Colors.Length; i++)
                    {
                        var c = d.Colors[i];
                        h = h * 31 + (c.r << 24 | c.g << 16 | c.b << 8 | c.a);
                    }
                return h;
            }
        }
    }
}
