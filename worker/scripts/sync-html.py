#!/usr/bin/env python3
"""server.PAGE로 Worker의 HTML 모듈을 다시 생성한다."""

import json
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER.parent))
import server


if __name__ == "__main__":
    destination = WORKER / "src" / "html.js"
    destination.parent.mkdir(exist_ok=True)
    page = json.dumps(server.PAGE, ensure_ascii=False)
    destination.write_text(f"export default {page};\n", encoding="utf-8")
