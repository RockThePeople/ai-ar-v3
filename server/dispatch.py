"""`EditSpec.op` → 소비자 배선. **방어가 있는 자리다** (D26).

────────────────────────────────────────────────────────────────────────
🔴 가장 위험한 실패 모드: `add` 를 assemble 로 보내는 것
────────────────────────────────────────────────────────────────────────
`llm.py` 의 모듈 docstring 이 이미 적어 놓은 것이다:

    add   assemble 경로 → **원리적으로 불가** (없던 가지를 뻗게 할 수 없다 — D22 ①)
          VoxHammer 경로 → 마스크 조건부 편집 (레벨2: 머리를 3개로)

    ⚠️ 소비자가 `add` 를 assemble 로 보내면 조용히 아무것도 안 일어난다 —
       그 방어는 소비자 쪽에 둔다.

assemble 은 "마스크를 비우고 도너를 정수 이동으로 끼운다" 이다. `add` 를 주면
비우고 → 끼울 도너가 없거나 → 결과가 base 와 사실상 같아진다. **예외가 안 난다.**
지표도 돈다. 신규 복셀이 0 에 가깝게 나오고, 그건 "모델이 약하다" 로 오독된다.
이 프로젝트가 여섯 번 물린 모양 그대로다:

    "예외가 안 났다" ≠ "안전하다" — 그 경로가 안 돌았을 수 있다

그래서 **거부한다.** 무시하지도, 다른 op 로 바꿔 주지도 않는다. 자동 강등
(`add` → `replace_region`)은 특히 하지 않는다 — 그러면 게이트가 "레벨2 를 했다"
고 적으면서 실제로는 레벨1 을 한 결과를 재게 된다.

────────────────────────────────────────────────────────────────────────
거부는 **로그와 응답 양쪽에** 드러난다
────────────────────────────────────────────────────────────────────────
로그에만 남기면 API 호출자는 성공으로 본다. 응답에만 담으면 배치 실행에서
아무도 안 본다. 둘 다 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

from .llm import OPS, EditSpec

__all__ = [
    "CONSUMERS",
    "UnsupportedOp",
    "ConsumerCapability",
    "capability_table",
    "check_supported",
    "dispatch",
]

log = logging.getLogger("aiarv3.dispatch")


class UnsupportedOp(RuntimeError):
    """소비자가 이 op 를 처리할 수 없다. **조용히 넘어가지 않는다.**

    필드를 들고 다니는 이유: 호출부가 문자열을 파싱하지 않고도 응답 본문에
    구조화해서 실을 수 있어야 하기 때문이다.
    """

    #: 잡 상태에 그대로 실린다. `INTERNAL` 로 나가면 클라이언트가 "서버 버그" 와
    #: "이 경로로는 못 하는 요청" 을 구분하지 못한다 — 후자는 사용자가 고칠 수 있다.
    error_code = "UNSUPPORTED_OP"

    def __init__(self, op: str, consumer: str, reason: str, remedy: str) -> None:
        super().__init__(
            f"소비자 {consumer!r} 는 op={op!r} 를 처리할 수 없다. {reason} → {remedy}"
        )
        self.op = op
        self.consumer = consumer
        self.reason = reason
        self.remedy = remedy

    def as_dict(self) -> Dict[str, str]:
        return {
            "error": "unsupported_op",
            "op": self.op,
            "consumer": self.consumer,
            "reason": self.reason,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class ConsumerCapability:
    """한 소비자가 처리할 수 있는 op 집합 + 못 하는 것의 **이유**.

    이유를 데이터로 들고 있는 이유: 거부 메시지가 "지원 안 함" 으로만 나오면
    다음 세션이 "왜" 를 다시 조사한다. 근거를 여기 한 번만 적는다.
    """

    name: str
    supported: FrozenSet[str]
    reasons: Dict[str, str]
    remedy: str

    def rejects(self, op: str) -> Optional[str]:
        return None if op in self.supported else self.reasons.get(op, "지원하지 않는다")


CONSUMERS: Dict[str, ConsumerCapability] = {
    "assemble": ConsumerCapability(
        name="assemble",
        # 🔴 add 도 recolor 도 없다. 이것이 이 파일의 핵심이다.
        supported=frozenset({"replace_region", "remove"}),
        reasons={
            "add": (
                "assemble 은 마스크를 비우고 도너를 **정수 평행이동으로 끼우는** 연산이라, "
                "없던 가지를 몸통에서 갈라져 나오게 만들 수 없다 (D22 ①). "
                "그냥 보내면 예외 없이 신규 복셀이 0 에 가깝게 나오고, "
                "그 숫자가 '모델이 약하다' 로 오독된다"
            ),
            "recolor": (
                "assemble 은 `occupancy_to_mesh` 를 거치는데 그 함수는 정점·면만 낸다 — "
                "색 채널이 없어서 이 경로를 타는 순간 **색이 통째로 사라진다** (D24 원인 진단). "
                "색 지시를 여기로 보내면 예외 없이 색이 없어지고, 결과는 '색이 안 바뀌었다' 로 보인다"
            ),
        },
        remedy=(
            "op=add 는 VoxHammer 로 (consumer='voxhammer'), "
            "op=recolor 는 recolor 경로로 보내라 (consumer='recolor')"
        ),
    ),
    "recolor": ConsumerCapability(
        name="recolor",
        # 색만 한다. 형태를 바꾸는 op 는 이 경로에 없다.
        supported=frozenset({"recolor"}),
        reasons={
            "replace_region": (
                "recolor 경로는 정점 색만 갈아끼운다 (D24). 기하를 만들 수단이 없다"
            ),
            "add": "recolor 경로는 기하를 만들지 않는다. 복셀을 추가할 수단이 없다",
            "remove": "recolor 경로는 기하를 지우지 않는다. 청크를 비울 수단이 없다",
        },
        remedy="형태를 바꾸는 op 는 assemble 또는 VoxHammer 로 보내라",
    ),
    "voxhammer": ConsumerCapability(
        name="voxhammer",
        # 🔴 recolor 가 **빠져 있다.** 아래 이유 참고 — W8/맥북 판단.
        supported=frozenset({"replace_region", "add", "remove"}),
        reasons={
            "recolor": (
                "레벨1 의 정의는 **기하 불변**인데 VoxHammer 는 자산을 재디코딩하므로 "
                "기하가 흔들린다 (A5000 실측 마스크 밖 IoU 0.853 = 잡음 바닥값 대비 2.10배). "
                "즉 VoxHammer 로 색만 바꾸면 **레벨1 의 판정 조건 자체가 무너지고** GPU 까지 쓴다. "
                "recolor 경로는 같은 일을 기하 바이트 100% 보존으로 한다 (D24)"
            ),
        },
        remedy="op=recolor 는 recolor 경로로 보내라 (consumer='recolor'). GPU 가 필요 없다",
    ),
}


def capability_table() -> Dict[str, Dict[str, bool]]:
    """{consumer: {op: 지원여부}}. DebugView·문서가 같은 한 소스를 본다."""
    return {
        name: {op: (op in cap.supported) for op in OPS}
        for name, cap in CONSUMERS.items()
    }


def check_supported(spec: EditSpec, consumer: str) -> None:
    """처리 불가면 `UnsupportedOp` 를 던진다. 통과하면 아무것도 안 한다.

    ⚠️ 반환값으로 bool 을 주지 않는다. bool 을 주면 호출부가 검사하고도
       무시할 수 있고, 그게 곧 조용한 실패다. 예외만 낸다.
    """
    cap = CONSUMERS.get(consumer)
    if cap is None:
        raise UnsupportedOp(
            op=spec.op,
            consumer=consumer,
            reason=f"모르는 소비자다. 아는 것: {sorted(CONSUMERS)}",
            remedy="consumer 이름을 확인하라",
        )
    reason = cap.rejects(spec.op)
    if reason is None:
        return

    # 로그 **와** 예외 둘 다. 하나만 하면 한쪽 관측자가 못 본다.
    log.error(
        "★ op 거부 — consumer=%s op=%s source=%s target=%r · %s",
        consumer, spec.op, spec.source, spec.target_prompt, reason,
    )
    raise UnsupportedOp(
        op=spec.op, consumer=consumer, reason=reason, remedy=cap.remedy
    )


def dispatch(spec: EditSpec, consumer: str) -> Dict[str, object]:
    """검사를 통과한 스펙을 소비자에게 넘길 형태로 만든다.

    ⚠️ 여기서 op 를 갈아끼우지 않는다. 자동 강등(`add` → `replace_region`)은
       게이트가 "레벨2 를 했다" 고 적으면서 레벨1 결과를 재게 만든다.
       못 하는 것은 못 한다고 하고 멈춘다.

    Raises:
        UnsupportedOp: 소비자가 이 op 를 처리할 수 없다.
    """
    check_supported(spec, consumer)
    log.info(
        "op 배선 — consumer=%s op=%s factor=%s source=%s",
        consumer, spec.op, spec.factor, spec.source,
    )
    return {"consumer": consumer, **spec.as_dict()}
