# 独立审查执行与故障接续

本机 ralph 与 Actions `agent:review` 的独立轴均使用 `review-sandbox.ts`；实现者与修复者保持原入口。
原 `AFK_INSTALL_CMD` 宿主命令已移除。候选的 `npm ci` 和 `uv sync` 在
`.sandcastle/sandbox-prepare.sh` 内执行；依赖失败按容器安装错误处理，不能回宿主跑候选安装脚本。

审查固定源分支 SHA，以独立 Git 副本进入容器；SDK 所需宿主挂载只读，工作目录为容器内
`/home/agent/review`。临时修改仅影响副本，退出时核对 HEAD/已跟踪文件；原分支变化需重新验收。
默认日志留宿主 `.sandcastle/logs/`；证据不得只留将被销毁的容器目录。
正常退出清理本次随机目录；异常进程退出遗留的 `.sandcastle/reviews/` 先确认无运行容器再按具体目录处理，
不要批量删除别的任务数据。

## 复验命令

```bash
npm ci --ignore-scripts --no-audit --no-fund
npx tsx .sandcastle/review-boundary.check.ts
npx tsx .sandcastle/review-boundary.check.ts --docker
```

第一条仅安装受信任控制器依赖，不执行候选生命周期脚本。普通检查验证裁决、环境过滤与独立对象库；
Docker 检查使用既有 AFK 镜像，临时创建合成仓库，通过同一 SDK/provider 真正运行安装和 agent 调度。
它断言安装发生在容器、宿主假凭据不可见、宿主挂载拒写、`update-ref` 不影响源仓库、
裁决回读成功；不调用模型、不用真实业务数据。CI 执行普通检查，Docker 证据由交付者记录。

## 凭据事件接续

凭据事件的受影响清单与轮换操作记录只存私有运维位置；
交接仅提供变量用途和处理状态，不给 Hacker 原文件、路径或令牌。
轮换步骤：列出使用该身份的客户端/常驻服务与在途会话；供应商签发新上游 key；更新受控配置并
验证新连接；确认依赖者已切换后撤销旧身份，再验证旧身份拒绝。只增加新 key 而保留旧 key 不算轮换完成。
在途进程可能缓存旧身份，改磁盘 settings 不会更新已启动会话；需要重建会话的切换不可冒充已完成。

模型连接是必要授权：允许列表仍含模型连接参数，profile 仍挂载其受控认证文件，
`claude-deepseek` 仍使用既有 host 网络。文件隔离不等于模型秘密或网络完全隔离。
不得为核对型号读取原凭据材料；由派发者给脱敏结论，失败标未验。
