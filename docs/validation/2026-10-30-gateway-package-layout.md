# Gateway目录归位

基线`5f8da5d9`（接通用/会话迁移），分支`refactor/gateway-package-layout`。
10个Gateway模块归gateway，operator_conversation归operator；知识页面资源归knowledge；22测试归类。
包根保留版本文件和三个实际运维/服务命令实现，命令名称与entrypoint不变，无转发壳。
11模块业务AST一致，仅知识页面资源包名变化。原装配循环6条边只更新路径，未扩大许可。
初次专项16失败均为静态检查旧路径/循环边名称，修正清单后314 passed；全量待完成。
ruff/format、镜像重建、wheel/sdist、临时Gateway通过。未修改业务状态/权限/存储格式。
本片尚未部署，未完成业务验收；基线的节点/Gateway版本错配由另一验收会话补齐，后续复验。

全量首次3426通过/9跳过/3失败，均为知识页面测试常量未随资源移动，已修正；复测待记录。

最终全量3430 passed / 9既有跳过，knowledge页面3项复测通过；镜像新包与两套资源读取通过。
