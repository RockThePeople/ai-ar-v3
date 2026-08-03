"""pytest 진입 설정 — 리포 루트를 import 경로에 넣는다.

계약(`contract/python`) 경로는 `server/__init__.py` 가 붙인다. 여기서 또 붙이면
두 곳이 어긋날 때 어느 쪽이 이겼는지 알 수 없게 되므로 한 곳에만 둔다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
