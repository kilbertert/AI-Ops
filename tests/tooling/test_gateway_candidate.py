"""P0候选预演：制品边界和清理，不将fixture当目标业务容量。"""

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("candidate", ROOT / "ops/environments/gateway/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def bundle(tmp_path, *, extra=None, corrupt=False):
    revision = "a" * 40
    body = dict(
        schema=1, revision=revision, python="3.12", platform="linux-x86_64", version="1.0.0+" + revision[:12]
    )
    files = {
        "manifest.json": json.dumps(body).encode(),
        "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"wheel",
    }
    sums = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in files.items())
    files["SHA256SUMS"] = sums.encode()
    if corrupt:
        files["wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl"] = b"tampered"
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
        if extra:
            out.addfile(extra)
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest(), body


def test_exact_artifact_and_python_are_required(tmp_path):
    archive, sha, expected = bundle(tmp_path)
    assert probe.manifest(archive, sha, "3.12") == expected
    for digest, minor in [("0" * 64, "3.12"), (sha, "3.13")]:
        with pytest.raises(ValueError):
            probe.manifest(archive, digest, minor)
    archive, sha, _ = bundle(tmp_path, corrupt=True)
    with pytest.raises(ValueError, match="member mismatch"):
        probe.manifest(archive, sha, "3.12")


@pytest.mark.parametrize("kind", ["traversal", "symlink", "extra", "duplicate", "device"])
def test_unsafe_tar_refused_before_extract(tmp_path, kind):
    name = {"traversal": "../escape", "extra": "secret.env", "duplicate": "manifest.json"}.get(
        kind, "wheelhouse/x.whl"
    )
    member = tarfile.TarInfo(name)
    if kind == "symlink":
        member.type, member.linkname = tarfile.SYMTYPE, "/etc/shadow"
    if kind == "device":
        member.type = tarfile.CHRTYPE
    archive, sha, _ = bundle(tmp_path, extra=member)
    with pytest.raises(ValueError):
        probe.manifest(archive, sha, "3.12")
    assert not (tmp_path.parent / "escape").exists()


def test_install_failure_cleans_only_own_root(tmp_path, monkeypatch):
    archive, sha, meta = bundle(tmp_path)
    # Parent ownership is a separate host precondition, simulated here; content and cleanup are real.
    original_stat = Path.stat

    def owned(path, *args, **kwargs):
        data = original_stat(path, *args, **kwargs)
        return type(data)(
            (
                data.st_mode & ~0o022,
                data.st_ino,
                data.st_dev,
                data.st_nlink,
                0,
                data.st_gid,
                data.st_size,
                data.st_atime,
                data.st_mtime,
                data.st_ctime,
            )
        )

    monkeypatch.setattr(Path, "stat", owned)
    monkeypatch.setattr(
        probe.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=995, pw_gid=995, pw_shell="/usr/sbin/nologin")
    )
    monkeypatch.setattr(
        probe, "command", lambda *args: (_ for _ in ()).throw(subprocess.CalledProcessError(1, args))
    )
    sentinel = tmp_path / "company-data"
    sentinel.write_text("unchanged")
    args = SimpleNamespace(
        user="aiops", root=tmp_path, archive=archive, sha256=sha, python="python3", environment="verify"
    )
    with pytest.raises(subprocess.CalledProcessError):
        probe.run(args, meta)
    assert sentinel.read_text() == "unchanged"
    assert not list(tmp_path.glob("candidate-*"))


def test_reject_existing_symlink_archive(tmp_path):
    archive, sha, _ = bundle(tmp_path)
    link = tmp_path / "link.tar.gz"
    link.symlink_to(archive)
    with pytest.raises(ValueError, match="regular file"):
        probe.manifest(link, sha, "3.12")
