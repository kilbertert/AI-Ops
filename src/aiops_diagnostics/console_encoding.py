from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from typing import Any


def configure_windows_stdio(
    streams: Iterable[Any] | None = None,
    *,
    platform_name: str | None = None,
) -> None:
    if (platform_name if platform_name is not None else os.name) != "nt":
        return
    selected = streams if streams is not None else (sys.stdout, sys.stderr)
    for stream in selected:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
