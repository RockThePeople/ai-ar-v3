// TouchLassoController — 실기에서 **손가락으로** 라쏘를 그린다 (W22 ②).
//
// AR 없이 한다 (D57 ③ · W22 ②). 오브젝트를 화면 앞에 고정 배치하고 손가락으로 그린다 —
// plane detection 은 다음 단계다.
//
// 🔴 이 스크립트가 답해야 할 것은 "터치가 되는가" 가 아니라
//    **"Editor 골든과 같은 값이 나오는가"** 다. 그래서 앱이 뜨자마자 자동으로
//    **골든 재생(replay)** 을 돌려 logcat 에 PASS/FAIL 을 찍는다.
//
//    ⚠️ 터치 좌표는 마우스와 다르고, 실기 화면(1080×2340)은 골든(1080×1920)과
//       해상도·화면비가 다르다. 그 차이는 **예외 없이 조용히** 경계 셀을 가른다
//       (D64 손잡이가 그랬듯이). 그래서 재생용 카메라는 RenderTexture 로
//       **골든의 해상도를 강제**하고, 손가락 라쏘는 실기 카메라로 따로 잰다.
//
// ⚠️ D60-a — 비대칭 케이스를 쓴다. moto-b 뒷바퀴는 좌우가 뒤집히면 **앞바퀴**가
//    잡히므로 소속으로 드러난다. 대칭 자산이면 셀 수까지 같아서 못 잡는다.

using System.Collections.Generic;
using System.Globalization;
using System.IO;
using UnityEngine;

namespace DeltaContract
{
    public sealed class TouchLassoController : MonoBehaviour
    {
        [Tooltip("StreamingAssets 안의 .case 파일 이름")]
        public string CaseFile = "moto-rear-wheel.case";

        [Tooltip("손가락 라쏘용 카메라. 비우면 Camera.main")]
        public Camera LiveCamera;

        // 🔴 실기 보고: "옆면 검은 복셀이 전부다. 2D 인지 3D 인지 모르겠다."
        //    케이스 카메라는 FOV 6° · 거리 20 이라 **사실상 정사영**이고, 정축에서 보면
        //    껍질 표현이 평면으로 읽힌다. 보는 것과 판정하는 것을 분리한다:
        //      · 화면 카메라 — 가까이·넓은 화각·**천천히 회전**. 시차가 3D 를 만든다
        //      · 재생 카메라 — 케이스 그대로 (골든 비교는 여기서만)
        [Tooltip("화면 카메라 거리 (NORMALIZED 단위. 자산은 ±0.5 안에 있다)")]
        //   가로가 잘리지 않는 거리: 세로 화각 35° · 세로화면(aspect 0.46) 기준
        //   가로 가시폭 = 2·d·tan(17.5°)·0.46 ≥ 1.0 → d ≥ 3.4. 여유를 둬 4.0.
        public float ViewDistance = 4.0f;
        [Tooltip("화면 카메라 세로 화각")]
        public float ViewFov = 35f;
        [Tooltip("초당 회전 각도. 0 이면 정지")]
        public float OrbitSpeed = 14f;

        float _orbit;

        public const string LogTag = "[LassoW22]";

        LassoCase _case;
        Transform _asset;
        ChunkSceneApplier _applier;
        readonly List<Vector2> _stroke = new List<Vector2>();
        bool _drawing;
        string _hud = "준비 중…";
        GUIStyle _style;

        void Start()
        {
            var path = Path.Combine(Application.streamingAssetsPath, CaseFile);
            var text = ReadAll(path);
            if (string.IsNullOrEmpty(text))
            {
                _hud = $"케이스를 못 읽었다: {path}";
                Debug.LogError($"{LogTag} {_hud}");
                return;
            }
            _case = LassoCase.Parse(text);
            Debug.Log($"{LogTag} 케이스 {_case.Name} · 복셀 {_case.Coords.Count} · " +
                      $"폴리곤 {_case.Polygon.Count} · 기대 셀 {_case.ExpectedCells}");

            _asset = new GameObject("asset").transform;   // 회전 없음 — 로컬 = 월드
            LoadChunks();          // 🔴 이게 없으면 화면에 아무것도 안 보인다
            AimCamera();
            ReplayGolden();
        }

        /// <summary>★ 골든 재생 — **엔진 밖(Editor)에서 맞춘 것이 실기에서도 맞는가.**</summary>
        void ReplayGolden()
        {
            var cam = _case.BuildCamera(out var rt);
            var r = Pick(cam, _case.Polygon);
            bool ok = r.Fingerprint == _case.ExpectedFingerprint && r.Cells.Count == _case.ExpectedCells;

            // 🔴 D60 — 셀 수·지문만이 아니라 **단계별 계수까지** 대조한다.
            var mismatches = new List<string>();
            Cmp(mismatches, "n_cells", r.Cells.Count);
            Cmp(mismatches, "mask_fingerprint", r.Fingerprint);
            Cmp(mismatches, "projected", r.Projected);
            Cmp(mismatches, "behind_camera", r.BehindCamera);
            Cmp(mismatches, "in_polygon", r.InPolygon);
            Cmp(mismatches, "after_solidify", r.AfterSolidify);
            Cmp(mismatches, "solidify_added", r.SolidifyAdded);
            Cmp(mismatches, "intersect_removed", r.IntersectRemoved);
            Cmp(mismatches, "dominant_axis", r.DominantAxis);

            Debug.Log($"{LogTag} REPLAY 셀 {r.Cells.Count} (기대 {_case.ExpectedCells}) · " +
                      $"지문 {r.Fingerprint}");
            Debug.Log($"{LogTag} REPLAY 계수 투영 {r.Projected} 뒤 {r.BehindCamera} " +
                      $"폴리곤안 {r.InPolygon} 압출후 {r.AfterSolidify}(+{r.SolidifyAdded}) " +
                      $"교집합제거 {r.IntersectRemoved} 축 {r.DominantAxis}");

            // ⚠️ 비대칭 확인 (D60-a): 좌우가 뒤집혔다면 **앞바퀴**가 잡힌다.
            int minY = int.MaxValue, maxY = int.MinValue;
            foreach (var c in r.Cells) { if (c.y < minY) minY = c.y; if (c.y > maxY) maxY = c.y; }
            Debug.Log($"{LogTag} REPLAY y범위 {minY}–{maxY} " +
                      "(뒷바퀴는 y≥46. 좌우가 뒤집히면 앞바퀴 쪽이 잡힌다)");

            if (mismatches.Count == 0)
                Debug.Log($"{LogTag} REPLAY PASS — Editor 골든과 전건 일치");
            else
                Debug.LogError($"{LogTag} REPLAY FAIL — 불일치 {mismatches.Count}건: " +
                               string.Join(" · ", mismatches));

            _hud = (ok ? "✅ 골든 일치" : "❌ 골든 불일치") +
                   $"\n셀 {r.Cells.Count}/{_case.ExpectedCells}\n지문 {Short(r.Fingerprint)}" +
                   $"\ny {minY}–{maxY}\n손가락으로 그려라";

            if (cam != null) Destroy(cam.gameObject);
            if (rt != null) rt.Release();
        }

        void Cmp(List<string> outp, string key, object got)
        {
            if (!_case.Expect.TryGetValue(key, out var want)) return;
            var g = System.Convert.ToString(got, CultureInfo.InvariantCulture);
            if (g != want) outp.Add($"{key}: 골든 {want} ≠ 실기 {g}");
        }

        LassoMaskResult Pick(Camera cam, List<Vector2> polygon)
        {
            var tr = _asset;
            // 🔴 복셀 중심 → **D9 로 Unity 프레임** → 월드 → 화면.
            //    렌더링이 타는 변환과 **같은 것**을 태워야 화면과 판정이 일치한다.
            System.Func<Vector3, Vector3> project =
                local => cam.WorldToScreenPoint(
                    tr.TransformPoint(ChunkSceneApplier.VoxelToUnity(local)));
            // 지배축은 복셀 인덱스 공간에서 정해진다 — 시선을 되돌려 넘긴다.
            var viewDirLocal = ChunkSceneApplier.UnityToVoxel(
                tr.InverseTransformDirection(cam.transform.forward));
            return SlatLassoPicker.Pick(_case.Coords, polygon, project, viewDirLocal);
        }

        /// <summary>StreamingAssets 의 `.cbin` 청크를 씬에 세운다 — **청크 1개 = GameObject 1개**.
        /// 목록은 `chunks.txt` 로 받는다: Android 의 StreamingAssets 는 APK 안이라
        /// **디렉터리를 훑을 수 없다.**</summary>
        void LoadChunks()
        {
            var list = ReadAll(Path.Combine(Application.streamingAssetsPath, "chunks.txt"));
            if (string.IsNullOrEmpty(list)) { Debug.LogWarning($"{LogTag} chunks.txt 가 없다"); return; }

            var mat = new Material(Shader.Find("DeltaContract/VoxelUnlit"));
            _applier = new ChunkSceneApplier(_asset, addCollider: false);
            var blobs = new Dictionary<string, byte[]>();
            foreach (var raw in list.Split('\n'))
            {
                var key = raw.Trim();
                if (key.Length == 0) continue;
                var bytes = ReadBytes(Path.Combine(Application.streamingAssetsPath, "chunks/" + key + ".cbin"));
                if (bytes != null && bytes.Length > 0) blobs[key] = bytes;
            }
            _applier.Load(blobs);
            foreach (var n in _applier.Nodes.Values)
            {
                var mr = n.Go.GetComponent<MeshRenderer>();
                if (mr != null) mr.sharedMaterial = mat;
            }
            Debug.Log($"{LogTag} 청크 {_applier.Nodes.Count}/{blobs.Count} 로드 · " +
                      $"셰이더 {(mat.shader != null ? mat.shader.name : "NULL")}");
        }

        /// <summary>자산이 화면에 **3D 로 보이도록** 화면 카메라를 세운다.</summary>
        void AimCamera()
        {
            var cam = LiveCamera != null ? LiveCamera : Camera.main;
            if (cam == null) return;
            cam.clearFlags = CameraClearFlags.SolidColor;
            cam.backgroundColor = Color.white;      // 흰 배경 — 어두운 정점 색이 드러난다
            cam.nearClipPlane = 0.01f;
            cam.farClipPlane = 100f;
            cam.fieldOfView = ViewFov;              // 진짜 원근. 6° 는 정사영처럼 보인다
            _orbit = 90f;                           // 측면에서 시작 (골든과 같은 방향)
            PlaceOrbit(cam);
        }

        void PlaceOrbit(Camera cam)
        {
            // 자산은 Unity 프레임에서 원점 주위 ±0.5 안에 있다 (NORMALIZED).
            float rad = _orbit * Mathf.Deg2Rad;
            var pos = new Vector3(Mathf.Cos(rad), 0.28f, Mathf.Sin(rad)) * ViewDistance;
            cam.transform.position = pos;
            cam.transform.LookAt(Vector3.zero, Vector3.up);
        }

        /// <summary>케이스의 **가로** 화각을 실기 화면비에 맞춘 세로 FOV.</summary>
        float MatchedVerticalFov()
        {
            float focal = (_case.Height * 0.5f) / Mathf.Tan(_case.Fov * Mathf.Deg2Rad * 0.5f);
            float focalDev = focal * (Screen.width / _case.Width);
            return 2f * Mathf.Atan((Screen.height * 0.5f) / focalDev) * Mathf.Rad2Deg;
        }

        // ══════════ 손가락 라쏘
        void Update()
        {
            var view = LiveCamera != null ? LiveCamera : Camera.main;
            // 그리는 동안에는 **멈춘다** — 움직이는 화면 위에 그린 폴리곤은 무의미하다.
            if (view != null && !_drawing && OrbitSpeed != 0f)
            {
                _orbit += OrbitSpeed * Time.deltaTime;
                PlaceOrbit(view);
            }

            if (_case == null || Input.touchCount == 0) return;
            var t = Input.GetTouch(0);

            // 🔴 터치 좌표는 **좌하단 원점**이라 Camera.WorldToScreenPoint 와 같다.
            //    GUI(좌상단)와 다르다 — 여기서 뒤집으면 상하가 반대가 되고 예외는 안 난다.
            switch (t.phase)
            {
                case TouchPhase.Began:
                    _stroke.Clear(); _drawing = true;
                    _stroke.Add(t.position);
                    break;
                case TouchPhase.Moved:
                    if (_drawing) _stroke.Add(t.position);
                    break;
                case TouchPhase.Ended:
                case TouchPhase.Canceled:
                    if (_drawing && _stroke.Count >= 3) PickWithFinger();
                    _drawing = false;
                    break;
            }
        }

        void PickWithFinger()
        {
            var cam = LiveCamera != null ? LiveCamera : Camera.main;
            if (cam == null) { Debug.LogError($"{LogTag} 카메라가 없다"); return; }

            // 🔴 카메라를 케이스 자세로 되돌리지 **않는다.** 사용자가 보고 그린 그 화면이
            //    판정 기준이다. 되돌리면 "본 것"과 "잡힌 것"이 갈라진다.
            Debug.Log($"{LogTag} TOUCH 시점 궤도 {_orbit % 360f:F0}° · FOV {cam.fieldOfView:F1}° " +
                      $"· 화면 {Screen.width}x{Screen.height}");

            var r = Pick(cam, _stroke);
            Debug.Log($"{LogTag} TOUCH 점 {_stroke.Count} · 화면 {Screen.width}x{Screen.height} " +
                      $"· 셀 {r.Cells.Count} · 지문 {r.Fingerprint} · " +
                      $"폴리곤안 {r.InPolygon} 압출 +{r.SolidifyAdded} 제거 {r.IntersectRemoved}");
            _hud = $"터치 라쏘\n점 {_stroke.Count} · 셀 {r.Cells.Count}\n지문 {Short(r.Fingerprint)}" +
                   $"\n화면 {Screen.width}x{Screen.height}";
        }

        void OnGUI()
        {
            if (_style == null)
                _style = new GUIStyle(GUI.skin.label) { fontSize = 34, wordWrap = true };
            GUI.Label(new Rect(24, 24, Screen.width - 48, 420), _hud, _style);

            if (_stroke.Count > 1)   // 그린 궤적을 화면에 보인다 (좌상단 원점으로 변환)
                for (int i = 1; i < _stroke.Count; i++)
                {
                    var a = _stroke[i - 1]; var b = _stroke[i];
                    DrawLine(new Vector2(a.x, Screen.height - a.y),
                             new Vector2(b.x, Screen.height - b.y));
                }
        }

        static Texture2D _px;
        static void DrawLine(Vector2 a, Vector2 b)
        {
            if (_px == null)
            {
                _px = new Texture2D(1, 1);
                _px.SetPixel(0, 0, Color.yellow);
                _px.Apply();
            }
            var d = b - a;
            var m = GUI.matrix;
            GUIUtility.RotateAroundPivot(Mathf.Atan2(d.y, d.x) * Mathf.Rad2Deg, a);
            GUI.DrawTexture(new Rect(a.x, a.y - 2, d.magnitude, 4), _px);
            GUI.matrix = m;
        }

        /// <summary>바이트 읽기. Android 는 APK 안이라 파일 경로로 못 읽는다.</summary>
        static byte[] ReadBytes(string path)
        {
            if (path.Contains("://"))
            {
                using (var req = UnityEngine.Networking.UnityWebRequest.Get(path))
                {
                    req.SendWebRequest();
                    while (!req.isDone) { }
                    return req.result == UnityEngine.Networking.UnityWebRequest.Result.Success
                        ? req.downloadHandler.data : null;
                }
            }
            return File.Exists(path) ? File.ReadAllBytes(path) : null;
        }

        static string Short(string s) => s.Length > 16 ? s.Substring(0, 16) + "…" : s;

        /// <summary>StreamingAssets 읽기. Android 는 APK 안이라 **파일 경로로 못 읽는다** —
        /// UnityWebRequest 경로를 타야 한다. 그 분기를 여기 한 곳에 둔다.</summary>
        static string ReadAll(string path)
        {
            if (path.Contains("://"))
            {
                using (var req = UnityEngine.Networking.UnityWebRequest.Get(path))
                {
                    req.SendWebRequest();
                    while (!req.isDone) { }          // 시작 시 1회. 앱이 뜨기 전이다
                    return req.result == UnityEngine.Networking.UnityWebRequest.Result.Success
                        ? req.downloadHandler.text : "";
                }
            }
            return File.Exists(path) ? File.ReadAllText(path) : "";
        }
    }
}
