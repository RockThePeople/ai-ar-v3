# `<EDIT_HOST>:8082` — 외부 판본 수납 (ingest) API   [결정 7]

3090 이 로컬에서 만든 판본을 **커밋된 base** 로 세운다. v1 을 손으로 세운 것을 API 로 만든 것.
기존 `commit(job_id + staging)` 은 **이 서버가 만든 것만** 커밋할 수 있어 외부 판본은 길이 없었다.

```
POST /v2/trellis/assets/{asset_id}/ingest        multipart/form-data
  meta      = {"version": N}          (필수)  N>=1
  manifest  = manifest.json           (필수)
  slat      = slat.safetensors        (필수)  coords+feats + norm 메타
  input     = input.png               (필수)
  chunks    = chunks.tar.gz           (필수)  내부에 {key}.cbin
→ 201 {"asset_id","version","chunks",​"committed_latest","verified":"sha256+byte_length"}
```

## 진입점에서 거부하는 것 (전부 실측 확인)

| 조건 | 응답 |
|---|---|
| 파트 누락 (`slat`/`input`/`manifest`/`chunks`) | **422** `"… 파트가 없다 (V1-REQUIRED-FILES)"` |
| slat 메타에 `norm_mean`/`norm_std`/`slat_space` 없음 | **422** `"… 막다른 판본이 된다 (ops.py:265)"` |
| 청크 1바이트라도 변조 | **422** `"sha256/byte_length 불일치 1건: ['10_5_10']"` |
| 매니페스트↔tar 청크 집합 불일치 | **422** (없는 것/남는 것 개수와 예시) |
| `contract_version` / `chunk_size` 불일치 | **422** — 옛 판본을 새 코드에 물리지 않는다 |

★ **부분 수신은 성공이 아니다.** 전부 통과한 뒤에만 임시 디렉터리를 **원자적으로 교체**한다.
  실패하면 기존 판본은 그대로 남는다.

## 보내는 쪽 준비 (한 줄)

```bash
tar czf chunks.tar.gz -C <version_dir> chunks
curl -H "X-Blockedit-Key: $KEY" \
  -F 'meta={"version":2}' \
  -F "manifest=@manifest.json" -F "slat=@slat.safetensors" \
  -F "input=@input.png" -F "chunks=@chunks.tar.gz" \
  http://<EDIT_HOST>:8082/v2/trellis/assets/<asset_id>/ingest
```

## 주의
- `version` 이 현재 `committed latest` 보다 크면 `committed.json` 이 갱신된다. 작거나 같으면
  그 판본만 쓰이고 latest 는 유지된다(과거 판본 보정용).
- slat 메타 규약은 `V1-REQUIRED-FILES.md` 와 동일하다. `slat_space` 는 `"denormalized"` 여야 한다.
- 결정 8: recolor 우회는 만들지 않는다. **이 경로가 단일 경로다.**
