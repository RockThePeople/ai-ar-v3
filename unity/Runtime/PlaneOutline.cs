// PlaneOutline — 감지된 평면을 **테두리(윤곽선)로만** 그린다 (W26b①).
//
// 🔴 지금까지는 "평면 2개" 라는 **글자만** 있었다. 사용자가 어디를 탭해야 할지 모르고,
//    내가 adb 탭으로 평면 폴리곤을 못 맞힌 것도 같은 이유다. **보이면 사람도 기계도 맞힌다.**
//
// ⚠️ **면을 색으로 채우지 않는다** (사용자 명시). ai-ar-v2 는 좋은 선례지만
//    접지 평면을 색으로 채우는 부분은 참조하지 않는다. `ARPlane.boundary` 폴리곤을
//    LineRenderer 로 이어 **윤곽선만** 그린다.
//
// ARPlaneManager 가 이 프리팹을 복제하면서 ARPlane 을 붙여 준다. 메시 시각화
// (ARPlaneMeshVisualizer / MeshRenderer)는 **일부러 안 넣는다** — 넣는 순간 면이 찬다.

using UnityEngine;
using UnityEngine.XR.ARFoundation;

namespace DeltaContract
{
    [RequireComponent(typeof(LineRenderer))]
    public sealed class PlaneOutline : MonoBehaviour
    {
        LineRenderer _line;
        ARPlane _plane;

        void Awake()
        {
            _line = GetComponent<LineRenderer>();
            _plane = GetComponent<ARPlane>();
            _line.useWorldSpace = false;
            _line.loop = true;
            _line.widthMultiplier = 0.006f;      // 6mm — 실제 크기다 (AssetScale 과 같은 세계)
            _line.numCornerVertices = 2;
            _line.alignment = LineAlignment.View;
            _line.textureMode = LineTextureMode.Stretch;
            _line.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            _line.receiveShadows = false;
        }

        void OnEnable()
        {
            if (_plane != null) _plane.boundaryChanged += OnBoundaryChanged;
            Redraw();
        }

        void OnDisable()
        {
            if (_plane != null) _plane.boundaryChanged -= OnBoundaryChanged;
        }

        void OnBoundaryChanged(ARPlaneBoundaryChangedEventArgs _) => Redraw();

        void Redraw()
        {
            if (_plane == null || _line == null) return;
            var b = _plane.boundary;             // 평면 로컬 XZ 평면상의 2D 폴리곤
            if (b.Length < 3) { _line.positionCount = 0; return; }

            _line.positionCount = b.Length;
            for (int i = 0; i < b.Length; i++)
                _line.SetPosition(i, new Vector3(b[i].x, 0f, b[i].y));
        }
    }
}
