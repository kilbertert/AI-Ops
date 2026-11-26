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
    kb_path = inst.config / "knowledge/knowledge-delegation.json"
    if installed.get("knowledge_sha256"):
        if not kb_path.exists() or install.digest(kb_path) != installed["knowledge_sha256"]:
            raise ValueError("publication knowledge baseline changed")
    elif kb_path.exists():
        raise ValueError("publication knowledge baseline changed")
    inst.check_database(inst.release(installed))
    body = json.loads((inst.config / "registry/environment.json").read_text())
    row = next(r for r in body["environments"] if r["id"] == inst.environment)
    tenant = next(t for t in row["tenants"] if t["id"] == document["tenant_id"])
    if tenant["enabled"]:
        raise ValueError("first publication requires disabled tenant")
    if directory.parent.is_symlink():
        raise ValueError("untrusted publication task root")
    if directory.parent.exists():
        install.trusted(directory.parent)
        os.chmod(directory.parent, 0o700)
    else:
        directory.parent.mkdir(mode=0o700)
    install.trusted(directory.parent, private=True)
    directory.mkdir(mode=0o700)
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


def _capability_app_id(row):
    import base64

    if not isinstance(row, dict) or not isinstance(row.get("knowledge_id"), str):
        raise ValueError("publication knowledge delegation invalid")
    kid = row["knowledge_id"]
    if not kid.startswith("kdx_"):
        raise ValueError("publication knowledge delegation invalid")
    encoded = kid[4:]
    try:
        scope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError) as exc:
        raise ValueError("publication knowledge delegation invalid") from exc
    if not isinstance(scope, list) or len(scope) < 4 or not isinstance(scope[3], str) or not scope[3]:
        raise ValueError("publication knowledge delegation invalid")
    return scope[3]


def _baseline_path(inst):
    # 独立置于 publication-tasks/ 之外（与 installed.json 同级），避免原子写入中途被杀遗留的临时文件
    # 污染任务目录扫描（Devin Review BUG_0004）。
    return inst.root / "publication-delegation-baseline.json"


def _read_baseline(inst):
    path = _baseline_path(inst)
    if path.is_symlink():
        raise ValueError("untrusted publication baseline")
    if not path.exists():
        return None
    install.trusted(path, private=True)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("untrusted publication baseline")
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("environment") != inst.environment
        or not isinstance(data.get("production_env"), str)
        or not isinstance(data.get("delegated_env"), str)
    ):
        raise ValueError("untrusted publication baseline")
    return data


def _record_baseline(inst, before_prod, after_prod):
    if before_prod == after_prod:
        return
    install.write(
        _baseline_path(inst),
        json.dumps(
            {
                "environment": inst.environment,
                "production_env": before_prod,
                "production_sha256": hashlib.sha256(before_prod.encode()).hexdigest(),
                "delegated_env": after_prod,
                "delegated_sha256": hashlib.sha256(after_prod.encode()).hexdigest(),
            },
            sort_keys=True,
        ),
    )


def _trusted_sibling_operations(inst, exclude_id):
    import re

    root = inst.root / "publication-tasks"
    if root.is_symlink():
        raise ValueError("untrusted publication task root")
    if not root.exists():
        return []
    install.trusted(root, private=True)
    siblings = []
    for entry in sorted(root.iterdir()):
        if not re.fullmatch(r"task_[a-f0-9]{32}", entry.name) or entry.is_symlink() or not entry.is_dir():
            raise ValueError("untrusted publication task entry")
        if entry.name == exclude_id:
            continue
        install.trusted(entry, private=True)
        op_file = entry / "operation.json"
        if op_file.is_symlink():
            raise ValueError("untrusted publication task entry")
        if not op_file.exists():
            continue
        if not op_file.is_file() or op_file.stat().st_nlink != 1:
            raise ValueError("untrusted publication task entry")
        install.trusted(op_file, private=True)
        other = json.loads(op_file.read_text())
        if (
            not isinstance(other, dict)
            or not isinstance(other.get("document"), dict)
            or other["document"].get("id") != entry.name
        ):
            raise ValueError("untrusted publication task entry")
        if other["document"].get("environment") != inst.environment:
            continue
        siblings.append(other)
    return siblings


def _peer_capabilities_by_app(siblings):
    """按相位优先级汇总受信任同级任务的能力（高相位覆盖低相位；同相位保留全集，无字典序依赖）。"""
    phase_rank = {"configuring": 1, "staged": 2, "activating": 3, "restoring": 3, "activated": 4}
    peers = {}
    ranks = {}
    for other in siblings:
        rank = phase_rank.get(other.get("phase"), 0)
        if not rank:
            continue
        doc = other.get("document") or {}
        app_id = ((doc.get("apps") or {}).get("consumer") or {}).get("app_id")
        caps = (doc.get("knowledge") or {}).get("capabilities")
        if not (isinstance(app_id, str) and app_id and isinstance(caps, list)):
            continue
        prev = ranks.get(app_id, 0)
        if rank > prev:
            peers[app_id] = [caps]
            ranks[app_id] = rank
        elif rank == prev and caps not in peers[app_id]:
            peers[app_id].append(caps)
    return peers


def _enabled_peer_app_ids(inst, siblings, my_tenant_id):
    """返回当前实时目录中仍 enabled=true 的对端租户对应的 activated consumer app_id 集合。"""
    reg_body = json.loads((inst.config / "registry/environment.json").read_text())
    row = next(r for r in reg_body["environments"] if r["id"] == inst.environment)
    enabled_tenants = {t["id"] for t in row["tenants"] if t["enabled"] and t["id"] != my_tenant_id}
    required = set()
    for other in siblings:
        if other.get("phase") != "activated":
            continue
        doc = other.get("document") or {}
        if doc.get("tenant_id") in enabled_tenants:
            app_id = ((doc.get("apps") or {}).get("consumer") or {}).get("app_id")
            if isinstance(app_id, str) and app_id:
                required.add(app_id)
    return required


def _knowledge_file_allowed(inst, state, siblings, *, require_self=None, require_enabled_peers=True):
    kb_path = inst.config / "knowledge/knowledge-delegation.json"
    kb_change = next((c for c in state.get("changes", []) if c["path"] == str(kb_path)), None)
    if kb_change is None:
        return True
    raw = file_state(inst, kb_path)
    doc = state["document"]
    required_peers = (
        _enabled_peer_app_ids(inst, siblings, doc["tenant_id"]) if require_enabled_peers else set()
    )
    if raw is None:
        return require_self is not True and not required_peers
    try:
        body = json.loads(raw)
    except ValueError:
        return False
    expected_knowledge = doc["knowledge"]
    if (
        not isinstance(body, dict)
        or set(body) != set(expected_knowledge)
        or body.get("schema") != expected_knowledge.get("schema")
        or body.get("base_url") != expected_knowledge["base_url"]
        or not isinstance(body.get("capabilities"), list)
        or not body["capabilities"]
    ):
        return False
    my_app_id = doc["apps"]["consumer"]["app_id"]
    my_caps = expected_knowledge["capabilities"]
    peers = _peer_capabilities_by_app(siblings)
    grouped = {}
    for row in body["capabilities"]:
        try:
            app_id = _capability_app_id(row)
        except ValueError:
            return False
        grouped.setdefault(app_id, []).append(row)
    for app_id, rows in grouped.items():
        if app_id == my_app_id:
            if require_self is False:
                return False
            if rows != my_caps and not (require_self is None and rows in peers.get(app_id, [])):
                return False
        else:
            if rows not in peers.get(app_id, []):
                return False
    if require_self is True and my_app_id not in grouped:
        return False
    return required_peers.issubset(grouped)


def changes(inst, document):
    knowledge = inst.config / "knowledge/knowledge-delegation.json"
    existing = file_state(inst, knowledge)
    directory = json.loads((inst.config / "registry/environment.json").read_text())
    if existing is None and any(t["enabled"] for row in directory["environments"] for t in row["tenants"]):
        raise ValueError("active direct knowledge needs an explicit migration window")
    body = document["knowledge"]
    if existing:
        current = json.loads(existing)
        if current["base_url"] != body["base_url"]:
            raise ValueError("knowledge origin changed")
        selected = document["apps"]["consumer"]["app_id"]
        body = {
            **body,
            "capabilities": [r for r in current["capabilities"] if _capability_app_id(r) != selected]
            + body["capabilities"],
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

    siblings = _trusted_sibling_operations(inst, state["document"]["id"])
    baseline = _read_baseline(inst)
    prod_path = str(inst.config / "production.env")
    kb_path = str(inst.config / "knowledge/knowledge-delegation.json")
    restored_by_path = {c["path"]: c["after"] for c in state.get("restored_changes", [])}
    pending = inst.root / "pending.json"
    is_pending = pending.exists()
    if is_pending and inst.read_record("pending.json") != {
        "mode": "publication",
        "operation": state["document"]["id"],
        "environment": inst.environment,
        "user": inst.user,
        "port": inst.port,
    }:
        raise ValueError("another installation is pending")
    installed = inst.read_record("installed.json")
    if (
        installed.get("environment") != inst.environment
        or installed.get("user") != inst.user
        or installed.get("port") != inst.port
    ):
        raise ValueError("publication installed receipt drift")
    if is_pending:
        # 本操作在飞中断：仅允许本操作自身的过渡快照与注册表摘要，绝不接受外部修改。
        own_records = [
            state[k]
            for k in (
                "previous",
                "staged_installed",
                "activate_from_installed",
                "activated_installed",
                "recover_from_installed",
                "restored_installed",
            )
            if k in state
        ]
        if installed not in own_records:
            raise ValueError("publication installed receipt drift")
        own_registries = {r["registry_sha256"] for r in own_records if "registry_sha256" in r}
        for key in ("activated_registry", "restored_registry"):
            if key in state:
                own_registries.add(hashlib.sha256(state[key].encode()).hexdigest())
        if inst.registry_digest() not in own_registries:
            raise ValueError("publication registry drift")
    else:
        # 稳态核验：磁盘 production.env、environment.json、knowledge-delegation.json 必须与当前
        # 受信任 installed.json 摘要严格一致，堵死第三方回滚历史快照或删减对端能力绕过。
        inst.check_registry(installed)
        if install.digest(inst.config / "production.env") != installed["config_sha256"]:
            raise ValueError("publication file drift")
        kb_file = inst.config / "knowledge/knowledge-delegation.json"
        if installed.get("knowledge_sha256"):
            if not kb_file.exists() or install.digest(kb_file) != installed["knowledge_sha256"]:
                raise ValueError("publication file drift")
        elif kb_file.exists():
            raise ValueError("publication file drift")
    for change in state.get("changes", []):
        if change["path"] == kb_path:
            if not _knowledge_file_allowed(inst, state, siblings, require_enabled_peers=not is_pending):
                raise ValueError("publication file drift")
            continue
        allowed_values = {change["before"], change["after"]}
        if change["path"] in restored_by_path:
            allowed_values.add(restored_by_path[change["path"]])
        if change["path"] == prod_path and baseline is not None:
            allowed_values.update({baseline["production_env"], baseline["delegated_env"]})
        if file_state(inst, Path(change["path"])) not in allowed_values:
            raise ValueError("publication file drift")
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
    _record_baseline(inst, state["changes"][0]["before"], state["changes"][0]["after"])
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
    state.update(phase="staged", versions=imported["versions"], staged_at=__import__("time").time())
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

        siblings = _trusted_sibling_operations(inst, document["id"])
        prod_path = str(inst.config / "production.env")
        kb_path = str(inst.config / "knowledge/knowledge-delegation.json")
        require_self = state["phase"] in {"staged", "activated"}
        if not _knowledge_file_allowed(inst, state, siblings, require_self=require_self):
            raise ValueError("publication configuration incomplete")
        if state["phase"] == "restored":
            # 恢复终态仅断言本租户已停用、本租户密钥已还原、本租户知识能力已移除；
            # 不锁死后续其他租户再退回 production.env 基线后的字节。
            reg_body = json.loads((inst.config / "registry/environment.json").read_text())
            env_row = next(r for r in reg_body["environments"] if r["id"] == inst.environment)
            if any(t["id"] == document["tenant_id"] and t["enabled"] for t in env_row["tenants"]):
                raise ValueError("publication configuration incomplete")
            for change in state.get("changes", []):
                if change["path"] in {kb_path, prod_path}:
                    continue
                if file_state(inst, Path(change["path"])) != change["before"]:
                    raise ValueError("publication configuration incomplete")
        else:
            for change in state.get("changes", []):
                if change["path"] == kb_path:
                    continue
                if file_state(inst, Path(change["path"])) != change["after"]:
                    raise ValueError("publication configuration incomplete")
        inst.check_registry(current)
        if install.digest(inst.config / "production.env") != current["config_sha256"]:
            raise ValueError("publication configuration drift")
        if current.get("knowledge_sha256") and (
            install.digest(inst.config / "knowledge/knowledge-delegation.json") != current["knowledge_sha256"]
        ):
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
    if refreshed.get("state") != "passed" or any(
        refreshed.get(k) != proof[k] for k in ("operation", "run_id", "operator_runs", "scope_fingerprint")
    ):
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
        or document["expires_at"] <= time.time()
    ):
        raise ValueError("formal configuration verification required")
    receipt = inst.state / "gateway/environment-publications" / (document["id"] + ".verification.json")
    install.trusted(receipt, owner=inst.account.pw_uid, private=True)

    # receipt核同一份证据身份而非字节：observed_at随每次复验刷新，proof携带的
    # 旧确认时刻不构成另一份proof；证据面任一项不同才是receipt differs。
    def evidence(proof):
        return {k: v for k, v in proof.items() if k != "observed_at"}

    if evidence(json.loads(receipt.read_text())) != evidence(verification):
        raise ValueError("verification receipt differs")
    # 准入新鲜度只信本次重核：refresh在运行Gateway上按持久锚点重核身份/案例/
    # 原run/配置/知识并回写新确认时刻；过期proof由其复验结果替换而非照收。
    verification = refresh_verification(inst, document, verification)
    if (
        verification.get("state") != "passed"
        or not 0 <= time.time() - verification.get("observed_at", 0) < 300
    ):
        raise ValueError("formal configuration verification required")
    check_files(inst, state)
    siblings = _trusted_sibling_operations(inst, document["id"])
    if not _knowledge_file_allowed(inst, state, siblings, require_self=True):
        raise ValueError("publication configuration drift")
    live_installed = inst.read_record("installed.json")
    state["activate_from_installed"] = live_installed
    state["activated_registry"] = revised_registry(
        (inst.config / "registry/environment.json").read_text(), document, enable=True
    )
    state["activated_installed"] = {
        **live_installed,
        "registry_sha256": hashlib.sha256(state["activated_registry"].encode()).hexdigest(),
        "knowledge_sha256": install.digest(inst.config / "knowledge/knowledge-delegation.json"),
        "publication_operation": document["id"],
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


def _has_delegation_file_setting(raw):
    for line in (raw or "").splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key == "AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE":
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = value.strip().strip('"').strip("'")
            return bool(parsed)
    return False


def recover(inst, identifier):
    from pathlib import Path

    directory = inst.root / "publication-tasks" / identifier
    install.trusted(directory, private=True)
    install.trusted(directory / "operation.json", private=True)
    state = json.loads((directory / "operation.json").read_text())
    document = state["document"]
    if state["phase"] == "restored":
        return observe(inst, document)
    siblings = _trusted_sibling_operations(inst, document["id"])
    my_staged_at = state.get("staged_at", 0)
    if any(
        other["document"].get("tenant_id") == document["tenant_id"]
        and (
            other.get("phase") in {"activated", "activating"}
            or (other.get("phase") == "staged" and other.get("staged_at", 0) > my_staged_at)
        )
        for other in siblings
    ):
        raise ValueError("superseded by active publication for same tenant")
    check_files(inst, state)
    if "restored_registry" not in state:
        raw = (inst.config / "registry/environment.json").read_text()
        previous = json.loads((directory / "previous-registry.json").read_text())
        original = next(r for r in previous["environments"] if r["id"] == inst.environment)
        body = json.loads(raw)
        row = next(r for r in body["environments"] if r["id"] == inst.environment)
        other_enabled = any(t["enabled"] for t in row["tenants"] if t["id"] != document["tenant_id"])
        any_prior_enabled = any(t["enabled"] for t in original["tenants"])
        row["enabled"] = True if other_enabled else (original["enabled"] if not any_prior_enabled else False)
        state["restored_registry"] = revised_registry(json.dumps(body), document, enable=False)

        current_installed = inst.read_record("installed.json")
        state["recover_from_installed"] = current_installed
        prod_path = inst.config / "production.env"
        kb_path = inst.config / "knowledge/knowledge-delegation.json"
        key_path = inst.state / "keys" / (document["apps"]["consumer"]["app_id"] + ".key")
        my_app_id = document["apps"]["consumer"]["app_id"]
        changes_list = state.get("changes", [])
        prod_change = next((c for c in changes_list if c["path"] == str(prod_path)), None)
        key_change = next((c for c in changes_list if c["path"] == str(key_path)), None)

        current_kb_raw = file_state(inst, kb_path)
        target_kb = None
        if current_kb_raw is not None:
            current_kb = json.loads(current_kb_raw)
            remaining_caps = [r for r in current_kb["capabilities"] if _capability_app_id(r) != my_app_id]
            if remaining_caps:
                target_kb = json.dumps({**current_kb, "capabilities": remaining_caps}, sort_keys=True) + "\n"
        if other_enabled and target_kb is None:
            raise ValueError("peer tenant delegated knowledge missing")

        current_prod = file_state(inst, prod_path)
        baseline = _read_baseline(inst)
        if target_kb is not None:
            target_prod = current_prod
        elif not other_enabled:
            # 全部租户离开知识委派时必须回退到无悬空 KNOWLEDGE_DELEGATION_FILE 的基线，
            # 避免 Gateway 重启或在后续 candidate-install 阶段加载已删除文件崩溃（S1 / NEW-M1）。
            if baseline is not None and current_prod == baseline["delegated_env"]:
                target_prod = baseline["production_env"]
            elif prod_change is not None and prod_change["before"] != prod_change["after"]:
                target_prod = prod_change["before"]
            else:
                sibling_before = next(
                    (
                        c["before"]
                        for other in siblings
                        for c in other.get("changes", [])
                        if c.get("path") == str(prod_path)
                        and c.get("before") != c.get("after")
                        and c.get("after") == current_prod
                    ),
                    None,
                )
                target_prod = sibling_before if sibling_before is not None else current_prod
            if _has_delegation_file_setting(target_prod):
                raise ValueError("delegation baseline missing for final restore")
        else:
            target_prod = current_prod

        restored_changes = []
        if prod_change is not None:
            restored_changes.append({"path": str(prod_path), "after": target_prod})
        if any(c["path"] == str(kb_path) for c in changes_list):
            restored_changes.append({"path": str(kb_path), "after": target_kb})
        if key_change is not None:
            restored_changes.append({"path": str(key_path), "after": key_change["before"]})
        state["restored_changes"] = restored_changes

        base_installed = {
            k: v
            for k, v in current_installed.items()
            if k not in {"publication_operation", "publication_phase", "knowledge_sha256"}
        }
        base_installed["config_sha256"] = hashlib.sha256(target_prod.encode()).hexdigest()
        base_installed["registry_sha256"] = hashlib.sha256(state["restored_registry"].encode()).hexdigest()
        if target_kb is not None:
            base_installed["knowledge_sha256"] = hashlib.sha256(target_kb.encode()).hexdigest()
            base_installed["publication_operation"] = document["id"]
            base_installed["publication_phase"] = "restored"
        state["restored_installed"] = base_installed
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
    target_changes = state.get("restored_changes")
    if target_changes is None:
        target_changes = [{"path": c["path"], "after": c["before"]} for c in state.get("changes", [])]
    needs_restart = any(
        change["path"] == str(inst.config / "production.env")
        and file_state(inst, Path(change["path"])) != change["after"]
        for change in target_changes
    )
    if needs_restart:
        inst.stop(inst.name)
        inst.check_database(inst.release(state["restored_installed"]))
    for change in target_changes:
        path = Path(change["path"])
        if change["after"] is None:
            path.unlink(missing_ok=True)
        elif file_state(inst, path) != change["after"]:
            inst.config_snapshot_bytes(change["after"].encode(), path)
    inst.record("installed.json", state["restored_installed"])
    # 配置写入前的失败可能已停止自有服务；start是原systemd幂等动作。
    inst.command("systemctl", "start", inst.name)
    inst.health(state["restored_installed"]["version"])
    state["phase"] = "restored"
    record(inst, document, state)
    (inst.root / "pending.json").unlink(missing_ok=True)
    return observe(inst, document)
