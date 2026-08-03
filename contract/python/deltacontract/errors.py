"""
프로토콜 예외 — 에러코드 · HTTP 상태 · 재시도 가능 여부를 한 곳에 묶는다.

3090 자기보고(2026-07-29)의 지적으로 추가됐다:

  "계약이 예외 클래스로 주는지 에러코드 문자열로 주는지에 따라 스토어 시그니처가
   달라집니다. 계약에 없으면 3090 쪽에서 정의하게 되는데, 그러면 A5000 이 HTTP 로
   붙을 때 코드 문자열이 두 번 정의됩니다."

맞는 지적이다. `schemas.ErrorBody.error_code` 는 **응답 본문의 모양**만 정의할 뿐,
서버 내부에서 그 상황을 어떻게 신호할지는 안 정했다. 각자 정의하면 두 세션에서
문자열이 갈리고, 갈린 걸 알아채는 건 통합 시점이다.

여기서 정의하는 것:
  예외 클래스 ↔ error_code ↔ HTTP status ↔ 재시도 가능 여부  (1:1:1:1)

사용:

    from deltacontract.errors import VersionConflict, DeterminismViolation

    raise VersionConflict("base_version 이 최신이 아니다",
                          detail={"latest_version": "7"})

FastAPI 핸들러 한 개로 전부 받는다:

    @app.exception_handler(DeltaContractError)
    async def _handle(request, exc: DeltaContractError):
        return JSONResponse(exc.to_dict(), status_code=exc.http_status)

이 모듈은 **아무것도 import 하지 않는다** (pydantic 조차). 어떤 환경에서도 뜬다.
"""

from __future__ import annotations

from typing import Dict, Optional


class DeltaContractError(Exception):
    """모든 프로토콜 예외의 기반. 직접 raise 하지 마라 — 하위 클래스를 써라."""

    error_code: str = "INTERNAL"
    http_status: int = 500
    # 같은 요청을 그대로 다시 보내는 게 의미 있는가.
    # 멱등성 키를 유지한 재시도를 전제한다 (FINAL §10-1).
    retriable: bool = False

    def __init__(self, message: str = "", detail: Optional[Dict[str, str]] = None):
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, object]:
        """`schemas.ErrorBody` 와 동일한 모양의 dict. pydantic 없이도 만들 수 있다."""
        body: Dict[str, object] = {"error_code": self.error_code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.__class__.__name__}({self.error_code}, {self.http_status}, {self.message!r})"


class ContractMismatch(DeltaContractError):
    """상대편이 다른 계약 상수를 쓰고 있다.

    재시도 금지 — 재시도해도 같은 결과다. 배포 버전이 갈린 것이므로 사람이 봐야 한다.
    """

    error_code = "CONTRACT_MISMATCH"
    http_status = 409
    retriable = False


class Unauthorized(DeltaContractError):
    """공유 시크릿 헤더가 없거나 틀렸다.

    3090 지적(2026-07-29): 401 만 본문 모양이 달랐다. `/v2/*` 는 새 API 이므로
    클라이언트가 에러 파서를 두 벌 들 이유가 없다 — 전부 ErrorBody 로 통일한다.

    ⚠️ message 와 detail 에 **키 값을 절대 넣지 마라.** "없음"/"불일치" 까지만.
    """

    error_code = "UNAUTHORIZED"
    http_status = 401
    retriable = False


class NotFound(DeltaContractError):
    """요청한 리소스가 없다 — 자산이 아닌 것 포함 (잡, 없는 경로 등).

    3090 지적(2026-07-29): 매칭되는 라우트가 없을 때와 모르는 job_id 일 때 둘 다
    404 가 맞는데 쓸 코드가 ASSET_NOT_FOUND 뿐이었다. 자산이 아닌 것에 자산 코드를
    붙이면 클라이언트 분기가 틀어진다.
    """

    error_code = "NOT_FOUND"
    http_status = 404
    retriable = False


class AssetNotFound(NotFound):
    """자산 자체가 없다. 클라이언트는 재생성을 유도한다."""

    error_code = "ASSET_NOT_FOUND"
    http_status = 404
    retriable = False


class InvalidRequest(DeltaContractError):
    """요청 본문/파라미터가 스키마를 위반한다.

    3090 지적(2026-07-29): 422 만 FastAPI 기본 형식으로 나가 본문 모양이 갈렸다.
    서버는 `RequestValidationError` 를 잡아 이 예외로 변환하고, 필드별 메시지는
    `detail` 에 문자열로 담는다:

        detail = {"body.mask.voxels": "list 가 필요하다", ...}
    """

    error_code = "INVALID_REQUEST"
    http_status = 422
    retriable = False


class VersionConflict(DeltaContractError):
    """`base_version` 이 최신이 아니다. `detail["latest_version"]` 을 채워 보내라.

    클라이언트는 `GET .../patch?from_version=` 으로 따라잡은 뒤 재시도한다.
    """

    error_code = "VERSION_CONFLICT"
    http_status = 409
    retriable = False


class MaskEmpty(DeltaContractError):
    """마스크가 활성 복셀을 하나도 안 덮는다. 사용자에게 다시 선택을 요청해야 한다."""

    error_code = "MASK_EMPTY"
    http_status = 422
    retriable = False


class UpstreamTimeout(DeltaContractError):
    error_code = "UPSTREAM_TIMEOUT"
    http_status = 504
    retriable = True


class UpstreamOOM(DeltaContractError):
    error_code = "UPSTREAM_OOM"
    http_status = 503
    retriable = True


class BookkeepingMismatch(DeltaContractError):
    """B 가 부기(마스크+halo)가 지목한 청크 집합과 **다른 집합**을 보냈다.

    3.4.0 에서 의미가 바뀌었다 (이전 이름: `DETERMINISM_VIOLATION`).

    이전에는 "안 바뀐 청크는 바이트 동일"을 해시로 판정했고, 부기가 실제 해시 변경을
    덮지 못하면 결정성 위반이었다. 지금은 **부기가 무엇이 바뀌었는지를 정의한다** —
    부기 밖 청크는 이전 버전 바이트를 그대로 재사용한다. 그래서 해시 비교 자체가 없다.

    그럼 무엇을 검사하는가: B 가 부기가 지목한 청크를 **하나라도 빠뜨렸는지**.
    빠뜨리면 그 청크는 낡은 바이트로 남고, 편집이 반영되지 않은 조각이 화면에 남는다.
    예외가 안 나고 눈으로만 보이는 종류라 여기서 잡는다.

    ★ 재시도 금지. 재시도해도 같은 결과이고, 자동 복구하면 사실이 조용히 묻힌다.

    detail 에 최소한 이걸 채워라:
        {"missing_chunks": "5_3_4,6_3_4", "bookkeeping_size": "24", "returned_size": "22"}
    """

    error_code = "BOOKKEEPING_MISMATCH"
    http_status = 500
    retriable = False


#: 3.3.x 이하 호환용 별칭. 새 코드는 BookkeepingMismatch 를 써라.
#:
#: ⚠️ 3.11.1: 이 별칭이 **필요한 구분을 접었다.** 3.4.0 이 "결정성 위반" 개념을
#: 폐기하면서 두 클래스를 하나로 합쳤는데, `_materialize` 의 docstring 은 줄곧
#: "전송 바이트가 보고와 다르면 DETERMINISM_VIOLATION" 이라고 적고 있었다.
#: 그 상황을 가리킬 코드가 계약에서 사라진 것이다 (3090 발견, 2026-07-30).
#: → 아래 `ChunkHashMismatch` 가 그 자리를 채운다.
DeterminismViolation = BookkeepingMismatch


class ChunkHashMismatch(DeltaContractError):
    """부기 집합은 맞게 왔으나 **전송된 청크 바이트의 해시가 보고와 다르다.**

    3.11.1 신설. `BookkeepingMismatch` 와 혼동하지 마라 — 그쪽은 **집합**의 문제이고
    이쪽은 **바이트**의 문제다. 3.4.0 이후 해시는 델타 판정에 쓰이지 않지만,
    전송 무결성 확인에는 여전히 쓰인다. 그 둘은 다른 검사다.

    ★ **재시도 가능하다.** 이게 `BookkeepingMismatch` 와 갈라야 하는 실질적 이유다 —
    부기 누락은 재시도해도 같은 결과지만 바이트 불일치는 아니다.
    서버가 이미 1회 자동 재시도를 하고 있고, 그래도 안 되면 이 예외가 나온다.

    detail 에 **세 갈래를 가를 수 있는 것**을 담아라 (3090 설계, 실측 기반):

        {"chunk_id": "3_3_5",
         "reported_hash": "...", "received_hash": "...",
         "reported_length": "10824", "received_length": "10824",
         "length_matches": "true",
         "retry_stable": "false",          # 1차와 2차 시도의 바이트가 다른가
         "diagnosis": "source_changing"}

    ── 세 갈래 ──
      길이가 다르다              → 전송 손상 / 잘림
      길이는 같은데 내용이 다르다  → 재계산 비결정성
      **두 시도가 서로 다르다**    → 원본이 변하고 있다 (staging 덮어쓰기 등)

    셋째는 3090 이 실측에서 찾았다. 앞의 둘로만 분류하면 경합을 **비결정성으로
    오진한다** — 훨씬 무거운 결론으로 잘못 간다. 그래서 1차 시도 바이트를 버리지
    말고 비교해라.
    """

    error_code = "CHUNK_HASH_MISMATCH"
    http_status = 500
    retriable = True


class StagingExpired(DeltaContractError):
    """`ephemeral` 편집 결과의 staging 슬롯이 **만료됐다.**

    3.12.0 신설. A5000 이 로컬 정의해 쓰던 것을 계약이 채택했다.

    ★ **`NotFound`(404) 와 갈라야 하는 이유가 클라이언트 분기다:**

        404  URI 가 틀렸다        → 버그다. 고쳐야 한다
        410  존재했고 만료됐다     → 정상. 편집을 다시 요청하면 된다

    뭉치면 사용자가 "편집이 깨졌다" 와 "너무 오래 기다렸다" 를 구분 못 한다.

    `retriable=False` 인 이유: 같은 URI 를 다시 때려도 영원히 410 이다.
    되살리려면 **편집 자체를 다시 요청**해야 하고, 그건 새 요청이지 재시도가 아니다.

    detail 에 TTL 을 담아라 — 클라이언트가 사용자에게 설명할 수 있다:

        {"ttl_s": "3600", "chunk_id": "3_3_5.cbin"}
    """

    error_code = "STAGING_EXPIRED"
    http_status = 410
    retriable = False


class InternalError(DeltaContractError):
    error_code = "INTERNAL"
    http_status = 500
    retriable = True


#: error_code -> 예외 클래스. 상대 서버 응답을 예외로 되살릴 때 쓴다.
ERROR_CODE_TO_EXCEPTION: Dict[str, type] = {
    cls.error_code: cls
    for cls in (
        ContractMismatch,
        Unauthorized,
        NotFound,
        AssetNotFound,
        InvalidRequest,
        VersionConflict,
        MaskEmpty,
        UpstreamTimeout,
        UpstreamOOM,
        BookkeepingMismatch,
        ChunkHashMismatch,
        StagingExpired,
        InternalError,
    )
}


def raise_from_error_body(body: Dict[str, object]) -> None:
    """상대 서버의 `ErrorBody` 를 받아 대응하는 예외로 다시 던진다.

    3090이 A5000 응답을 처리할 때 쓴다. 모르는 코드는 InternalError 로 떨어뜨리되
    원본 코드를 detail 에 남겨서 조용히 삼키지 않는다.
    """
    code = str(body.get("error_code", "INTERNAL"))
    message = str(body.get("message", ""))
    detail = body.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {"raw_detail": str(detail)}
    cls = ERROR_CODE_TO_EXCEPTION.get(code)
    if cls is None:
        detail = {**detail, "unknown_error_code": code}
        raise InternalError(message, detail)  # type: ignore[arg-type]
    raise cls(message, detail)  # type: ignore[arg-type]
