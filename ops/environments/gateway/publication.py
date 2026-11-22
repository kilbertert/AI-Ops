"""正式配置和准入的安装器事务；目标普通入口最后开放。"""

import hashlib
import json
import os

import install


def invoke(inst, document, action):
    root = inst.state / "gateway/environment-publications"
    root.mkdir(mode=0o700, exist_ok=True)
    os.chown(root, inst.account.pw_uid, inst.account.pw_gid)
    file = root / (document["id"] + ".release.json")
    raw = json.dumps(document, sort_keys=True).encode()
    if file.exists():
        install.trusted(file, owner=inst.account.pw_uid, private=True)
        if file.read_bytes() != raw:
            raise ValueError("publication artifact changed")
    else:
        inst.config_snapshot_bytes(raw, file)
    approved = inst.root / "publication-tasks" / document["id"] / "document.json"
    if not approved.exists():
        install.write(approved, raw.decode())
    install.trusted(approved, private=True)
    if approved.read_bytes() != raw:
        raise ValueError("approved publication changed")
    with (inst.root / "installed.json").open("rb") as receipt, approved.open("rb") as approval:
        return json.loads(
            inst.command(
                "runuser",
                "-u",
                inst.user,
                "--",
                *inst.environment_args(inst.state),
                str(inst.root / "current/venv/bin/aiops-admin"),
                "environment-release",
                action,
                "--file",
                str(file),
                "--receipt-fd",
                str(receipt.fileno()),
                "--release-fd",
                str(approval.fileno()),
                pass_fds=(receipt.fileno(), approval.fileno()),
            )
        )


def record(inst, document, state):
    install.write(inst.root / "publication-tasks" / document["id"] / "operation.json", json.dumps(state))


def setup(inst, document, *, observe_only=False):
    import re
    import time

    if (
        document.get("schema") != "aiops-environment-release/1"
        or document["environment"] != inst.environment
        or not re.fullmatch(r"task_[a-f0-9]{32}", str(document.get("id")))
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(document.get("tenant_id")))
        or set(document.get("apps", {})) != {"operator", "consumer"}
    ):
        raise ValueError("publication environment mismatch")
    for app in document["apps"].values():
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(app.get("app_id"))):
            raise ValueError("publication application invalid")
    directory = inst.root / "publication-tasks" / document["id"]
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    if directory.exists():
        install.trusted(directory, private=True)
        install.trusted(directory / "operation.json", private=True)
        old = json.loads((directory / "operation.json").read_text())
        if old["digest"] != digest:
            raise ValueError("publication operation mismatch")
        return old
    if observe_only or document["expires_at"] <= time.time():
        raise ValueError("publication outcome absent or expired")
    installed = inst.read_record("installed.json")
    if (
        (inst.root / "pending.json").exists()
        or any(
            installed[k] != document["installed"][k]
            for k in ("artifact_sha256", "config_sha256", "registry_sha256", "runtime_sha256")
        )
        or installed["revision"] != document["installed"]["source_revision"]
        or installed["revision"] != inst.revision
    ):
        raise ValueError("publication installed baseline changed")
    inst.check_registry(installed)
    inst.check_runtime(installed)
    if install.digest(inst.config / "production.env") != installed["config_sha256"]:
        raise ValueError("publication configuration baseline changed")
    inst.check_database(inst.release(installed))
    body = json.loads((inst.config / "registry/environment.json").read_text())
    row = next(r for r in body["environments"] if r["id"] == inst.environment)
    tenant = next(t for t in row["tenants"] if t["id"] == document["tenant_id"])
    if tenant["enabled"]:
        raise ValueError("first publication requires disabled tenant")
    directory.mkdir(mode=0o700, parents=True)
    install.trusted(directory, private=True)
    install.write(directory / "previous-registry.json", json.dumps(body))
    state = {"digest": digest, "previous": installed, "phase": "prepared", "document": document}
    record(inst, document, state)
    return state


def file_state(inst, path):
    if path.is_symlink():
        raise ValueError("publication file symlink refused")
    if not path.exists():
        return None
    install.trusted(path, owner=inst.account.pw_uid, private=True)
    if path.stat().st_nlink != 1:
        raise ValueError("publication file hardlink refused")
    return path.read_text()


def configuration(raw, changes):
    # 输入沿原投影的JSON值env；保留其他行，不依赖root Python安装dotenv。
    lines = {}
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key in lines:
            raise ValueError("publication configuration is not canonical")
        lines[key] = value
    lines.update({key: json.dumps(value, ensure_ascii=False) for key, value in changes.items()})
    return "".join(key + "=" + value + "\n" for key, value in sorted(lines.items()))


def changes(inst, document):
    knowledge = inst.config / "knowledge/knowledge-delegation.json"
    existing = file_state(inst, knowledge)
    directory = json.loads((inst.config / "registry/environment.json").read_text())
    if existing is None and any(t["enabled"] for row in directory["environments"] for t in row["tenants"]):
        raise ValueError("active direct knowledge needs an explicit migration window")
    body = document["knowledge"]
    if existing:
        import base64

        current = json.loads(existing)
        if current["base_url"] != body["base_url"]:
            raise ValueError("knowledge origin changed")
        selected = document["apps"]["consumer"]["app_id"]

        def app(row):
            encoded = row["knowledge_id"][4:]
            return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))[3]

        body = {
            **body,
            "capabilities": [r for r in current["capabilities"] if app(r) != selected] + body["capabilities"],
        }
    key = inst.state / "keys" / (document["apps"]["consumer"]["app_id"] + ".key")
    previous_key = file_state(inst, key)
    if previous_key is not None and previous_key.strip() != document["apps"]["consumer"]["key"]:
        raise ValueError("existing app key differs")
    production = inst.config / "production.env"
    previous = file_state(inst, production)
    return [
        {
            "path": str(production),
            "before": previous,
            "after": configuration(
                previous,
                {
                    "AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED": "true",
                    "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": "",
                    "AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE": str(knowledge),
                },
            ),
        },
        {"path": str(knowledge), "before": existing, "after": json.dumps(body, sort_keys=True) + "\n"},
        {
            "path": str(key),
            "before": previous_key,
            "after": previous_key if previous_key is not None else document["apps"]["consumer"]["key"] + "\n",
        },
    ]


def check_files(inst, state):
    from pathlib import Path

    for change in state.get("changes", []):
        if file_state(inst, Path(change["path"])) not in (change["before"], change["after"]):
            raise ValueError("publication file drift")
    pending = inst.root / "pending.json"
    if pending.exists() and inst.read_record("pending.json") != {
        "mode": "publication",
        "operation": state["document"]["id"],
        "environment": inst.environment,
        "user": inst.user,
        "port": inst.port,
    }:
        raise ValueError("another installation is pending")
    installed = inst.read_record("installed.json")
    if installed not in [
        state["previous"],
        *[state[k] for k in ("staged_installed", "activated_installed", "restored_installed") if k in state],
    ]:
        raise ValueError("publication installed receipt drift")
    allowed = {state["previous"]["registry_sha256"]}
    for key in ("activated_registry", "restored_registry"):
        if key in state:
            allowed.add(hashlib.sha256(state[key].encode()).hexdigest())
    if inst.registry_digest() not in allowed:
        raise ValueError("publication registry drift")
    inst.check_runtime(installed)


def pending(inst, document):
    inst.record(
        "pending.json",
        {
            "mode": "publication",
            "operation": document["id"],
            "environment": inst.environment,
            "user": inst.user,
            "port": inst.port,
        },
    )


def stage(inst, document, *, observe_only=False):
    from pathlib import Path

    state = setup(inst, document, observe_only=observe_only)
    if state["phase"] != "prepared" or observe_only:
        return observe(inst, document)
    if document["expires_at"] <= __import__("time").time():
        raise ValueError("publication authorization expired")
    # 所有前驱/派生字节在首个副作用前持久；失败只能对账或明确恢复。
    state["changes"] = changes(inst, document)
    state["staged_installed"] = {
        **state["previous"],
        "config_sha256": hashlib.sha256(state["changes"][0]["after"].encode()).hexdigest(),
        "publication_operation": document["id"],
        "publication_phase": "staged",
        "knowledge_sha256": hashlib.sha256(state["changes"][1]["after"].encode()).hexdigest(),
    }
    check_files(inst, state)
    state["phase"] = "configuring"
    record(inst, document, state)
    pending(inst, document)
    needs_restart = state["changes"][0]["before"] != state["changes"][0]["after"]
    if needs_restart:
        inst.stop(inst.name)
        inst.check_database(inst.release(state["previous"]))
    for change in state["changes"]:
        path = Path(change["path"])
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700)
            os.chown(path.parent, inst.account.pw_uid, inst.account.pw_gid)
        if path.parent != inst.config:
            install.trusted(path.parent, owner=inst.account.pw_uid, private=True)
        if change["before"] != change["after"]:
            inst.config_snapshot_bytes(change["after"].encode(), path)
    inst.record("installed.json", state["staged_installed"])
    if needs_restart:
        inst.command("systemctl", "start", inst.name)
    inst.health(state["previous"]["version"])
    imported = invoke(inst, document, "prepare")
    state.update(phase="staged", versions=imported["versions"])
    record(inst, document, state)
    (inst.root / "pending.json").unlink()
    return observe(inst, document)


def observe(inst, document):
    state = setup(inst, document, observe_only=True)
    check_files(inst, state)
    current = inst.read_record("installed.json")
    complete = state["phase"] in {"staged", "activated", "restored"}
    if complete:
        from pathlib import Path

        wanted = "before" if state["phase"] == "restored" else "after"
        if any(
            file_state(inst, Path(change["path"])) != change[wanted] for change in state.get("changes", [])
        ):
            raise ValueError("publication configuration incomplete")
        inst.check_registry(current)
        if install.digest(inst.config / "production.env") != current["config_sha256"]:
            raise ValueError("publication configuration drift")
        if state["phase"] == "activated":
            actual = invoke(inst, document, "observe")
            if actual["phase"] != "published" or actual["routes"] != state["routes"]:
                raise ValueError("publication routes drift")
    return {
        "state": "observed" if complete else "reconcile",
        "phase": state["phase"],
        "activated": state["phase"] == "activated",
        "installed": current,
        "versions": state.get("versions", {}),
        "routes": state.get("routes", {}),
    }


def revised_registry(raw, document, *, enable):
    import time

    body = json.loads(raw)
    row = next(r for r in body["environments"] if r["id"] == document["environment"])
    next(t for t in row["tenants"] if t["id"] == document["tenant_id"])["enabled"] = enable
    row["enabled"] = row["enabled"] if not enable else True
    body["revision"] += 1
    body["audit"].append(
        {
            "revision": body["revision"],
            "actor": "publication-activation" if enable else "publication-recovery",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        }
    )
    return json.dumps(body, sort_keys=True) + "\n"


def refresh_verification(inst, document, proof):
    # 传输后、准入前由正在运行的Gateway重新核正常身份、原run和现行知识授权。
    raw = inst.command(
        "runuser",
        "-u",
        inst.user,
        "--",
        *inst.environment_args(inst.state),
        f"AIOPS_GATEWAY_PORT={inst.port}",
        str(inst.root / "current/venv/bin/aiops-admin"),
        "--config",
        str(inst.config / "production.env"),
        "environment-trial-dispatch",
        input=json.dumps(
            {
                "action": "publication-trial-complete",
                "operation": document["id"],
                "spec": proof["spec"],
                "trials": proof["trials"],
            }
        ),
    )
    refreshed = json.loads(raw)
    if any(refreshed[k] != proof[k] for k in ("operation", "run_id", "operator_runs", "scope_fingerprint")):
        raise ValueError("formal verification changed")
    return refreshed


def activate(inst, document, verification, *, observe_only=False):
    import time

    state = setup(inst, document, observe_only=True)
    if state["phase"] == "activated" or observe_only:
        return observe(inst, document)
    if (
        state["phase"] != "staged"
        or verification.get("state") != "passed"
        or verification.get("operation") != document["id"]
        or len(verification.get("operator_runs", [])) != 2
        or not 0 <= time.time() - verification.get("observed_at", 0) < 300
        or document["expires_at"] <= time.time()
    ):
        raise ValueError("formal configuration verification required")
    receipt = inst.state / "gateway/environment-publications" / (document["id"] + ".verification.json")
    install.trusted(receipt, owner=inst.account.pw_uid, private=True)
    if json.loads(receipt.read_text()) != verification:
        raise ValueError("verification receipt differs")
    refresh_verification(inst, document, verification)
    check_files(inst, state)
    state["activated_registry"] = revised_registry(
        (inst.config / "registry/environment.json").read_text(), document, enable=True
    )
    state["activated_installed"] = {
        **state["staged_installed"],
        "registry_sha256": hashlib.sha256(state["activated_registry"].encode()).hexdigest(),
        "publication_phase": "activated",
    }
    state["phase"] = "activating"
    record(inst, document, state)
    pending(inst, document)
    published = invoke(inst, document, "publish")
    state["routes"] = published["routes"]
    record(inst, document, state)
    inst.registry_snapshot_bytes(
        state["activated_registry"].encode(), inst.config / "registry/environment.json"
    )
    inst.record("installed.json", state["activated_installed"])
    state["phase"] = "activated"
    record(inst, document, state)
    (inst.root / "pending.json").unlink()
    return observe(inst, document)


def recover(inst, identifier):
    from pathlib import Path

    directory = inst.root / "publication-tasks" / identifier
    install.trusted(directory, private=True)
    install.trusted(directory / "operation.json", private=True)
    state = json.loads((directory / "operation.json").read_text())
    document = state["document"]
    if state["phase"] == "restored":
        return observe(inst, document)
    check_files(inst, state)
    if "restored_registry" not in state:
        raw = (inst.config / "registry/environment.json").read_text()
        previous = json.loads((directory / "previous-registry.json").read_text())
        original = next(r for r in previous["environments"] if r["id"] == inst.environment)
        body = json.loads(raw)
        row = next(r for r in body["environments"] if r["id"] == inst.environment)
        row["enabled"] = original["enabled"]
        state["restored_registry"] = revised_registry(json.dumps(body), document, enable=False)
        state["restored_installed"] = {
            **state["previous"],
            "registry_sha256": hashlib.sha256(state["restored_registry"].encode()).hexdigest(),
        }
    state["phase"] = "restoring"
    record(inst, document, state)
    pending(inst, document)
    # 先关闭本次准入，再恢复路由；另一作者改过的指针必须由原CAS拒绝。
    inst.registry_snapshot_bytes(
        state["restored_registry"].encode(), inst.config / "registry/environment.json"
    )
    local = inst.state / "gateway/environment-publications" / (identifier + ".json")
    if local.exists():
        restored = invoke(inst, document, "restore")
        if restored["phase"] != "restored":
            raise ValueError("publication route recovery unconfirmed")
    needs_restart = any(
        change["path"] == str(inst.config / "production.env")
        and file_state(inst, Path(change["path"])) != change["before"]
        for change in state.get("changes", [])
    )
    if needs_restart:
        inst.stop(inst.name)
        inst.check_database(inst.release(state["previous"]))
    for change in state.get("changes", []):
        path = Path(change["path"])
        if change["before"] is None:
            path.unlink(missing_ok=True)
        elif file_state(inst, path) != change["before"]:
            inst.config_snapshot_bytes(change["before"].encode(), path)
    inst.record("installed.json", state["restored_installed"])
    # 配置写入前的失败可能已停止自有服务；start是原systemd幂等动作。
    inst.command("systemctl", "start", inst.name)
    inst.health(state["previous"]["version"])
    state["phase"] = "restored"
    record(inst, document, state)
    (inst.root / "pending.json").unlink(missing_ok=True)
    return observe(inst, document)
