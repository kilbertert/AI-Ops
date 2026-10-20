"""36 上 Dify 栈的部署件形状约束（#858 后续 / 2026-10-20 三处缺陷）。

这些不是「跑起来了吗」的测试（那要宿主），而是**部署件本身**的约束。三条各自对应
一次实测缺陷：

1. **没有向量库** ⇒ vendor 数据集的 1564 个分段全部停在 indexing，
   `documents.error` = `Vector store must be specified.`，`embeddings` 表 0 行。
   只起 weaviate 容器而不设 `VECTOR_STORE` 是同一个故障的第二种形状，所以两者都断言。
2. **上游补丁没有落点** ⇒ `/v1/datasets/{id}/retrieve` 对 external 数据集返回一条空
   记录（看起来像"没命中"），API 模板的 key 明文落库。补丁文件挂在镜像原文件上，
   漏挂一个就是静默回到旧行为，所以挂载集合与补丁集必须逐一对上。
3. **补丁挂载可写** ⇒ 容器里的改动不会回到仓库，二进制就成了第二份真相。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/dify-36"
PATCH_DIR = DEPLOY / "patches/1.17.1"
PINNED = DEPLOY / "patches/apply.sh"


@pytest.fixture(scope="module")
def compose(tmp_path_factory) -> dict:
    return _resolved_compose(tmp_path_factory.mktemp("dify36"))


def _resolved_compose(tmp_path: Path) -> dict:
    """用一份假 .env 解析 compose —— 只做插值，不读任何真实凭据。"""
    import shutil
    import subprocess

    work = tmp_path / "deploy"
    shutil.copytree(DEPLOY, work)
    (work / ".env").write_text(
        "\n".join(
            [
                "DIFY_REGISTRY=docker.1panel.live",
                "DIFY_EXPOSE_PORT=10018",
                f"SECRET_KEY={'x' * 43}",
                f"INIT_PASSWORD={'x' * 16}",
                f"DB_PASSWORD={'x' * 16}",
                f"REDIS_PASSWORD={'x' * 16}",
                f"PLUGIN_DAEMON_KEY={'x' * 43}",
                f"PLUGIN_DIFY_INNER_API_KEY={'x' * 43}",
                "CELERY_BROKER_URL=redis://:x@redis:6379/1",
                f"WEAVIATE_API_KEY={'y' * 48}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(work / "docker-compose.yaml"),
            "--env-file",
            str(work / ".env"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        pytest.skip(f"需要 docker compose 才能解析部署件：{out.stderr.strip()[:200]}")
    import json

    return json.loads(out.stdout)


def test_vector_store_is_configured_not_merely_installed():
    """容器在、变量不在 —— 这正是本次事故的形状，所以变量单独断言。"""
    env = (DEPLOY / "env.example").read_text(encoding="utf-8")
    assert "\nVECTOR_STORE=weaviate\n" in env
    assert "\nWEAVIATE_ENDPOINT=http://weaviate:8080\n" in env, "容器内必须用 compose 内网名"
    # 密钥必须留空由现场生成：写死一个默认值等于把镜像文档里的样例带上生产。
    assert "\nWEAVIATE_API_KEY=\n" in env


def test_weaviate_publishes_no_host_port_and_is_healthy_gated(compose):
    assert "weaviate" in compose["services"]
    assert not compose["services"]["weaviate"].get("ports"), "向量库不得发布宿主端口"
    for name in ("api", "worker"):
        dep = (compose["services"][name].get("depends_on") or {}).get("weaviate") or {}
        assert dep.get("condition") == "service_healthy", f"{name} 没有等向量库就绪"


def _patched_paths_from_patches() -> set[str]:
    paths = set()
    for patch in sorted(PATCH_DIR.glob("*.patch")):
        paths.update(re.findall(r"^\+\+\+ b/(.+)$", patch.read_text(encoding="utf-8"), re.M))
    return paths


def test_every_patch_is_mounted_read_only_on_both_processes(compose):
    """补丁文件与挂载必须一一对应：漏挂 = 静默回旧行为，多挂 = 挂了没人读。"""
    patched = _patched_paths_from_patches()
    assert patched, "patches/1.17.1 下没有补丁"

    for name in ("api", "worker"):
        mounted = set()
        for vol in compose["services"][name].get("volumes") or []:
            target = vol.get("target") or ""
            if target.startswith("/app/api/") and "patched-api" in (vol.get("source") or ""):
                assert vol.get("read_only"), f"{name} 的补丁挂载可写：{target}"
                mounted.add(target[len("/app/api/") :])
        assert mounted == patched, f"{name} 的补丁挂载与补丁集不符"


def test_patches_pin_the_image_files_they_were_written_against():
    """补丁绑定具体上游版本的**原文件内容**。

    `.patch` 的上下文只能说明"大概长这样"，真正决定它能不能用的是原文件摘要。
    `apply.sh` 先核对再打补丁；这条断言保证它核对的正是补丁所针对的那几个文件。
    """
    pinned = dict(re.findall(r"^(\S+\.py) ([0-9a-f]{64})$", PINNED.read_text(encoding="utf-8"), re.M))
    assert set(pinned) == _patched_paths_from_patches(), "apply.sh 的固定清单与补丁集不一致"
    for rel, digest in pinned.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert rel.endswith(".py")


def test_patch_files_are_unified_diffs_with_fuzz_zero_context():
    """`patch --fuzz=0` 能被上下文满足的前提：每块都带上下文行。"""
    for patch in sorted(PATCH_DIR.glob("*.patch")):
        for line in patch.read_text(encoding="utf-8").splitlines():
            if line.startswith("@@"):
                # hunk 头必须写成 “-a,b +c,d” —— 缺了长度字段，patch 会按 1 行处理。
                assert re.fullmatch(r"@@ -\d+(,\d+)? \+\d+(,\d+)? @@.*", line), (patch.name, line)
