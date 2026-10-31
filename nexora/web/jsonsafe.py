"""JSON response that keeps Discord snowflakes intact for JavaScript (which loses precision above 2^53)."""
from __future__ import annotations

import json
from typing import Any

from fastapi.responses import JSONResponse

JS_MAX = 2**53


def make_safe(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) >= JS_MAX else value
    if isinstance(value, dict):
        return {k: make_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [make_safe(v) for v in value]
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(make_safe(content), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
