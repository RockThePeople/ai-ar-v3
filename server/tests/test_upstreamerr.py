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


def test_no_host_or_port_leaks_into_the_message():
    """🔴 §7 — 사유에 호스트·포트가 들어가면 안 된다.

    실측: `httpx.raise_for_status()` 의 메시지에 URL 이 통째로 들어가 공인 IP 가
    잡 상태로 올라갔다. 사유는 전파하되 **주소는 전파하지 않는다.**
    """
    import re

    e = parse_upstream_error(500, "Internal Server Error", action="잡 조회")
    msg = str(e)
    assert not re.search(r"\d{1,3}(\.\d{1,3}){3}", msg), f"IP 가 샜다: {msg}"
    assert "://" not in msg, f"URL 이 샜다: {msg}"
    assert "<EDIT_HOST>" in msg, "출처 표기가 자리표시여야 한다"


def test_connect_error_is_an_upstream_reason_not_internal():
    """🔴 상대가 내려가 있는 것을 'INTERNAL' 로 내면 화면이 '서버 버그' 라고 말한다.

    고칠 사람이 다르다 (D71). 그리고 httpx 메시지에는 URL 이 들어갈 수 있어
    **예외 종류만** 쓴다 (§7).
    """
    import re

    import httpx
    import pytest

    from server.upstreamerr import UpstreamError, upstream_call

    def boom():
        raise httpx.ConnectError("[Errno 111] refused",
                                 request=httpx.Request("POST", "http://10.1.2.3:9999/x"))

    with pytest.raises(UpstreamError) as e:
        upstream_call(boom, action="편집 제출")
    assert e.value.error_code == "UPSTREAM_UNREACHABLE"
    msg = str(e.value)
    assert not re.search(r"\d{1,3}(\.\d{1,3}){3}", msg), f"IP 가 샜다: {msg}"
    assert "://" not in msg
