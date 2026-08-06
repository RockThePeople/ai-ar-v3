// PlaneLine — 평면 윤곽선 전용. **면을 채우지 않는다** (선만 그린다).
//
// 🔴 왜 직접 두는가. `Shader.Find("Unlit/Color")` 같은 빌트인 찾기는 IL2CPP 스트립에
//    걸려 **머티리얼이 null 셰이더가 되고 선이 안 보인다** — 예외는 안 난다.
//    W26b 실기에서 평면 19개가 잡혔는데 윤곽선이 하나도 안 보인 원인이 이것이다.
//    ChunkSurface 로 이미 겪은 함정이고, 같은 처방(Always Included)을 쓴다.
//
// 🔴 ZTest 는 **LEqual** 이다 (사용자 지시). Always 로 두면 선이 **오브젝트를 뚫고 앞에**
//    그려져 자산을 가린다. 대신 바닥 z-fighting 은 Offset 으로 피한다 —
//    "앞에 있어야 할 것은 오브젝트" 라는 순서를 깨지 않으면서 선도 먹히지 않는다.
Shader "DeltaContract/PlaneLine"
{
    Properties { _Color ("Color", Color) = (0.15, 0.85, 1, 1) }
    SubShader
    {
        Tags { "RenderType"="Transparent" "Queue"="Transparent" }
        Blend SrcAlpha OneMinusSrcAlpha
        ZWrite Off
        ZTest LEqual
        Offset -1, -1
        Cull Off
        Pass
        {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "UnityCG.cginc"
            struct appdata { float4 vertex : POSITION; fixed4 color : COLOR; };
            struct v2f { float4 pos : SV_POSITION; fixed4 col : COLOR; };
            fixed4 _Color;
            v2f vert (appdata v)
            {
                v2f o; o.pos = UnityObjectToClipPos(v.vertex); o.col = v.color * _Color; return o;
            }
            fixed4 frag (v2f i) : SV_Target { return i.col; }
            ENDCG
        }
    }
}
