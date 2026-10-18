"""把平台 `application.properties` 的 keystore 口令同步成本次生成的 p12 口令。

单独成文件而不是内嵌在 shell 里：一是 shell 的引号/多行在这里特别容易出错（写错过一次，
表现为平台起不来、报 "keystore password was incorrect"），二是这个行为可以被测试直接覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

KEY = "server.ssl.key-store-password="


def sync(props_path: Path, password_path: Path) -> None:
    """把口令写进配置；**已经一致时不写**。

    preflight 允许 `application.properties` 是 0400，而 0400 打不开写。复用一份有效
    证书时本来就不需要改文件 —— 先比对再决定写，是把「不需要写」变成「不去写」，
    而不是靠调用方记得先判断。
    """
    password = password_path.read_text().strip()
    original = props_path.read_text()
    lines = [line for line in original.splitlines() if not line.startswith(KEY)]
    lines.append(f"{KEY}{password}")
    updated = "\n".join(lines) + "\n"
    if updated == original:
        return
    props_path.chmod(0o600)  # 只读文件必须先恢复写权限（属主可以）
    props_path.write_text(updated)
    props_path.chmod(0o600)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: sync-keystore-password.py <application.properties> <password-file>", file=sys.stderr)
        return 2
    sync(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
