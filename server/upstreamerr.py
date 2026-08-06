"""상류 오류를 **그대로 전파한다.** "실패" 로 뭉뚱그리지 않는다.

────────────────────────────────────────────────────────────────────────
왜
────────────────────────────────────────────────────────────────────────
앱 화면에 `UPSTREAM_EDIT_FAILED 편집 제출 실패` 만 떴다. 실제 사유는

    409 VERSION_CONFLICT — "base_version=1 은 커밋되지 않았다 (committed latest=0)"

인데 그게 화면에 없었다. **사유가 없으면 사용자는 재시도만 한다** — 그리고 재시도는
이 오류를 절대 못 고친다. `UNSUPPORTED_OP` 에서 op 를 밝힌 것과 같은 이유다:
"서버 버그" 와 "사용자가 고칠 수 있는 것" 과 "다른 기기가 고쳐야 하는 것" 은 다르다.

────────────────────────────────────────────────────────────────────────
🔴 출처를 접두사로 남긴다
────────────────────────────────────────────────────────────────────────
상류 코드를 **그대로** 쓰면 우리 것과 구분이 안 된다 — `VERSION_CONFLICT` 가
3090 에서 난 것인지 `<EDIT_HOST>` 에서 난 것인지 화면만 봐서는 모른다. 고칠 사람이
달라지므로 `UPSTREAM_` 을 붙인다.

⚠️ 상류가 JSON 을 안 줄 수도 있다 (실측: `500 Internal Server Error` 평문). 그때는
   **지어내지 않고** 상태 코드와 본문 앞부분을 그대로 싣는다.
"""

from __future__ import annotations

import json
from typing import Optional, Tuple

__all__ = ["UpstreamError", "parse_upstream_error"]


class UpstreamError(RuntimeError):
    """상류가 준 사유를 들고 다닌다. `error_code` 는 잡 상태에 그대로 실린다."""

    def __init__(self, error_code: str, message: str, *,
                 status: Optional[int] = None, where: str = "<EDIT_HOST>") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status
        self.where = where


def parse_upstream_error(status: int, body: str, *, where: str = "<EDIT_HOST>",
                         action: str = "요청") -> UpstreamError:
    """상류 응답 → `UpstreamError`. 사유를 **버리지 않는다.**

    Returns:
        `error_code` 는 `UPSTREAM_<상류코드>` 다. 상류가 코드를 안 주면
        `UPSTREAM_HTTP_<상태>` — 그래도 "무엇이 일어났는지" 는 남는다.
    """
    code: Optional[str] = None
    msg: Optional[str] = None
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            code = d.get("error_code") or d.get("code")
            msg = d.get("message") or d.get("detail")
    except (ValueError, TypeError):
        pass

    if code:
        return UpstreamError(
            f"UPSTREAM_{code}",
            f"{where} {action} 실패 ({status} {code}): {msg or body[:200]}",
            status=status, where=where)
    # 🔴 JSON 이 아니어도 지어내지 않는다 — 상태와 본문을 그대로 싣는다.
    return UpstreamError(
        f"UPSTREAM_HTTP_{status}",
        f"{where} {action} 실패 ({status}): {(body or '(본문 없음)')[:200]}",
        status=status, where=where)
