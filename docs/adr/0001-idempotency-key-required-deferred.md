# 0001. idempotency_key 필수화 — 보류

- 상태: 제안 (보류)
- 날짜: 2026-10-15
- 관련: PROGRESS §3 결정 5 · 브랜치 `contract/w27-idem` (커밋 12d27eb, 삭제됨)

## 맥락

맥북 세션이 계약 3.27.0 으로 `EditRequest`·조립 요청의 `idempotency_key` 를 `Optional[str]`
에서 `str`(필수)로 올리는 변경을 만들었다 (conformance 테스트 +29줄 포함). 같은 시기에
3090 은 `server/routes_v2.py` 에서 **키가 없으면 내용에서 유도**하는
`server.editreq.derive_idempotency_key()` 를 붙였다 (W29).

브랜치 정리 때 둘을 main 에 함께 병합하자 테스트 4건이 깨졌다:

```
test_idempotency::test_route_derives_key_when_client_omits_it   422
test_idempotency::test_retry_without_key_reuses_the_same_job     422
test_routes_v2::test_edit_on_unknown_asset_is_404                422
test_routes_v2::test_edit_on_missing_base_version_is_409          422
```

라우트 본문 검증이 유도 코드보다 **먼저** 돌아서 키 없는 요청을 422 로 거부한다.
그리고 🔴 **Unity 는 `idempotency_key` 를 보내지 않는다** (`unity/` grep 0건) —
그대로 넣으면 실기 앱의 모든 편집 요청이 422 가 된다.

## 결정

main 에는 넣지 않는다 (병합을 되돌렸다). 변경 내용은 아래에 원문 그대로 보존한다.

다시 올리려면 둘 중 하나가 먼저 서야 한다:
1. Unity 클라이언트가 키를 보낸다 — 그 뒤 필수화. 서버 유도는 하위호환용으로 남기거나 제거
2. 계약은 Optional 로 두고 "서버가 내용에서 유도한다" 를 계약 문서에 명시 — 지금 동작 그대로

## 보존된 변경 (contract/w27-idem → main 기준 diff)

```diff
diff --git a/contract/conformance/test_contract.py b/contract/conformance/test_contract.py
index 2c36125..4dd4c25 100644
--- a/contract/conformance/test_contract.py
+++ b/contract/conformance/test_contract.py
@@ -596,6 +596,35 @@ def test_edit_mask_validation():
     EditMask(mode="bbox", bbox_min=(-0.1, -0.1, -0.1), bbox_max=(0.1, 0.1, 0.1))
 
 
+@needs_schemas
+def test_idempotency_key_is_required_on_work_creating_requests():
+    """🔴 3.27.0 (결정 5) — 연산을 만드는 요청은 멱등 키를 **반드시** 싣는다.
+
+    계약이 Optional 인데 구현이 필수면 어느 쪽이 진실인지 매번 확인해야 한다
+    (`manifest.v99 → 200` 과 같은 계열). 그리고 **서버가 지어내면 안 된다** —
+    이 필드는 연산을 식별하므로(3.7.1), 서버 발급은 같은 요청 두 번이 다른 슬롯을
+    가져가게 만들고 그때 멱등성은 **예외 없이** 사라진다.
+
+    ⚠️ 응답인 `JobStatus.idempotency_key` 는 Optional 그대로다 — 그건 에코이지 입력이 아니다.
+    """
+    from deltacontract.schemas import AssembleRequest, EditMask, EditRequest, JobStatus
+
+    mask = EditMask(mode="voxels", voxels=[(1, 2, 3)], grid_source="slat_coords")
+    with pytest.raises(ValueError, match="idempotency_key"):
+        EditRequest(session_id="s", base_version=1, raw_prompt="x", mask=mask)
+
+    ok = EditRequest(session_id="s", base_version=1, raw_prompt="x", mask=mask,
+                     idempotency_key="deadbeefdeadbeef")
+    assert ok.idempotency_key == "deadbeefdeadbeef"
+
+    # 조립도 연산이다 — 같은 규칙을 받는다.
+    assert "idempotency_key" in AssembleRequest.model_fields
+    assert AssembleRequest.model_fields["idempotency_key"].is_required()
+
+    # 응답은 그대로 Optional (에코).
+    assert not JobStatus.model_fields["idempotency_key"].is_required()
+
+
 @needs_schemas
 def test_slat_coords_response():
     """`GET /v2/assets/{id}/slat_coords.v{n}.json` — 라쏘가 투영할 대상 (3.26.0)."""
diff --git a/contract/python/deltacontract/schemas.py b/contract/python/deltacontract/schemas.py
index 8026fc9..53ed175 100644
--- a/contract/python/deltacontract/schemas.py
+++ b/contract/python/deltacontract/schemas.py
@@ -439,7 +439,20 @@ class EditRequest(BaseModel):
     #
     # 마스크 지문은 `coords.mask_fingerprint()` 를 쓴다 — 직렬화를 손으로 정하면
     # 클라이언트와 서버가 조용히 갈린다 (3.14.0 에서 실제로 갈렸다).
-    idempotency_key: Optional[str] = None
+    #
+    # 🔴 3.27.0 (결정 5) — **Optional 에서 필수로 올린다.**
+    #
+    # 계약은 Optional 인데 구현은 없으면 못 돈다 (슬롯·재생색인·job 기록이 전부 이 키다).
+    # 그 어긋남이 `manifest.v99 → 200` 과 같은 계열이다 — 문서가 허용한 입력을 구현이
+    # 거부하거나 그 반대이면, **어느 쪽이 진실인지 매번 확인해야 한다.**
+    #
+    # 그리고 서버가 지어내면 안 된다: 이 필드는 **연산을 식별한다**(3.7.1).
+    # 서버가 발급하면 같은 요청 두 번이 **다른 슬롯**을 가져가고, 그때 멱등성은
+    # 예외 없이 사라진다 — 재시도가 GPU 재계산이 되고 아무도 모른다.
+    #
+    # ⚠️ 필수로 올리면서 **함수를 같이 준다**: `server.editreq.derive_idempotency_key()`.
+    #    규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다 (이 리포에서 세 번 반복됐다).
+    idempotency_key: str
 
 
 JobState = Literal["queued", "running", "succeeded", "failed"]
@@ -470,7 +483,8 @@ class AssembleRequest(BaseModel):
     offset: Optional[Tuple[int, int, int]] = None
     # 대상에서 **비울** 영역. 기증자 위치를 정하는 것이 아니다 (§53).
     mask: EditMask
-    idempotency_key: Optional[str] = None
+    # 🔴 3.27.0 — EditRequest 와 같은 이유로 필수다 (결정 5). 조립도 연산이다.
+    idempotency_key: str
 
 
 class JobStatus(BaseModel):
```
