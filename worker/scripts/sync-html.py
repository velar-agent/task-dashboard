#!/usr/bin/env python3
"""server.PAGE로 Worker의 HTML 모듈을 다시 생성한다."""

import argparse
import json
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER.parent))
import server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="파일을 쓰지 않고 동기화 상태를 확인한다.")
    args = parser.parse_args()
    destination = WORKER / "src" / "html.js"
    page = json.dumps(server.PAGE, ensure_ascii=False)
    module = f"export default {page};\n".encode("utf-8")
    if args.check:
        if not destination.is_file() or destination.read_bytes() != module:
            sys.exit("worker/src/html.js를 sync-html.py로 다시 생성하세요.")
    else:
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(module)
