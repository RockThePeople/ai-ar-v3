// LassoProbeWindow — Scene 뷰에서 **마우스로 그려서** slat 마스크를 낸다 (W17 ①).
//
// AR 없이 Editor 에서 확인하기 위한 창이다. AR 은 이번 범위가 아니다 (D57 ③) —
// 배치·앵커 없이 **판정만** 본다. 라쏘 코드는 AR 이든 Editor 든 같다.
//
// ⚠️⚠️ **이 파일은 미검증이다.** 이 리포에는 Unity 프로젝트가 없어서 맥북 세션이
//      실행해 보지 못했다. 검증된 것은 같은 코드 경로를 엔진 없이 돌린
//      `unity/Headless` 쪽이다 (server/tests/test_lasso.py, 골든 대조).
//      Unity 에서 처음 띄우는 세션은 **결과 셀 수·지문을 헤드리스 골든과 대조**해라.
//      "예외가 안 났다" 는 "맞다" 가 아니다.
//
// 사용:
//   1) 이 파일을 Unity 프로젝트의 Assets/Editor/ 아래 둔다
//      (unity/Runtime/SlatLassoPicker.cs 와 contract/unity/LassoVolume.cs 도 함께)
//   2) Window > DeltaContract > Lasso Probe
//   3) slat coords JSON 을 고르고 [드로잉 시작] → Scene 뷰에서 드래그 → [판정]

#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEngine;

namespace DeltaContract.EditorTools
{
    public class LassoProbeWindow : EditorWindow
    {
        string _coordsPath = "";
        string _outPath = "";
        Transform _target;
        readonly List<Vector3Int> _coords = new List<Vector3Int>();
        readonly List<Vector2> _polygon = new List<Vector2>();
        bool _drawing;
        LassoMaskResult _result;

        [MenuItem("Window/DeltaContract/Lasso Probe")]
        static void Open() => GetWindow<LassoProbeWindow>("Lasso Probe");

        void OnEnable() { SceneView.duringSceneGui += OnScene; }
        void OnDisable() { SceneView.duringSceneGui -= OnScene; }

        void OnGUI()
        {
            EditorGUILayout.HelpBox(
                "🔴 D58: 정점이 아니라 **slat 복셀**을 투영한다. JSON 은 자산의 slat coords 다.",
                MessageType.Info);

            using (new EditorGUILayout.HorizontalScope())
            {
                _coordsPath = EditorGUILayout.TextField("slat coords JSON", _coordsPath);
                if (GUILayout.Button("…", GUILayout.Width(28)))
                    _coordsPath = EditorUtility.OpenFilePanel("slat coords", "", "json");
            }
            if (GUILayout.Button("불러오기")) LoadCoords();
            EditorGUILayout.LabelField("복셀", _coords.Count.ToString());

            _target = (Transform)EditorGUILayout.ObjectField(
                "자산 Transform", _target, typeof(Transform), true);

            using (new EditorGUI.DisabledScope(_coords.Count == 0 || _target == null))
            {
                if (GUILayout.Button(_drawing ? "드로잉 중 — 클릭해서 중단" : "드로잉 시작"))
                {
                    _drawing = !_drawing;
                    if (_drawing) _polygon.Clear();
                    SceneView.RepaintAll();
                }
                EditorGUILayout.LabelField("폴리곤 점", _polygon.Count.ToString());
                using (new EditorGUI.DisabledScope(_polygon.Count < 3))
                    if (GUILayout.Button("판정")) Pick();
            }

            if (_result == null) return;
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("셀 수", _result.Cells.Count.ToString());
            EditorGUILayout.LabelField("grid_source", _result.GridSource);
            EditorGUILayout.SelectableLabel("지문 " + _result.Fingerprint,
                GUILayout.Height(EditorGUIUtility.singleLineHeight));
            EditorGUILayout.LabelField(
                $"투영 {_result.Projected} · 뒤 {_result.BehindCamera} · 폴리곤안 {_result.InPolygon}");
            EditorGUILayout.LabelField(
                $"압출 +{_result.SolidifyAdded} · 교집합제거 {_result.IntersectRemoved} · 축 {_result.DominantAxis}");
            EditorGUILayout.HelpBox(
                "껍질 표현에서는 압출이 넣은 셀을 교집합이 도로 지운다 — 버그가 아니다 " +
                "(LassoVolume 머리말). 두 수가 같으면 압출이 이번엔 일을 안 한 것이다.",
                MessageType.None);

            _outPath = EditorGUILayout.TextField("저장 경로", _outPath);
            if (GUILayout.Button("JSON 으로 저장")) Save();
        }

        void LoadCoords()
        {
            _coords.Clear();
            _result = null;
            if (string.IsNullOrEmpty(_coordsPath) || !File.Exists(_coordsPath)) return;
            // 형식: {"slat_coords": [[x,y,z], …]}  또는 그냥  [[x,y,z], …]
            var text = File.ReadAllText(_coordsPath);
            var nums = new List<int>();
            var cur = new StringBuilder();
            bool started = text.IndexOf("slat_coords", StringComparison.Ordinal) < 0;
            foreach (var ch in text)
            {
                if (!started) { if (ch == '[') started = true; continue; }
                if (ch == '-' || char.IsDigit(ch)) cur.Append(ch);
                else
                {
                    if (cur.Length > 0) { nums.Add(int.Parse(cur.ToString())); cur.Clear(); }
                }
            }
            if (cur.Length > 0) nums.Add(int.Parse(cur.ToString()));
            for (int i = 0; i + 2 < nums.Count; i += 3)
                _coords.Add(new Vector3Int(nums[i], nums[i + 1], nums[i + 2]));
        }

        void OnScene(SceneView view)
        {
            if (!_drawing) return;
            var e = Event.current;
            HandleUtility.AddDefaultControl(GUIUtility.GetControlID(FocusType.Passive));

            if (e.type == EventType.MouseDown && e.button == 0) _polygon.Clear();
            if ((e.type == EventType.MouseDrag || e.type == EventType.MouseDown) && e.button == 0)
            {
                // 🔴 GUI 좌표는 **좌상단 원점**, Camera.WorldToScreenPoint 는 **좌하단** 원점이다.
                //    뒤집지 않으면 폴리곤이 상하 반전돼 엉뚱한 부분이 잡히고, 예외는 안 난다.
                var cam = view.camera;
                var p = e.mousePosition * EditorGUIUtility.pixelsPerPoint;
                _polygon.Add(new Vector2(p.x, cam.pixelHeight - p.y));
                e.Use();
                view.Repaint();
            }

            Handles.BeginGUI();
            if (_polygon.Count > 1)
            {
                var pts = new Vector3[_polygon.Count + 1];
                for (int i = 0; i < _polygon.Count; i++)
                    pts[i] = new Vector3(_polygon[i].x / EditorGUIUtility.pixelsPerPoint,
                        (view.camera.pixelHeight - _polygon[i].y) / EditorGUIUtility.pixelsPerPoint, 0);
                pts[_polygon.Count] = pts[0];
                Handles.color = Color.yellow;
                Handles.DrawAAPolyLine(3f, pts);
            }
            Handles.EndGUI();
        }

        void Pick()
        {
            var cam = SceneView.lastActiveSceneView.camera;
            var tr = _target;
            Func<Vector3, Vector3> project = local => cam.WorldToScreenPoint(tr.TransformPoint(local));
            // 카메라 전방을 **오브젝트 프레임으로** 옮긴다 — 압출 축은 대상 프레임 기준이다.
            var viewDirLocal = tr.InverseTransformDirection(cam.transform.forward);
            _result = SlatLassoPicker.Pick(_coords, _polygon, project, viewDirLocal);
            Debug.Log($"[LassoProbe] 셀 {_result.Cells.Count} · 지문 {_result.Fingerprint}");
        }

        void Save()
        {
            if (_result == null) return;
            if (string.IsNullOrEmpty(_outPath))
                _outPath = EditorUtility.SaveFilePanel("마스크 저장", "", "lasso_mask", "json");
            if (string.IsNullOrEmpty(_outPath)) return;

            var sb = new StringBuilder();
            sb.Append("{\"grid_source\":\"").Append(_result.GridSource)
              .Append("\",\"mask_fingerprint\":\"").Append(_result.Fingerprint)
              .Append("\",\"n_cells\":").Append(_result.Cells.Count)
              .Append(",\"cells\":[");
            for (int i = 0; i < _result.Cells.Count; i++)
            {
                var c = _result.Cells[i];
                if (i > 0) sb.Append(',');
                sb.Append('[').Append(c.x).Append(',').Append(c.y).Append(',').Append(c.z).Append(']');
            }
            sb.Append("]}");
            File.WriteAllText(_outPath, sb.ToString());
            Debug.Log($"[LassoProbe] 저장 {_outPath}");
        }
    }
}
#endif
