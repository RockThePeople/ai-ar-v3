"""서버측 순수 로직 — 오케스트레이터·DebugView 는 아직 없다 (S2/3090 담당).

계약 패키지는 리포 안 고정 경로(`contract/python`)에 있다. 세 기기가 각자 다른
방식으로 `PYTHONPATH` 를 맞추다가 어긋나는 것을 막기 위해, 여기서 한 번만 붙인다.
"규칙만 적고 함수를 안 주면 그 규칙은 안 지켜진다" 의 같은 처방이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CONTRACT_PYTHON = Path(__file__).resolve().parent.parent / "contract" / "python"
if _CONTRACT_PYTHON.is_dir() and str(_CONTRACT_PYTHON) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_PYTHON))
