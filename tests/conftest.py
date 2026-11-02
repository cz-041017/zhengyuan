"""pytest 根配置：把项目根加入 sys.path，使 tests 可 import interface.hikvision。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
