// VoxelUnlit — 정점 색을 그대로 보여 주는 최소 언릿 셰이더.
//
// 🔴 왜 직접 두는가. 런타임에 `Shader.Find` 로 빌트인 셰이더를 찾으면 IL2CPP
//    스트립에 걸려 **메시가 shader=NULL 로 안 보인다** (ai-ar-v2 가 실기에서 겪은 것).
//    이 파일을 프로젝트에 두고 Always Included Shaders 에 넣어 그 경로를 막는다.
Shader "DeltaContract/VoxelUnlit"
{
    Properties { _Tint ("Tint", Color) = (1,1,1,1) }
    SubShader
    {
        Tags { "RenderType"="Opaque" "Queue"="Geometry" }
        Cull Off
        Pass
        {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "UnityCG.cginc"

            struct appdata { float4 vertex : POSITION; float4 color : COLOR; float3 normal : NORMAL; };
            struct v2f { float4 pos : SV_POSITION; fixed4 color : COLOR; float3 n : TEXCOORD0; };
            fixed4 _Tint;

            v2f vert (appdata v)
            {
                v2f o;
                o.pos = UnityObjectToClipPos(v.vertex);
                o.color = v.color * _Tint;
                o.n = UnityObjectToWorldNormal(v.normal);
                return o;
            }

            fixed4 frag (v2f i) : SV_Target
            {
                // 면 방향에 따라 약간 음영을 준다 — 흰 배경에서 형태가 보이게.
                float l = saturate(dot(normalize(i.n), normalize(float3(0.3, 1.0, -0.4))) * 0.5 + 0.6);
                return fixed4(i.color.rgb * l, 1);
            }
            ENDCG
        }
    }
}
