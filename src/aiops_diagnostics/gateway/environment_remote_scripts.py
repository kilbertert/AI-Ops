"""受管主机远端只读探针与安装自举 Python 脚本模板。

集中维护 `environment_task_worker.py` 通过 ``dev-host exec ... -- python3 -``
投递到目标主机执行的远端脚本，避免业务编排函数内联几十行无缩进的字符串拼接。
所有外部参数均经 ``repr(...)`` 注入，不拼 shell。
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

#: 固定安装归档允许解压的唯一文件白名单。
ALLOWED_INSTALLER_MEMBERS = {
    "REVISION",
    "SHA256SUMS",
    "install.py",
    "runtime_check.py",
    "probe.py",
    "artifact_checks.py",
    "task.py",
    "publication.py",
}


def guard_remote_archive(archive_path: str, expected_sha256: str) -> str:
    """上传前校验远端归档路径非符号链接、属主为 root 且既有内容与预期一致。"""
    return textwrap.dedent(
        f"""\
        from pathlib import Path
        import hashlib

        p = Path({archive_path!r})
        if p.exists() or p.is_symlink():
            assert not p.is_symlink() and p.is_file() and p.stat().st_uid == 0 and p.stat().st_nlink == 1
            assert hashlib.sha256(p.read_bytes()).hexdigest() == {expected_sha256!r}
        """
    )


def extract_preflight_installer(remote_dir: str, expected_sha256: str) -> str:
    """核验固定安装包摘要后，在 ``0700`` root 目录内按白名单展开成员。"""
    allowed = sorted(ALLOWED_INSTALLER_MEMBERS)
    return textwrap.dedent(
        f"""\
        import hashlib
        import os
        import tarfile
        from pathlib import Path

        p = Path({remote_dir + ".tar.gz"!r})
        assert p.is_file() and not p.is_symlink() and p.stat().st_uid == 0
        assert hashlib.sha256(p.read_bytes()).hexdigest() == {expected_sha256!r}

        d = Path({remote_dir!r})
        os.umask(0o077)
        d.mkdir(mode=0o700, exist_ok=True)
        assert not d.is_symlink() and d.stat().st_uid == 0 and d.stat().st_mode & 0o077 == 0

        allowed = set({allowed!r})
        with tarfile.open(p) as t:
            for m in t:
                if m.name == "." and m.isdir():
                    continue
                name = m.name[2:] if m.name.startswith("./") else m.name
                assert m.isfile() and name in allowed
                q = d / name
                body = t.extractfile(m).read()
                if q.exists():
                    assert not q.is_symlink() and q.stat().st_uid == 0 and q.read_bytes() == body
                else:
                    with q.open("xb") as f:
                        f.write(body)
        """
    )


def run_preflight_driver(remote_dir: str, args: list[str]) -> str:
    """自举受管解释器（旧包回退 PATH ``python3``）并运行固定包 ``preflight``。"""
    return textwrap.dedent(
        f"""\
        import subprocess
        import sys

        sys.path.insert(0, {remote_dir!r})
        import probe

        py = (
            probe.system_python(probe.managed_python())[0]
            if hasattr(probe, "managed_python")
            else "python3"
        )
        r = subprocess.run(
            [py] + {args!r},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        sys.stdout.write(r.stdout)
        sys.exit(r.returncode)
        """
    )


def probe_host_identity(service_user: str) -> str:
    """只读采集目标主机名、``machine-id`` SHA-256 摘要与服务用户存在性。"""
    return textwrap.dedent(
        f"""\
        import hashlib
        import json
        import pwd
        import socket

        out = {{"hostname": socket.gethostname()}}
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                body = open(p, "rb").read().strip()
                if body:
                    out["machine_id_sha256"] = hashlib.sha256(body).hexdigest()
                    break
            except OSError:
                pass
        try:
            pwd.getpwnam({service_user!r})
            out["service_user_present"] = True
        except KeyError:
            out["service_user_present"] = False
        print(json.dumps(out))
        """
    )


def probe_registration_intent(lockpath: str, marker: str) -> str:
    """在环境排他锁内检查登记意图文件：在场退出 0，缺席退出 7，锁占/异常退出 9。"""
    return textwrap.dedent(
        f"""\
        import fcntl
        import os
        import sys
        from pathlib import Path

        fd = os.open({lockpath!r}, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, "w")
        st = os.fstat(lock.fileno())
        if st.st_uid != 0 or st.st_mode & 0o077 or st.st_nlink != 1:
            sys.exit(9)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(9)
        sys.exit(0 if Path({marker!r}).is_file() else 7)
        """
    )


def probe_registration_assets(lockpath: str, database: str, tenant: str, environment: str) -> str:
    """在环境排他锁内只读核对工作区与双端应用资产行：在场退出 0，缺席退出 7，锁占退出 9。"""
    return textwrap.dedent(
        f"""\
        import fcntl
        import os
        import sqlite3
        import sys
        from pathlib import Path

        fd = os.open({lockpath!r}, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, "w")
        st = os.fstat(lock.fileno())
        if st.st_uid != 0 or st.st_mode & 0o077 or st.st_nlink != 1:
            sys.exit(9)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(9)
        p = Path({database!r})
        if not p.is_file():
            sys.exit(7)
        con = sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True)
        q = (
            "SELECT count(*) FROM dify_workspaces WHERE tenant_id=? AND environment=? "
            "UNION ALL SELECT count(*) FROM operator_apps WHERE tenant_id=? AND environment=?"
        )
        n = sum(r[0] for r in con.execute(q, ({tenant!r}, {environment!r}, {tenant!r}, {environment!r})))
        sys.exit(0 if n else 7)
        """
    )


def read_central_archive(source_path: str, expected_sha256: str) -> str:
    """在控制网关侧核验私有归档权限与 SHA-256 后将字节流输出至 stdout。"""
    return textwrap.dedent(
        f"""\
        import hashlib
        import sys
        from pathlib import Path

        p = Path({source_path!r})
        assert p.is_file() and not p.is_symlink() and p.stat().st_mode & 0o077 == 0
        actual = hashlib.file_digest(p.open("rb"), "sha256").hexdigest()
        assert actual == {expected_sha256!r}
        sys.stdout.buffer.write(p.read_bytes())
        """
    )


def guard_staged_archive(remote_dir: str, filename: str, expected_sha256: str) -> str:
    """准备 ``0700`` root 暂存目录并核验目标归档位置无符号链接或冲突内容。"""
    return textwrap.dedent(
        f"""\
        import hashlib
        import os
        from pathlib import Path

        d = Path({remote_dir!r})
        d.mkdir(mode=0o700, exist_ok=True)
        assert not d.is_symlink() and d.stat().st_uid == 0 and d.stat().st_mode & 0o077 == 0
        p = d / {filename!r}
        if p.exists() or p.is_symlink():
            assert p.is_file() and not p.is_symlink() and p.stat().st_uid == 0
            assert p.stat().st_nlink == 1
            assert hashlib.sha256(p.read_bytes()).hexdigest() == {expected_sha256!r}
        """
    )


def run_installer_task(
    remote_dir: str,
    installer_sha256: str,
    installer_revision: str,
    arguments: list[str],
    spec: dict[str, Any],
) -> str:
    """核验固定包并展开至 ``remote_dir/installer``，随后以受管 Python 运行 ``task.py``。"""
    allowed = sorted(ALLOWED_INSTALLER_MEMBERS)
    return textwrap.dedent(
        f"""\
        import hashlib
        import json
        import subprocess
        import sys
        import tarfile
        from pathlib import Path

        archive = Path({remote_dir + "/installer.tar.gz"!r})
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert actual == {installer_sha256!r}

        d = Path({remote_dir + "/installer"!r})
        d.mkdir(mode=0o700, exist_ok=True)
        assert not d.is_symlink() and d.stat().st_uid == 0 and d.stat().st_mode & 0o077 == 0

        allowed = set({allowed!r})
        with tarfile.open(archive) as t:
            for m in t:
                if m.name == "." and m.isdir():
                    continue
                n = m.name[2:] if m.name.startswith("./") else m.name
                assert m.isfile() and n in allowed
                q = d / n
                body = t.extractfile(m).read()
                if q.exists() or q.is_symlink():
                    assert q.is_file() and not q.is_symlink() and q.stat().st_uid == 0
                    assert q.read_bytes() == body
                else:
                    with q.open("xb") as f:
                        f.write(body)

        assert (d / "REVISION").read_text().strip() == {installer_revision!r}
        assert (d / "task.py").is_file()

        sys.path.insert(0,str(d))
        import probe

        py = (
            probe.system_python(probe.managed_python())[0]
            if hasattr(probe, "managed_python")
            else "/usr/bin/python3"
        )
        r = subprocess.run(
            [py] + {arguments!r},input={json.dumps(spec)!r},stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,universal_newlines=True,
        )
        sys.stdout.write(r.stdout)
        sys.exit(r.returncode)
        """
    )
