"""D20投影：唯一草稿决定连接，秘密不公开，导出不能激活或覆盖。"""

import hashlib
import io
import json
import tarfile

import pytest
from dotenv import dotenv_values
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_projection as projection
from aiops_diagnostics.gateway import environment_registry as registry

ADMIN = ("control", "platform", "admin")


@pytest.fixture
def candidate(tmp_path):
    path = tmp_path / "registry/directory.json"
    registry.apply(
        path,
        [],
        expected_revision=0,
        actor="fixture",
        managers=[dict(environment=ADMIN[0], tenant_id=ADMIN[1], user_id=ADMIN[2], environments=["*"])],
    )
    draft = dict(
        id="a",
        name="环境A",
        enabled=True,
        gateway_url="https://a.example.test",
        cpms_base_url="https://a.example.test/cpms",
        company_gateway_url="https://a.example.test",
        deployment_target="host-a",
        credential_ref="config-a",
        timezone="Asia/Shanghai",
        tenants=[dict(id="same", name="租户", enabled=True)],
    )
    drafts.save(path, ADMIN, draft, expected_revision=0, registry_revision=1)
    files = {}
    revision = "a" * 40
    for key, contents in (
        (
            "application",
            {
                "manifest.json": json.dumps(
                    dict(
                        schema=1,
                        revision=revision,
                        python="3.12",
                        platform="linux-x86_64",
                        version="1.0.0+" + revision[:12],
                    )
                ).encode(),
                "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"opaque",
            },
        ),
        (
            "installer",
            {
                "REVISION": revision.encode(),
                **{name: b"never executed" for name in ("install.py", "runtime_check.py", "probe.py")},
            },
        ),
    ):
        checks = "".join(
            hashlib.sha256(value).hexdigest() + "  " + name + "\n" for name, value in contents.items()
        ).encode()
        archive = tmp_path / (key + ".tar.gz")
        with tarfile.open(archive, "w:gz") as bundle:
            for name, value in {**contents, "SHA256SUMS": checks}.items():
                member = tarfile.TarInfo(name)
                member.size = len(value)
                bundle.addfile(member, io.BytesIO(value))
        archive.chmod(0o600)
        files[key] = dict(
            path=str(archive), sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), revision=revision
        )
    template = write_private_text(
        tmp_path / "template.env", "AIOPS_MYSQL_PASSWORD='literal-secret'\nAIOPS_SSH_ENABLED=false\n"
    )
    files["configuration"] = dict(
        id="config-a", path=str(template), sha256=hashlib.sha256(template.read_bytes()).hexdigest()
    )
    ref = write_private_text(
        tmp_path / "references.json",
        json.dumps(
            dict(
                schema="aiops-environment-onboarding/1",
                targets=[
                    dict(
                        id="host-a",
                        host="managed-a",
                        user="aiops",
                        port=18951,
                        python="3.12",
                        application=files["application"],
                        installer=files["installer"],
                        configurations=[files["configuration"]],
                    )
                ],
            )
        ),
    )
    return path, draft, template, ref, files


def test_projection_roundtrip_has_one_environment_and_never_activates(candidate):
    path, draft, template, _, files = candidate
    secret = "quote'\\ literal $HOME \nnext"
    write_private_text(
        template, "AIOPS_MYSQL_PASSWORD='" + secret.replace("\\", "\\\\").replace("'", "\\'") + "'\n"
    )
    result = projection.render(
        draft,
        template,
        hashlib.sha256(template.read_bytes()).hexdigest(),
        registry.load(path)["audit"][-1]["at"],
    )
    values = dotenv_values(stream=io.StringIO(result["production.env"].decode()))
    assert values["AIOPS_MYSQL_PASSWORD"] == secret
    assert values["AIOPS_GATEWAY_EXECUTION_ENVIRONMENT"] == "a"
    assert values["AIOPS_UPMS_BASE_URL"] == "https://a.example.test/upms"
    assert values["AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL"] == draft["cpms_base_url"]
    row = json.loads(result["environment.json"])["environments"][0]
    assert row["id"] == "a" and row["enabled"] is False and row["tenants"][0]["enabled"] is False
    assert (
        "AIOPS_GATEWAY_ENVIRONMENT" not in values and not json.loads(result["environment.json"])["managers"]
    )


@pytest.mark.parametrize("escaped", [r"left\rright", r"left\r\nright"])
def test_projection_preserves_carriage_return_credentials_on_file_read(candidate, escaped):
    path, draft, template, _, _ = candidate
    write_private_text(template, 'AIOPS_MYSQL_PASSWORD="' + escaped + '"\n')
    files = projection.render(
        draft,
        template,
        hashlib.sha256(template.read_bytes()).hexdigest(),
        registry.load(path)["audit"][-1]["at"],
    )
    output = write_private_text(template.with_name("projected.env"), files["production.env"].decode())
    assert dotenv_values(output)["AIOPS_MYSQL_PASSWORD"] == dotenv_values(template)["AIOPS_MYSQL_PASSWORD"]


@pytest.mark.parametrize(
    "text",
    [
        "AIOPS_GATEWAY_ENVIRONMENT=b\n",
        "AIOPS_GATEWAY_ENVIRONMENT_INSTALLATION_FILE=/private/control-install.json\n",
        "AIOPS_GATEWAY_ENVIRONMENT_INSTALLATION_FILE=/private/control-install.json\n",
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL=https://b.test\n",
        "AIOPS_MYSQL_PASSWORD=x\nAIOPS_MYSQL_PASSWORD=y\n",
        "AIOPS_MYSQL_PASSWORD=${HOST_SECRET}\n",
        "AIOPS_MYSQL_PASSWORD\n",
        "AIOPS_MYSQL_PASSWORD='unterminated",
        "AIOPS_SSH_ENABLED=true",
        "AIOPS_GATEWAY_PORT=9876",
        "AIOPS_GATEWAY_INTERNAL_TOKEN_EXPIRE_SECONDS=bad",
    ],
)
def test_invalid_or_overriding_template_is_not_a_projection(candidate, text):
    path, draft, template, _, _ = candidate
    write_private_text(template, text)
    with pytest.raises(ValueError):
        projection.render(
            draft,
            template,
            hashlib.sha256(template.read_bytes()).hexdigest(),
            registry.load(path)["audit"][-1]["at"],
        )


def test_same_plan_exports_identical_private_bundle_and_refuses_drift(candidate, tmp_path):
    path, draft, template, ref, _ = candidate
    original = path.read_bytes()
    plan = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    assert {"id": "config-projection", "state": "passed"} in plan["checks"]
    assert "literal-secret" not in json.dumps(plan) and str(tmp_path) not in json.dumps(plan)
    output = tmp_path / "candidate.tar.gz"
    request = write_private_text(tmp_path / "read.json", json.dumps({"identity": ADMIN, "id": plan["id"]}))
    argv = [
        "environment-plan",
        "--registry",
        str(path),
        "--file",
        str(request),
        "--references",
        str(ref),
        "--output",
        str(output),
    ]
    cli = CliRunner().invoke(admin_app, argv)
    assert cli.exit_code == 0, cli.output
    second = tmp_path / "second.tar.gz"
    assert (
        plans.export(path, ADMIN, plan["id"], ref, second)["archive_sha256"]
        == json.loads(cli.output)["archive_sha256"]
    )
    assert output.read_bytes() == second.read_bytes() and output.stat().st_mode & 0o077 == 0
    with tarfile.open(output) as bundle:
        assert set(bundle.getnames()) == {"production.env", "environment.json", "manifest.json"}
        manifest = json.load(bundle.extractfile("manifest.json"))
        assert manifest["executable"] is False and manifest["plan_digest"] == plan["digest"]
        assert (
            manifest["projected_config_sha256"]
            == hashlib.sha256(bundle.extractfile("production.env").read()).hexdigest()
        )
    assert CliRunner().invoke(admin_app, argv).exit_code == 1 and path.read_bytes() == original
    with pytest.raises((plans.PlanRefused, registry.RegistryError)):
        plans.export(path, ("other", "platform", "admin"), plan["id"], ref, tmp_path / "forbidden.tar")
    write_private_text(template, "AIOPS_MYSQL_PASSWORD=changed")
    with pytest.raises(plans.PlanRefused):
        plans.export(path, ADMIN, plan["id"], ref, tmp_path / "stale.tar")
    assert not (tmp_path / "stale.tar").exists()
    assert path.read_bytes() == original
