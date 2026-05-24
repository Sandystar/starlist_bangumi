from __future__ import annotations

import json

from starlist_bangumi.exceptions import AppError


def exception_message(exc: Exception) -> str:
    base_message = str(exc) or f"{type(exc).__module__}.{type(exc).__name__}"
    if isinstance(exc, AppError) and exc.details:
        details = json.dumps(exc.details, ensure_ascii=False)
        return f"{base_message}: {details}"
    return base_message
