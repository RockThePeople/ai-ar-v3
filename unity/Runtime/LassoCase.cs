// LassoCase — 라쏘 케이스 파일(.case) 파서. **Editor 와 실기가 같은 코드를 쓴다.**
//
// 🔴 W22 의 질문은 "Editor 골든과 실기가 같은 값을 내는가" 다. 파서가 양쪽에 하나씩
//    있으면 **그 파서 차이가 결과 차이로 나타나고**, 우리는 그것을 카메라 차이로
//    오해한다. 그래서 한 벌만 둔다 — D64(손잡이)에서 배운 것과 같은 이유다.
//
// 형식은 tools/lasso_case_export.py 가 낸다:
//   NAME <이름>
//   CAM  eye(3) target(3) up(3) fov width height
//   EXPECT <키> <값>            ← 단계별 계수까지 전부 (D60)
//   POLY / <sx> <sy> …
//   COORDS / <x> <y> <z> …

using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace DeltaContract
{
    public sealed class LassoCase
    {
        public string Name = "";
        public Vector3 Eye, Target, Up;
        public float Fov, Width, Height;
        public readonly List<Vector2> Polygon = new List<Vector2>();
        public readonly List<Vector3Int> Coords = new List<Vector3Int>();
        public readonly Dictionary<string, string> Expect = new Dictionary<string, string>();

        public string ExpectedFingerprint =>
            Expect.TryGetValue("mask_fingerprint", out var v) ? v : "";

        public int ExpectedCells =>
            Expect.TryGetValue("n_cells", out var v) ? int.Parse(v, CultureInfo.InvariantCulture) : -1;

        public static LassoCase Parse(string text)
        {
            var c = new LassoCase();
            var mode = "";
            foreach (var raw in text.Split('\n'))
            {
                var line = raw.Trim();
                if (line.Length == 0 || line[0] == '#') continue;
                var t = line.Split(' ');
                switch (t[0])
                {
                    case "NAME": c.Name = t[1]; continue;
                    case "CAM":
                        c.Eye = V3(t, 1); c.Target = V3(t, 4); c.Up = V3(t, 7);
                        c.Fov = F(t[10]); c.Width = F(t[11]); c.Height = F(t[12]);
                        continue;
                    case "EXPECT": c.Expect[t[1]] = t[2]; continue;
                    case "POLY": case "COORDS": mode = t[0]; continue;
                }
                if (mode == "POLY") c.Polygon.Add(new Vector2(F(t[0]), F(t[1])));
                else if (mode == "COORDS")
                    c.Coords.Add(new Vector3Int(int.Parse(t[0]), int.Parse(t[1]), int.Parse(t[2])));
            }
            return c;
        }

        /// <summary>케이스가 정한 카메라를 **그대로** 세운다.
        ///
        /// ⚠️ 실기 화면은 1080×2340 이고 골든은 1080×1920 이다. 화면비가 다르면
        ///    같은 폴리곤이 다른 것을 잡는다 — **예외는 안 난다.** 그래서 비교용
        ///    카메라는 RenderTexture 로 **골든의 해상도를 강제**한다.</summary>
        public Camera BuildCamera(out RenderTexture rt)
        {
            var go = new GameObject($"cam_{Name}");
            var cam = go.AddComponent<Camera>();
            rt = new RenderTexture((int)Width, (int)Height, 24);
            cam.targetTexture = rt;
            cam.fieldOfView = Fov;              // 수직 FOV
            cam.nearClipPlane = 0.01f;
            cam.farClipPlane = 1000f;
            cam.enabled = false;                // 렌더는 필요 없다. 투영만 쓴다
            // 🔴 케이스의 카메라 자세는 **복셀 프레임**이다. 씬의 기하는 D9 로
            //    Unity 프레임(Y-up)에 놓이므로 카메라도 같이 옮겨야 한다.
            //    안 옮기면 **화면에 보이는 것과 라쏘가 잡는 것이 다른 프레임**이 되고 —
            //    W22 실기 스크린샷에서 오토바이가 90° 누워 보인 것이 그 증상이다 —
            //    사용자가 뒷바퀴를 둘러 그려도 엉뚱한 데가 잡힌다. **예외는 안 난다.**
            go.transform.position = VoxelFrame.ToUnity(Eye);
            go.transform.LookAt(VoxelFrame.ToUnity(Target),
                                VoxelFrame.ToUnity(Up));
            return cam;
        }

        static Vector3 V3(string[] t, int i) => new Vector3(F(t[i]), F(t[i + 1]), F(t[i + 2]));
        static float F(string s) => float.Parse(s, CultureInfo.InvariantCulture);
    }
}
