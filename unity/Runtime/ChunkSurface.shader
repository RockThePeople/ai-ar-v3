// ChunkSurface — `.cbin` 청크 메시를 그리고, **선택된 볼륨만 표면에서 빛나게** 한다 (W24③).
//
// 🔴 사용자 확정: **평소엔 복셀이 안 보이고, 선택하면 그 볼륨이 반투명/하이라이트로 보인다.**
//    ⇒ 선택 표시를 위해 큐브를 겹쳐 그리지 않는다. 큐브가 다시 나타나면 지금 불만의 재발이다.
//
// 방식: **오브젝트 공간 영역 셰이더 + Texture3D 마스크** (ai-ar-v2 MeshObject.cs 의 원리만).
//   · 정점 색으로 칠하면 **메시 정점 밀도**가 해상도를 정한다 — 정점이 복셀보다 성기면 뭉갠다.
//   · Texture3D 는 64³ 복셀 격자와 **정확히 1:1** 이라 경계가 복셀 단위로 선다.
//   · 마스크가 전역이라 청크마다 머티리얼을 만들 필요가 없다 — 드로콜이 안 늘어난다.
//
// 좌표: 메시 정점은 이미 **Unity 프레임**(D9 로 변환됨)이다. 마스크는 **복셀 프레임**이라
//       샘플링 직전에 되돌린다. `VoxelFrame.ToVoxel` 과 **같은 순열**이어야 한다 —
//       다르면 하이라이트만 조용히 엉뚱한 데 뜬다 (W22 의 프레임 혼재).
//
// ⚠️ 런타임 생성 머티리얼의 셰이더는 IL2CPP 에서 스트립된다 → Always Included (V3AppBuild).

Shader "DeltaContract/ChunkSurface"
{
    Properties
    {
        _Tint ("Tint", Color) = (1,1,1,1)
        _HighlightColor ("Highlight", Color) = (1.0, 0.45, 0.1, 1)
        _HighlightMix ("Highlight Mix", Range(0,1)) = 0.85
        _RimBoost ("Rim Boost", Range(0,2)) = 0.6
    }
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
            struct v2f
            {
                float4 pos : SV_POSITION;
                fixed4 color : COLOR;
                float3 n : TEXCOORD0;
                float3 objPos : TEXCOORD1;   // 오브젝트 로컬 (NORMALIZED · Unity 프레임)
                float3 viewDir : TEXCOORD2;
            };

            fixed4 _Tint;
            fixed4 _HighlightColor;
            float _HighlightMix;
            float _RimBoost;

            // 🔴 전역 마스크. 64³ R8 — 값 > 0 이면 그 복셀이 선택됐다.
            //    `Shader.SetGlobalTexture("_SelMask", ...)` 로 한 번만 물린다.
            UNITY_DECLARE_TEX3D(_SelMask);
            float _SelMaskOn;        // 0 이면 마스크가 아직 없다 (전역 float)

            v2f vert (appdata v)
            {
                v2f o;
                o.pos = UnityObjectToClipPos(v.vertex);
                o.color = v.color * _Tint;
                o.n = UnityObjectToWorldNormal(v.normal);
                o.objPos = v.vertex.xyz;
                o.viewDir = normalize(WorldSpaceViewDir(v.vertex));
                return o;
            }

            // Unity 프레임 로컬 → 마스크 uvw.
            //   VoxelFrame.ToVoxel : voxel = (x, −z, y)   ← C# 과 같은 순열
            //   NORMALIZED [-0.5,0.5] → [0,1]
            float3 MaskUVW(float3 p)
            {
                float3 vox = float3(p.x, -p.z, p.y);
                return vox + 0.5;
            }

            fixed4 frag (v2f i) : SV_Target
            {
                float l = saturate(dot(normalize(i.n), normalize(float3(0.3, 1.0, -0.4))) * 0.5 + 0.6);
                fixed3 base = i.color.rgb * l;

                float sel = 0;
                if (_SelMaskOn > 0.5)
                {
                    float3 uvw = MaskUVW(i.objPos);
                    // 격자 밖은 선택일 수 없다 — 경계에서 래핑이 거짓 양성을 만든다.
                    if (all(uvw > 0.0) && all(uvw < 1.0))
                        sel = UNITY_SAMPLE_TEX3D(_SelMask, uvw).r;
                }

                // 표면에서 **빛나게** 한다. 큐브를 덧대지 않으므로 형태는 원본 메시 그대로다.
                float rim = pow(1.0 - saturate(dot(normalize(i.n), i.viewDir)), 2.0);
                fixed3 hi = _HighlightColor.rgb * (1.0 + _RimBoost * rim);
                fixed3 rgb = lerp(base, lerp(base, hi, _HighlightMix), saturate(sel));
                return fixed4(rgb, 1);
            }
            ENDCG
        }
    }
}
