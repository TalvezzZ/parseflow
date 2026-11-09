"""Private bounded Excel export process."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.tasks.exports import ExportLimitExceeded, generate_document_exports


def main() -> None:
    request_path, response_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        generate_document_exports(request["document"], Path(request["workspace"]), max_bytes=request["max_bytes"])
        status = "success"
    except ExportLimitExceeded:
        status = "limit"
    except Exception:
        status = "failed"
    response_path.write_text(status, encoding="utf-8")


if __name__ == "__main__":
    main()
