#!/usr/bin/env python
"""一次性数据迁移：外部知识库的孤儿绑定与明文 api_key（2026-10-20）。

在 **api 容器内**运行（要 Dify 的应用上下文与租户密钥提供者）：

    docker exec -w /app/api deploy-api-1 python /tmp/migrate_20261020.py          # 只报告
    docker exec -w /app/api deploy-api-1 python /tmp/migrate_20261020.py --apply  # 落库

两件事，互不依赖，可分别判定：

1. **孤儿绑定**：``external_knowledge_bindings.dataset_id`` 指向已删除的 dataset。
   这张表没有外键，删库路径也不清理它，于是模板的 ``dataset_bindings`` 与使用计数
   会列出不存在的数据集。只删「目标 dataset 确实不存在」的行 —— 判断依据是 join
   结果为空，而不是一张手写 id 清单。

2. **明文 api_key**：``external_knowledge_apis.settings`` 是 JSON 文本，上游按明文存。
   改为 ``enc:v1:<base64>``（见补丁 `services/external_knowledge_service.py`）。
   迁移**不换值**：换值是另一件需要两侧同步的独立动作（见 qa-plan.md）。

默认 dry-run；``--apply`` 才写库。两个动作都在同一个事务里，异常则全部回滚。
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app/api")

ENCRYPTED_PREFIX = "enc:v1:"


def _is_encrypted(tenant_id: str, value: str) -> bool:
    """Whether a stored ``api_key`` is one of ours, decided by *decrypting* it.

    Not by the prefix. A provider is free to issue a key that starts with
    ``enc:v1:``; judging by the prefix alone would leave that row in the
    "already encrypted" bucket and the retrieval path would later hand the
    literal key — as ciphertext — to the provider. Trying the decrypt is the
    only test that answers the question actually being asked.
    """
    from core.helper.encrypter import decrypt_token

    if not value.startswith(ENCRYPTED_PREFIX):
        return False
    try:
        decrypt_token(tenant_id, value[len(ENCRYPTED_PREFIX) :])
    except Exception:
        return False
    return True


def main(apply: bool) -> int:
    import app_factory
    from extensions.ext_database import db
    from models.dataset import Dataset, ExternalKnowledgeApis, ExternalKnowledgeBindings
    from services.external_knowledge_service import ExternalDatasetService
    from sqlalchemy import select

    app = app_factory.create_app()[1]
    with app.app_context():
        session = db.session()

        orphans = [
            binding
            for binding in session.scalars(select(ExternalKnowledgeBindings)).all()
            if session.scalar(select(Dataset.id).where(Dataset.id == binding.dataset_id).limit(1)) is None
        ]
        plaintext = []
        for api in session.scalars(select(ExternalKnowledgeApis)).all():
            settings = json.loads(api.settings or "{}")
            key = settings.get("api_key")
            # 判断依据是"能不能解回来"，不是前缀 —— 供应商完全可以发一把以 enc:v1:
            # 开头的 key（见 `_is_encrypted`）。
            if isinstance(key, str) and key and not _is_encrypted(api.tenant_id, key):
                plaintext.append((api, settings))

        print(f"孤儿绑定 {len(orphans)} 条：")
        for binding in orphans:
            print(
                f"  {binding.id}  dataset={binding.dataset_id}  knowledge_id={binding.external_knowledge_id}"
            )
        print(f"明文 api_key {len(plaintext)} 条：")
        for api, _ in plaintext:
            print(f"  {api.id}  {api.name}")

        if not apply:
            print("\n(dry-run) 加 --apply 落库")
            return 0

        for binding in orphans:
            session.delete(binding)
        for api, settings in plaintext:
            api.settings = json.dumps(
                ExternalDatasetService.settings_for_storage(api.tenant_id, settings), ensure_ascii=False
            )
            session.add(api)
        session.commit()
        print(f"\n已删除 {len(orphans)} 条孤儿绑定，已加密 {len(plaintext)} 条 api_key")
        return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv[1:]))
