"""在 RAGFlow 容器内检查/修复已选 baoyun 的租户；默认只读，显式 --apply 才写。"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import requests

MODEL_SPECS = (
    ("text-embedding-3-large", "embedding", 8192),
    ("deepseek-v4-flash", "chat", 131072),
    ("gpt-4o-mini", "vision", 128000),
)
VISION_NAME = "gpt-4o-mini@baoyun@OpenAI"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--tenant", help="只对一个已在修复范围内的租户先做金丝雀验证")
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply 需要 --backup（只存原默认值，不存凭据）")
    logging.disable(logging.CRITICAL)
    # 只加载模型目录，避免 init_settings 创建/迁移无关的向量表。
    from common import settings

    settings.FACTORY_LLM_INFOS = json.loads(Path("conf/llm_factories.json").read_text())["factory_llm_infos"]
    from api.db.db_models import APIToken, Tenant, TenantModelInstance, TenantModelProvider
    from api.db.joint_services.tenant_model_service import (
        get_model_config_by_id,
        get_tenant_default_model_by_type,
    )

    tenants = list(Tenant.select().where(Tenant.img2txt_id == VISION_NAME))
    if args.tenant:
        tenants = [tenant for tenant in tenants if tenant.id == args.tenant]
    if not tenants:
        raise RuntimeError("没有匹配的租户，拒绝猜测修复范围")
    snapshots = []
    failures = []
    for tenant in tenants:
        snapshots.append(
            {
                "tenant": tenant.id,
                **{
                    name: getattr(tenant, name)
                    for name in (
                        "img2txt_id",
                        "tenant_img2txt_id",
                        "embd_id",
                        "tenant_embd_id",
                        "llm_id",
                        "tenant_llm_id",
                    )
                },
            }
        )
        try:
            config = get_model_config_by_id(tenant.id, "vision", tenant.tenant_img2txt_id)
            assert (config["llm_factory"], config["llm_name"]) == ("OpenAI", "gpt-4o-mini")
        except (LookupError, AssertionError):
            failures.append(tenant.id)
    print(json.dumps({"tenants": len(tenants), "invalid_vision_defaults": len(failures)}), flush=True)
    if not args.apply:
        if failures:
            raise SystemExit(1)
        return

    os.umask(0o077)
    with args.backup.open("x") as stream:
        json.dump(snapshots, stream, indent=2)
    for tenant in tenants:
        token = APIToken.select().where(APIToken.tenant_id == tenant.id).first()
        if token is None:
            raise RuntimeError(f"租户 {tenant.id} 缺少已有 API token")
        session = requests.Session()
        session.headers["Authorization"] = "Bearer " + token.token

        def call(method, path, body=None, client=session):
            response = client.request(method, "http://127.0.0.1:9380/api/v1" + path, json=body, timeout=180)
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                # 上游可能把请求信息放进错误，不将其复制进证据。
                raise RuntimeError(f"{method} {path}: code={data.get('code')}")
            return data.get("data")

        providers = list(TenantModelProvider.select().where(TenantModelProvider.tenant_id == tenant.id))
        provider = next((p for p in providers if p.provider_name == "OpenAI"), None)
        instances = list(
            TenantModelInstance.select().where(TenantModelInstance.provider_id.in_([p.id for p in providers]))
        )
        instance = next(
            (
                i
                for i in instances
                if provider and i.provider_id == provider.id and i.instance_name == "baoyun"
            ),
            None,
        )
        if instance is None:
            candidates = [
                i
                for i in instances
                if json.loads(i.extra or "{}").get("base_url", "").rstrip("/")
                == "https://ai-api.baoyun.com/v1"
            ]
            if len(candidates) != 1:
                raise RuntimeError(f"租户 {tenant.id} 的原 baoyun 凭据来源不唯一")
            source = candidates[0]
            if provider is None:
                call("PUT", "/providers", {"provider_name": "OpenAI"})
            call(
                "POST",
                "/providers/OpenAI/instances",
                {
                    "instance_name": "baoyun",
                    "api_key": source.api_key,
                    "base_url": json.loads(source.extra)["base_url"],
                    "model_info": [
                        {"model_name": "gpt-4o-mini", "model_type": ["image2text"], "max_tokens": 128000}
                    ],
                },
            )
        for name, kind, maximum in MODEL_SPECS:
            rows = call("GET", "/models")
            matches = [
                m
                for m in rows
                if m["provider_name"] == "OpenAI" and m["instance_name"] == "baoyun" and m["name"] == name
            ]
            usable = [m for m in matches if kind in m["model_type"]]
            if not usable:
                if matches:
                    raise RuntimeError(f"租户 {tenant.id} 的 {name} 类型不符，保留现状")
                call(
                    "POST",
                    "/providers/OpenAI/instances/baoyun/models",
                    {
                        "model_name": name,
                        "model_type": ["image2text" if kind == "vision" else kind],
                        "max_tokens": maximum,
                    },
                )
                rows = call("GET", "/models")
                usable = [
                    m
                    for m in rows
                    if m["provider_name"] == "OpenAI"
                    and m["instance_name"] == "baoyun"
                    and m["name"] == name
                    and kind in m["model_type"]
                ]
            if len(usable) != 1:
                raise RuntimeError(f"租户 {tenant.id} 的 {name} 模型不唯一")
            call("PATCH", "/models/default", {"model_type": kind, "model_id": usable[0]["model_id"]})
            config = get_tenant_default_model_by_type(tenant.id, kind)
            assert (config["llm_factory"], config["llm_name"]) == ("OpenAI", name)
        print(json.dumps({"tenant": tenant.id, "defaults": "verified"}), flush=True)


if __name__ == "__main__":
    main()
