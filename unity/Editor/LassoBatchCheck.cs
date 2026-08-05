// LassoBatchCheck — Unity 를 **실제로 띄워서** 라쏘를 돌리고 헤드리스 골든과 대조한다 (W18).
//
// 🔴 이 검사가 답하는 것은 하나다: **엔진 밖에서 맞춘 것이 엔진 안에서도 맞는가.**
//
// 헤드리스 하네스는 핀홀 카메라를 손으로 구현했다. Unity 는 `Camera.WorldToScreenPoint`
// 를 쓰고, 좌표계가 **왼손**이며, `transform.LookAt` 의 기저가 다르다. 이 셋 중 하나라도
// 어긋나면 폴리곤이 좌우/상하로 뒤집히고 **엉뚱한 부분이 잡히는데 예외는 안 난다.**
//
// ⚠️ D60 — **단계별 계수까지 전부 대조한다.** 최종 산출물(셀 수·지문)만 보면 상쇄된
//    오류를 놓친다. W17 에서 압출 +1 훼손이 셀 목록·지문 둘 다 통과했다.
//
// 실행:  tools/unity_lasso_check.sh   (배치모드 · -executeMethod)

#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEngine;

namespace DeltaContract.EditorTools
{
    public static class LassoBatchCheck
    {
        class Case
        {
            public string Name;
            public Vector3 Eye, Target, Up;
            public float Fov, Width, Height;
            public List<Vector2> Polygon = new List<Vector2>();
            public List<Vector3Int> Coords = new List<Vector3Int>();
            public Dictionary<string, string> Expect = new Dictionary<string, string>();
        }

        static int _fail;
        static int _flipSensitive;

        /// <summary>배치모드 진입점. 실패하면 **0 이 아닌 코드로 종료**한다 —
        /// 로그만 남기면 CI 도 사람도 통과로 읽는다.</summary>
        public static void Run()
        {
            var dir = Environment.GetEnvironmentVariable("LASSO_CASE_DIR");
            if (string.IsNullOrEmpty(dir)) dir = Path.Combine(Application.dataPath, "..", "Cases");
            Log($"케이스 경로 {Path.GetFullPath(dir)}");

            foreach (var path in Directory.GetFiles(dir, "*.case"))
            {
                var c = Parse(File.ReadAllText(path));
                try { CheckCase(c); }
                catch (Exception e) { Fail($"[{c.Name}] 예외: {e}"); }
            }
            CheckGuiFlip();
            if (_flipSensitive == 0)
                Fail("상하 반전에 민감한 케이스가 하나도 없다 — 이 검사는 원점 오류를 못 잡는다");

            if (_fail > 0) { Log($"❌ 실패 {_fail}건"); EditorApplication.Exit(1); }
            else { Log("✅ 전건 일치"); EditorApplication.Exit(0); }
        }

        static void CheckCase(Case c)
        {
            // ── 실제 Camera. RenderTexture 를 붙여야 pixelWidth/Height 가 정해진다.
            var go = new GameObject("cam");
            var cam = go.AddComponent<Camera>();
            var rt = new RenderTexture((int)c.Width, (int)c.Height, 24);
            cam.targetTexture = rt;
            cam.fieldOfView = c.Fov;              // Unity 는 **수직** FOV — 헤드리스와 같은 정의
            cam.nearClipPlane = 0.01f;
            cam.farClipPlane = 1000f;
            go.transform.position = c.Eye;
            go.transform.LookAt(c.Target, c.Up);

            var obj = new GameObject("asset");    // 회전 없음 — 로컬 = 월드
            var tr = obj.transform;

            Func<Vector3, Vector3> project = local => cam.WorldToScreenPoint(tr.TransformPoint(local));
            var viewDirLocal = tr.InverseTransformDirection(cam.transform.forward);

            var r = SlatLassoPicker.Pick(c.Coords, c.Polygon, project, viewDirLocal);

            Log($"[{c.Name}] 화면 {cam.pixelWidth}x{cam.pixelHeight} · 셀 {r.Cells.Count} · " +
                $"투영 {r.Projected} 뒤 {r.BehindCamera} 폴리곤안 {r.InPolygon} " +
                $"압출후 {r.AfterSolidify}(+{r.SolidifyAdded}) 교집합제거 {r.IntersectRemoved} " +
                $"축 {r.DominantAxis}");
            Log($"[{c.Name}] 지문 {r.Fingerprint}");

            // 🔴 D60 — 단계별 계수까지 전부 본다.
            Cmp(c, "n_cells", r.Cells.Count);
            Cmp(c, "mask_fingerprint", r.Fingerprint);
            Cmp(c, "grid_source", r.GridSource);
            Cmp(c, "projected", r.Projected);
            Cmp(c, "behind_camera", r.BehindCamera);
            Cmp(c, "in_polygon", r.InPolygon);
            Cmp(c, "after_solidify", r.AfterSolidify);
            Cmp(c, "solidify_added", r.SolidifyAdded);
            Cmp(c, "intersect_removed", r.IntersectRemoved);
            Cmp(c, "dominant_axis", r.DominantAxis);

            // ── 음성 대조: **뒤집기를 빼면** 결과가 달라져야 한다.
            //    안 달라지면 이 케이스는 상하 반전을 못 잡는 것이고, 그러면
            //    "일치했다" 가 뒤집기가 맞다는 증거가 되지 못한다.
            var flipped = new List<Vector2>();
            foreach (var p in c.Polygon) flipped.Add(new Vector2(p.x, c.Height - p.y));
            var bad = SlatLassoPicker.Pick(c.Coords, flipped, project, viewDirLocal);
            if (bad.Fingerprint == r.Fingerprint)
            {
                // 실패가 아니라 **이 케이스의 한계**다. 화면 전체를 감싼 폴리곤은
                // 뒤집어도 같은 것을 잡는다 — 당연하다. 다만 그런 케이스만 있으면
                // 원점 오류를 아무도 못 잡으므로, 아래에서 최소 1건을 요구한다.
                Log($"[{c.Name}] ⚠️ 상하 반전에 둔감한 케이스다 — 원점 오류를 못 잡는다");
            }
            else
            {
                _flipSensitive++;
                Log($"[{c.Name}] 음성 대조 ok — 반전 폴리곤은 {bad.Cells.Count}셀 (다른 부분을 잡는다)");
            }

            UnityEngine.Object.DestroyImmediate(obj);
            UnityEngine.Object.DestroyImmediate(go);
            rt.Release();
        }

        /// <summary>GUI(좌상단) ↔ 화면(좌하단) 왕복이 항등인가.
        /// 이 창에서 실제로 쓰는 함수 그대로 부른다.</summary>
        static void CheckGuiFlip()
        {
            const int camH = 1920;
            foreach (var ppp in new[] { 1f, 2f })
            {
                foreach (var g in new[] { new Vector2(0, 0), new Vector2(100, 40), new Vector2(539, 960) })
                {
                    var screen = LassoProbeWindow.GuiToScreen(g, ppp, camH);
                    var back = LassoProbeWindow.ScreenToGui(screen, ppp, camH);
                    if ((back - g).sqrMagnitude > 1e-6f)
                        Fail($"GUI 왕복이 항등이 아니다: {g} → {screen} → {back} (ppp {ppp})");
                    if (Mathf.Approximately(g.y, 0f) && !Mathf.Approximately(screen.y, camH))
                        Fail($"GUI 최상단이 화면 최상단으로 안 간다: {screen} (기대 y={camH})");
                }
            }
            Log("GUI↔화면 뒤집기 왕복 ok (최상단 y=0 → 화면 y=1920)");
        }

        static void Cmp(Case c, string key, object got)
        {
            if (!c.Expect.TryGetValue(key, out var want)) { Fail($"[{c.Name}] 기대값에 {key} 가 없다"); return; }
            var g = Convert.ToString(got, CultureInfo.InvariantCulture);
            if (g != want) Fail($"[{c.Name}] {key}: 골든 {want} ≠ Unity {g}");
        }

        static Case Parse(string text)
        {
            var c = new Case();
            var mode = "";
            foreach (var raw in text.Split('\n'))
            {
                var line = raw.Trim();
                if (line.Length == 0 || line[0] == '#') continue;
                var tok = line.Split(' ');
                switch (tok[0])
                {
                    case "NAME": c.Name = tok[1]; continue;
                    case "CAM":
                        c.Eye = V3(tok, 1); c.Target = V3(tok, 4); c.Up = V3(tok, 7);
                        c.Fov = F(tok[10]); c.Width = F(tok[11]); c.Height = F(tok[12]);
                        continue;
                    case "EXPECT": c.Expect[tok[1]] = tok[2]; continue;
                    case "POLY": case "COORDS": mode = tok[0]; continue;
                }
                if (mode == "POLY") c.Polygon.Add(new Vector2(F(tok[0]), F(tok[1])));
                else if (mode == "COORDS")
                    c.Coords.Add(new Vector3Int(int.Parse(tok[0]), int.Parse(tok[1]), int.Parse(tok[2])));
            }
            return c;
        }

        static Vector3 V3(string[] t, int i) => new Vector3(F(t[i]), F(t[i + 1]), F(t[i + 2]));
        static float F(string s) => float.Parse(s, CultureInfo.InvariantCulture);

        static void Log(string m) => Debug.Log("[LassoBatchCheck] " + m);
        static void Fail(string m) { _fail++; Debug.LogError("[LassoBatchCheck] ❌ " + m); }
    }
}
#endif
