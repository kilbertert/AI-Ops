"""``gateway.db`` 连接的唯一渲染（#573）。

十几个 store 共用同一个 SQLite 文件，「拿一条连接要做什么」曾经各写
一遍：三种持锁约定、四个等待上限、外键与回滚各有取舍——差别不是
笔误，是四种决定各写了一遍且没有任何一行记录。同一个文件上不存在
「不参与锁」这个选项：另一个 store 正以 ``BEGIN IMMEDIATE`` 持租约时，
你的选择只有「等多久再 BEGIN」与「不等、写时再升级」两种。这里把
连接参数与锁纪律各收敛成一处，让「新增一个 store」只能在两个具名
profile 之间选择。

读路径两个 profile 相同：``timeout=5``、``isolation_level=None``（自动
提交）、``row_factory``、``PRAGMA foreign_keys=ON``、
``PRAGMA busy_timeout=5000``。写路径是分歧点：

``LEASE``
    整段事务持锁。进入块时 ``BEGIN IMMEDIATE`` 先拿写锁（不是第一条
    写语句才升级——读-改-写在锁内进行）、异常整段回滚。读后写、
    多语句写、DDL 一律走它。
``SINGLE_STATEMENT``
    仅当一次调用**只写一条语句**且块内不读后写时使用。每个语句自动
    提交；放弃的是：跨语句原子性（块里多写一条就各算各的）与整段
    回滚（已提交的语句回不来）。它是「我的写恰好单语句」的显式声明，
    不是少写四行的省略。

初始化先在事务外设置 WAL，再用 LEASE 逐条 ``execute`` 建表、补列与建索引。
Python sqlite3 的 legacy 事务控制下，``executescript`` 会先提交挂起事务；
``isolation_level=None`` 不改变这点，不能靠外层事务让脚本失败时整批回滚。

``ChatRouteBindingStore`` 也使用本连接约定，受管节点场景与 Gateway 同库。
唯一独立约定是 ``dify_login_service.MappingStore`` 的 ``mappings.db``
（台账见 ``tests/common/test_scalar_rules.py``）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: 整段事务持锁——读-改-写、多语句写、DDL 的唯一正确选择。
LEASE = "lease"

#: 单语句写、不读后写。放弃跨语句原子性与整段回滚；选择它是声明
#: 「恰好单语句」，块内多写一条就各自提交、互不相干。
SINGLE_STATEMENT = "single_statement"


@contextmanager
def connection(path: Path, *, write: bool = False, profile: str = LEASE) -> Iterator[sqlite3.Connection]:
    """打开一条 ``gateway.db`` 连接。

    读路径两个 profile 一致；写路径由 ``profile`` 决定锁纪律（见模块
    docstring）。调用方不得再自行 ``sqlite3.connect`` 这个文件——那会把
    锁与等待的决定藏回各 store 的正文里。
    """
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if write and profile not in {LEASE, SINGLE_STATEMENT}:
        # 拼错的 profile 不能静默退回自动提交——那等于把租约选择藏起来。
        conn.close()
        raise ValueError(f"unknown write profile: {profile!r}")
    leased = write and profile == LEASE
    try:
        if leased:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if leased:
            conn.commit()
    except Exception:
        if leased:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def initialize(path: Path) -> Iterator[sqlite3.Connection]:
    """事务外设置 WAL，再用一次 LEASE 原子初始化；不回滚 WAL 文件属性。"""
    with connection(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    with connection(path, write=True) as conn:
        yield conn
