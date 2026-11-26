"""数据面全量只读预检与白名单修复规划测试（ADR-0357 P0-1）。

拦截的具体错误：
1. aiops_ro 缺 qumall_upms.{sys_user,sys_user_role,sys_role} SELECT 在预检期漏放，公网报 503；
2. 缺少离线监测复合索引 (device_id, offline_time) 漏放或生成非无锁 DDL；
3. 客户网关 /tsdata/** 指向失效端口时未发现本机直连回环端口；
4. 非白名单表名/索引名/非法标识符注入 SQL，或在报告中回显数据库/Redis 明文密码。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.onboarding.data_preflight import (
    REQUIRED_MYSQL_INDEXES,
    REQUIRED_MYSQL_TABLES,
    build_missing_index_sql,
    build_upms_grant_sql,
    has_leading_index,
    run_data_preflight,
)


def _full_grants() -> dict[str, dict[str, bool]]:
    return {db_key: {tbl: True for tbl in tables} for db_key, tables in REQUIRED_MYSQL_TABLES.items()}


def _full_indexes() -> dict[str, list[dict[str, object]]]:
    return {
        "iot_device_offline_monitor": [
            {"Key_name": "aiops_offline_device_time", "Seq_in_index": 1, "Column_name": "device_id"},
            {
                "Key_name": "aiops_offline_device_time",
                "Seq_in_index": 2,
                "Column_name": "offline_time",
            },
        ],
        "ch_order_info": [
            {"Key_name": "ch_order_info_device_id_index", "Seq_in_index": 1, "Column_name": "device_id"},
        ],
    }


def _sample_env() -> dict[str, str]:
    return {
        "AIOPS_MYSQL_HOST": "10.0.0.32",
        "AIOPS_MYSQL_PORT": "3306",
        "AIOPS_MYSQL_USER": "aiops_ro",
        "AIOPS_MYSQL_PASSWORD": "SUPER_SECRET_DB_PASS_999",
        "AIOPS_REDIS_HOST": "10.0.0.32",
        "AIOPS_REDIS_PORT": "6379",
        "AIOPS_REDIS_PASSWORD": "SUPER_SECRET_REDIS_PASS_888",
        "AIOPS_GATEWAY_OPERATOR_TSDATA_URL": "http://10.0.0.31:30899/tsdata",
    }


def test_data_preflight_blocks_missing_upms_grants_and_offline_index_with_exact_sql() -> None:
    grants = _full_grants()
    grants["upms"] = {"sys_user": False, "sys_user_role": False, "sys_role": False}
    indexes = _full_indexes()
    indexes["iot_device_offline_monitor"] = [{"Key_name": "PRIMARY", "Seq_in_index": 1, "Column_name": "id"}]

    report = run_data_preflight(
        _sample_env(),
        company_gateway_url="https://api.example.invalid",
        discover_samples=True,
        mysql_probe=lambda cfg, dbs, *, discover_samples: {
            "connected": True,
            "client_host": "10.0.0.31",
            "table_grants": grants,
            "indexes": indexes,
            "samples": {
                "orders": [{"order_no": "2125879212020924417", "device_id": "dev-1"}],
                "offline_devices": [{"device_id": "dev-1", "event_count": 3}],
                "consumer_orders": [{"order_no": "2092158656121733121", "user_id_suffix": "3170"}],
            },
        },
        tsdata_probe=lambda url: (
            {"url": url, "reachable": False, "status": 502}
            if url.endswith("/tsdata")
            else {"url": url, "reachable": True, "status": 404}
        ),
        redis_probe=lambda cfg: {"reachable": True},
        upms_tenants_probe=lambda url: {
            "reachable": True,
            "status": 200,
            "tenants": [{"id": "1990990768688599040", "name": "捷佳", "code": "20251119", "status": "0"}],
        },
    )

    assert report["ready"] is False
    assert report["state"] == "blocked"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "SUPER_SECRET_DB_PASS_999" not in serialized
    assert "SUPER_SECRET_REDIS_PASS_888" not in serialized

    rems = {r["id"]: r for r in report["remediations"]}
    assert set(rems) == {"mysql_upms_select_grants", "mysql_required_indexes", "tsdata_switch_loopback"}
    assert rems["mysql_upms_select_grants"]["sql_statements"] == [
        "GRANT SELECT ON `qumall_upms`.`sys_user` TO 'aiops_ro'@'10.0.0.31';",
        "GRANT SELECT ON `qumall_upms`.`sys_user_role` TO 'aiops_ro'@'10.0.0.31';",
        "GRANT SELECT ON `qumall_upms`.`sys_role` TO 'aiops_ro'@'10.0.0.31';",
    ]
    assert rems["mysql_required_indexes"]["sql_statements"] == [
        "ALTER TABLE `cloud_charging_pile`.`iot_device_offline_monitor` "
        "ADD INDEX `aiops_offline_device_time` (`device_id`, `offline_time`), "
        "ALGORITHM=INPLACE, LOCK=NONE;"
    ]
    assert rems["tsdata_switch_loopback"]["suggested_config"] == {
        "AIOPS_GATEWAY_OPERATOR_TSDATA_URL": "http://127.0.0.1:30888"
    }
    assert report["discovered"]["healthy_tsdata_url"] == "http://127.0.0.1:30888"
    assert report["discovered"]["tenants"][0]["id"] == "1990990768688599040"
    assert report["discovered"]["sample_candidates"]["orders"][0]["order_no"] == "2125879212020924417"


def test_data_preflight_passes_when_all_grants_indexes_and_services_healthy() -> None:
    report = run_data_preflight(
        _sample_env(),
        mysql_probe=lambda cfg, dbs, *, discover_samples: {
            "connected": True,
            "client_host": "10.0.0.31",
            "table_grants": _full_grants(),
            "indexes": _full_indexes(),
            "samples": {"orders": [], "offline_devices": [], "consumer_orders": []},
        },
        tsdata_probe=lambda url: {"url": url, "reachable": True, "status": 200},
        redis_probe=lambda cfg: {"reachable": True},
    )
    assert report["ready"] is True
    assert report["state"] == "passed"
    assert report["remediations"] == []
    assert all(c["state"] == "passed" for c in report["checks"])


def test_remediation_sql_builders_reject_non_allowlisted_targets_and_injection() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        build_upms_grant_sql("aiops_ro", "10.0.0.31", ["sys_secret"])
    with pytest.raises(ValueError, match="mysql_user"):
        build_upms_grant_sql("aiops_ro'; DROP TABLE x;--", "10.0.0.31", ["sys_user"])
    with pytest.raises(ValueError, match="mysql client host"):
        build_upms_grant_sql("aiops_ro", "10.0.0.31';--", ["sys_user"])
    with pytest.raises(ValueError, match="allowlist"):
        build_missing_index_sql([("charging", "ch_order_info", "arbitrary_idx", ("order_no",))])

    # Valid allowlisted index matches exact specification
    sqls = build_missing_index_sql([REQUIRED_MYSQL_INDEXES[0]])
    assert len(sqls) == 1
    assert "ALGORITHM=INPLACE, LOCK=NONE" in sqls[0]


def test_has_leading_index_respects_column_order() -> None:
    wrong_order = [
        {"Key_name": "wrong_idx", "Seq_in_index": 1, "Column_name": "offline_time"},
        {"Key_name": "wrong_idx", "Seq_in_index": 2, "Column_name": "device_id"},
    ]
    assert has_leading_index(wrong_order, ("device_id", "offline_time")) is None
    right_order = [
        {"Key_name": "good_idx", "Seq_in_index": 1, "Column_name": "device_id"},
        {"Key_name": "good_idx", "Seq_in_index": 2, "Column_name": "offline_time"},
        {"Key_name": "good_idx", "Seq_in_index": 3, "Column_name": "id"},
    ]
    assert has_leading_index(right_order, ("device_id", "offline_time")) == "good_idx"


def test_admin_cli_environment_data_preflight_refuses_non_private_config(tmp_path: Path) -> None:
    env_file = tmp_path / "open.env"
    env_file.write_text("AIOPS_MYSQL_HOST=127.0.0.1\n", encoding="utf-8")
    os.chmod(env_file, 0o644)

    runner = CliRunner()
    res = runner.invoke(admin_app, ["environment-data-preflight", "--config", str(env_file)])
    assert res.exit_code == 1
    body = json.loads(res.stdout)
    assert body["error"] == "ENVIRONMENT_DATA_PREFLIGHT_REFUSED"
