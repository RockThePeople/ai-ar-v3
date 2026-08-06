// V3AppBuild — 라쏘 실기 앱을 **배치모드로** 빌드한다 (W22 ①).
//
// 🔴 §7-A: **"빌드 완료" 메시지는 증거가 아니다.** 씬에 구워진 값이 APK 안에 그대로
//    들어갔는지는 APK 를 열어야 안다 (세션 9 발생 · 세션 11 재발). 그래서 이 스크립트는
//    빌드만 하고, **검증은 tools/build_lasso_apk.sh 가 APK 안을 열어서** 한다.
//
// 이번 단계는 AR 을 켜지 않는다 (W22 ②는 AR 없이). plane 배치는 다음 단계다.

#if UNITY_EDITOR
using System;
using System.IO;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEditor.XR.Management;
using UnityEditor.XR.Management.Metadata;
using UnityEngine.XR.Management;
using ARCoreSettings = UnityEditor.XR.ARCore.ARCoreSettings;

namespace DeltaContract.EditorTools
{
    public static class V3AppBuild
    {
        const string ScenePath = "Assets/Scenes/LassoEdit.unity";

        public static void Build()
        {
            var apk = Environment.GetEnvironmentVariable("V3_APK_PATH") ?? "Builds/LassoProbe.apk";
            var caseFile = Environment.GetEnvironmentVariable("V3_CASE_FILE") ?? "moto-rear-wheel.case";

            BuildScene(caseFile);
            ConfigurePlayer();
            ConfigureXr();
            ConfigureInputHandler();
            ConfigureGraphicsApi();

            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(apk)));
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { ScenePath },
                locationPathName = apk,
                target = BuildTarget.Android,
                targetGroup = BuildTargetGroup.Android,
                options = BuildOptions.Development | BuildOptions.AllowDebugging,
            });

            var s = report.summary;
            Debug.Log($"[V3AppBuild] 결과 {s.result} · {s.totalSize} 바이트 · {s.totalTime}");
            if (s.result != BuildResult.Succeeded) { EditorApplication.Exit(1); return; }
            Debug.Log($"[V3AppBuild] APK {Path.GetFullPath(apk)}");
            EditorApplication.Exit(0);
        }

        static void BuildScene(string caseFile)
        {
            var scene = EditorSceneManager.NewScene(NewSceneSetup.DefaultGameObjects,
                                                    NewSceneMode.Single);
            var camGo = Camera.main != null ? Camera.main.gameObject : new GameObject("Main Camera");
            var cam = camGo.GetComponent<Camera>() ?? camGo.AddComponent<Camera>();
            cam.clearFlags = CameraClearFlags.SolidColor;
            cam.backgroundColor = Color.white;      // 사용자 요청 — 흰 배경이라야 보인다
            camGo.tag = "MainCamera";

            // 🔴 런타임에 만드는 머티리얼의 셰이더는 씬이 참조하지 않아 **IL2CPP 에서
            //    스트립된다** — 그러면 메시가 shader=NULL 로 **안 보인다** (v2 실기 경험).
            IncludeShader("DeltaContract/ChunkSurface");
            IncludeShader("DeltaContract/PlaneLine");   // 안 넣으면 선이 조용히 사라진다

            var go = new GameObject("LassoEditApp");
            var app = go.AddComponent<LassoEditApp>();
            app.CaseFile = caseFile;      // ★ 씬에 **구워진다** — APK 안에서 확인해야 한다
            app.ViewCamera = cam;

            Directory.CreateDirectory("Assets/Scenes");
            EditorSceneManager.SaveScene(scene, ScenePath);
            Debug.Log($"[V3AppBuild] 씬 저장 {ScenePath} · case={caseFile}");
        }

        /// <summary>Always Included Shaders 에 넣어 스트립을 막는다.</summary>
        static void IncludeShader(string name)
        {
            var shader = Shader.Find(name);
            if (shader == null) { Debug.LogError($"[V3AppBuild] 셰이더를 못 찾았다: {name}"); return; }
            var so = new SerializedObject(
                UnityEngine.Rendering.GraphicsSettings.GetGraphicsSettings());
            var arr = so.FindProperty("m_AlwaysIncludedShaders");
            for (int i = 0; i < arr.arraySize; i++)
                if (arr.GetArrayElementAtIndex(i).objectReferenceValue == shader) return;
            arr.InsertArrayElementAtIndex(arr.arraySize);
            arr.GetArrayElementAtIndex(arr.arraySize - 1).objectReferenceValue = shader;
            so.ApplyModifiedProperties();
            Debug.Log($"[V3AppBuild] Always Included 에 추가: {name}");
        }

        /// <summary>🔴 XR 로더를 **붙인다.** 이게 없으면 실기에서
        ///
        ///     "No active UnityEngine.XR.XRInputSubsystem ... valid loader configuration"
        ///
        /// 만 뜨고 **카메라 영상이 안 나온다.** 게다가 ARCore 매니페스트 병합이 안 돌아
        /// **CAMERA 권한도 APK 에 안 들어간다** — 앱은 뜨는데 화면만 검다.
        /// `Assets/XR` 애셋을 복사하는 것만으로는 부족하다. 로더 할당은 별개다.
        /// (ai-ar-v2 의 ConfigureXrManagement 와 같은 원리. 필요한 것만 가져왔다)</summary>
        static void ConfigureXr()
        {
            const string arCoreLoader = "UnityEngine.XR.ARCore.ARCoreLoader";
            const string folder = "Assets/XR";
            const string path = folder + "/XRGeneralSettingsPerBuildTarget.asset";

            EditorBuildSettings.TryGetConfigObject(XRGeneralSettings.k_SettingsKey,
                                                   out XRGeneralSettingsPerBuildTarget perTarget);
            if (perTarget == null)
            {
                if (!AssetDatabase.IsValidFolder(folder)) AssetDatabase.CreateFolder("Assets", "XR");
                perTarget = ScriptableObject.CreateInstance<XRGeneralSettingsPerBuildTarget>();
                AssetDatabase.CreateAsset(perTarget, path);
                AssetDatabase.SaveAssets();
                EditorBuildSettings.AddConfigObject(XRGeneralSettings.k_SettingsKey, perTarget, true);
            }

            if (!perTarget.HasSettingsForBuildTarget(BuildTargetGroup.Android))
                perTarget.CreateDefaultSettingsForBuildTarget(BuildTargetGroup.Android);
            if (!perTarget.HasManagerSettingsForBuildTarget(BuildTargetGroup.Android))
                perTarget.CreateDefaultManagerSettingsForBuildTarget(BuildTargetGroup.Android);

            var settings = perTarget.SettingsForBuildTarget(BuildTargetGroup.Android);
            var manager = perTarget.ManagerSettingsForBuildTarget(BuildTargetGroup.Android);
            if (settings == null || manager == null)
            {
                Debug.LogError("[V3AppBuild] Android XR 설정을 만들지 못했다");
                EditorApplication.Exit(1); return;
            }

            settings.InitManagerOnStart = true;        // 앱 시작 시 ARCore 초기화
            if (!XRPackageMetadataStore.IsLoaderAssigned(arCoreLoader, BuildTargetGroup.Android))
                if (!XRPackageMetadataStore.AssignLoader(manager, arCoreLoader, BuildTargetGroup.Android))
                    Debug.LogError("[V3AppBuild] ARCore 로더 할당 실패");

            EditorUtility.SetDirty(settings);
            EditorUtility.SetDirty(manager);

            var arcore = ARCoreSettings.GetOrCreateSettings();
            arcore.requirement = ARCoreSettings.Requirement.Required;
            EditorUtility.SetDirty(arcore);
            AssetDatabase.SaveAssets();

            bool ok = XRPackageMetadataStore.IsLoaderAssigned(arCoreLoader, BuildTargetGroup.Android);
            Debug.Log($"[V3AppBuild] XR: ARCore 로더 할당={ok} · InitOnStart={settings.InitManagerOnStart}");
            if (!ok) EditorApplication.Exit(1);
        }

        /// <summary>🔴 원인 1 의 둘째 겹 — `activeInputHandler = 2 (Both)`.
        ///
        /// **패키지를 설치해도 안 바뀌고 public API 도 없다.** 그런데 이게 Input System 이
        /// 아니면 `TrackedPoseDriver` 가 값을 못 받아 **카메라가 원점에 고정**된다.
        /// ai-ar-prototype 이 ProjectSettings.asset 직렬화 속성을 직접 고쳤고
        /// (ProjectConfigurator.cs:347-359), v3 에는 이 설정 자체가 없었다.</summary>
        static void ConfigureInputHandler()
        {
            var assets = AssetDatabase.LoadAllAssetsAtPath("ProjectSettings/ProjectSettings.asset");
            if (assets == null || assets.Length == 0)
            {
                Debug.LogError("[V3AppBuild] ProjectSettings.asset 을 못 읽었다"); return;
            }
            var so = new SerializedObject(assets[0]);
            var prop = so.FindProperty("activeInputHandler");
            if (prop == null) { Debug.LogError("[V3AppBuild] activeInputHandler 속성이 없다"); return; }
            int before = prop.intValue;
            if (before != 2)
            {
                prop.intValue = 2;                       // 0=Old, 1=New, 2=Both
                so.ApplyModifiedProperties();
                AssetDatabase.SaveAssets();
            }
            Debug.Log($"[V3AppBuild] activeInputHandler {before} → {prop.intValue} (2=Both)");
        }

        /// <summary>그래픽 API 를 **고정**한다. v2 는 OpenGLES3("ARCore 는 이 경로가 가장 안전"),
        /// prototype 은 Vulkan 전용 — **둘 다 자동 API 를 껐다.** v3 는 미확인이었다.
        /// 여기서는 v2 쪽(OpenGLES3)을 따른다. ARCore 와의 조합 실적이 이 리포 계열에 있다.</summary>
        static void ConfigureGraphicsApi()
        {
            PlayerSettings.SetUseDefaultGraphicsAPIs(BuildTarget.Android, false);
            PlayerSettings.SetGraphicsAPIs(BuildTarget.Android,
                new[] { UnityEngine.Rendering.GraphicsDeviceType.OpenGLES3 });
            Debug.Log("[V3AppBuild] 그래픽 API 고정: OpenGLES3 (자동 꺼짐)");
        }

        static void ConfigurePlayer()
        {
            PlayerSettings.companyName = "ai-ar-v3";
            PlayerSettings.productName = "LassoEdit";
            PlayerSettings.SetApplicationIdentifier(
                UnityEditor.Build.NamedBuildTarget.Android, "com.aiarv3.lassoprobe");

            // ARM64 + IL2CPP — ARCore 인증 기기(S22)의 요구이자 64비트 스토어 요건이다.
            PlayerSettings.Android.targetArchitectures = AndroidArchitecture.ARM64;
            PlayerSettings.SetScriptingBackend(UnityEditor.Build.NamedBuildTarget.Android,
                                               ScriptingImplementation.IL2CPP);
            PlayerSettings.Android.minSdkVersion = AndroidSdkVersions.AndroidApiLevel26;
            PlayerSettings.Android.targetSdkVersion = AndroidSdkVersions.AndroidApiLevelAuto;
            PlayerSettings.defaultInterfaceOrientation = UIOrientation.Portrait;
            // 평문 HTTP — 서버 붙일 때 필요하다 (두 관문 중 하나. 나머지는 매니페스트 처리기)
            PlayerSettings.insecureHttpOption = InsecureHttpOption.AlwaysAllowed;
        }
    }
}
#endif
