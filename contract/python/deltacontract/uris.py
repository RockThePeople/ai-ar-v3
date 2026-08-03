"""
URL 경로 규칙. pydantic 에 의존하지 않는다 — 어떤 환경에서도 뜬다.

3090 자기보고(2026-07-29)의 지적으로 추가됐다:

  "`/v2/` 접두사가 uri 에 들어가는지 클라이언트가 붙이는지가 안 정해져 있습니다.
   Unity 쪽과 어긋나기 쉬운 지점이라 계약에 한 줄 필요합니다."

────────────────────────────────────────────────────────────────────────
규칙
────────────────────────────────────────────────────────────────────────
`ChunkEntry.uri` 는 **선행 슬래시를 포함한 절대 경로 컴포넌트**다. 스킴·호스트는 없다.
클라이언트는 `baseUrl + uri` 로 그냥 이어붙인다.

    uri     = "/v2/assets/a-77c0/chunks/5_3_4.v7.cbin"
    baseUrl = "https://example.com"
    최종      "https://example.com/v2/assets/a-77c0/chunks/5_3_4.v7.cbin"

스킴·호스트를 서버가 박으면 터널/LAN/로컬 전환마다 깨진다. 반대로 경로 접두사를
클라이언트가 붙이게 하면 `/v2` 를 아는 곳이 두 군데가 되고, 어긋나면 404 로만
보인다 — 원인이 어디인지 드러나지 않는다.

**손으로 문자열을 만들지 마라.** 아래 함수만 쓴다.
"""

from __future__ import annotations

import re
from typing import Tuple

from .errors import InvalidRequest


class UriRuleViolation(InvalidRequest, ValueError):
    """URI 규칙 위반. **`InvalidRequest` 이면서 동시에 `ValueError` 다.**

    3090 지적(2026-07-31): 계약 함수가 순수 `ValueError` 를 던지면 그게 요청
    핸들러까지 새어 **`ErrorBody` 없는 500** 이 된다. 3090 의 로컬 정의는
    `InternalError`(계약 예외)였고, 계약 것으로 교체하면서 오히려 나빠졌다.

    그렇다고 `ValueError` 를 떼면 기존 `except ValueError` 호출부가 조용히
    안 잡게 된다. 그래서 **둘 다 상속한다** — 기존 코드는 그대로 돌고,
    FastAPI 의 `DeltaContractError` 핸들러 하나가 422 + ErrorBody 로 받는다.

    이름을 바꾼 게 아니라 넓힌 것이라 이관 메모가 필요 없다.
    """

    pass

#: Unity ↔ 3090 API 접두사. 기존 `/blocks/*` 와 공존시키기 위한 네임스페이스이며,
#: 롤백이 "라우터에서 /v2 를 떼는 것"으로 끝나게 한다.
API_PREFIX = "/v2"

#: 3090 ↔ A5000 내부 API 접두사.
TRELLIS_PREFIX = "/v2/trellis"

_CHUNK_URI_RE = re.compile(
    r"^/v2/assets/(?P<asset_id>[^/]+)/chunks/(?P<chunk_key>\d+_\d+_\d+)\.v(?P<version>\d+)\.cbin$"
)

_STAGING_URI_RE = re.compile(
    r"^/v2/assets/(?P<asset_id>[^/]+)/staging/(?P<job_id>[^/]+)"
    r"/chunks/(?P<chunk_key>\d+_\d+_\d+)\.cbin$"
)

#: `job_id` 는 URI 경로 컴포넌트로 들어간다. 3090 이 traversal 을 막았다(2026-07-31):
#: 문자 집합에 `/` 가 없어도 `..` 자체는 통과하고, 정규화하면 staging 을 한 단계
#:벗어나 자산 청크 경로를 가리킨다. 출처가 A5000 이라 실제 위험은 낮았지만,
#: **출처를 신뢰의 근거로 삼으면 그 가정이 바뀌었을 때 아무도 모른다.**
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: `job_id` 길이 상한. 3090 지적(2026-07-31): 계약에 상한이 없어 200자가 통과했다
#: (3090 로컬 정의는 128자였다). traversal 은 여전히 불가하지만, 상한 없는 값이
#: 경로에 들어가면 파일시스템·로그·인덱스 어딘가에서 잘리고 그 자리가 조용해진다.
JOB_ID_MAX_LEN = 128


def chunk_uri(asset_id: str, chunk_key: str, version: int) -> str:
    """`ChunkEntry.uri` 의 유일한 생성 경로 (Unity 가 받는 것)."""
    return f"{API_PREFIX}/assets/{asset_id}/chunks/{chunk_key}.v{int(version)}.cbin"


def trellis_chunk_uri(asset_id: str, chunk_key: str, version: int) -> str:
    """3090이 A5000 에서 청크 바이트를 가져올 때 쓰는 경로."""
    return f"{TRELLIS_PREFIX}/assets/{asset_id}/chunks/{chunk_key}.v{int(version)}.cbin"


def parse_chunk_uri(uri: str) -> Tuple[str, str, int]:
    """`chunk_uri()` 의 역함수. (asset_id, chunk_key, version).

    라우팅에서 쓰라는 게 아니라, 저장된 매니페스트가 규칙을 지키는지 검증하는 용도다.
    """
    m = _CHUNK_URI_RE.match(uri)
    if not m:
        raise UriRuleViolation(
            f"청크 uri 규칙 위반: {uri!r}. "
            f"기대 형식: {API_PREFIX}/assets/{{asset_id}}/chunks/{{x_y_z}}.v{{n}}.cbin"
        )
    return m.group("asset_id"), m.group("chunk_key"), int(m.group("version"))


# ══════════════════════════════════════════════════════════════════════════
# staging (미커밋 편집분) 청크 — 3.13.0 에서 추가
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 **왜 이 함수가 뒤늦게 생겼나** (2026-07-31)
#
# 3.11.0 이 잡 범위 staging 경로를 **문서에만** 정하고 함수를 안 만들었다.
# 그 결과 3090 과 A5000 이 각자 손으로 문자열을 만들었고, 접두사가 갈렸다:
#
#     /v2/trellis/assets/{id}/staging/{job}/chunks/{cid}.cbin   → 404  (3090 이 유추)
#     /v2/assets/{id}/staging/{job}/chunks/{cid}.cbin           → 200  (A5000 구현)
#
# 이 파일 맨 위가 바로 그 상황을 금지하고 있었다 — "손으로 문자열을 만들지 마라.
# 접두사를 아는 곳이 두 군데가 되면 어긋났을 때 404 로만 보인다."
# **계약이 금지한 것을 계약이 강요했다.** 규율이 아니라 함수가 필요했다.
#
# ⚠️ **접두사 비대칭은 의도된 것이 아니라 실측된 것이다.**
# 일반 청크는 3090↔A5000 내부 홉에 `/v2/trellis` 가 붙는데(`trellis_chunk_uri`),
# staging 은 **양쪽 홉이 같은 문자열**이다. 3.11.0 이 외부 경로 모양으로 적었고
# A5000 이 그대로 구현했기 때문이다. 지금 와서 맞추면 세 세션이 동시에 움직여야
# 하므로, 비대칭을 **고정하고 명시한다.** 유추하지 마라 — 이 함수만 쓴다.


def validate_job_id(job_id: str) -> str:
    """URI 에 들어갈 `job_id` 를 검증한다. 통과하면 그대로 돌려준다.

    `.` 과 `..` 을 거부한다. 3090 실측(2026-07-31): 문자 집합에 `/` 가 없어도
    `..` 은 통과하고, 경로 정규화가 staging 을 한 단계 벗어나게 만든다.
    """
    if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        raise UriRuleViolation(f"job_id 규칙 위반: {job_id!r}")
    if job_id in (".", "..") or "/" in job_id or "\\" in job_id:
        raise UriRuleViolation(f"job_id 에 경로 구분자·상위 참조가 들어갔다: {job_id!r}")
    if len(job_id) > JOB_ID_MAX_LEN:
        raise UriRuleViolation(
            f"job_id 가 {JOB_ID_MAX_LEN}자를 넘는다 ({len(job_id)}자)"
        )
    return job_id


def staging_chunk_uri(asset_id: str, job_id: str, chunk_key: str) -> str:
    """미커밋(`ephemeral`) 편집분 청크의 유일한 생성 경로.

    **Unity←3090 과 3090←A5000 이 같은 문자열이다.** 일반 청크와 달리 내부 홉에도
    `/v2/trellis` 가 붙지 않는다 (위 주석 참조).

    버전 번호가 없다 — 커밋되지 않았으므로 `v{n}` 이 존재하지 않는다.
    바이트를 식별하는 것은 `(asset_id, job_id, chunk_key)` 다.
    """
    return (
        f"{API_PREFIX}/assets/{asset_id}"
        f"/staging/{validate_job_id(job_id)}/chunks/{chunk_key}.cbin"
    )


def parse_staging_chunk_uri(uri: str) -> Tuple[str, str, str]:
    """`staging_chunk_uri()` 의 역함수. (asset_id, job_id, chunk_key)."""
    m = _STAGING_URI_RE.match(uri)
    if not m:
        raise UriRuleViolation(
            f"staging uri 규칙 위반: {uri!r}. 기대 형식: "
            f"{API_PREFIX}/assets/{{asset_id}}/staging/{{job_id}}/chunks/{{x_y_z}}.cbin"
        )
    return m.group("asset_id"), validate_job_id(m.group("job_id")), m.group("chunk_key")


def is_staging_uri(uri: str) -> bool:
    """`ChunkEntry.uri` 가 커밋본인지 미커밋 staging 인지 가른다.

    클라이언트가 캐시 정책을 여기서 나눈다 — staging 바이트는 디스크에 쓰면 안 된다
    (`UNITY_CLIENT.md` §4-1). `PatchPackage.ephemeral` 과 어긋나면 그 자체가 사고다.
    """
    return _STAGING_URI_RE.match(uri) is not None
