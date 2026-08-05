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

namespace DeltaContract.EditorTools
{
    public static class V3AppBuild
    {
        const string ScenePath = "Assets/Scenes/LassoProbe.unity";

        public static void Build()
        {
            var apk = Environment.GetEnvironmentVariable("V3_APK_PATH") ?? "Builds/LassoProbe.apk";
            var caseFile = Environment.GetEnvironmentVariable("V3_CASE_FILE") ?? "moto-rear-wheel.case";

            BuildScene(caseFile);
            ConfigurePlayer();

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
            IncludeShader("DeltaContract/VoxelUnlit");

            var go = new GameObject("TouchLasso");
            var ctl = go.AddComponent<TouchLassoController>();
            ctl.CaseFile = caseFile;      // ★ 씬에 **구워진다** — APK 안에서 확인해야 한다
            ctl.LiveCamera = cam;

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

        static void ConfigurePlayer()
        {
            PlayerSettings.companyName = "ai-ar-v3";
            PlayerSettings.productName = "LassoProbe";
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
