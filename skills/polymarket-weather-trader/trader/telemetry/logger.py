from __future__ import annotations

import json
from typing import Any


class StructuredLogger:
    """Small wrapper that can emit plain text or JSON-ish event lines."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def log(self, message: str, force: bool = False, **fields: Any) -> None:
        if self.quiet and not force:
            return
        if fields:
            payload = {"message": message, **fields}
            print(json.dumps(payload, ensure_ascii=True))
        else:
            print(message)

    def event(self, event: str, force: bool = False, **fields: Any) -> None:
        self.log("", force=force, event=event, **fields)

