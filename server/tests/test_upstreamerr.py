"""상류 사유가 **버려지지 않는가.**

앱 화면에 `UPSTREAM_EDIT_FAILED 편집 제출 실패` 만 뜨고 실제 사유
(`409 VERSION_CONFLICT — base_version=1 은 커밋되지 않았다`)가 안 실렸다.
사유가 없으면 사용자는 재시도만 하고, 재시도로는 절대 안 고쳐지는 오류다.
"""

from __future__ import annotations

import json

from server.upstreamerr import parse_upstream_error


def test_upstream_code_and_message_survive():
    body = json.dumps({"error_code": "VERSION_CONFLICT",
                       "message": "base_version=1 은 커밋되지 않았다 (committed latest=0)"})
    e = parse_upstream_error(409, body, action="편집 제출")
    assert e.error_code == "UPSTREAM_VERSION_CONFLICT"
    assert "커밋되지 않았다" in str(e), "사유가 사라졌다"
    assert "409" in str(e)


def test_origin_prefix_distinguishes_from_local_errors():
    """`VERSION_CONFLICT` 가 3090 것인지 상류 것인지 화면에서 갈려야 한다 — 고칠 사람이 다르다."""
    e = parse_upstream_error(409, json.dumps({"error_code": "VERSION_CONFLICT"}))
    assert e.error_code.startswith("UPSTREAM_")


def test_non_json_body_is_not_invented():
    """실측: 상류가 `500 Internal Server Error` 평문을 준다. 지어내지 않는다."""
    e = parse_upstream_error(500, "Internal Server Error", action="편집 제출")
    assert e.error_code == "UPSTREAM_HTTP_500"
    assert "Internal Server Error" in str(e)


def test_empty_body_still_says_what_happened():
    e = parse_upstream_error(502, "")
    assert e.error_code == "UPSTREAM_HTTP_502"
    assert "본문 없음" in str(e)


def test_detail_key_is_also_read():
    """FastAPI 상류는 `detail` 을 쓴다 — 그것도 버리지 않는다."""
    e = parse_upstream_error(404, json.dumps({"error_code": "NOT_FOUND",
                                              "detail": "staging 도 버전도 없다"}))
    assert "staging" in str(e)
