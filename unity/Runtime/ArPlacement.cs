// ArPlacement — plane 감지 → **탭 배치** → ARAnchor (W26① · 대목표 (D)).
//
// 🔴 이번 웨이브의 진짜 어려움은 **입력 충돌**이다.
//    라쏘는 상시 드래그이고 배치도 화면 터치다. ai-ar-v2 가 정확히 이걸로 물렸다 —
//    "EDIT_ON 상태에서 빈 공간을 탭하면 오브젝트가 재배치되던" 버그. 거기선 탭이라
//    드물었지만 여기선 라쏘가 **매번** 드래그라 훨씬 자주 터진다.
//
//    ⇒ **모드를 배타적으로 나눈다.** 배치가 끝나면 배치 모드를 닫고, 재배치는
//      명시적 버튼으로만 연다. 배치 모드에서는 편집 입력을 아예 안 받는다.
//
// ⚠️ **자동 폴백을 기본 경로에 두지 않는다** (사용자 지시). 평면이 없을 때 카메라 앞
//    고정 거리에 놓으면 그건 정의상 **화면에 붙은 물체**이지 AR 이 아니다. 폴백은
//    두되 화면에 "AR 아님" 을 상시 표시해서, 폴백인 줄 모르고 "AR 됐다" 고
//    보고하는 일이 없게 한다.
//
// ★ 재는 것: **앵커 pose 델타**. 배치 시점 pose 를 기록하고 편집을 거친 뒤 다시 읽는다.
//   0 이어야 한다 — 이것이 (C) in-place 의 AR 쪽 절반이다.

using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;
using Unity.XR.CoreUtils;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.XR;

namespace DeltaContract
{
    public sealed class ArPlacement : MonoBehaviour
    {
        public enum Mode { Placing, Placed }

        public const string Tag = "[ArPlace]";

        public Mode CurrentMode { get; private set; } = Mode.Placing;
        public bool ArActive { get; private set; }          // false = 폴백 (AR 아님)
        public string Status { get; private set; } = "AR 시작 중…";
        public int PlaneCount { get; private set; }
        public Transform Content { get; private set; }      // 자산 루트가 붙는 자리

        ARSession _session;
        XROrigin _origin;
        ARPlaneManager _planes;
        ARRaycastManager _raycasts;
        ARAnchor _anchor;
        Camera _cam;

        readonly List<ARRaycastHit> _hits = new List<ARRaycastHit>();
        ARAnchorManager _anchors;
        TrackedPoseDriver _tpd;
        InputAction _posAction, _rotAction;
        float _camTravel;                    // 카메라가 실제로 이동한 누적 거리(m)
        Vector3 _lastCamPos;
        bool _hasLastCam;
        float _diagAt;

        // ★ 앵커 pose 기록 — 편집 전후 델타를 재려고 남긴다.
        Pose _poseAtPlacement;
        bool _hasBaseline;

        /// <summary>AR 을 세운다. 실패하면 폴백으로 내려가되 **그 사실을 감추지 않는다.**</summary>
        public void Initialize(Camera fallbackCamera)
        {
            _cam = fallbackCamera;
            if (!SetupAr())
            {
                ArActive = false;
                Status = "⚠️ AR 아님 — 폴백(카메라 앞 고정). 평면 위가 아니다";
                Debug.LogWarning($"{Tag} {Status}");
                return;
            }
            // 원인 2 — 세션이 붙은 뒤 한 번 더 못 박는다 (prototype 이 두 곳에서 하는 이유).
            if (_planes != null)
                _planes.requestedDetectionMode = PlaneDetectionMode.Horizontal | PlaneDetectionMode.Vertical;
            ArActive = true;
            Status = "평면을 비춰라 — 바닥/책상을 훑으면 감지된다";
            LogDiag();
        }

        bool SetupAr()
        {
            if (ARSession.state == ARSessionState.Unsupported)
            {
                Debug.LogWarning($"{Tag} ARSession.state = Unsupported");
                return false;
            }

            var sessionGo = new GameObject("ARSession");
            _session = sessionGo.AddComponent<ARSession>();
            sessionGo.AddComponent<ARInputManager>();

            var originGo = new GameObject("XROrigin");
            _origin = originGo.AddComponent<XROrigin>();

            // AR 카메라는 세션이 자세를 넣어 준다 — 우리가 옮기지 않는다.
            var camOffset = new GameObject("Camera Offset");
            camOffset.transform.SetParent(originGo.transform, false);
            _origin.CameraFloorOffsetObject = camOffset;

            if (_cam != null)
            {
                _cam.transform.SetParent(camOffset.transform, false);
                _cam.transform.localPosition = Vector3.zero;
                _cam.transform.localRotation = Quaternion.identity;
                _cam.clearFlags = CameraClearFlags.SolidColor;   // 배경은 카메라 영상이 채운다
                _cam.backgroundColor = Color.black;
                _cam.nearClipPlane = 0.05f;
                _cam.farClipPlane = 20f;
                if (_cam.GetComponent<ARCameraManager>() == null) _cam.gameObject.AddComponent<ARCameraManager>();
                if (_cam.GetComponent<ARCameraBackground>() == null) _cam.gameObject.AddComponent<ARCameraBackground>();
                _origin.Camera = _cam;
                AttachPoseDriver(_cam.gameObject);          // 🔴 원인 1 — 이게 없으면 카메라가 원점에 고정된다
            }

            _planes = originGo.AddComponent<ARPlaneManager>();
            _planes.planePrefab = BuildOutlinePrefab();
            // 🔴 원인 2 — 미설정이면 기본값에 기댄다. 수평·수직을 **명시**한다.
            _planes.requestedDetectionMode = PlaneDetectionMode.Horizontal | PlaneDetectionMode.Vertical;
            _raycasts = originGo.AddComponent<ARRaycastManager>();
            _anchors = originGo.AddComponent<ARAnchorManager>();

            // 🔴 원인 5 — 프레임을 못 박는다. 기본값에 맡기면 30fps 로 떨어지고
            //    그 상태의 트래킹 품질을 "AR 이 나쁘다" 로 오해한다.
            Application.targetFrameRate = 60;

            Content = new GameObject("ArContent").transform;
            Content.gameObject.SetActive(false);   // 배치 전에는 안 보인다
            return true;
        }

        /// <summary>🔴 **원인 1 — `TrackedPoseDriver` 가 리포 전체에 0건이었다.**
        ///
        /// AF6 의 XROrigin 은 카메라를 스스로 움직이지 않는다. 드라이버가 없으면
        /// **카메라가 원점에 고정**된다 — 영상 나오고 평면 잡히고 예외도 없는데
        /// 오브젝트가 화면에 붙어 있다. ai-ar-prototype 이 한 사이클 태워 문서화한 "누끼".
        ///
        /// ⚠️ `AddComponent` 만 하면 **액션이 빈 채로** 붙는다. 바인딩을 코드로 넣는다.</summary>
        void AttachPoseDriver(GameObject camGo)
        {
            _tpd = camGo.GetComponent<TrackedPoseDriver>();
            if (_tpd == null) _tpd = camGo.AddComponent<TrackedPoseDriver>();

            _posAction = new InputAction("ARCameraPosition", InputActionType.Value, expectedControlType: "Vector3");
            _posAction.AddBinding("<XRHMD>/centerEyePosition");
            _posAction.AddBinding("<HandheldARInputDevice>/devicePosition");

            _rotAction = new InputAction("ARCameraRotation", InputActionType.Value, expectedControlType: "Quaternion");
            _rotAction.AddBinding("<XRHMD>/centerEyeRotation");
            _rotAction.AddBinding("<HandheldARInputDevice>/deviceRotation");

            _tpd.positionInput = new InputActionProperty(_posAction);
            _tpd.rotationInput = new InputActionProperty(_rotAction);
            _tpd.trackingType = TrackedPoseDriver.TrackingType.RotationAndPosition;
            _tpd.updateType = TrackedPoseDriver.UpdateType.UpdateAndBeforeRender;

            _posAction.Enable();
            _rotAction.Enable();
            Debug.Log($"{Tag} TrackedPoseDriver 부착 · pos bindings={_posAction.bindings.Count}");
        }

        /// <summary>★★ 세 겹이 한 줄에 드러나는 진단. **로그로 판정하지 않되, 진단은 로그로 한다.**</summary>
        void LogDiag()
        {
            int xr = 0;
            var names = new System.Text.StringBuilder();
            foreach (var d in InputSystem.devices)
            {
                bool isXr = d is UnityEngine.InputSystem.XR.XRHMD || d.description.interfaceName == "XRInput"
                            || d.name.Contains("Handheld") || d.name.Contains("XR");
                if (isXr) { xr++; names.Append(d.name).Append(' '); }
            }
            int posControls = _posAction != null ? _posAction.controls.Count : -1;
            var world = _cam != null ? _cam.transform.position : Vector3.zero;
            // camRel — 앵커 기준 상대 위치. world 와 **둘 다** 찍는다.
            var camRel = (_anchor != null && _cam != null)
                ? _anchor.transform.InverseTransformPoint(_cam.transform.position) : Vector3.zero;

            Debug.Log($"{Tag} DIAG dev={InputSystem.devices.Count} xr={xr} [{names}] " +
                      $"posControls={posControls} " +
                      $"world=({world.x:F3},{world.y:F3},{world.z:F3}) " +
                      $"camRel=({camRel.x:F3},{camRel.y:F3},{camRel.z:F3}) " +
                      $"travel={_camTravel:F3}m planes={PlaneCount} state={ARSession.state}");
        }

        /// <summary>평면 프리팹 — **윤곽선만**. MeshRenderer 를 안 붙이는 것이 요점이다
        /// (붙이는 순간 면이 찬다. 사용자가 면 채우기를 명시적으로 배제했다).</summary>
        static GameObject BuildOutlinePrefab()
        {
            var go = new GameObject("PlaneOutline");
            // 🔴 검증된 경로 (prototype ARSceneBuilder.cs:164-206): MeshRenderer 를 **끄는 게
            //    아니라 아예 안 붙인다.** ARPlaneMeshVisualizer.SetVisible 이 매번
            //    SetRendererEnabled<MeshRenderer>(true) 로 되켜는데, 그 함수가 `if (component)` 로
            //    null 을 건너뛰므로 안 붙이면 면이 **영원히** 안 그려진다.
            //    ⇒ MeshFilter 는 남긴다 — 레이캐스트가 그 메시를 친다.
            go.AddComponent<MeshFilter>();
            go.AddComponent<ARPlaneMeshVisualizer>();
            var line = go.AddComponent<LineRenderer>();
            // 🔴 빌트인 셰이더를 찾으면 IL2CPP 스트립에 걸려 **선이 안 보인다** (W26b 실기).
            //    리포에 둔 전용 셰이더 + Always Included 로 그 경로를 막는다.
            var sh = Shader.Find("DeltaContract/PlaneLine");
            if (sh == null)
            {
                Debug.LogError($"{Tag} PlaneLine 셰이더가 없다 — 윤곽선이 안 보인다 (스트립?)");
                sh = Shader.Find("Sprites/Default");
            }
            var col = new Color(0.15f, 0.85f, 1f, 1f);       // 청록 — 자산 하이라이트(주황)와 안 겹친다
            var mat = new Material(sh) { color = col };
            line.material = mat;
            line.startColor = line.endColor = col;
            go.AddComponent<PlaneOutline>();
            return go;                                        // 활성 상태로 둔다 — 꺼 두면 복제본도 꺼진 채 시작한다
        }

        void Update()
        {
            if (_planes != null) PlaneCount = _planes.trackables.count;

            // ★ 카메라 실제 이동 누적 — drift 를 말하려면 cam_travel ≥ 1.0m 가 전제다.
            if (_cam != null)
            {
                if (_hasLastCam) _camTravel += Vector3.Distance(_cam.transform.position, _lastCamPos);
                _lastCamPos = _cam.transform.position; _hasLastCam = true;
            }
            if (Time.unscaledTime - _diagAt > 3f) { _diagAt = Time.unscaledTime; LogDiag(); }

            if (CurrentMode == Mode.Placing)
            {
                Status = ArActive
                    ? (PlaneCount == 0 ? "평면 탐색 중… 바닥/책상을 천천히 훑어라"
                                       : $"평면 {PlaneCount}개 — 놓을 자리를 탭하라")
                    : "⚠️ AR 아님 — 폴백. 탭하면 카메라 앞에 놓는다";
            }
        }

        /// <summary>배치 탭. **배치 모드일 때만** 받는다 — 라쏘와 싸우지 않게.</summary>
        public bool TryPlace(Vector2 screenPos)
        {
            if (CurrentMode != Mode.Placing) return false;

            Pose pose;
            ARPlane hitPlane = null;
            // 🔴 PlaneWithinPolygon 만 쓰면 경계 다각형 안쪽만 맞는다 — 감지 초기의 작은
            //    평면에서는 사람이 "평면 위" 를 눌러도 빗나간다. 폴리곤 → 경계 → 추정 순으로 넓힌다.
            bool hitOk = ArActive && _raycasts != null &&
                (_raycasts.Raycast(screenPos, _hits, TrackableType.PlaneWithinPolygon)
                 || _raycasts.Raycast(screenPos, _hits, TrackableType.PlaneWithinBounds)
                 || _raycasts.Raycast(screenPos, _hits, TrackableType.PlaneEstimated))
                && _hits.Count > 0;
            Debug.Log($"{Tag} TAP ({screenPos.x:F0},{screenPos.y:F0}) ar={ArActive} " +
                      $"planes={PlaneCount} hits={_hits.Count} ok={hitOk}");
            if (hitOk)
            {
                // 🔴 원인 3 — 히트 pose 의 **회전을 그대로 쓰지 않는다.** 기울어진 평면에
                //    붙으면 오브젝트가 기울어 서고 "놓였다" 는 감각이 깨진다.
                //    v2·prototype 둘 다 법선을 버리고 **카메라를 향한 yaw** 만 쓴다.
                var hit = _hits[0];
                pose = new Pose(hit.pose.position, YawTowardCamera(hit.pose.position));
                hitPlane = hit.trackable as ARPlane;
            }
            else if (!ArActive && _cam != null)
            {
                // 폴백 — 카메라 앞 고정. **이것은 AR 이 아니다.** 화면에 그렇게 적힌다.
                pose = new Pose(_cam.transform.position + _cam.transform.forward * 0.5f,
                                YawTowardCamera(_cam.transform.position + _cam.transform.forward * 0.5f));
            }
            else
            {
                Status = "평면 위를 탭해라 (평면이 아직 없다)";
                return false;
            }

            Attach(pose, hitPlane);
            return true;
        }

        /// <summary>수평만 남긴 회전 — 카메라 쪽을 본다. 평면 법선은 버린다 (원인 3).</summary>
        Quaternion YawTowardCamera(Vector3 at)
        {
            if (_cam == null) return Quaternion.identity;
            var d = _cam.transform.position - at;
            d.y = 0f;
            return d.sqrMagnitude < 1e-6f ? Quaternion.identity : Quaternion.LookRotation(d.normalized, Vector3.up);
        }

        void Attach(Pose pose, ARPlane plane)
        {
            if (_anchor != null) Destroy(_anchor.gameObject);

            // 🔴 원인 4 — `AddComponent<ARAnchor>()` 는 AF6 이 명시적으로 경고하는 형태다
            //    ("failed to add itself to the anchor subsystem … use ARAnchorManager").
            //    평면이 있으면 **AttachAnchor** 로 그 평면에 붙인다 — 더 안정적이다.
            GameObject anchorGo;
            if (_anchors != null && plane != null)
            {
                _anchor = _anchors.AttachAnchor(plane, pose);
                if (_anchor != null) anchorGo = _anchor.gameObject;
                else { anchorGo = new GameObject("AssetAnchor"); anchorGo.transform.SetPositionAndRotation(pose.position, pose.rotation); }
                Debug.Log($"{Tag} AttachAnchor(plane={plane.trackableId}) → {(_anchor != null ? "ok" : "실패")}");
            }
            else
            {
                anchorGo = new GameObject("AssetAnchor");
                anchorGo.transform.SetPositionAndRotation(pose.position, pose.rotation);
                _anchor = anchorGo.AddComponent<ARAnchor>();
                Debug.LogWarning($"{Tag} 평면 없이 앵커 생성 (AF6 권장 형태가 아니다)");
            }

            Content.SetParent(anchorGo.transform, false);
            Content.localPosition = Vector3.zero;
            Content.localRotation = Quaternion.identity;
            Content.localScale = AssetScale.RootScale;       // 🔴 스케일의 유일한 출처
            // 자산은 원점 중심이라 **절반만큼** 띄워야 바닥에 앉는다.
            // ⚠️ 앵커 로컬은 스케일 1(미터)이다 — 여기에 0.5 를 넣으면 50cm 떠오른다.
            //    절반은 NORMALIZED 0.5 가 아니라 `0.5 × footprint` 다.
            Content.localPosition = new Vector3(0f, AssetScale.FootprintMeters * 0.5f, 0f);
            Content.gameObject.SetActive(true);

            _poseAtPlacement = new Pose(anchorGo.transform.position, anchorGo.transform.rotation);
            _camTravel = 0f; _hasLastCam = false;
            _hasBaseline = true;

            // 🔴 배치가 끝나면 **배치 모드를 닫는다.** 재배치는 버튼으로만.
            CurrentMode = Mode.Placed;
            Status = ArActive ? "배치 완료 — 편집을 켜고 라쏘로 골라라"
                              : "⚠️ AR 아님(폴백)으로 배치됨";
            Debug.Log($"{Tag} 배치 pose=({pose.position.x:F3},{pose.position.y:F3},{pose.position.z:F3}) " +
                      $"scale={AssetScale.FootprintMeters}m ar={ArActive} planes={PlaneCount}");
        }

        /// <summary>재배치 — **명시적으로만** 연다.</summary>
        public void BeginRelocate()
        {
            CurrentMode = Mode.Placing;
            Status = "재배치 — 새 자리를 탭하라";
            Debug.Log($"{Tag} 재배치 모드");
        }

        /// <summary>★ 앵커 pose 델타. 편집 전후로 불러 0 인지 본다.</summary>
        public (float pos, float rotDeg) PoseDelta()
        {
            if (!_hasBaseline || _anchor == null) return (-1f, -1f);
            var t = _anchor.transform;
            float dp = Vector3.Distance(t.position, _poseAtPlacement.position);
            float dr = Quaternion.Angle(t.rotation, _poseAtPlacement.rotation);
            return (dp, dr);
        }

        public float CamTravel => _camTravel;

        public string AnchorInfo()
        {
            if (_anchor == null) return $"앵커 없음 · 이동 {_camTravel:F2}m";
            var (dp, dr) = PoseDelta();
            return $"앵커 {_anchor.trackableId} · 상태 {_anchor.trackingState} · " +
                   $"pose 델타 {dp * 1000f:F2}mm / {dr:F3}° · 이동 {_camTravel:F2}m";
        }
    }
}
