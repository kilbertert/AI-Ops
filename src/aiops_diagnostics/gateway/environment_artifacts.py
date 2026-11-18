"""P3应用包与安装器的只读成员校验；不执行归档代码、不证明Git合并来源。"""

import hashlib
import json
import re
import tarfile
from pathlib import PurePosixPath


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular file required")
    return path


def manifest(archive, digest, python_minor):
    regular(archive)
    if archive.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("archive too large")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("archive digest mismatch")
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if len(members) > 101 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError("artifact too large")
        files = {}
        for member in members:
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or str(name) != member.name
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
                or (member.isdir() and member.name != "wheelhouse")
            ):
                raise ValueError("unsafe archive member")
            if member.isfile():
                if member.name in files or member.size > 128 * 1024 * 1024:
                    raise ValueError("duplicate or oversized member")
                if member.name not in {"manifest.json", "SHA256SUMS"} and not (
                    name.parent == PurePosixPath("wheelhouse") and name.suffix == ".whl"
                ):
                    raise ValueError("unexpected artifact member")
                files[member.name] = member
        if not {"manifest.json", "SHA256SUMS"} <= files.keys() or len(files) > 100:
            raise ValueError("artifact file set invalid")
        checks = {}
        for line in bundle.extractfile(files["SHA256SUMS"]).read().decode().splitlines():
            sha, name = line.split("  ", 1)
            if not re.fullmatch(r"[a-f0-9]{64}", sha) or name in checks:
                raise ValueError("invalid digest manifest")
            checks[name] = sha
        if set(checks) != set(files) - {"SHA256SUMS"}:
            raise ValueError("artifact file set mismatch")
        for name, sha in checks.items():
            with bundle.extractfile(files[name]) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                    raise ValueError("artifact member mismatch")
        body = json.load(bundle.extractfile(files["manifest.json"]))
        if (
            not isinstance(body, dict)
            or set(body) != {"schema", "revision", "python", "platform", "version"}
            or type(body["schema"]) is not int
            or body["schema"] != 1
            or not all(isinstance(body[k], str) for k in ("revision", "python", "platform", "version"))
            or body["python"] != python_minor
            or body["platform"] != "linux-x86_64"
            or not re.fullmatch(r"[a-f0-9]{40}", body["revision"])
            or not re.fullmatch(r"\d+\.\d+\.\d+\+" + body["revision"][:12], body["version"])
            or sum(name.startswith("wheelhouse/aiops_diagnostics-") for name in files) != 1
        ):
            raise ValueError("incompatible artifact")
        return body


def installer_manifest(archive, digest):
    regular(archive)
    if archive.stat().st_size > 8 * 1024**2:
        raise ValueError("installer too large")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("installer digest mismatch")
    with tarfile.open(archive) as bundle:
        files = {}
        size = 0
        for index, member in enumerate(bundle):
            if index >= 7:
                raise ValueError("too many installer members")
            if member.name == "." and member.isdir():
                continue
            name = member.name.removeprefix("./")
            size += member.size
            if (
                not member.isfile()
                or name
                not in {
                    "REVISION",
                    "SHA256SUMS",
                    "install.py",
                    "runtime_check.py",
                    "probe.py",
                    "artifact_checks.py",
                }
                or name in files
                or size > 8 * 1024**2
            ):
                raise ValueError("invalid installer member")
            files[name] = bundle.extractfile(member).read()
        required = {"REVISION", "SHA256SUMS", "install.py", "runtime_check.py", "probe.py"}
        if not required <= files.keys():
            raise ValueError("installer files missing")
        checks = {}
        for line in files["SHA256SUMS"].decode().splitlines():
            sha, name = line.split("  ", 1)
            if not re.fullmatch(r"[a-f0-9]{64}", sha) or name in checks:
                raise ValueError("invalid installer checksums")
            checks[name] = sha
        if set(checks) != files.keys() - {"SHA256SUMS"} or any(
            hashlib.sha256(files[name]).hexdigest() != sha for name, sha in checks.items()
        ):
            raise ValueError("installer member mismatch")
        revision = files["REVISION"].decode().strip()
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("invalid installer revision")
        return revision
