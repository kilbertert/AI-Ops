"""草稿版本＋受控引用构成冻结计划；文件校验不冒充主机/网络/业务验收。"""

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import zlib
from pathlib import Path

from aiops_diagnostics.common.private_files import validate_private_directory, validate_private_file
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_projection as projection
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.environment_artifacts import installer_manifest, manifest


class PlanRefused(ValueError):
    pass


def update_artifact_references(
    path, *, registry_path, identity, environment, expected_sha256, application, installer, configuration=None
):
    """CAS-update one target under the environment draft/plan lock; retain a rollback copy."""
    path, registry_path = Path(path).absolute(), Path(registry_path).absolute()
    with drafts._locked(registry_path):
        current = registry.load(registry_path)
        allowed = drafts.access(current, identity)
        if "*" not in allowed and environment not in allowed:
            raise PlanRefused("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
        all_drafts = drafts._rows(drafts._database(registry_path))
        draft = next((row for row in all_drafts if row["environment"] == environment), None)
        if draft is None:
            raise PlanRefused("ENVIRONMENT_DRAFT_UNAVAILABLE")
        draft_body = json.loads(draft["body"])
        target_id = draft_body["deployment_target"]
        if "*" not in allowed:
            shared_scopes = {
                row["environment"]
                for row in all_drafts
                if json.loads(row["body"])["deployment_target"] == target_id
            }
            if not shared_scopes <= allowed:
                raise PlanRefused("ENVIRONMENT_DEPLOYMENT_TARGET_SHARED")
        result = _update_artifact_references_locked(
            path,
            target_id,
            expected_sha256,
            application,
            installer,
            configuration=configuration,
            configuration_id=draft_body["credential_ref"],
            draft=draft_body,
            updated_at=current["audit"][-1]["at"],
        )
        return {**result, "environment": environment}


def _update_artifact_references_locked(
    path,
    environment,
    expected_sha256,
    application,
    installer,
    *,
    configuration,
    configuration_id,
    draft,
    updated_at,
):
    path = Path(path).absolute()
    asset_root = path.parent if path.parent.name == "onboarding-assets" else path.parent.parent
    if path.parent.name != "onboarding-assets" and asset_root.name != "onboarding-assets":
        raise PlanRefused("ENVIRONMENT_REFERENCES_INVALID")
    validate_private_directory(asset_root)
    if (
        not isinstance(environment, str)
        or not drafts.REFERENCE_PATTERN.fullmatch(environment)
        or path.is_symlink()
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise PlanRefused("ENVIRONMENT_REFERENCES_INVALID")
    validate_private_directory(path.parent)
    _private(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise PlanRefused("ENVIRONMENT_REFERENCES_CHANGED")
    backup = path.with_name(f"{path.name}.{expected_sha256}.bak")
    if backup.exists() or backup.is_symlink():
        raise PlanRefused("ENVIRONMENT_REFERENCES_BACKUP_EXISTS")
    if (
        not isinstance(application, dict)
        or not isinstance(installer, dict)
        or not isinstance(application.get("revision"), str)
        or application.get("revision") != installer.get("revision")
    ):
        raise PlanRefused("ENVIRONMENT_REFERENCES_REVISION_MISMATCH")
    body = json.loads(path.read_text(), object_pairs_hook=registry._unique_object)
    _references(path)
    target = next((row for row in body["targets"] if row["id"] == environment), None)
    if target is None:
        raise PlanRefused("ENVIRONMENT_REFERENCES_TARGET_MISSING")
    selected_configuration = (
        next((row for row in target["configurations"] if row["id"] == configuration_id), None)
        if configuration is not None
        else None
    )
    if configuration is not None and selected_configuration is None:
        raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_MISSING")
    configuration_source = None
    configuration_sha = None
    if configuration is not None:
        if not isinstance(configuration, dict) or set(configuration) != {"path", "sha256"}:
            raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_INVALID")
        configuration_source = _private(configuration["path"])
        configuration_sha = configuration["sha256"]
        if (
            not isinstance(configuration_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", configuration_sha)
            or configuration_source.stat().st_size > 1048576
            or _file_check(configuration, 1048576)[0] != configuration_sha
        ):
            raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_INVALID")
        try:
            projection.render(draft, configuration_source, configuration_sha, updated_at)
        except (OSError, ValueError, RuntimeError) as exc:
            raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_INVALID") from exc

    def checked_artifact(value, kind):
        if not isinstance(value, dict) or set(value) != {"path", "sha256", "revision"}:
            raise PlanRefused("ENVIRONMENT_REFERENCES_INVALID")
        source = _private(value["path"])
        sha = value["sha256"]
        revision = value["revision"]
        if (
            not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or _file_check(value, 512 * 1024**2)[0] != sha
        ):
            raise PlanRefused("ENVIRONMENT_REFERENCES_INVALID")
        actual = (
            manifest(source, sha, target["python"])["revision"]
            if kind == "application"
            else installer_manifest(source, sha)
        )
        if actual != revision:
            raise PlanRefused("ENVIRONMENT_REFERENCES_REVISION_MISMATCH")
        return {"source": source, "sha256": sha, "revision": revision, "kind": kind}

    def retain_artifact(value):
        source = value["source"]
        sha = value["sha256"]
        revision = value["revision"]
        kind = value["kind"]
        artifact_directory = asset_root / revision
        if path.parent.name != "onboarding-assets":
            artifact_directory = asset_root / f"runtime-install-{revision[:12]}"
        expected_name = (
            f"application-{target['python']}.tar.gz" if kind == "application" else "installer.tar.gz"
        )
        artifact_directory.mkdir(mode=0o700, exist_ok=True)
        validate_private_directory(artifact_directory)
        artifact_path = artifact_directory / expected_name
        if artifact_path.exists() or artifact_path.is_symlink():
            _private(artifact_path)
            if _file_check({"path": str(artifact_path), "sha256": sha}, 512 * 1024**2)[0] != sha:
                raise PlanRefused("ENVIRONMENT_REFERENCES_ARTIFACT_CONFLICT")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=expected_name + ".", suffix=".tmp", dir=artifact_directory
            )
            temporary_artifact = Path(temporary_name)
            try:
                with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                temporary_artifact.chmod(0o600)
                try:
                    os.link(temporary_artifact, artifact_path)
                    directory = os.open(artifact_directory, os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except FileExistsError:
                    _private(artifact_path)
                    if _file_check({"path": str(artifact_path), "sha256": sha}, 512 * 1024**2)[0] != sha:
                        raise PlanRefused("ENVIRONMENT_REFERENCES_ARTIFACT_CONFLICT") from None
            finally:
                temporary_artifact.unlink(missing_ok=True)
            _private(artifact_path)
        if _file_check({"path": str(artifact_path), "sha256": sha}, 512 * 1024**2)[0] != sha:
            raise PlanRefused("ENVIRONMENT_REFERENCES_ARTIFACT_CONFLICT")
        return dict(path=str(artifact_path), sha256=sha, revision=revision)

    checked_application = checked_artifact(application, "application")
    checked_installer = checked_artifact(installer, "installer")
    if checked_application["revision"] != checked_installer["revision"]:
        raise PlanRefused("ENVIRONMENT_REFERENCES_REVISION_MISMATCH")
    app = retain_artifact(checked_application)
    package = retain_artifact(checked_installer)
    retained_configuration = selected_configuration
    if configuration is not None:
        sha = configuration_sha
        asset_root = path.parent if path.parent.name == "onboarding-assets" else path.parent.parent
        config_directory = asset_root / "configurations"
        retained = config_directory / f"{sha}.env"
        if (
            selected_configuration["sha256"] == sha
            and Path(selected_configuration["path"]).absolute() == retained.absolute()
            and retained.is_file()
            and not retained.is_symlink()
            and _file_check({"path": str(retained), "sha256": sha}, 1048576)[1]
        ):
            retained_configuration = selected_configuration
        else:
            config_directory.mkdir(mode=0o700, exist_ok=True)
            validate_private_directory(config_directory)
            if retained.exists() or retained.is_symlink():
                _private(retained)
                if _file_check({"path": str(retained), "sha256": sha}, 1048576)[0] != sha:
                    raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_CONFLICT")
            else:
                descriptor, name = tempfile.mkstemp(prefix=sha + ".", suffix=".tmp", dir=config_directory)
                temporary = Path(name)
                try:
                    with configuration_source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                        shutil.copyfileobj(reader, writer)
                        writer.flush()
                        os.fsync(writer.fileno())
                    temporary.chmod(0o600)
                    if _file_check({"path": str(temporary), "sha256": sha}, 1048576)[0] != sha:
                        raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_CHANGED")
                    try:
                        os.link(temporary, retained)
                        descriptor = os.open(config_directory, os.O_DIRECTORY)
                        try:
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                    except FileExistsError:
                        _private(retained)
                        if _file_check({"path": str(retained), "sha256": sha}, 1048576)[0] != sha:
                            raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_CONFLICT") from None
                finally:
                    temporary.unlink(missing_ok=True)
                _private(retained)
            if _file_check({"path": str(retained), "sha256": sha}, 1048576)[0] != sha:
                raise PlanRefused("ENVIRONMENT_REFERENCES_CONFIGURATION_CHANGED")
            retained_configuration = {"id": configuration_id, "path": str(retained), "sha256": sha}
    desired_configurations = target["configurations"]
    if configuration is not None:
        desired_configurations = [
            retained_configuration if row["id"] == configuration_id else row
            for row in target["configurations"]
        ]
    if (
        target["application"] == app
        and target["installer"] == package
        and target["configurations"] == desired_configurations
    ):
        raise PlanRefused("ENVIRONMENT_REFERENCES_ALREADY_CURRENT")
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_sha256:
        raise PlanRefused("ENVIRONMENT_REFERENCES_CHANGED")
    target["application"], target["installer"] = app, package
    if configuration is not None:
        target["configurations"] = desired_configurations
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    backup_created = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        # Keep the rollback copy private and complete before the atomic replacement.
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        backup_created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        _private(backup)
        if path.read_bytes() != original:
            raise PlanRefused("ENVIRONMENT_REFERENCES_CHANGED")
        temporary.replace(path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        if path.read_bytes() == original:
            if backup_created:
                backup.unlink(missing_ok=True)
        elif path.read_bytes() == encoded.encode():
            descriptor, restore_name = tempfile.mkstemp(
                prefix=path.name + ".", suffix=".restore", dir=path.parent
            )
            restore = Path(restore_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                restore.chmod(0o600)
                restore.replace(path)
                directory = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                backup.unlink(missing_ok=True)
            finally:
                restore.unlink(missing_ok=True)
        raise
    return {
        "environment": environment,
        "revision": app["revision"],
        "application_sha256": app["sha256"],
        "installer_sha256": package["sha256"],
        "configuration_sha256": (retained_configuration["sha256"] if configuration is not None else None),
        "previous_references_sha256": expected_sha256,
        "references_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "configuration_ids": [item["id"] for item in target["configurations"]],
    }


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _private(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in path.parents):
        raise ValueError("invalid private path")
    validate_private_file(path)
    return path


def _references(path):
    if not path:
        raise ValueError("references missing")
    path = _private(path)
    if path.stat().st_size > 262144:
        raise ValueError("references too large")
    body = json.loads(path.read_text(), object_pairs_hook=registry._unique_object)
    if (
        not isinstance(body, dict)
        or set(body) != {"schema", "targets"}
        or body["schema"] != "aiops-environment-onboarding/1"
    ):
        raise ValueError("invalid references")
    rows = body["targets"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("invalid target count")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "id",
            "host",
            "user",
            "port",
            "python",
            "application",
            "installer",
            "configurations",
        }:
            raise ValueError("invalid target")
        for key in ("id", "host", "user"):
            if not isinstance(row[key], str) or not drafts.REFERENCE_PATTERN.fullmatch(row[key]):
                raise ValueError("invalid target identity")
        if (
            row["id"] in seen
            or type(row["port"]) is not int
            or not 1024 <= row["port"] <= 65535
            or row["python"] not in ("3.11", "3.12", "3.13")
        ):
            raise ValueError("invalid target deployment")
        seen.add(row["id"])
        for key in ("application", "installer"):
            value = row[key]
            if not isinstance(value, dict) or set(value) != {"path", "sha256", "revision"}:
                raise ValueError("invalid artifact reference")
            if not isinstance(value["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["revision"]):
                raise ValueError("invalid artifact revision")
        configs = row["configurations"]
        if not isinstance(configs, list) or not 1 <= len(configs) <= 64:
            raise ValueError("invalid configurations")
        ids = set()
        for value in configs:
            if (
                not isinstance(value, dict)
                or set(value) != {"id", "path", "sha256"}
                or not isinstance(value["id"], str)
                or not drafts.REFERENCE_PATTERN.fullmatch(value["id"])
                or value["id"] in ids
            ):
                raise ValueError("invalid configuration reference")
            ids.add(value["id"])
        for value in [row["application"], row["installer"], *configs]:
            if (
                not isinstance(value["path"], str)
                or not Path(value["path"]).is_absolute()
                or not isinstance(value["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            ):
                raise ValueError("invalid file reference")
    return rows


def _file_check(value, limit):
    path = _private(value["path"])
    if path.stat().st_size > limit:
        raise ValueError("file too large")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    return actual, actual == value["sha256"]


def _inputs(path, identity, environment, revision, registry_revision, reference_file):
    if not isinstance(environment, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,30}[a-z0-9]|[a-z0-9]", environment
    ):
        raise PlanRefused("ENVIRONMENT_PLAN_INVALID")
    effective = registry.load(path)
    allowed = drafts.access(effective, identity)
    if "*" not in allowed and environment not in allowed:
        raise PlanRefused("ENVIRONMENT_PLAN_UNAVAILABLE")
    row = next((r for r in drafts._rows(drafts._database(path)) if r["environment"] == environment), None)
    if row is None:
        raise PlanRefused("ENVIRONMENT_DRAFT_UNAVAILABLE")
    if (
        type(revision) is not int
        or row["revision"] != revision
        or type(registry_revision) is not int
        or (effective["revision"] != registry_revision or row["registry_revision"] != registry_revision)
    ):
        raise PlanRefused("ENVIRONMENT_PLAN_REVISION_CONFLICT")
    proposed = json.loads(row["body"])
    checks, reference, facts, observed, files = [], None, {}, {}, None
    try:
        targets = _references(reference_file)
        target = next(t for t in targets if t["id"] == proposed["deployment_target"])
        config = next(c for c in target["configurations"] if c["id"] == proposed["credential_ref"])
        reference = {**{k: v for k, v in target.items() if k != "configurations"}, "configuration": config}
        checks.append({"id": "references", "state": "passed"})
        for key, limit in (
            ("application", 512 * 1024**2),
            ("installer", 8 * 1024**2),
            ("configuration", 1048576),
        ):
            try:
                observed[key], matched = _file_check(reference[key], limit)
                checks.append({"id": key, "state": "passed" if matched else "blocked"})
            except (ValueError, OSError, RuntimeError) as exc:
                observed[key] = type(exc).__name__
                checks.append({"id": key, "state": "blocked"})
        same = target["application"]["revision"] == target["installer"]["revision"]
        checks.append({"id": "declared-revision", "state": "passed" if same else "blocked"})
        embedded = {}
        for key in ("application", "installer"):
            value = reference[key]
            try:
                if observed.get(key) != value["sha256"]:
                    raise ValueError("file integrity not confirmed")
                embedded[key] = (
                    manifest(Path(value["path"]), value["sha256"], target["python"])["revision"]
                    if key == "application"
                    else installer_manifest(Path(value["path"]), value["sha256"])
                )
                checks.append({"id": key + "-contents", "state": "passed"})
            except (
                ValueError,
                OSError,
                RuntimeError,
                tarfile.TarError,
                EOFError,
                zlib.error,
                TypeError,
                KeyError,
            ):
                checks.append({"id": key + "-contents", "state": "blocked"})
        matches = same and all(
            embedded.get(key) == reference[key]["revision"] for key in ("application", "installer")
        )
        checks.append({"id": "embedded-revision", "state": "passed" if matches else "blocked"})
        facts = {
            "artifact_sha256": target["application"]["sha256"],
            "source_revision": target["application"]["revision"],
            "config_sha256": config["sha256"],
        }
        try:
            files = projection.render(
                proposed, config["path"], config["sha256"], effective["audit"][-1]["at"]
            )
            facts.update(projection.summary(files))
            checks.append({"id": "config-projection", "state": "passed"})
        except (OSError, ValueError, RuntimeError):
            checks.append({"id": "config-projection", "state": "blocked"})
    except (ValueError, OSError, RuntimeError, StopIteration):
        checks.append({"id": "references", "state": "blocked"})
    # 现场验收由后继受限执行器实际取证，不接受表单传passed。
    if not any(check["id"] == "config-projection" for check in checks):
        checks.append({"id": "config-projection", "state": "pending"})
    checks += [
        {"id": key, "state": "pending"} for key in ("host-identity", "capacity", "network", "business")
    ]
    public = {
        "scope": "local-file-integrity-only",
        "environment": environment,
        "draft_revision": revision,
        "registry_revision": registry_revision,
        "checks": checks,
        **facts,
    }
    return (
        _digest({"draft": row["digest"], "input": public, "reference": reference, "observed": observed}),
        public,
        files,
    )


def preflight(path, identity, environment, revision, registry_revision, reference_file):
    # 复用目录锁，当前输入与持久化计划之间不允许草稿/目录更新。
    with drafts._locked(path):
        digest, inputs, _ = _inputs(path, identity, environment, revision, registry_revision, reference_file)
        db = drafts._database(path)
        now = time.time()
        with gateway_db.initialize(db) as conn:
            drafts.initialize_plans(conn)
            found = conn.execute(
                "SELECT * FROM environment_plans WHERE digest=? AND expires_at>? "
                "ORDER BY created_at DESC LIMIT 1",
                (digest, now),
            ).fetchone()
            if found is None:
                identifier = "plan_" + hashlib.sha256((digest + str(now)).encode()).hexdigest()[:32]
                conn.execute(
                    "INSERT INTO environment_plans VALUES(?,?,?,?,?,?)",
                    (identifier, environment, digest, json.dumps(inputs), now, now + 900),
                )
                found = conn.execute("SELECT * FROM environment_plans WHERE id=?", (identifier,)).fetchone()
        return _public(dict(found), current=True)


def read(path, identity, identifier, reference_file):
    if not isinstance(identifier, str) or not re.fullmatch(r"plan_[0-9a-f]{32}", identifier):
        raise PlanRefused("ENVIRONMENT_PLAN_UNAVAILABLE")
    with drafts._locked(path):
        # 权限先于查计划；不可借编号读取另一环境的部署摘要。
        effective = registry.load(path)
        allowed = drafts.access(effective, identity)
        permitted = {
            r["environment"]
            for r in drafts._rows(drafts._database(path))
            if "*" in allowed or r["environment"] in allowed
        }
        with gateway_db.connection(drafts._database(path), readonly=True) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_plans'").fetchone():
                raise PlanRefused("ENVIRONMENT_PLAN_UNAVAILABLE")
            row = conn.execute("SELECT * FROM environment_plans WHERE id=?", (identifier,)).fetchone()
        if row is None or row["environment"] not in permitted:
            raise PlanRefused("ENVIRONMENT_PLAN_UNAVAILABLE")
        row = dict(row)
        inputs = json.loads(row["inputs"])
        try:
            digest, _, _ = _inputs(
                path,
                identity,
                row["environment"],
                inputs["draft_revision"],
                inputs["registry_revision"],
                reference_file,
            )
            current = digest == row["digest"]
        except (PlanRefused, registry.RegistryError):
            current = False
        return _public(row, current=current)


def export(path, identity, identifier, reference_file, output):
    plan = read(path, identity, identifier, reference_file)
    if plan["state"] == "expired" or any(
        c["state"] != "passed"
        for c in plan["checks"]
        if c["id"] not in {"host-identity", "capacity", "network", "business"}
    ):
        raise PlanRefused("ENVIRONMENT_PROJECTION_UNAVAILABLE")
    output = Path(output)
    if not output.is_absolute() or any(p.is_symlink() for p in output.parents):
        raise PlanRefused("ENVIRONMENT_PROJECTION_OUTPUT_INVALID")
    validate_private_directory(output.parent)
    with drafts._locked(path):
        digest, _, files = _inputs(
            path,
            identity,
            plan["environment"],
            plan["draft_revision"],
            plan["registry_revision"],
            reference_file,
        )
        if digest != plan["digest"] or plan["expires_at"] <= time.time() or files is None:
            raise PlanRefused("ENVIRONMENT_PLAN_EXPIRED")
        files["manifest.json"] = (
            json.dumps(
                {
                    "schema": "aiops-environment-candidate/1",
                    "scope": "bootstrap-configuration-only",
                    "timezone": json.loads(
                        next(
                            r
                            for r in drafts._rows(drafts._database(path))
                            if r["environment"] == plan["environment"]
                        )["body"]
                    )["timezone"],
                    "plan_id": identifier,
                    "plan_digest": digest,
                    "draft_revision": plan["draft_revision"],
                    "registry_revision": plan["registry_revision"],
                    "executable": False,
                    **projection.summary(files),
                },
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        descriptor, name = tempfile.mkstemp(dir=output.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w+b") as stream:
                with (
                    gzip.GzipFile(fileobj=stream, filename="", mode="wb", mtime=0) as compressed,
                    tarfile.open(fileobj=compressed, mode="w") as bundle,
                ):
                    for filename, body in sorted(files.items()):
                        member = tarfile.TarInfo(filename)
                        member.size, member.mode = len(body), 0o600
                        bundle.addfile(member, io.BytesIO(body))
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                artifact = hashlib.file_digest(stream, "sha256").hexdigest()
            current, _, _ = _inputs(
                path,
                identity,
                plan["environment"],
                plan["draft_revision"],
                plan["registry_revision"],
                reference_file,
            )
            if current != digest or plan["expires_at"] <= time.time():
                raise PlanRefused("ENVIRONMENT_PLAN_EXPIRED")
            os.link(temporary, output)
            directory = os.open(output.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "id": identifier,
        "state": "candidate-exported",
        "executable": False,
        "archive_sha256": artifact,
        **projection.summary({k: v for k, v in files.items() if k != "manifest.json"}),
    }


def _public(row, *, current):
    return {
        "id": row["id"],
        "digest": row["digest"],
        **json.loads(row["inputs"]),
        "state": "blocked" if current and row["expires_at"] > time.time() else "expired",
        "executable": False,
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }
