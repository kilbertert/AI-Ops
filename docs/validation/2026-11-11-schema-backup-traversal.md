# schema备份父目录遍历权限补修

日期：2026-11-11。PR #1466首轮Devin发现经人工确认有效；本轮只修该部署边界。
候选基础为8a453cc0，提交分支feat/gateway-schema-authority；未部署，未完成业务验收。

## 判据与改动

root创建的本次备份B不能依赖umask或历史目录模式。B保留root owner，
仅对B执行chgrp服务组与chmod0710；B/schema为服务owner、0700。
备份祖先、源码备份、其他备份不做权限变更；既有祖先允许服务遍历是明确前置条件。
不使用ACL、递归chown或放开others。权限准备失败发生在源码替换前。

新增长期检查用于拦截umask导致的父目录不可遍历；判据来自已确认的权限合同，
旧部署测试将服务身份映射到当前用户，无法单独证明不同账号间的访问。

## 执行证据

执行真实远端目录准备片段，分别使用umask027/077；只将chgrp/chown中的服务身份
映射到当前测试账号，真实执行chmod/chgrp/chown。严格检查命令仅作用B或B/schema、
B保留owner且mode0710、schema mode0700、祖先与refs权限不变。

- 修复前：两个用例均失败，B实际为0750/0700，且无显式服务组遍历准备。
- 修复后：`uv run pytest -n 0 tests/test_cd_deploy_scripts.py`，33 passed，7.94秒。
- Ruff check、format检查、bash语法检查通过；逐提交ADR检查随提交执行。
- schema源码SHA256仍为`1feeac95ccad8c96be23e6a513330f5f40635a09e94e06e31d87b2dd87ff14d0`。

这是无特权命令片段与权限位检查，未冒充真实root到服务账号的访问验证；
跨账号临时权限验证由交付会话补充。作者没有sudo、创建账号、读生产或变更服务。
本轮不重复全量；既有全量/定向证据保留各自版本，PR CI验证最终提交。
