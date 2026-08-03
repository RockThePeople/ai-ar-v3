# 만들려던 것과 그 메커니즘

작성 2026-08-03. **설계 의도의 기록**이다 — 무엇을 하려 했고, 왜 그 방법이었는지.
실제 결과(무엇이 됐고 안 됐는지)는 §6 에 짧게만 적는다.

---

## 1. 목표 기능

```
(1) 자연어로 3D 오브젝트를 생성한다
(2) 그 오브젝트의 **일부 영역**을 화면에서 자유롭게 선택한다 (라쏘 → 부피)
(3) 자연어로 "그 부분을 무엇으로 바꿀지" 지시한다
(4) 바뀐 부분만 전송받아 **그 자리만** 교체한다 (통째 재로딩 없음)
```

한 줄 주장: **"부분만 바뀌고, 바뀐 부분만 오간다."**

---

## 2. 생성 파이프라인 (간단히)

```
자연어 프롬프트
  ↓  T2I_PROMPT_TEMPLATE 로 구도 강제
     "{prompt}, full body, 3/4 view, product photography, studio lighting,
      plain background, centered"
     ← 이게 없으면 납작하거나 잘린 GLB 가 나온다 (실측)
  ↓
Z-Image-Turbo (3090)          1024×1024 · bf16 · steps 9 · guidance 0.0
  ↓  RGB
BiRefNet (3090)               배경 제거 → RGBA
     ← RMBG-2.0 이 게이트 403 이라 MIT 라이선스인 BiRefNet 으로 대체
  ↓
TRELLIS.2-4B (A5000)          image → 3D · 512³ 기본 (1024³/1536³ 선택 가능)
  ↓
GLB
```

**왜 t2i 를 거치나 — TRELLIS 백본이 이미지 조건이기 때문이다.**
텍스트→3D 직행보다 텍스트→이미지→3D 가 품질이 좋고, 중간 이미지가
**편집 시 조건 이미지로 재사용**된다(§3-C).

---

## 3. 편집 — 상세

### 3-A. 마스킹: 화면 라쏘 → 부피

**입력은 2자유도(2DOF)다.** 폰 화면 터치는 x,y 뿐이고 깊이가 없다.
이 조건에서 3D 부피를 고르는 문제는 시각화 분야의 오래된 주제다.

```
① 손가락으로 자유곡선을 그린다               List<Vector2> 화면 경로
② 오브젝트의 정점(357,773개)을 화면에 투영     cam.WorldToScreenPoint
   카메라 뒤(sp.z ≤ 0)는 버린다
③ 폴리곤 내부 판정                            even-odd ray casting
   → 후보 셀 집합 (앞면·뒷면 **둘 다**. "관통"이 의도다)
④ solidify — 카메라 최정렬 축을 깊이축으로
   같은 열의 min..max 를 채운다                → 껍질 사이 내부까지 포함
⑤ 점유 셀과 교집합                            없는 곳은 마스킹할 대상이 없다
⑥ [0,64) 클램프                               경계 이탈 좌표 제거
```

**④가 "부피"의 핵심이다.** ③만 하면 앞뒤 껍질 두 장이고, ④가 그 사이를 채운다.

#### 왜 라쏘인가 — 근거

- **Yu et al., "Efficient Structure-Aware Selection Techniques for 3D Point Cloud
  Visualizations with 2DOF Input", IEEE TVCG 2012 (CloudLasso / TeddySelection)**
  — 제목의 **2DOF** 가 핵심이다. 논문이 상정한 입력 조건이 폰 터치와 정확히 같다.
  화면에 그린 라쏘로부터 **밀도 기반 bounding selection surface** 를 자동 산출한다.
  통제된 사용자 연구에서 실린더 기반 선택(= 화면 원을 카메라 방향으로 그대로 압출)
  **대비 일관되게 빨랐고, 여러 경우 절반의 시간**이었다.

  ⚠️ 우리가 구현한 ③④는 **CloudLasso 가 이긴 쪽(단순 압출)** 이다.
  밀도 기반 곡면은 구현하지 않았다. 점유 셀 교집합(⑤)이 압출의 최악(빈 공간 선택)을
  막아주지만, 원 논문의 방법은 아니다.

- **Argelaguet & Andujar, "A Survey of 3D Object Selection Techniques for Virtual
  Environments", Computers & Graphics 37(3), 2013** — 이 분야의 정전 서베이.
- **Yu et al., "LassoNet: Deep Lasso-Selection of 3D Point Clouds" (arXiv:1907.13538)**
  — 밀도 휴리스틱을 신경망으로 대체한 후속. 휴리스틱이 부족할 때의 대안.
- **Galyean & Hughes, "Sculpting: An Interactive Volumetric Modeling Technique",
  SIGGRAPH '91** — 볼륨 조작 UI 의 시초.

**그리고 결정적으로 — 편집 엔진이 부피 마스크를 요구한다.**
VoxHammer 는 표면이 아니라 **볼류메트릭 마스크**를 입력으로 받는다.
"블록 개별 탭"은 표면 선택이라 맞지 않는다. 라쏘가 필요했던 직접적 이유다.

### 3-B. 마스크 좌표 변환

```
NORMALIZED [-0.5,0.5]³   오브젝트 로컬
  ↓ floor((p - min)/span × 64)
VOXEL [0,64)³            SLat 격자 = 편집이 실제로 일어나는 단위
  ↓ halo 팽창 (기본 1칸)
  ↓ // 8
CHUNK [0,8)³ = 512슬롯   전송·교체 단위
```

**halo 를 왜 두나** — 디코더의 receptive field 때문이다. 마스크 경계 딱 그만큼만
바꾸면 경계 밖 정점이 디코딩 과정에서 영향을 받아 이음새가 생긴다.
**팽창은 서버가 한다** — 클라가 미리 팽창시키면 "서버가 무엇을 건드릴지"를
클라가 예측(`dilate_cells`)해서 검증하는 봉쇄 검사가 깨진다.

### 3-C. 편집 실행 — 두 갈래를 검토했다

#### 갈래 A: VoxHammer 방식 (inversion + KV 주입)

> **VoxHammer: Training-Free Precise and Coherent 3D Editing in Native 3D Space**
> arXiv:2508.19247 · **3DV 2026 Oral**

```
① Inversion — 기존 3D 자산을 flow 모델의 종단 노이즈까지 거꾸로 밀어 올리며
   각 timestep 의 **inverted latent** 와 attention 의 **key/value 텐서**를 캐시한다
   TRELLIS 1 에서는 ST(sparse structure) 와 SLAT 스테이지 **양쪽 모두** 수행
② Denoising — 보존 영역의 denoising feature 를 ①의 inverted latent 와
   캐시된 KV 토큰으로 **치환**한다
```

**왜 이게 정확한가**: 기존 방법들은 멀티뷰 이미지를 편집한 뒤 3D 를 재구성하는데,
**미편집 영역의 정밀 보존과 전체 일관성**에서 무너진다. VoxHammer 는
멀티뷰가 아니라 **네이티브 3D latent 공간**에서 편집한다.

- Ablation(Table 3): ST 만 역추적하면 디테일 일관성이 부족하고 **양쪽을 다 해야**
  미세 기하·텍스처가 복원된다.
- 실측(우리): 피크 VRAM **14,180 MiB (24GB의 57.7%)**, ~201초.
  KV 를 `k.cpu()` 로 **CPU RAM 오프로드** 하는 구조라 시스템 RAM 124GB 가 예산이 된다.

#### 갈래 B: RePaint-lite (inversion 없음) — **이걸 채택했다**

> **Lugmayr et al., "RePaint: Inpainting using Denoising Diffusion Probabilistic
> Models", CVPR 2022, pp.11461-11471**

```
매 denoising step 마다 **마스크 밖 latent 을 원본 값으로 강제 복원**한다
마스크 안만 자유롭게 샘플링된다
```

RePaint 원본은 마스크 무관(mask-agnostic)하게 동작하며 마스크별 재학습이 필요 없고,
두 부분을 조화시키기 위해 forward 과정을 다시 밟는 **resampling** 을 기본 20회 반복한다.

**왜 A 대신 B 였나** — 비용이다. inversion 궤적과 KV 캐시를 들고 있지 않아도
"마스크 밖 보존"이 성립하는지 먼저 확인하면, VoxHammer 의 124GB RAM 예산을
**통째로 스킵**할 수 있다. 게이트를 그렇게 설계했다:

```
V3   RePaint-lite unconditional        구조 보존되는가        → PASS
V3b  + DINOv3 실제 이미지 조건화        편집 내용이 반영되는가  → 여기가 관건이었다
V3c  + material 마스킹                  색 충실도
V4/V5  inversion + KV 교체              V3b 가 PASS 면 **생략 검토**
```

조건화는 **DINOv3**(`facebook/dinov3-vitl16-pretrain-lvd1689m`)로 넣기로 했다 —
백본이 이미지 조건이므로 §2 의 중간 이미지를 그대로 재사용하는 방향이 맞다.

### 3-D. 델타 산출 — "무엇이 바뀌었나"를 **부기(bookkeeping)로** 정한다

```
🔴 해시 비교로 정하지 않는다
   같은 입력을 다시 디코딩하면 **152/152 청크가 전부 다른 해시**가 나온다
   (기하 변화는 중앙값 0.0002셀 = 부동소수 잡음)
   → 해시 비교는 절감률 0% 를 낸다. 무의미하다

⇒ 마스크 + halo → 청크 = **이번 연산이 새 바이트를 책임지는 집합**
  그 밖은 부모 바이트를 **승계**한다 (재디코딩 결과를 안 쓴다)
```

**"나머지가 안 망가진다"를 보장하는 건 청킹이 아니라 승계다.**
청킹은 그걸 할 수 있는 주소 체계를 줄 뿐이다.

전칭 규칙: `∀ c ∈ 부기 : c ∈ changed_chunks ∨ c ∈ removed_chunk_ids`
— "아무 데도 안 넣기"는 거부한다. 빠뜨린 것과 비었다고 알려준 것을 구분할 수 없기 때문.

### 3-E. 전송·적용

```
.cbin   40바이트 헤더 · magic CBN1 · POSITION/NORMAL/COLOR(u8×4)/UV/INDEX(u32)
        헤더에 청크 좌표가 들어가서 **해시가 위치까지 포함**한다
.cbz    gzip. 확장자를 바꾼 이유 — 안드로이드 파이프라인이 .gz 를 자동 해제한다

클라    청크 1개 = GameObject 1개
        받은 것만 Mesh 교체. GameObject·EntityId·콜라이더·앵커 유지
        removed 는 파괴 + **사전에서 제거** (안 그러면 다음 패치가
        이미 파괴된 MeshFilter 에 ApplyTo 를 걸고 **예외가 안 난다**)
```

**왜 통째 GLB 가 아닌가** — 이전 시스템(`ai-ar-v2`)이 편집 결과로 GLB 전체를
돌려줬고, 클라가 통째로 재로딩하면서 GameObject 파괴·재생성 → 앵커·물리 리셋 +
화면 pop 이 났다. 디코딩이 재현되지 않으니 **안 건드린 부분까지 전부 미세하게 움직였다.**

---

## 4. 자연어 → 편집 스펙

```
instruction + 마스크 요약(bbox·개수)
  ↓  단일 LLM 호출, 구조화 출력
{op: "replace_region"|"add"|"remove"|"scale", target_prompt: str, factor?: float}
```

**설계 불변식: LLM 은 좌표를 만들지 않는다.**
좌표의 유일한 진실은 클라가 보낸 마스크이고, 마스크 밖 불변은 **서버 코드가 강제**한다
(프롬프트로 부탁하지 않는다).

근거: LLM 의 3D 좌표 직접 예측은 경계 이탈이 잦다는 것이 LayoutVLM 계열에서 지적됐다.
그리고 오케스트레이션 프레임워크(LangGraph/CrewAI)는 도입하지 않았다 —
분기가 둘뿐이라 다단계 조율이 없다.

---

## 5. 전체 그림

```
[폰 Unity AR]                [3090]                      [A5000]
 라쏘 → 부피 마스크      →   오케스트레이션              TRELLIS.2 / TRELLIS 1
 청크 GameObject 152개        t2i(Z-Image) + BiRefNet     SLat 편집 (RePaint-lite)
 in-place 교체           ←   부기 → 델타 패키징      ←   FlexiCubes 디코딩 → .cbin
```

---

## 6. 실제로 어떻게 됐나 (짧게)

```
✅ 생성                     동작
✅ 라쏘 폴리곤 선택          동작 (188셀 vs 직육면체 9,996셀 = 1.9%)
✅ 마스크 밖 보존            바이트 128/128 동일 · EntityId 152/152 유지
✅ 부분 전송                 41~50% 절감
✅ 조립(생성+스플라이스)      실루엣 6.1~8.0% — **눈에 보인다**

🔴 RePaint 편집이 형태를 못 바꾼다
   활성 복셀 **100%** 를 순수 노이즈에서 재샘플링해도 실루엣 대칭차 0.46%/0.70%
   원인 ① masked_sampler Phase 1 제약 — coords 고정, 복셀 추가·제거 불가
        ② 조건이 parent/input.png 하나뿐 → prompt 가 no-op

🔴 부피 채움(solidify)이 실측 1회에서 0개를 추가했다
   SLat 은 껍질 표현(두께 중앙값 3복셀)이라 "빈 내부"에 latent 가 없다
```

**갈래 B(RePaint-lite)로 비용을 아끼려던 판단이 여기서 무너졌다.**
V3 는 "구조 보존"만 물었고 PASS 했는데, **"내용이 바뀌는가"를 묻는 게이트가 없었다.**
그게 §3-C 게이트 설계의 결함이다 — 보존만 재고 효능을 안 쟀다.

**갈래 A(VoxHammer inversion)를 스킵한 것이 결과적으로 틀렸을 가능성이 있다.**
다만 Phase 1 의 coords 고정은 inversion 과 무관한 별개 제약이라,
VoxHammer 를 했어도 **껍질 밖 돌출은 여전히 불가능**했을 것이다.

---

## References

**편집 메커니즘**
- Huang et al., *VoxHammer: Training-Free Precise and Coherent 3D Editing in Native 3D Space*, **3DV 2026 (Oral)**, [arXiv:2508.19247](https://arxiv.org/abs/2508.19247) · [프로젝트 페이지](https://huanngzh.github.io/VoxHammer-Page/) · [코드](https://github.com/Nelipot-Lee/VoxHammer)
- Lugmayr, Danelljan, Romero, Yu, Timofte, Van Gool, *RePaint: Inpainting using Denoising Diffusion Probabilistic Models*, **CVPR 2022**, pp.11461-11471 — [CVF Open Access](https://openaccess.thecvf.com/content/CVPR2022/html/Lugmayr_RePaint_Inpainting_Using_Denoising_Diffusion_Probabilistic_Models_CVPR_2022_paper.html) · [코드](https://github.com/andreas128/RePaint)

**선택 UI**
- Yu, Efstathiou, Isenberg, Isenberg, *Efficient Structure-Aware Selection Techniques for 3D Point Cloud Visualizations with 2DOF Input* (**CloudLasso / TeddySelection**), **IEEE TVCG 2012** — [HAL](https://hal.science/hal-00718310) · [저자 페이지](https://tobias.isenberg.cc/VideosAndDemos/Yu2012ESA)
- Chen et al., *LassoNet: Deep Lasso-Selection of 3D Point Clouds*, [arXiv:1907.13538](https://arxiv.org/pdf/1907.13538)
- Argelaguet & Andujar, *A Survey of 3D Object Selection Techniques for Virtual Environments*, **Computers & Graphics 37(3), 2013**
- Galyean & Hughes, *Sculpting: An Interactive Volumetric Modeling Technique*, **SIGGRAPH '91**, pp.267-274

**모델**
- [microsoft/TRELLIS.2-4B](https://huggingface.co/microsoft/TRELLIS.2-4B) — SLat(Structured LATents) 백본, ST + SLAT 2스테이지
- `facebook/dinov3-vitl16-pretrain-lvd1689m` — 이미지 조건화 (gated:manual)
- `Tongyi-MAI/Z-Image-Turbo` — t2i
- `ZhengPeng7/BiRefNet` (MIT) — 배경 제거, RMBG-2.0 대체
