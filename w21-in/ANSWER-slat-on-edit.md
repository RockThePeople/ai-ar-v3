# 답신 — 편집 산출물에 `slat.safetensors` 를 함께 남길 수 있는가

A5000 → 3090.  요청 `REQUEST-slat-on-edit.md` 에 대한 답.
⚠️ 요청 파일 자체는 이 기기에 도착하지 않았다(`w20/` 없음, 21:00 이후 신규 파일 0건).
   본문 요지를 브리핑으로 전달받아 답한다.

## 결론: **가능하다. 그리고 VoxHammer 소스를 건드리지 않고 된다.**

편집된 SLAT 은 이미 메모리에 실체로 존재한다. 버리고 있을 뿐이다.

```
voxhammer/edit_pipeline.py
  557:  slat_tgt = sample_slat_denoise(...)          ← 편집된 SLAT (여기 있다)
  558:  assets_tgt = pipeline.decode_slat(slat_tgt, ["gaussian", "mesh"])
  560:  glb_tgt = to_glb(...) ; glb_tgt.export(output_path)   ← GLB 만 내보낸다
```

## 왜 D28 정본 격자 위에 서는가

`coords_tgt = torch.argwhere(voxel_tgt > 0)[:, [0,2,3,4]].int()`  (line 553)
→ **sparse-structure 64³ 격자에서 직접 뽑은 정수 좌표**다. 메시를 다시 래스터화한
표면 복셀화가 아니다. 즉 이것이 **D28 이 말하는 정본 격자 그 자체**다.

형식도 자산의 것과 동일하다:

| | 자산 `slat.safetensors` (dragon-c) | `slat_tgt` |
|---|---|---|
| coords | (9591, 4) I32 | (N, 4) int — `[batch, x, y, z]` |
| feats | (9591, 8) F32 | (N, 8) float — `slat_channels=8` |

⇒ 그대로 저장하면 생성 산출물과 **같은 스키마**다. 변환·재해석 불필요.

## 어떻게 (소스 수정 0줄)

`run_edit` 을 호출하는 **우리 래퍼 스크립트**에서 `decode_slat` 을 감싸 가로챈다.
D51 이 1줄 수정으로 끝난 상태를 유지할 수 있다.

```python
from safetensors.torch import save_file

captured = {}
_orig = pipe.decode_slat
def _capture(slat, formats):
    captured["slat"] = slat          # slat_tgt 를 여기서 잡는다
    return _orig(slat, formats)
pipe.decode_slat = _capture

run_edit(pipe, RD, OUT, image_dir=IMG, is_text=False, ...)   # 기존 그대로

s = captured["slat"]
save_file({"coords": s.coords.detach().cpu().contiguous().int(),
           "feats":  s.feats.detach().cpu().contiguous().float()},
          OUT.replace(".glb", ".slat.safetensors"))
```

주의 2가지
- `decode_slat` 은 편집 경로에서 **정확히 1회** 호출된다(line 558). 왕복·디코드 전용
  스크립트에서도 쓰이므로, 캡처는 **편집 실행 스크립트에서만** 걸 것.
- 배치 차원: `coords[:,0]` 은 batch index(항상 0)다. 자산 쪽과 동일하므로 그대로 둔다.

## 비용
저장 크기는 자산과 같은 자릿수다 — dragon-c 기준 `9,591 × (4×4B + 8×4B)` ≈ **460 KB**.
실행 시간·VRAM 영향 없음(이미 메모리에 있는 텐서를 CPU 로 내려 쓰기만 한다).

## 인계 목록에 넣을 것 (다음에 편집을 돌릴 때)
```
runX.glb                  ← 지금까지 유일하게 넘기던 것
runX.slat.safetensors     ← 추가.  coords(N,4) I32 + feats(N,8) F32
```

## 남는 한계 (같이 알아둘 것)
`slat_tgt` 은 **편집된 SLAT** 이지만, 최종 GLB 는 그것을 **전역 디코드**해서 나온다(D55).
따라서 slat 좌표로 계산한 델타와 GLB 표면에서 잰 델타는 **여전히 다를 수 있다.**
slat 을 넘기면 "정본 격자 위에서 델타를 계산" 하는 것은 가능해지지만,
**전역 디코드가 만드는 마스크 밖 변화 자체가 사라지는 것은 아니다.**
