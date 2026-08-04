from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: codex_launcher REAL_CODEX [ARGS...]")
    real_codex = sys.argv[1]
    clean_env = _clean_environment(os.environ)
    os.execve(real_codex, [real_codex, *sys.argv[2:]], clean_env)


def _clean_environment(source: os._Environ[str] | dict[str, str]) -> dict[str, str]:
    allowed = (
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CODEX_HOME",
        "AIOPS_CODEX_PROVIDER_KEY",
    )
    result = {name: source[name] for name in allowed if source.get(name)}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        value = source.get(name) or source.get(name.lower())
        if value and _proxy_without_credentials(value):
            result[name] = value
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    result["NO_COLOR"] = "1"
    return result


def _proxy_without_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.username is None and parsed.password is None
    except ValueError:
        return False


if __name__ == "__main__":
    main()
