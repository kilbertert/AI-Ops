# 部署独立执行槽与PR任务生命周期

Status: Accepted

## 决定

已测得agent任务等待43/78分钟，而部署执行约2分钟。保留一个agent实例，新增无默认标签的
aiops-deploy实例只承接CD/发布；纯GitHub API调度移至hosted。PR agent开始前、交付前和push前
验证实时状态，closed事件与定期扫描清理明确已失效的白名单运行，取消后核对终态。

来源与完整取舍见[方案](../architecture/2026-11-05-runner-queue-design.md)。
GitHub标签采用AND匹配，因此新实例不得带通用self-hosted；标签不构成账户/凭据隔离。
PR运行关联用可信事件生成的run-name，不能假定pull_request_target的pull_requests列表非空。

## 替代方案

增加通用runner可增加平均吞吐，但agent仍可占满部署槽；缩短所有超时会切断有效任务。
开启更长并发队列只保存更多积压。ARC需要额外Kubernetes控制面，当前两实例规模不采用。

## 后果

同主机故障与资源争用仍存在，保持agent并发为1并按Q1–Q8实测，不宣称零排队。
清理仅取消失效计算，不修改业务数据；GitHub取消API没有CAS，最终复读与请求间保留窄竞争窗。
部署锁和必需CI不变；先使新runner就绪再合并路由。20次合并观察属于上线后的持续证据。
