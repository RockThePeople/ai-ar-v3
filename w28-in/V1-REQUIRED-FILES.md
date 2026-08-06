# `v1/` (커밋된 버전) 필수 파일 목록 — 커밋 **전에** 검사할 것

3090 이 두 번 연속 "다음 파일이 없다" 로 진행된 것을 막기 위한 목록.
**실측으로 확정**했다 — 각 항목 옆이 "없으면 무엇이 어떻게 죽는가" 다.

```
{DATA_ROOT}/assets/{asset_id}/
├── committed.json              {"latest": N}
└── vN/
    ├── chunks/{key}.cbin       × 전 청크
    ├── manifest.json
    ├── slat.safetensors        coords + feats + 메타
    └── input.png
```

| 파일 | 없으면 | 실측 증상 |
|---|---|---|
| `committed.json` | `committed()=0` | **409 VERSION_CONFLICT** — 3090 이 본 UPSTREAM_EDIT_FAILED |
| `vN/chunks/*.cbin` | 청크 전송 불가 | 404 / Unity 로드 실패 |
| `vN/manifest.json` | 무결성 대조 불가 | 부분 수신을 성공으로 오인 |
| **`vN/slat.safetensors`** | `_load_slat` (ops.py:83) | **job INTERNAL** `FileNotFoundError: …/v1/slat.safetensors` |
| **`vN/input.png`** | `pipe.get_cond([...])` (ops.py:263) | **job INTERNAL** `FileNotFoundError: …/v1/input.png` |

★ 뒤 둘은 **라우트가 202 를 준 뒤 워커에서** 죽는다. 즉 "요청은 성공했는데 결과가 없다" 로
보이므로 커밋 전에 검사하지 않으면 원인을 찾기 어렵다.

## `slat.safetensors` 메타데이터 — 텐서만 넣으면 안 된다

| 키 | 필수 | 없으면 |
|---|---|---|
| `norm_mean` · `norm_std` | 🔴 | `op_edit` (ops.py:265-266) 이 `KeyError` — **다음 편집이 불가능한 막다른 버전** |
| `slat_space` | 🔴 | `"denormalized"` 여야 한다. 정규화 상태로 넣으면 조용히 틀린 기하가 나온다 |
| `contract_version` · `slat_channels` · `voxel_res` · `coord_order` | 권장 | 판정·대조용 |

⚠️ W28 실측: 내 `slat_capture` 가 **텐서만** 써서 `slat_space=None`, metadata `{}` 로 나왔다.
   모듈을 고쳐 메타 없이는 **저장 자체가 실패**하게 했다(조용히 넘어가지 않는다).

## 커밋 전 자체 검사 (한 줄)

```bash
A=$DATA_ROOT/assets/$ID/v$N
for f in chunks manifest.json slat.safetensors input.png; do [ -e "$A/$f" ] || echo "MISSING $f"; done
python3 - <<'P'
from safetensors import safe_open
with safe_open("$A/slat.safetensors","pt") as f:
    md=f.metadata() or {}
    miss=[k for k in ("norm_mean","norm_std","slat_space") if k not in md]
    print("META MISSING:",miss or "none")
P
```

## 무결성 (D27)
- 청크: `sha256` + `byte_length` 를 manifest 와 대조 (A5000 은 376/376 로 확인)
- slat: `deltacontract.mask_fingerprint(coords)` 가 `slat_coords.vN.json` 의 `fingerprint` 와 일치
