# Gateway构建身份验收计划

环境：任务worktree、临时源码目录、动态本地测试资源；不访问业务或服务宿主。

| 编号 | 前置与操作 | 可观察结果 | 清理 |
|---|---|---|---|
| BI-01 | 固定输入锁，执行真实build-image.sh；修改临时源码后再构建 | 两份记录不同，各自校验通过，输入锁逐字未变 | 删除临时根，保留摘要报告 |
| BI-02 | 真实checks.sh配隔离Docker替身；逐个改镜像ID/三个摘要标签/基础镜像 | 全部非零，禁止启动；合法记录通过 | pytest临时根自动清理 |
| BI-03 | 删除记录、破坏JSON、缺字段、给空摘要 | 预检非零，不能借仓库旧摘要或镜像标签通过 | 同上 |
| BI-04 | 正常记录校验后改变源码或uv.lock | context_digest.py --check拒绝旧记录 | 同上 |
| BI-05 | 一次真实Docker构建与镜像内导入 | 输出记录与Docker实读ID/标签一致；锁和Git状态无生成物修改 | 保留本任务构建证据 |
| BI-06 | 固定本次批准记录摘要，再换成彼此匹配的旧镜像与旧记录，或删除批准摘要 | 预检非零；不能用旧对证明新交付 | pytest临时根自动清理 |

对应自动化：`tests/tooling/test_gateway_build_identity.py`、
`tests/tooling/test_m2_live03_manifest_preflight.py`及`test_m2_live03_deploy.py`。
BI-01替身只替代Docker构建器，真实打包/摘要/构建脚本均执行；BI-05再用真实Docker核对。
不跳过锁校验测试、不关闭工作树保护；普通源码改动不更新共享锁，Java节点和基础镜像变更仍需评审。
