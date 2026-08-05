// InPlaceBatchCheck — Unity 를 **실제로 띄워서** `.cbin` 델타를 씬에 반영하고 잰다 (W21).
//
// 게이트는 절감률이 아니다 (D70). **무엇이 살아남았는가**다:
//     EntityId 유지율 · 재생성 수 · apply 시간 · changed/added/removed (셋으로)
//
// ⚠️ 음성 대조가 없으면 **아무것도 안 하는 구현이 EntityId 유지율 100% 를 받는다.**
//    이 프로젝트가 반복해 물린 자리다 (방법론 3조). 그래서 "Mesh 가 실제로 바뀐
//    GameObject 수" 를 같이 재고, no-op 실행이 **떨어지는지** 확인한다.
//
// 실행:  tools/unity_inplace_check.sh

#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace DeltaContract.EditorTools
{
    public static class InPlaceBatchCheck
    {
        static int _fail;

        public static void Run()
        {
            var dir = Environment.GetEnvironmentVariable("INPLACE_DIR");
            if (string.IsNullOrEmpty(dir)) { Fail("INPLACE_DIR 이 없다"); Exit(); return; }
            Log($"자료 {Path.GetFullPath(dir)}");

            var parent = LoadDir(Path.Combine(dir, "parent"));
            var patchBlobs = LoadDir(Path.Combine(dir, "patch"));
            var patch = ParsePatch(File.ReadAllText(Path.Combine(dir, "patch.json")));
            Log($"부모 {parent.Count}청크 · 패치 changed {patch.Changed.Count} / " +
                $"added {patch.Added.Count} / removed {patch.Removed.Count}");

            if (parent.Count != patch.Total) Fail($"부모 수 {parent.Count} ≠ 패치의 총계 {patch.Total}");

            // 🔴 recolor 는 청크 집합을 바꾸지 않는다. added/removed 가 있으면 경로가 깨진 것이다.
            if (patch.Op == "recolor" && (patch.Added.Count != 0 || patch.Removed.Count != 0))
                Fail($"recolor 인데 added {patch.Added.Count} / removed {patch.Removed.Count} 다 — " +
                     "청크 집합이 바뀌면 GameObject 가 생기거나 파괴된다");

            CheckMainApply(parent, patchBlobs, patch);
            CheckNoOpIsCaught(parent, patchBlobs, patch);
            CheckRemovedPath(dir, parent, patchBlobs);
            CheckFrameConversion();
            CheckNaiveRebuildIsWorse(parent, patchBlobs, patch);

            Exit();
        }

        // ══════════ ① 본 검사 — in-place 로 갈아끼운다
        static void CheckMainApply(Dictionary<string, byte[]> parent,
                                   Dictionary<string, byte[]> patchBlobs, Patch patch)
        {
            var root = new GameObject("moto-b").transform;
            var app = new ChunkSceneApplier(root);
            app.Load(parent);
            int loaded = app.Nodes.Count;

            var changed = Subset(patchBlobs, patch.Changed);
            var st = app.Apply(changed, new Dictionary<string, byte[]>(), patch.Removed);
            Log("① in-place: " + st.Describe());

            if (st.NodesAfter != loaded) Fail($"노드 수가 바뀌었다 {loaded}→{st.NodesAfter} (recolor 인데)");
            if (st.Recreated != 0) Fail($"재생성 {st.Recreated}개 — GameObject 를 내렸다");
            if (st.EntitiesKept != loaded) Fail($"EntityId 유지 {st.EntitiesKept}/{loaded}");
            if (st.Created != 0 || st.Destroyed != 0) Fail($"생성 {st.Created} 파괴 {st.Destroyed} — 0 이어야 한다");
            if (st.UnexpectedChanged != 0) Fail($"씬에 없던 키의 changed {st.UnexpectedChanged}개 — 판본이 어긋났다");

            // ★ 음성 대조의 짝: **실제로 갈아끼워졌는가**
            if (st.MeshesReplaced != patch.Changed.Count)
                Fail($"Mesh 실제 교체 {st.MeshesReplaced} ≠ changed {patch.Changed.Count} — " +
                     "유지율만 100% 이고 교체가 안 일어났을 수 있다");

            Log($"① 유지율 {100.0 * st.EntitiesKept / loaded:F1}% · " +
                $"apply {st.ElapsedMs.ToString("F2", CultureInfo.InvariantCulture)} ms");
            UnityEngine.Object.DestroyImmediate(root.gameObject);
        }

        // ══════════ ② 음성 대조 — 아무것도 안 하는 구현
        static void CheckNoOpIsCaught(Dictionary<string, byte[]> parent,
                                      Dictionary<string, byte[]> patchBlobs, Patch patch)
        {
            var root = new GameObject("noop").transform;
            var app = new ChunkSceneApplier(root);
            app.Load(parent);
            var st = app.Apply(Subset(patchBlobs, patch.Changed),
                               new Dictionary<string, byte[]>(), patch.Removed, noOp: true);
            Log("② 음성 대조(no-op): " + st.Describe());

            // 🔴 아무것도 안 했으니 유지율은 **만점**이다. 그게 함정이다.
            if (st.EntitiesKept != parent.Count)
                Fail("no-op 인데 EntityId 유지율이 만점이 아니다 — 대조가 성립하지 않는다");
            if (st.MeshesReplaced != 0)
                Fail($"no-op 인데 Mesh 가 {st.MeshesReplaced}개 바뀌었다");
            else
                Log("② ok — 유지율은 만점인데 실제 교체 0. **유지율만으로는 못 가른다**는 증거다");
            UnityEngine.Object.DestroyImmediate(root.gameObject);
        }

        // ══════════ ③ removed 경로 — §3-E
        static void CheckRemovedPath(string dir, Dictionary<string, byte[]> parent,
                                     Dictionary<string, byte[]> patchBlobs)
        {
            var p2 = ParsePatch(File.ReadAllText(Path.Combine(dir, "patch-removed.json")));
            if (p2.Removed.Count == 0) { Fail("removed 시험 자료에 removed 가 없다"); return; }
            var victim = p2.Removed[0];

            var root = new GameObject("removed").transform;
            var app = new ChunkSceneApplier(root);
            app.Load(parent);
            int before = app.Nodes.Count;

            var st = app.Apply(Subset(patchBlobs, p2.Changed), new Dictionary<string, byte[]>(), p2.Removed);
            Log("③ removed: " + st.Describe());

            if (st.Destroyed != 1) Fail($"파괴 {st.Destroyed} — 1 이어야 한다");
            if (app.Has(victim)) Fail($"🔴 파괴한 {victim} 이 사전에 남아 있다 (§3-E). " +
                                      "다음 패치가 파괴된 MeshFilter 에 ApplyTo 를 걸고 예외가 안 난다");
            if (app.Nodes.Count != before - 1) Fail("노드 수가 하나 줄지 않았다");

            // 🔴 후속 패치가 그 키를 changed 로 다시 보낸다 — 조용히 아무 일도 안 일어나면 안 된다.
            var again = new Dictionary<string, byte[]> { { victim, parent[victim] } };
            var st2 = app.Apply(again, new Dictionary<string, byte[]>(), new string[0]);
            Log("③ 후속 패치: " + st2.Describe());
            if (st2.UnexpectedChanged != 1)
                Fail("파괴된 키의 changed 가 표면에 안 올라왔다 — 조용한 실패다");
            if (!app.Has(victim)) Fail("씬이 복구되지 않았다");
            Log("③ ok — 파괴 후 사전에서 사라졌고, 후속 changed 는 숫자로 드러났다");
            UnityEngine.Object.DestroyImmediate(root.gameObject);
        }

        // ══════════ ⑤ 대조군 — **통짜 재생성**이 무엇을 잃는가
        //
        // "오브젝트를 내릴 필요 없이" 가 목표라면(D70), 내렸을 때와 비교해야 그 말이
        // 숫자가 된다. 여기서는 일부러 전부 파괴하고 다시 세운다.
        static void CheckNaiveRebuildIsWorse(Dictionary<string, byte[]> parent,
                                             Dictionary<string, byte[]> patchBlobs, Patch patch)
        {
            var root = new GameObject("rebuild").transform;
            var app = new ChunkSceneApplier(root);
            app.Load(parent);

            var keys = new List<string>(app.Nodes.Keys);
            var full = new Dictionary<string, byte[]>();
            foreach (var k in keys) full[k] = patchBlobs.ContainsKey(k) ? patchBlobs[k] : parent[k];

            // 전부 removed → 전부 added. 클라이언트가 자산을 내렸다 다시 받는 경로다.
            var st = app.Apply(new Dictionary<string, byte[]>(), full, keys);
            Log("⑤ 통짜 재생성 대조: " + st.Describe());

            if (st.Destroyed != keys.Count) Fail("대조군이 전부 파괴하지 않았다 — 대조가 성립 안 한다");
            if (st.EntitiesKept != 0)
                Fail($"전부 파괴했는데 EntityId 가 {st.EntitiesKept}개 살아 있다");
            Log($"⑤ ok — 내리면 유지 0/{keys.Count} · 생성 {st.Created} · " +
                $"{st.ElapsedMs.ToString("F2", CultureInfo.InvariantCulture)} ms. " +
                "in-place 는 이걸 안 한다는 뜻이다");
            UnityEngine.Object.DestroyImmediate(root.gameObject);
        }

        // ══════════ ④ 좌표 프레임 (D9)
        static void CheckFrameConversion()
        {
            // voxel (x, −z, y) 의 역: unity = (vx, vz, −vy)
            var got = ChunkSceneApplier.VoxelToUnity(new Vector3(1f, 2f, 3f));
            if (got != new Vector3(1f, 3f, -2f)) Fail($"D9 역변환이 틀렸다: {got} (기대 (1,3,-2))");
            var up = ChunkSceneApplier.VoxelToUnity(new Vector3(0f, 0f, 1f));
            if (up != Vector3.up) Fail($"복셀 Z-up 이 Unity Y-up 으로 안 간다: {up}");
            Log("④ D9 좌표 변환 ok — 복셀 Z-up → Unity Y-up");
        }

        // ══════════ 도구
        class Patch
        {
            public string Op = "";
            public int Total;
            public List<string> Changed = new List<string>();
            public List<string> Added = new List<string>();
            public List<string> Removed = new List<string>();
        }

        static Patch ParsePatch(string json)
        {
            var p = new Patch();
            p.Op = Str(json, "\"op\"");
            p.Total = (int)Num(json, "\"n_chunks_total\"");
            p.Changed = Arr(json, "\"changed\"");
            p.Added = Arr(json, "\"added\"");
            p.Removed = Arr(json, "\"removed\"");
            return p;
        }

        static string Str(string s, string key)
        {
            int i = s.IndexOf(key, StringComparison.Ordinal);
            if (i < 0) return "";
            i = s.IndexOf('"', s.IndexOf(':', i) + 1);
            int j = s.IndexOf('"', i + 1);
            return s.Substring(i + 1, j - i - 1);
        }

        static double Num(string s, string key)
        {
            int i = s.IndexOf(key, StringComparison.Ordinal);
            if (i < 0) return -1;
            i = s.IndexOf(':', i) + 1;
            int j = i;
            while (j < s.Length && (char.IsDigit(s[j]) || s[j] == '.' || s[j] == '-' || s[j] == ' ')) j++;
            return double.Parse(s.Substring(i, j - i).Trim(), CultureInfo.InvariantCulture);
        }

        static List<string> Arr(string s, string key)
        {
            var outp = new List<string>();
            int i = s.IndexOf(key, StringComparison.Ordinal);
            if (i < 0) return outp;
            int a = s.IndexOf('[', i), b = s.IndexOf(']', a);
            var body = s.Substring(a + 1, b - a - 1);
            foreach (var part in body.Split(','))
            {
                var t = part.Trim().Trim('"');
                if (t.Length > 0) outp.Add(t);
            }
            return outp;
        }

        static Dictionary<string, byte[]> LoadDir(string dir)
        {
            var d = new Dictionary<string, byte[]>();
            foreach (var f in Directory.GetFiles(dir, "*.cbin"))
                d[Path.GetFileNameWithoutExtension(f)] = File.ReadAllBytes(f);
            return d;
        }

        static Dictionary<string, byte[]> Subset(Dictionary<string, byte[]> all, List<string> keys)
        {
            var d = new Dictionary<string, byte[]>();
            foreach (var k in keys) if (all.ContainsKey(k)) d[k] = all[k];
            return d;
        }

        static void Exit()
        {
            if (_fail > 0) { Log($"❌ 실패 {_fail}건"); EditorApplication.Exit(1); }
            else { Log("✅ 전건 통과"); EditorApplication.Exit(0); }
        }

        static void Log(string m) => Debug.Log("[InPlaceCheck] " + m);
        static void Fail(string m) { _fail++; Debug.LogError("[InPlaceCheck] ❌ " + m); }
    }
}
#endif
