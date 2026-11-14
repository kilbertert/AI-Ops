# 公司发布流程只读核对方法

本手册描述如何确认正式发布路径，不授予构建、部署或配置修改权限。具体入口、账号、任务名、目标主机、
原始配置/构建日志保存在私有运维记录，禁止直接复制到公开仓库。公司源码继续使用company-gitlab-api.sh。

## 读取顺序

1. 使用操作者提供的现役Jenkins入口，通过只读JSON API列项目/目录；不依据名字猜环境。
2. 对明确相关job读取config.xml：确认Pipeline来自SCM还是直接存于job；记录所引用共享库、分支参数、
   编译目标、制品、发布开关与阶段。只提取白名单元数据，不输出密码、token或凭据参数默认值。
3. 读取最近构建的参数、SCM实际SHA、触发类型与wfapi/describe阶段；当前默认值不等于该次实际参数。
4. 限定读取该次consoleText，原件只私有留存；提取检出SHA、镜像digest、部署任务结果/rollout证据。
   SUCCESS只证明所执行阶段；单纯推镜像、删除Pod、rollout成功、用户原入口通过是四种不同结果。
5. 确认GitLab webhook实际触发。向部署分支提交可能自动推镜像或重启，不能把“只推代码”当无副作用。
6. 关联目标与已登记业务环境、当前运行制品及数据源。未知目标不能由任务名或镜像namespace推定。

只使用GET：根api/json、job/api/json、job/config.xml、build/api/json、build/wfapi/describe、build/consoleText。
认证只在进程内或受控凭据位置读取，不经argv、不保存密码脚本、不跟随未知重定向。
禁止触发build/buildWithParameters、replay、script、config POST或deploy任务来“验证权限”。

## 对一键开通的复用原则

优先复用公司已经投用的受认证发布API和任务账本。若存在request_id幂等提交与任务轮询，
开通步骤引用其请求/任务编号；网络中断先查原请求，不用新编号盲目重发。
发布接口仅对当前允许的服务执行，不给浏览器任意job/服务/URL或Jenkins管理员凭据。

当前已观察到的异步合同形状：GET健康；POST任务带request_id/action/services；GET任务按id读取
queued/running/succeeded/failed/interrupted；忙碌与暂不可用允许有界重试。它证明特定环境存在正式发布机制，
不证明可以任意安装新服务、改Nacos配置或业务schema；服务端白名单、制品绑定、回滚与目标范围仍须独立核对。

适用官方规格：[Jenkins Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/)。
权限、发布分支、目标范围以公司当前治理为准，不接管或改写外部仓库的评审流程。
