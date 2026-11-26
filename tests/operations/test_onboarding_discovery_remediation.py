"""自动环境发现与引导期二次确认受限库修复测试（ADR-0358 P1-2）。

拦截的具体错误：
1. 自动探测结果意外夹带 password/token 等敏感字段泄露至前端；
2. 未经运维显式二次确认或 confirmation_digest 被篡改时执行数据库修复；
3. 白名单外任意 SQL（如 DROP/DELETE/GRANT ALL/无 LOCK=NONE 锁表 DDL/多语句拼接）绕过校验；
4. 修复完成后未立刻复验或未以 0600 落盘审计记录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops_diagnostics.onboarding.db_remediation import (
    RemediationRefused,
    execute_confirmed_db_remediation,
    validate_allowlisted_sql,
)
from aiops_diagnostics.onboarding.discovery import (
    DiscoveryRefused,
    build_remote_discovery_probe_script,
    parse_and_enrich_discovery_report,
    write_server_side_config_template,
)


def test_discovery_enriches_report_with_confirmation_digest_and_blocks_secret_leaks() -> None:
    script = build_remote_discovery_probe_script(
        environment="jiejia",
        api_domain="api.juxichongdian.cn",
        internal_gateway_addr="10.0.0.31:30899",
    )
    assert "api.juxichongdian.cn" in script
    assert "30888" in script

    preflight = {
        "ready": False,
        "checks": [{"id": "mysql-grants-upms", "state": "blocked", "summary": "缺权限"}],
        "remediations": [
            {
                "id": "mysql_upms_select_grants",
                "kind": "mysql_grant",
                "auto_fixable": True,
                "sql_statements": [
                    "GRANT SELECT ON `qumall_upms`.`sys_user` TO 'aiops_ro'@'10.0.0.31';",
                    "GRANT SELECT ON `qumall_upms`.`sys_user_role` TO 'aiops_ro'@'10.0.0.31';",
                    "GRANT SELECT ON `qumall_upms`.`sys_role` TO 'aiops_ro'@'10.0.0.31';",
                ],
            },
            {
                "id": "mysql_required_indexes",
                "kind": "mysql_index",
                "auto_fixable": True,
                "sql_statements": [
                    "ALTER TABLE `cloud_charging_pile`.`iot_device_offline_monitor` "
                    "ADD INDEX `aiops_offline_device_time` (`device_id`, `offline_time`), "
                    "ALGORITHM=INPLACE, LOCK=NONE;"
                ],
            },
        ],
        "discovered": {
            "tenants": [{"id": "1990990768688599040", "name": "捷佳"}],
            "healthy_tsdata_url": "http://127.0.0.1:30888",
            "sample_candidates": {"orders": [{"order_no": "2125879212020924417"}]},
        },
    }
    report = parse_and_enrich_discovery_report(
        environment="jiejia",
        host_facts={
            "hostname": "jiejia-gw",
            "python_minor": "3.12",
            "timezone": "Asia/Shanghai",
            "nginx_flavor": "baota",
            "nginx_extension_dir": "/www/server/panel/vhost/nginx/extension/api.juxichongdian.cn",
        },
        data_preflight_report=preflight,
    )
    assert report["ready"] is False
    plan = report["db_remediation_plan"]
    assert plan["has_auto_fixable_db_gaps"] is True
    assert len(plan["confirmation_digest"]) == 64
    assert len(plan["sql_statements"]) == 4

    # 含敏感字段名必须直接阻断
    with pytest.raises(DiscoveryRefused, match="SECRET_LEAK"):
        parse_and_enrich_discovery_report(
            environment="jiejia",
            host_facts={"hostname": "h", "mysql_password": "leaked"},
            data_preflight_report={"ready": True, "checks": [], "remediations": [], "discovered": {}},
        )


def test_db_remediation_allowlist_blocks_arbitrary_sql_and_locking_ddl() -> None:
    bad_statements = [
        "DROP TABLE cloud_charging_pile.ch_order_info;",
        "GRANT ALL PRIVILEGES ON *.* TO 'aiops_ro'@'10.0.0.31';",
        "GRANT SELECT ON `qumall_upms`.`sys_secret` TO 'aiops_ro'@'10.0.0.31';",
        "ALTER TABLE `cloud_charging_pile`.`iot_device_offline_monitor` "
        "ADD INDEX `aiops_offline_device_time` (`device_id`, `offline_time`);",
        "GRANT SELECT ON `qumall_upms`.`sys_user` TO 'aiops_ro'@'10.0.0.31'; DROP TABLE x;",
    ]
    for stmt in bad_statements:
        with pytest.raises(RemediationRefused, match="SQL_FORBIDDEN"):
            validate_allowlisted_sql([stmt])


def test_confirmed_db_remediation_executes_rechecks_and_writes_0600_audit(tmp_path: Path) -> None:
    sqls = [
        "GRANT SELECT ON `qumall_upms`.`sys_user` TO 'aiops_ro'@'10.0.0.31';",
        "ALTER TABLE `cloud_charging_pile`.`iot_device_offline_monitor` "
        "ADD INDEX `aiops_offline_device_time` (`device_id`, `offline_time`), "
        "ALGORITHM=INPLACE, LOCK=NONE;",
    ]
    report = parse_and_enrich_discovery_report(
        environment="jiejia",
        host_facts={"hostname": "jiejia-gw", "python_minor": "3.12"},
        data_preflight_report={
            "ready": False,
            "checks": [],
            "remediations": [
                {
                    "id": "mysql_upms_select_grants",
                    "kind": "mysql_grant",
                    "auto_fixable": True,
                    "sql_statements": [sqls[0]],
                },
                {
                    "id": "mysql_required_indexes",
                    "kind": "mysql_index",
                    "auto_fixable": True,
                    "sql_statements": [sqls[1]],
                },
            ],
            "discovered": {},
        },
    )
    digest = report["db_remediation_plan"]["confirmation_digest"]
    rem_ids = report["db_remediation_plan"]["remediation_ids"]

    # 未显式二次确认必须拒绝
    with pytest.raises(RemediationRefused, match="CONFIRMATION_REQUIRED"):
        execute_confirmed_db_remediation(
            environment="jiejia",
            actor="41:t:u",
            remediation_ids=rem_ids,
            sql_statements=sqls,
            confirmed_by_operator=False,
            confirmation_digest=digest,
            executor=lambda s: None,
            reverify_runner=lambda: {"ready": True},
        )

    # 摘要篡改必须拒绝
    with pytest.raises(RemediationRefused, match="DIGEST_MISMATCH"):
        execute_confirmed_db_remediation(
            environment="jiejia",
            actor="41:t:u",
            remediation_ids=rem_ids,
            sql_statements=sqls,
            confirmed_by_operator=True,
            confirmation_digest="0" * 64,
            executor=lambda s: None,
            reverify_runner=lambda: {"ready": True},
        )

    executed: list[tuple[str, ...]] = []
    audit_file = tmp_path / "audit" / "db-remediation.jsonl"
    res = execute_confirmed_db_remediation(
        environment="jiejia",
        actor="41:t:u",
        remediation_ids=rem_ids,
        sql_statements=sqls,
        confirmed_by_operator=True,
        confirmation_digest=digest,
        executor=lambda s: executed.append(s),
        reverify_runner=lambda: {"ready": True, "state": "passed"},
        audit_log_path=audit_file,
    )
    assert res["executed"] is True
    assert res["verified_after_fix"] is True
    assert executed == [tuple(sqls)]
    assert (audit_file.stat().st_mode & 0o777) == 0o600
    audit_entry = json.loads(audit_file.read_text(encoding="utf-8").strip())
    assert audit_entry["verified_after_fix"] is True
    assert audit_entry["confirmation_digest"] == digest


def test_write_server_side_config_template_is_0600_and_never_echoes_secrets(tmp_path: Path) -> None:
    target = tmp_path / "priv" / "config-jiejia.env"
    res = write_server_side_config_template(
        target,
        mysql_host="10.0.0.32",
        mysql_port=3306,
        mysql_user="aiops_ro",
        mysql_password="SECRET_MYSQL_PW",
        redis_host="10.0.0.32",
        redis_port=6379,
        redis_password="SECRET_REDIS_PW",
        tsdata_url="http://127.0.0.1:30888",
    )
    assert res["written"] is True
    assert "SECRET_MYSQL_PW" not in json.dumps(res)
    assert (target.stat().st_mode & 0o777) == 0o600
