"""显式重键KB自有租户映射，保留owner、库和凭据；默认只读，恢复必须CAS。"""

import argparse
import base64
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path


def _private(path, *, exists=True):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("私有路径不能含符号链接")
    if exists and (not path.is_file() or path.stat().st_mode & 0o077):
        raise ValueError("文件须为私有普通文件")
    if not exists and (path.exists() or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077):
        raise ValueError("备份须为尚不存在的文件")
    return path


def migrate(database, source, target, expected_email, *, backup=None, restore=False):
    """只转移指定命名空间，不建账号、不更新密码或API token。"""
    database = _private(database)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source) or not target.startswith("env_"):
        raise ValueError("迁移命名空间无效")
    encoded = target[4:]
    try:
        scope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if (
            not isinstance(scope, list)
            or len(scope) != 2
            or scope[1] != source
            or not isinstance(scope[0], str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?", scope[0])
            or base64.urlsafe_b64encode(json.dumps(scope, ensure_ascii=False, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
            != encoded
        ):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise ValueError("目标须为该租户的规范环境命名空间") from exc
    if source == target or not isinstance(expected_email, str) or not expected_email:
        raise ValueError("须指定不同命名空间及既有owner")
    if restore and backup is None:
        raise ValueError("恢复须提供本次备份")
    applying = backup is not None
    original = None
    if restore:
        original = json.loads(_private(backup).read_text())
        if (
            original.get("schema") != "aiops-kb-namespace-migration/1"
            or original.get("database") != str(database)
            or (original.get("source"), original.get("target")) != (source, target)
            or original.get("row", {}).get("ragflow_email") != expected_email
        ):
            raise ValueError("备份不属于本次迁移")
        source, target = target, source
    elif applying:
        backup = _private(backup, exists=False)
    uri = database.as_uri() + ("?mode=rw" if applying else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE" if applying else "BEGIN")
            row = conn.execute("SELECT * FROM tenants WHERE biz_tenant_id=?", (source,)).fetchone()
            if row is None or row["ragflow_email"] != expected_email:
                raise ValueError("源命名空间与owner不匹配")
            row = dict(row)
            if conn.execute("SELECT 1 FROM tenants WHERE biz_tenant_id=?", (target,)).fetchone():
                raise ValueError("目标命名空间已存在；先对账，拒绝覆盖")
            if restore:
                expected = {**original["row"], "biz_tenant_id": source}
                if row != expected:
                    raise ValueError("迁移后资产已漂移，拒绝复活旧凭据")
            if not applying:
                conn.rollback()
                return {"state": "ready", "changed": False}
            if not restore:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(
                        {
                            "schema": "aiops-kb-namespace-migration/1",
                            "database": str(database),
                            "source": source,
                            "target": target,
                            "row": row,
                        },
                        out,
                    )
                    out.flush()
                    os.fsync(out.fileno())
                directory = os.open(backup.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            conn.execute("UPDATE tenants SET biz_tenant_id=? WHERE biz_tenant_id=?", (target, source))
            current = dict(conn.execute("SELECT * FROM tenants WHERE biz_tenant_id=?", (target,)).fetchone())
            if current != {**row, "biz_tenant_id": target}:
                raise ValueError("回读不一致，事务回滚")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"state": "restored" if restore else "migrated", "changed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--expected-email", required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    print(
        json.dumps(
            migrate(
                args.database,
                args.source,
                args.target,
                args.expected_email,
                backup=args.backup,
                restore=args.restore,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
