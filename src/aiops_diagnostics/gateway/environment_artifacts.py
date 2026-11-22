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
            if index >= 8:
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
                    "task.py",
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


def candidate_files(archive, digest, environment):
    """读取三文件停用候选包；外层摘要由受管调用者明确提供。"""
    import io
    import zlib
    from zoneinfo import ZoneInfo

    regular(archive)
    with archive.open("rb") as stream:
        raw = stream.read(10 * 1024**2 + 1)
    if len(raw) > 10 * 1024**2 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("candidate archive digest mismatch")
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as bundle:
            for index, member in enumerate(bundle):
                if (
                    index >= 3
                    or member.name not in {"production.env", "environment.json", "manifest.json"}
                    or not member.isfile()
                    or member.name in files
                    or member.size > (8 * 1024**2 if member.name == "environment.json" else 1048576)
                ):
                    raise ValueError("invalid candidate member")
                files[member.name] = bundle.extractfile(member).read()
        if set(files) != {"production.env", "environment.json", "manifest.json"}:
            raise ValueError("candidate files missing")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate candidate field")
                result[key] = value
            return result

        body = json.loads(files["manifest.json"], object_pairs_hook=unique)
        if (
            set(body)
            != {
                "schema",
                "scope",
                "timezone",
                "plan_id",
                "plan_digest",
                "draft_revision",
                "registry_revision",
                "executable",
                "projection_sha256",
                "projected_config_sha256",
                "projected_registry_sha256",
            }
            or body["schema"] != "aiops-environment-candidate/1"
            or body["scope"] != "bootstrap-configuration-only"
            or body["executable"] is not False
            or not re.fullmatch(r"plan_[0-9a-f]{32}", body["plan_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", body["plan_digest"])
            or any(type(body[k]) is not int or body[k] < 1 for k in ("draft_revision", "registry_revision"))
        ):
            raise ValueError("invalid candidate manifest")
        ZoneInfo(body["timezone"])
        checks = {
            name: hashlib.sha256(files[name]).hexdigest() for name in ("production.env", "environment.json")
        }
        if (
            checks["production.env"] != body["projected_config_sha256"]
            or checks["environment.json"] != body["projected_registry_sha256"]
            or hashlib.sha256(json.dumps(checks, sort_keys=True).encode()).hexdigest()
            != body["projection_sha256"]
        ):
            raise ValueError("candidate member digest mismatch")
        directory = json.loads(files["environment.json"], object_pairs_hook=unique)
        rows = directory["environments"]
        if (
            len(rows) != 1
            or rows[0]["id"] != environment
            or rows[0]["enabled"] is not False
            or directory["managers"]
            or any(t["enabled"] is not False for t in rows[0]["tenants"])
        ):
            raise ValueError("candidate environment mismatch or enabled")
        return files, body
    except (tarfile.TarError, EOFError, zlib.error, KeyError, TypeError) as exc:
        raise ValueError("invalid candidate archive") from exc


def runtime_files(archive, digest, environment):
    """读取D30固定运行包；不执行内容，语义由候选venv继续验证。"""
    regular(archive)
    if archive.stat().st_size > 8 * 1024**2:
        raise ValueError("runtime archive too large")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("runtime archive digest mismatch")
    required = {
        "agent-runs.json",
        "operator-manifest.json",
        "operator-profiles.json",
        "consumer-targets.json",
        "registrations.json",
        "operator-model.json",
        "consumer-model.json",
    }
    files = {}
    with tarfile.open(archive) as bundle:
        for index, member in enumerate(bundle):
            if (
                index >= 8
                or member.name not in required | {"manifest.json"}
                or not member.isfile()
                or member.name in files
                or member.size > 1048576
            ):
                raise ValueError("invalid runtime member")
            files[member.name] = bundle.extractfile(member).read()
    if set(files) != required | {"manifest.json"}:
        raise ValueError("runtime files missing")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate runtime field")
            result[key] = value
        return result

    for raw in files.values():
        if not isinstance(json.loads(raw, object_pairs_hook=unique), dict):
            raise ValueError("runtime JSON object required")
    body = json.loads(files["manifest.json"])
    if (
        not isinstance(body, dict)
        or body.get("schema") not in {"aiops-environment-runtime/1", "aiops-environment-runtime/2"}
        or body.get("scope") != "runtime-wiring-only"
        or body.get("environment") != environment
        or any(body.get(k) is not False for k in ("executable", "installed", "activated"))
        or not isinstance(body.get("tenant_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body["tenant_id"])
        or not re.fullmatch(r"plan_[0-9a-f]{32}", str(body.get("plan_id")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(body.get("plan_digest")))
        or body.get("files") != {n: hashlib.sha256(files[n]).hexdigest() for n in required}
    ):
        raise ValueError("invalid runtime manifest or member digest")
    if body["schema"] == "aiops-environment-runtime/2" and any(
        not isinstance(body.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", body[key])
        for key in ("source_runtime_sha256", "previous_runtime_sha256")
    ):
        raise ValueError("runtime composition provenance missing")
    return files, body


def compose_runtime(previous, previous_sha, incoming, incoming_sha, environment):
    """只累积同环境新租户的运行目标；共享执行资源不得顺带变化。"""
    import io

    before, previous_manifest = runtime_files(previous, previous_sha, environment)
    after, manifest = runtime_files(incoming, incoming_sha, environment)
    if manifest["schema"] != "aiops-environment-runtime/1":
        raise ValueError("incoming runtime must be original candidate")
    shared = set(after) - {"manifest.json", "registrations.json", "consumer-targets.json"}
    if any(before[name] != after[name] for name in shared):
        raise ValueError("shared runtime change requires separate configuration window")
    existing = json.loads(before["consumer-targets.json"])["targets"]
    proposed = json.loads(after["consumer-targets.json"])["targets"]
    if len(proposed) != 1:
        raise ValueError("one candidate tenant required")
    candidate = proposed[0]
    if candidate.get("tenant_id") != manifest["tenant_id"] or candidate.get("environment") != environment:
        raise ValueError("candidate target scope mismatch")
    same = [row for row in existing if row.get("tenant_id") == candidate["tenant_id"]]
    if same and same != [candidate]:
        raise ValueError("existing tenant runtime cannot be replaced by candidate")
    if any(
        row.get("app_id") == candidate["app_id"] and row.get("tenant_id") != candidate["tenant_id"]
        for row in existing
    ):
        raise ValueError("runtime application already owned")
    if before == after or (
        previous_manifest.get("source_runtime_sha256") == incoming_sha
        and before["registrations.json"] == after["registrations.json"]
        and same == [candidate]
    ):
        return previous.read_bytes(), previous_sha
    if not same:
        existing.append(candidate)
    after["consumer-targets.json"] = (
        json.dumps({"schema": "aiops-consumer-targets/1", "targets": existing}, sort_keys=True).encode()
        + b"\n"
    )
    manifest.update(
        schema="aiops-environment-runtime/2",
        source_runtime_sha256=incoming_sha,
        previous_runtime_sha256=previous_sha,
        files={
            name: hashlib.sha256(raw).hexdigest() for name, raw in after.items() if name != "manifest.json"
        },
    )
    after["manifest.json"] = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, raw in sorted(after.items()):
            member = tarfile.TarInfo(name)
            member.mode, member.size = 0o600, len(raw)
            bundle.addfile(member, io.BytesIO(raw))
    raw = output.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()
