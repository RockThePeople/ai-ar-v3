// LassoProbe — 라쏘 판정을 **엔진 없이** 돌린다 (W17 ①).
//
//   dotnet run --project unity/Headless -- <input.json> [output.json]
//
// 입력 JSON:
//   { "slat_coords": [[x,y,z], …],           ← 🔴 D58. 메시 정점이 아니다
//     "polygon":     [[sx,sy], …],           ← 화면 드래그 궤적
//     "camera": { "position":[x,y,z], "target":[x,y,z], "up":[x,y,z],
//                 "fov_deg":60, "width":1080, "height":1920 } }
//
// 출력에는 셀 수 · 지문 · 단계별 계수가 들어간다. 셀 목록도 같이 낸다.
//
// ⚠️ 카메라 모델은 Unity 의 `Camera.WorldToScreenPoint` 규약을 따른다:
//    화면 원점 **좌하단**, z = 카메라로부터의 전방 거리. Unity 에서는 이 함수 대신
//    실제 Camera 를 델리게이트로 넘기므로, 여기 있는 것은 **검증용 대역**이다.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.Json;
using DeltaContract;
using UnityEngine;

static class Program
{
    static int Main(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("usage: LassoProbe <input.json> [output.json]");
            return 2;
        }

        using var doc = JsonDocument.Parse(File.ReadAllText(args[0]));
        var root = doc.RootElement;

        var coords = new List<Vector3Int>();
        foreach (var e in root.GetProperty("slat_coords").EnumerateArray())
            coords.Add(new Vector3Int(e[0].GetInt32(), e[1].GetInt32(), e[2].GetInt32()));

        var poly = new List<Vector2>();
        foreach (var e in root.GetProperty("polygon").EnumerateArray())
            poly.Add(new Vector2(e[0].GetSingle(), e[1].GetSingle()));

        var cam = root.GetProperty("camera");
        var eye = Vec(cam.GetProperty("position"));
        var target = Vec(cam.GetProperty("target"));
        var up = Vec(cam.GetProperty("up"));
        float fov = cam.GetProperty("fov_deg").GetSingle();
        float w = cam.GetProperty("width").GetSingle();
        float h = cam.GetProperty("height").GetSingle();

        // 🔴🔴 카메라 기저 — **Unity 는 왼손 좌표계다.**
        //
        // W18 에서 실측으로 잡았다. 여기서 오른손 기저(right = fwd × up)를 쓰면
        // Unity 의 `transform.LookAt` 이 만드는 기저(right = up × fwd)와 **x 가 반대**가 되고,
        // 그 결과는:
        //
        //     단계별 계수가 **전부 일치한다** (투영 3884 · 폴리곤안 3017 · 압출 +2592 …)
        //     지문만 다르다 — 같은 개수의 **좌우 뒤집힌** 셀을 고른 것이다
        //
        // 즉 개수만 보면 통과한다. 실제로 W18 이전의 골든이 그 상태였고, Unity 결과가
        // 골든의 **x 미러**(63−x)와 바이트 단위로 일치하는 것으로 확정했다.
        //
        // ⇒ **Unity 가 정본이다.** 앱이 도는 곳이 거기다. 여기를 Unity 에 맞춘다.
        var fwd = Norm(Sub(target, eye));
        var right = Norm(Cross(up, fwd));      // 왼손 — Unity 와 같은 순서
        var trueUp = Cross(fwd, right);
        float focal = (h * 0.5f) / (float)Math.Tan(fov * Math.PI / 360.0);

        Func<Vector3, Vector3> project = local =>
        {
            var rel = Sub(local, eye);
            float z = Dot(rel, fwd);
            if (z <= 0f) return new Vector3(0, 0, z);      // 카메라 뒤
            float sx = Dot(rel, right) / z * focal + w * 0.5f;
            float sy = Dot(rel, trueUp) / z * focal + h * 0.5f;
            return new Vector3(sx, sy, z);
        };

        // 오브젝트 프레임 = 로컬 프레임 (이 하네스는 회전을 두지 않는다).
        var result = SlatLassoPicker.Pick(coords, poly, project, fwd);

        var cells = new List<int[]>(result.Cells.Count);
        foreach (var c in result.Cells) cells.Add(new[] { c.x, c.y, c.z });

        var outObj = new Dictionary<string, object>
        {
            ["grid_source"] = result.GridSource,
            ["n_cells"] = result.Cells.Count,
            ["mask_fingerprint"] = result.Fingerprint,
            ["projected"] = result.Projected,
            ["behind_camera"] = result.BehindCamera,
            ["in_polygon"] = result.InPolygon,
            ["after_solidify"] = result.AfterSolidify,
            ["solidify_added"] = result.SolidifyAdded,
            ["intersect_removed"] = result.IntersectRemoved,
            ["dominant_axis"] = result.DominantAxis,
            ["cells"] = cells,
        };
        var json = JsonSerializer.Serialize(outObj, new JsonSerializerOptions { WriteIndented = false });
        if (args.Length >= 2) File.WriteAllText(args[1], json); else Console.WriteLine(json);

        Console.Error.WriteLine(string.Format(CultureInfo.InvariantCulture,
            "투영 {0} · 뒤 {1} · 폴리곤안 {2} · 압출후 {3}(+{4}) · 교집합제거 {5} → 셀 {6} · 축 {7}",
            result.Projected, result.BehindCamera, result.InPolygon, result.AfterSolidify,
            result.SolidifyAdded, result.IntersectRemoved, result.Cells.Count, result.DominantAxis));
        return 0;
    }

    static Vector3 Vec(JsonElement e) => new Vector3(e[0].GetSingle(), e[1].GetSingle(), e[2].GetSingle());
    static Vector3 Sub(Vector3 a, Vector3 b) => new Vector3(a.x - b.x, a.y - b.y, a.z - b.z);
    static float Dot(Vector3 a, Vector3 b) => a.x * b.x + a.y * b.y + a.z * b.z;
    static Vector3 Cross(Vector3 a, Vector3 b) =>
        new Vector3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x);
    static Vector3 Norm(Vector3 a)
    {
        float m = (float)Math.Sqrt(Dot(a, a));
        return m == 0f ? a : new Vector3(a.x / m, a.y / m, a.z / m);
    }
}
