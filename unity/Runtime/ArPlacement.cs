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
            ArActive = true;
            Status = "평면을 비춰라 — 바닥/책상을 훑으면 감지된다";
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
            }

            _planes = originGo.AddComponent<ARPlaneManager>();
            _planes.planePrefab = BuildOutlinePrefab();
            _raycasts = originGo.AddComponent<ARRaycastManager>();
            originGo.AddComponent<ARAnchorManager>();

            Content = new GameObject("ArContent").transform;
            Content.gameObject.SetActive(false);   // 배치 전에는 안 보인다
            return true;
        }

        /// <summary>평면 프리팹 — **윤곽선만**. MeshRenderer 를 안 붙이는 것이 요점이다
        /// (붙이는 순간 면이 찬다. 사용자가 면 채우기를 명시적으로 배제했다).</summary>
        static GameObject BuildOutlinePrefab()
        {
            var go = new GameObject("PlaneOutline");
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
            if (ArActive && _raycasts != null &&
                _raycasts.Raycast(screenPos, _hits, TrackableType.PlaneWithinPolygon) && _hits.Count > 0)
            {
                pose = _hits[0].pose;
            }
            else if (!ArActive && _cam != null)
            {
                // 폴백 — 카메라 앞 고정. **이것은 AR 이 아니다.** 화면에 그렇게 적힌다.
                pose = new Pose(_cam.transform.position + _cam.transform.forward * 0.5f,
                                Quaternion.identity);
            }
            else
            {
                Status = "평면 위를 탭해라 (평면이 아직 없다)";
                return false;
            }

            Attach(pose);
            return true;
        }

        void Attach(Pose pose)
        {
            if (_anchor != null) Destroy(_anchor.gameObject);

            var anchorGo = new GameObject("AssetAnchor");
            anchorGo.transform.SetPositionAndRotation(pose.position, pose.rotation);
            _anchor = anchorGo.AddComponent<ARAnchor>();     // 6.x: 컴포넌트 추가가 곧 앵커 생성

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

        public string AnchorInfo()
        {
            if (_anchor == null) return "앵커 없음";
            var (dp, dr) = PoseDelta();
            return $"앵커 {_anchor.trackableId} · 상태 {_anchor.trackingState} · " +
                   $"pose 델타 {dp * 1000f:F2}mm / {dr:F3}°";
        }
    }
}
