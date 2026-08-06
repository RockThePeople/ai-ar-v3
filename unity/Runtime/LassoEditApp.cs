// LassoEditApp — 사용자 명세대로의 **앱 화면**. 검사 하네스가 아니다 (W23).
//
// 명세(사용자 원문):
//   · 자연어로 희망 오브젝트를 생성
//   · 생성되면 편집 기능 사용 가능 (연필 이모티콘으로 on/off)
//     · 편집 범위는 화면을 Lasso 로 드래그 → 볼륨을 자동 인식
//     · 볼륨 마스킹 상태에서 지우개 사용 (드래그하면 **지나간 궤적**의 마스킹이 제거)
//     · 편집 범위 확정 버튼 → 어떻게 수정할지 자연어로 입력
//
// 🔴 이번 웨이브는 **서버를 부르지 않는다.** 화면만 세운다. 다음 웨이브가 배선이다.
//
// 🔴 표현이 둘이고 **프레임이 같아야 한다** (W22 에서 갈렸던 자리):
//     판정용  slat coords  — 라쏘가 투영해 고르는 대상. **화면에 안 그린다**
//     표시용  .cbin 청크   — 사람이 보는 것. 청크 1개 = GameObject 1개
//    둘 다 `ToUnity()` 하나만 탄다. 다르면 화면과 판정이 갈리고 **예외가 안 난다.**
//
// ⚠️ 지우개는 **라쏘가 아니다.** 폴리곤 내부 판정을 쓰지 않는다. 손가락이 지나간
//    점들 반경 R px 안에 투영되는 **선택된 복셀**만 뺀다. 스트로크를 닫을 필요가 없다.

using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEngine;

namespace DeltaContract
{
    public sealed class LassoEditApp : MonoBehaviour
    {
        public string CaseFile = "moto-rear-wheel.case";   // slat coords 공급원
        public string ChunkList = "chunks.txt";
        public Camera ViewCamera;

        const string Tag = "[LassoApp]";
        enum Tool { None, Lasso, Eraser }

        // ── 자산
        readonly List<Vector3Int> _coords = new List<Vector3Int>();
        readonly Dictionary<string, Renderer> _chunkRenderers = new Dictionary<string, Renderer>();
        Transform _root;
        LassoCase _case;

        // ── 편집 상태
        readonly HashSet<Vector3Int> _selected = new HashSet<Vector3Int>();
        readonly List<Vector2> _stroke = new List<Vector2>();
        Tool _tool = Tool.None;
        bool _editOn;                     // ✏️ 토글
        bool _eraserOn;                   // 지우개 토글
        float _brush = 90f;               // 지우개 반경(px). 화면에 드러낸다
        bool _devOn;                      // 개발자 토글 — 지문·계수는 이 뒤로
        string _genPrompt = "";           // 생성 자연어
        string _editPrompt = "";          // 편집 자연어
        bool _askEdit;                    // [편집 범위 확정] 후 입력창
        string _notice = "";
        string _dev = "";

        // ── 보기
        float _orbit = 180f, _pitch = 12f, _dist = 4.0f;   // 측면 시작 (골든과 같은 방향)
        Vector2 _lastDrag; bool _dragging;
        // 🔴 선택 표시는 **Texture3D 마스크**다 (64³ R8). 청크 틴트도 큐브도 아니다 —
        //    사용자 확정: "평소엔 복셀이 안 보이고, 선택하면 그 볼륨이 하이라이트로".
        //    셰이더가 복셀 격자와 1:1 로 샘플링하므로 경계가 복셀 단위로 선다.
        Texture3D _selMask;
        byte[] _maskBytes;
        bool _maskDirty;
        static readonly int SelMaskId = Shader.PropertyToID("_SelMask");
        static readonly int SelMaskOnId = Shader.PropertyToID("_SelMaskOn");

        // 되돌리기 — 라쏘 **한 획씩**과 전체 초기화 둘 다 둔다 (명세에 없는 항목).
        //   지우개로만 되돌리게 하면 크게 잘못 잡았을 때 손해가 너무 크다.
        readonly List<List<Vector3Int>> _undo = new List<List<Vector3Int>>();

        TouchScreenKeyboard _kb;      // 🔴 IMGUI TextField 는 안드로이드에서 키보드를 안 띄운다
        int _kbTarget;                // 1 = 생성 프롬프트, 2 = 편집 프롬프트

        // 🔴 AR 배치. 라쏘(상시 드래그)와 배치 탭이 **같은 입력을 두고 싸운다** —
        //    ai-ar-v2 가 "EDIT_ON 에서 빈 공간 탭 시 재배치" 로 물린 자리다.
        //    모드를 배타적으로 나눈다: 배치 중에는 편집 입력을 아예 안 받는다.
        ArPlacement _ar;
        string _poseLog = "";

        // 🔴 D9 변환은 `VoxelFrame` 하나뿐이다. 판정용 slat 좌표도 표시용 `.cbin`
        //    정점도 같은 함수를 탄다 — 다르면 화면과 판정이 갈리고 예외가 안 난다 (W22).
        static Vector3 ToUnity(Vector3 v) => VoxelFrame.ToUnity(v);
        static Vector3 ToVoxel(Vector3 v) => VoxelFrame.ToVoxel(v);

        void Start()
        {
            _root = new GameObject("asset").transform;
            InitMask();

            _ar = gameObject.AddComponent<ArPlacement>();
            _ar.Initialize(Cam());
            if (_ar.Content != null) _root.SetParent(_ar.Content, false);
            LoadCoords();
            LoadChunks();
            AimCamera();
        }

        // ══════════════════════════ 적재
        void LoadCoords()
        {
            var text = Read(Path.Combine(Application.streamingAssetsPath, CaseFile));
            if (string.IsNullOrEmpty(text)) { Debug.LogError($"{Tag} 케이스 없음"); return; }
            _case = LassoCase.Parse(text);
            _coords.AddRange(_case.Coords);
            Debug.Log($"{Tag} 판정용 복셀 {_coords.Count}");
        }

        /// <summary>`.cbin` 은 **이미 메시**다 — POSITION/NORMAL/COLOR/UV/INDEX.
        /// 큐브를 세우지 않는다. 디코드해서 그대로 그린다.</summary>
        void LoadChunks()
        {
            var list = Read(Path.Combine(Application.streamingAssetsPath, ChunkList));
            if (string.IsNullOrEmpty(list)) { Debug.LogWarning($"{Tag} chunks.txt 없음"); return; }
            var shader = Shader.Find("DeltaContract/ChunkSurface");
            var mat = new Material(shader);
            int verts = 0, colored = 0;

            foreach (var raw in list.Split('\n'))
            {
                var key = raw.Trim();
                if (key.Length == 0) continue;
                var bytes = ReadBytes(Path.Combine(Application.streamingAssetsPath, "chunks/" + key + ".cbin"));
                if (bytes == null || bytes.Length == 0) continue;

                var data = ChunkBin.Decode(bytes);           // 계약 코드. 재구현하지 않는다
                for (int i = 0; i < data.Positions.Length; i++)
                    data.Positions[i] = ToUnity(data.Positions[i]);
                if (data.Normals != null)
                    for (int i = 0; i < data.Normals.Length; i++)
                        data.Normals[i] = ToUnity(data.Normals[i]);

                var go = new GameObject("chunk_" + key);      // 청크 1개 = GameObject 1개
                go.transform.SetParent(_root, false);
                var mesh = new Mesh { name = "cbin_" + key };
                ChunkBin.ApplyTo(mesh, data);
                if (data.Normals == null) mesh.RecalculateNormals();
                go.AddComponent<MeshFilter>().sharedMesh = mesh;
                var mr = go.AddComponent<MeshRenderer>();
                mr.sharedMaterial = mat;
                _chunkRenderers[key] = mr;

                verts += data.Positions.Length;
                if (data.Colors != null && data.Colors.Length > 0) colored++;
            }
            Debug.Log($"{Tag} 청크 {_chunkRenderers.Count}개 · 정점 {verts:N0} · " +
                      $"색 있는 청크 {colored}/{_chunkRenderers.Count} · 셰이더 " +
                      (shader != null ? shader.name : "NULL"));
        }

        void AimCamera()
        {
            // 🔴 AR 이면 카메라를 **우리가 옮기지 않는다.** 세션이 자세를 넣는다.
            if (_ar != null && _ar.ArActive) return;
            var cam = Cam(); if (cam == null) return;
            cam.clearFlags = CameraClearFlags.SolidColor;
            cam.backgroundColor = Color.white;
            cam.nearClipPlane = 0.01f; cam.farClipPlane = 100f;
            cam.fieldOfView = 35f;
            PlaceCamera();
        }

        Camera Cam() => ViewCamera != null ? ViewCamera : Camera.main;

        void PlaceCamera()
        {
            var cam = Cam(); if (cam == null) return;
            float a = _orbit * Mathf.Deg2Rad, p = _pitch * Mathf.Deg2Rad;
            cam.transform.position =
                new Vector3(Mathf.Cos(a) * Mathf.Cos(p), Mathf.Sin(p), Mathf.Sin(a) * Mathf.Cos(p)) * _dist;
            cam.transform.LookAt(Vector3.zero, Vector3.up);
        }

        // ══════════════════════════ 입력
        void Update()
        {
            PumpKeyboard();
            if (Input.touchCount == 0) { _dragging = false; return; }
            var t = Input.GetTouch(0);
            if (t.position.y > Screen.height - 300f || t.position.y < 260f) return;  // UI 영역

            // 🔴 **손가락 회전을 뺐다** (사용자 지시 · W26b).
            //    "실제로 사람이 움직이며 봐야 함. 즉 Anchoring 할 것."
            //    이건 기능 삭제가 아니라 **검증 장치**다 — 손가락으로 돌릴 수 있으면
            //    앵커링이 진짜인지 화면에서 구분이 안 된다.
            //    **회전이 없는데도 걸어가면 반대편이 보인다** 가 (D) 의 증명이다.
            if (!_editOn) return;

            switch (t.phase)
            {
                case TouchPhase.Began:
                    _tool = _eraserOn ? Tool.Eraser : Tool.Lasso;
                    _stroke.Clear(); _stroke.Add(t.position);
                    if (_tool == Tool.Eraser) EraseAlong(t.position);
                    break;
                case TouchPhase.Moved:
                    _stroke.Add(t.position);
                    if (_tool == Tool.Eraser) EraseAlong(t.position);   // 궤적을 따라 즉시 지운다
                    break;
                case TouchPhase.Ended:
                case TouchPhase.Canceled:
                    if (_tool == Tool.Lasso && _stroke.Count >= 3) AddByLasso();
                    _tool = Tool.None;
                    _stroke.Clear();
                    break;
            }
        }

        // ══════════════════════════ 라쏘 — 볼륨 인식 (더한다)
        void AddByLasso()
        {
            var cam = Cam(); if (cam == null || _case == null) return;
            var tr = _root;
            System.Func<Vector3, Vector3> project =
                local => cam.WorldToScreenPoint(tr.TransformPoint(ToUnity(local)));
            var viewDir = ToVoxel(tr.InverseTransformDirection(cam.transform.forward));

            var r = SlatLassoPicker.Pick(_coords, _stroke, project, viewDir);
            int before = _selected.Count;
            var addedNow = new List<Vector3Int>();
            foreach (var c in r.Cells)
                if (_selected.Add(c)) addedNow.Add(c);        // 다시 그리면 **더한다**
            if (addedNow.Count > 0) _undo.Add(addedNow);      // 한 획 = 되돌리기 한 단계

            _dev = $"라쏘 +{_selected.Count - before} · 폴리곤안 {r.InPolygon} · " +
                   $"압출 +{r.SolidifyAdded}/-{r.IntersectRemoved} · 축 {r.DominantAxis}\n" +
                   $"지문 {SlatLassoPicker.MaskFingerprint(SelectedList())}";
            Debug.Log($"{Tag} 라쏘 +{_selected.Count - before} → 선택 {_selected.Count}");
            RebuildMask();
        }

        // ══════════════════════════ 지우개 — **궤적**을 지운다 (영역이 아니다)
        void EraseAlong(Vector2 p)
        {
            var cam = Cam(); if (cam == null) return;
            var tr = _root;
            float r2 = _brush * _brush;
            int removed = 0;

            var doomed = new List<Vector3Int>();
            foreach (var c in _selected)
            {
                var sp = cam.WorldToScreenPoint(tr.TransformPoint(ToUnity(SlatLassoPicker.VoxelCenter(c))));
                if (sp.z <= 0f) continue;                      // 카메라 뒤
                float dx = sp.x - p.x, dy = sp.y - p.y;
                if (dx * dx + dy * dy <= r2) doomed.Add(c);
            }
            foreach (var c in doomed) { _selected.Remove(c); removed++; }
            if (removed > 0) { Debug.Log($"{Tag} 지우개 −{removed} → 선택 {_selected.Count}"); RebuildMask(); }
        }

        // ══════════════════════════ 선택 마스크 (Texture3D)
        void InitMask()
        {
            int res = DeltaConstants.VoxelRes;
            _selMask = new Texture3D(res, res, res, TextureFormat.R8, false)
            {
                filterMode = FilterMode.Point,    // 복셀 경계를 뭉개지 않는다
                wrapMode = TextureWrapMode.Clamp,
            };
            _maskBytes = new byte[res * res * res];
            PushMask();
            Shader.SetGlobalFloat(SelMaskOnId, 1f);
        }

        void PushMask()
        {
            _selMask.SetPixelData(_maskBytes, 0);
            _selMask.Apply(false, false);
            Shader.SetGlobalTexture(SelMaskId, _selMask);
        }

        /// <summary>선택 집합 → 마스크 바이트. 바뀐 프레임에만 올린다.</summary>
        void RebuildMask()
        {
            int res = DeltaConstants.VoxelRes;
            System.Array.Clear(_maskBytes, 0, _maskBytes.Length);
            foreach (var c in _selected)
            {
                if (c.x < 0 || c.y < 0 || c.z < 0) continue;
                if (c.x >= res || c.y >= res || c.z >= res) continue;
                _maskBytes[c.x + res * (c.y + res * c.z)] = 255;
            }
            _maskDirty = true;
            RecountChunks();
        }

        /// <summary>선택이 걸친 청크 수 — 사람이 오염을 판단할 근거로 계속 보여 준다.</summary>
        void RecountChunks()
        {
            // 🔴 청크 크기를 손으로 적지 않는다. v4 에서 8 → 4 로 바뀌었다 (D75).
            int cs = DeltaConstants.ChunkSize;
            var hit = new HashSet<int>();
            int g = DeltaConstants.ChunkGridRes;
            foreach (var c in _selected)
                hit.Add((c.x / cs) + g * ((c.y / cs) + g * (c.z / cs)));
            _tintedChunks = hit.Count;
        }

        void LateUpdate()
        {
            if (!_maskDirty) return;
            PushMask();
            _maskDirty = false;
        }

        // ══════════════════════════ (옛 청크 틴트는 걷어냈다 — 표면 하이라이트가 대신한다)
        int _tintedChunks;

        /// <summary>라쏘 **한 획**을 되돌린다. 지우개로 이미 빠진 셀은 건너뛴다.</summary>
        void UndoStroke()
        {
            if (_undo.Count == 0) return;
            var last = _undo[_undo.Count - 1];
            _undo.RemoveAt(_undo.Count - 1);
            int n = 0;
            foreach (var c in last) if (_selected.Remove(c)) n++;
            Debug.Log($"{Tag} 되돌리기 −{n} → 선택 {_selected.Count} (남은 획 {_undo.Count})");
            RebuildMask();
        }

        void ClearSelection()
        {
            Debug.Log($"{Tag} 전체 해제 −{_selected.Count}");
            _selected.Clear();
            _undo.Clear();
            RebuildMask();
        }

        List<Vector3Int> SelectedList()
        {
            var l = new List<Vector3Int>(_selected); return l;
        }

        // ══════════════════════════ UI
        /// <summary>🔴 IMGUI `TextField` 는 안드로이드에서 **소프트 키보드를 안 띄운다.**
        /// 창은 열리는데 글자가 안 들어가고 **예외는 안 난다** — W23 에서 실기로 확인했다.
        /// 그래서 키보드를 직접 열고, 그 결과를 매 프레임 받아 적는다.</summary>
        void OpenKeyboard(int target, string initial, string placeholder)
        {
            _kbTarget = target;
            _kb = TouchScreenKeyboard.Open(initial ?? "", TouchScreenKeyboardType.Default,
                                           false, false, false, false, placeholder);
        }

        void PumpKeyboard()
        {
            if (_kb == null) return;
            if (_kbTarget == 1) _genPrompt = _kb.text;
            else if (_kbTarget == 2) _editPrompt = _kb.text;

            if (_kb.status == TouchScreenKeyboard.Status.Done
                || _kb.status == TouchScreenKeyboard.Status.Canceled
                || !TouchScreenKeyboard.visible)
            {
                Debug.Log($"{Tag} 키보드 닫힘 status={_kb.status} 길이={_kb.text.Length}");
                _kb = null;
            }
        }

        void OnGUI()
        {
            float W = Screen.width, H = Screen.height;
            GUI.skin.label.fontSize = 30; GUI.skin.button.fontSize = 32;
            GUI.skin.textField.fontSize = 32; GUI.skin.toggle.fontSize = 30;

            // ── 상단: 자연어 생성
            GUI.Box(new Rect(0, 0, W, 210), GUIContent.none);
            GUI.Label(new Rect(24, 12, W - 48, 40), "만들고 싶은 오브젝트를 말해라");
            if (GUI.Button(new Rect(24, 56, W - 250, 76),
                           string.IsNullOrEmpty(_genPrompt) ? "  (탭해서 입력)" : "  " + _genPrompt,
                           GUI.skin.textField))
                OpenKeyboard(1, _genPrompt, "예: 빨간 오토바이");
            if (GUI.Button(new Rect(W - 214, 56, 190, 76), "생성"))
                _notice = "생성 호출은 다음 웨이브다. 지금은 moto-b 를 이미 생성된 것으로 띄운다";
            GUI.Label(new Rect(24, 142, W - 48, 60),
                $"선택 {_selected.Count} 셀 · 청크 {_tintedChunks}" +
                (string.IsNullOrEmpty(_notice) ? "" : "   " + _notice));

            // 🔴 AR 상태를 **항상** 보인다. 폴백인 줄 모르고 "AR 됐다" 고 하지 않게.
            if (_ar != null)
            {
                var prev = GUI.color;
                GUI.color = _ar.ArActive ? Color.white : new Color(1f, 0.55f, 0.2f);
                GUI.Label(new Rect(24, 198, W - 48, 46), _ar.Status);
                GUI.color = prev;
                if (!string.IsNullOrEmpty(_poseLog))
                    GUI.Label(new Rect(24, 244, W - 48, 46), _poseLog);
            }

            // ── 하단 도구
            float y = H - 250;
            bool edit = GUI.Toggle(new Rect(24, y, 250, 90), _editOn,
                _editOn ? "편집 ON" : "편집 OFF", GUI.skin.button);
            if (edit != _editOn) { _editOn = edit; if (!_editOn) _eraserOn = false; }

            using (new GUIEnabled(_editOn))
            {
                bool er = GUI.Toggle(new Rect(290, y, 250, 90), _eraserOn,
                    _eraserOn ? "지우개 ON" : "지우개", GUI.skin.button);
                _eraserOn = _editOn && er;
                if (_eraserOn)
                {
                    GUI.Label(new Rect(560, y - 4, 200, 40), $"굵기 {(int)_brush}");
                    _brush = GUI.HorizontalSlider(new Rect(560, y + 46, W - 600, 40), _brush, 30f, 220f);
                }
                else if (GUI.Button(new Rect(560, y, W - 590, 90), "편집 범위 확정"))
                {
                    _askEdit = _selected.Count > 0;
                    _notice = _selected.Count == 0 ? "먼저 라쏘로 범위를 잡아라" : "";
                    if (_askEdit) OpenKeyboard(2, _editPrompt, "예: 이 바퀴를 주황색으로");
                }
            }

            // ── 되돌리기 (명세엔 없다. 지우개로만 되돌리면 크게 잘못 잡았을 때 손해가 크다)
            using (new GUIEnabled(_editOn && _undo.Count > 0))
                if (GUI.Button(new Rect(24, y - 100, 250, 84), $"되돌리기 ({_undo.Count})"))
                    UndoStroke();
            using (new GUIEnabled(_editOn && _selected.Count > 0))
                if (GUI.Button(new Rect(290, y - 100, 250, 84), "전체 해제"))
                    ClearSelection();

            GUI.Label(new Rect(24, H - 140, W - 260, 40),
                _editOn ? (_eraserOn ? "지나간 궤적의 마스킹이 지워진다"
                                     : "라쏘로 감싸면 볼륨이 선택된다 (다시 그리면 더해진다)")
                        : "걸어서 둘러봐라 — 화면을 돌리지 않는다. [편집] 을 켜면 라쏘");
            _devOn = GUI.Toggle(new Rect(W - 230, H - 150, 210, 60), _devOn, " 개발자");

            // 재배치는 **명시적 버튼으로만** 연다 (라쏘 드래그가 배치를 건드리지 않게)
            if (_ar != null && _ar.CurrentMode == ArPlacement.Mode.Placed)
                if (GUI.Button(new Rect(W - 470, H - 152, 230, 66), "재배치"))
                {
                    _editOn = false; _eraserOn = false;      // 편집을 끄고 배치로 넘어간다
                    _ar.BeginRelocate();
                }

            // ── 편집 자연어 입력창
            if (_askEdit)
            {
                var box = new Rect(40, H * 0.32f, W - 80, 420);
                GUI.Box(box, GUIContent.none);
                GUI.Label(new Rect(box.x + 24, box.y + 20, box.width - 48, 44),
                          $"선택 {_selected.Count} 셀 · 어떻게 수정할까");
                if (GUI.Button(new Rect(box.x + 24, box.y + 82, box.width - 48, 90),
                               string.IsNullOrEmpty(_editPrompt) ? "  (탭해서 입력)" : "  " + _editPrompt,
                               GUI.skin.textField))
                    OpenKeyboard(2, _editPrompt, "예: 이 바퀴를 주황색으로");
                if (GUI.Button(new Rect(box.x + 24, box.y + 200, (box.width - 72) / 2, 90), "확인"))
                {
                    _notice = $"“{_editPrompt}” — 전송은 다음 웨이브다";
                    Debug.Log($"{Tag} 편집 지시 «{_editPrompt}» · 선택 {_selected.Count}셀");
                    // ★ (C) in-place 의 AR 쪽 절반 — 편집을 거친 뒤 앵커가 그대로인가.
                    if (_ar != null)
                    {
                        var (dp, dr) = _ar.PoseDelta();
                        _poseLog = $"앵커 델타 {dp * 1000f:F2}mm / {dr:F3}°";
                        Debug.Log($"{Tag} POSE-DELTA {_poseLog} · {_ar.AnchorInfo()}");
                    }
                    _askEdit = false;
                }
                if (GUI.Button(new Rect(box.x + 48 + (box.width - 72) / 2, box.y + 200, (box.width - 72) / 2, 90), "취소"))
                    _askEdit = false;
            }

            if (_devOn)
                GUI.Label(new Rect(24, 296, W - 48, 220),
                          $"복셀 {_coords.Count} · 청크 {_chunkRenderers.Count} · " +
                          $"스케일 {AssetScale.FootprintMeters}m · 평면 {(_ar != null ? _ar.PlaneCount : 0)}\n" +
                          $"{(_ar != null ? _ar.AnchorInfo() : "")}\n{_dev}");

            DrawStroke();
        }

        /// <summary>궤적과 **브러시 크기**를 그린다 — 안 보이면 사람이 조준할 수 없다.</summary>
        void DrawStroke()
        {
            if (_stroke.Count == 0) return;
            for (int i = 1; i < _stroke.Count; i++)
                Line(Flip(_stroke[i - 1]), Flip(_stroke[i]),
                     _tool == Tool.Eraser ? new Color(1f, 0.2f, 0.2f) : Color.yellow,
                     _tool == Tool.Eraser ? 6f : 4f);

            if (_tool == Tool.Eraser)
            {
                var c = Flip(_stroke[_stroke.Count - 1]);
                var r = _brush;
                // 원을 16각형으로 — 브러시 반경을 눈으로 확인할 수 있어야 한다
                for (int i = 0; i < 16; i++)
                {
                    float a0 = i * Mathf.PI / 8, a1 = (i + 1) * Mathf.PI / 8;
                    Line(c + new Vector2(Mathf.Cos(a0), Mathf.Sin(a0)) * r,
                         c + new Vector2(Mathf.Cos(a1), Mathf.Sin(a1)) * r,
                         new Color(1f, 0.35f, 0.35f, 0.9f), 3f);
                }
            }
        }

        Vector2 Flip(Vector2 p) => new Vector2(p.x, Screen.height - p.y);

        static Texture2D _px;
        static void Line(Vector2 a, Vector2 b, Color col, float w)
        {
            if (_px == null) { _px = new Texture2D(1, 1); _px.SetPixel(0, 0, Color.white); _px.Apply(); }
            var d = b - a; var m = GUI.matrix; var old = GUI.color;
            GUI.color = col;
            GUIUtility.RotateAroundPivot(Mathf.Atan2(d.y, d.x) * Mathf.Rad2Deg, a);
            GUI.DrawTexture(new Rect(a.x, a.y - w / 2, d.magnitude, w), _px);
            GUI.matrix = m; GUI.color = old;
        }

        struct GUIEnabled : System.IDisposable
        {
            readonly bool _prev;
            public GUIEnabled(bool on) { _prev = GUI.enabled; GUI.enabled = on; }
            public void Dispose() { GUI.enabled = _prev; }
        }

        // ══════════════════════════ StreamingAssets (Android 는 APK 안이다)
        static string Read(string path)
        {
            if (path.Contains("://"))
                using (var q = UnityEngine.Networking.UnityWebRequest.Get(path))
                {
                    q.SendWebRequest(); while (!q.isDone) { }
                    return q.result == UnityEngine.Networking.UnityWebRequest.Result.Success
                        ? q.downloadHandler.text : "";
                }
            return File.Exists(path) ? File.ReadAllText(path) : "";
        }

        static byte[] ReadBytes(string path)
        {
            if (path.Contains("://"))
                using (var q = UnityEngine.Networking.UnityWebRequest.Get(path))
                {
                    q.SendWebRequest(); while (!q.isDone) { }
                    return q.result == UnityEngine.Networking.UnityWebRequest.Result.Success
                        ? q.downloadHandler.data : null;
                }
            return File.Exists(path) ? File.ReadAllBytes(path) : null;
        }
    }
}
