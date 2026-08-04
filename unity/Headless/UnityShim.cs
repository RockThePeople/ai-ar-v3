// UnityEngine 최소 셰이딩 — **헤드리스 검증 전용**이다. Unity 빌드에는 안 들어간다.
//
// 왜 필요한가: `contract/unity/LassoVolume.cs` 는 "전부 static 이고 카메라를 안 받는다 —
// 테스트에서 엔진 없이 부를 수 있다" 고 스스로 적어 뒀다. 그 약속을 실제로 쓰는 것이
// 이 파일이다. LassoVolume 을 **한 글자도 고치지 않고** 그대로 컴파일해서 돌린다.
//
// ⚠️ 여기 있는 것은 LassoVolume·SlatLassoPicker 가 실제로 쓰는 멤버뿐이다.
//    Unity 의 동작을 흉내 내는 범위를 넘기지 않는다 — 넘기면 그 자체가 재구현이다.

namespace UnityEngine
{
    public struct Vector2
    {
        public float x, y;
        public Vector2(float x, float y) { this.x = x; this.y = y; }
    }

    public struct Vector3
    {
        public float x, y, z;
        public Vector3(float x, float y, float z) { this.x = x; this.y = y; this.z = z; }
    }

    public struct Vector2Int : System.IEquatable<Vector2Int>
    {
        public int x, y;
        public Vector2Int(int x, int y) { this.x = x; this.y = y; }
        public bool Equals(Vector2Int o) => x == o.x && y == o.y;
        public override bool Equals(object o) => o is Vector2Int v && Equals(v);
        public override int GetHashCode() => (x * 397) ^ y;
    }

    public struct Vector3Int : System.IEquatable<Vector3Int>
    {
        public int x, y, z;
        public Vector3Int(int x, int y, int z) { this.x = x; this.y = y; this.z = z; }
        public bool Equals(Vector3Int o) => x == o.x && y == o.y && z == o.z;
        public override bool Equals(object o) => o is Vector3Int v && Equals(v);
        public override int GetHashCode() => ((x * 397) ^ y) * 397 ^ z;
        public override string ToString() => $"({x},{y},{z})";
    }

    public static class Mathf
    {
        public static float Abs(float v) => System.Math.Abs(v);
        public static int Clamp(int v, int lo, int hi) => v < lo ? lo : (v > hi ? hi : v);
        public static int FloorToInt(float v) => (int)System.Math.Floor(v);
    }
}
