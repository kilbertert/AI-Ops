Feature: AFK 拉取请求可信交付
  AI-Ops 的 pull_request_target 自动化不得执行带交付凭据的候选宿主代码。

  Rule: PR 变更工作流使用当前 main 的可信控制面

    Scenario: 持久化 runner 使用当前 main 作为审核基线
      Given runner 的本地 main 已落后于 origin main
      When 仓库所有者创建的同仓库 PR 触发 agent:review
      Then 可信 controller 将本地 main 重置到当前 origin main
      And 审核差异以该当前基线计算

    Scenario: 候选代码不能获得交付凭据
      Given 仓库所有者创建的同仓库 PR 触发 AFK 变更工作流
      When 工作流执行候选分支
      Then 宿主依赖和编排只从 main controller 加载
      And 候选命令只在带只读 token 的 Docker 沙箱执行
      And 干净 delivery checkout 导入并推送已验证的 Git bundle

    Scenario: 不可信 PR 或缺失交付凭据时停止
      Given PR 来自 fork、作者不是仓库所有者，或 AGENT_PAT 不可用
      When PR 被添加 AFK 变更标签
      Then 工作流不报告成功交付
      And 凭据失败时记录 agent:blocked

Feature: 基于权限上下文的受限直连诊断运行时
  AI-Ops 诊断必须先建立不可变 ScopeContext，再在受限范围内直连 MySQL/TDengine/Redis，
  不信任前端裸传权限字段，UPMS/Dis 不可用或缺权限时 fail closed。

  Rule: 调用者身份只能来自已验证平台凭证

    Scenario: 有效凭证解析调用者身份与范围
      Given 已认证调用者提供平台凭证
      When 解析 ScopeContext
      Then 得到调用者身份、有效租户与业务数据范围
      And 产生可审计的范围指纹

    Scenario: 未知凭证或空凭证失败关闭
      Given 调用者凭证为空或无效
      When 解析 ScopeContext
      Then 以 ScopeError fail closed
      And 不触发任何数据库查询

  Rule: 目标主体与调用者分离

    Scenario: 无代查权限时目标主体被拒
      Given 调用者不具备代查/管理权限
      When 请求通过 target_b_user_id / target_c_user_id 指定目标主体
      Then 以 delegation_denied fail closed
      And 不执行目标主体查询

    Scenario: 有代查权限时目标主体可解析
      Given 调用者具备平台代查权限
      When 指定存在且不歧义的目标主体
      Then ScopeContext 明确区分 caller 与 subject
      And 有效租户按调用者或目标主体确定

  Rule: 租户选择不能扩大授权范围

    Scenario: 普通调用者指定他人租户被拒
      Given 调用者有效租户为 TENANT-A
      When 请求指定 tenant_id=TENANT-B
      Then 以 tenant_forbidden fail closed

    Scenario: 平台管理员显式切换租户被允许
      Given 调用者具备平台管理员角色
      When 请求显式切换租户
      Then ScopeContext 使用请求租户为有效租户

  Rule: 受限直连查询只访问允许范围

    Scenario: MySQL 订单查询下推租户/站点/用户
      Given 已建立带 QueryScope 的 ScopeContext
      When 执行订单/费用/占位费/设备查询
      Then SQL 过滤条件包含有效租户与可见站点
      And 不信任调用方传入的裸 tenant_id

    Scenario: 站点范围为空时短路返回空证据
      Given 可见站点集合为空
      When 执行范围查询
      Then 不发起 SQL，返回空结果或空证据

    Scenario: TDengine 请求设备不在允许集合时拒绝
      Given 已由订单元数据 seed 允许设备集合
      When 查询集合外设备
      Then 以 device_forbidden 拒绝
      And 不发起 TDengine 请求

    Scenario: Redis Stream 只统计租户归属可验证的消息
      Given 已构造租户归属谓词
      When 检查白名单 Stream
      Then 只对租户一致的消息计数
      And 无法验证归属时不返回原始消息正文

  Rule: 审计与失败语义

    Scenario: 审计摘要记录凭据外的范围与指纹
      Given 完成一次受限诊断运行
      When 生成本次运行审计记录
      Then 记录调用者、目标主体、有效租户与范围指纹
      And 不记录平台凭证、数据库口令或完整权限副本

    Scenario: 权限失败不误报为无业务数据
      Given UPMS/Dis 不可用、主体不存在或权限不足
      When 诊断运行结束
      Then 以权限失败类型区分数据源失败与空结果

Feature: 标准调用者认证与订单授权
  AI-Ops 标准资源接口必须从验证后的 Bearer token 建立调用者上下文，
  不接受设备令牌或裸身份字段代替授权。

  Rule: 标准接口只信任验证后的调用者上下文

    Scenario: 有效 access token 可以验证有权订单
      Given token resolver 返回当前调用者、租户、scope 与数据范围
      And 订单属于该不可变权限上下文
      When 调用订单授权探针
      Then 返回订单可访问
      And 返回范围指纹但不返回 token

    Scenario: token 无效或 scope 不足时失败关闭
      Given Bearer token 过期、audience 错误或缺少 required scope
      When 调用订单授权探针
      Then 返回稳定 401 或 403 错误码
      And 不触发订单数据源查询

    Scenario: 订单不在调用者范围时隐藏资源存在性
      Given token 有效但订单属于其他主体或租户
      When 调用订单授权探针
      Then 返回统一 ORDER_NOT_FOUND
      And 不泄露订单是否真实存在

  Rule: 现有设备接口保持兼容

    Scenario: device token 仍只访问既有 run 接口
      Given 已注册设备持有 aops_ token
      When 调用现有 runs 接口
      Then 行为与标准认证上线前一致
      And 该 token 不能访问标准订单接口

Feature: 最小异步充电健康报告
  标准调用方可以为授权且已结束的订单创建短生命周期报告作业，
  并通过轮询获得确定性最小报告。

  Rule: 报告以异步作业交付

    Scenario: 授权订单完成最小报告
      Given Bearer token 有订单读取 scope
      And 订单已结束且时间窗口有效
      When 创建并轮询健康报告作业
      Then 作业经过 queued 与 running 后 completed
      And 报告包含确定性摘要、停止原因指标、rule_version、data_as_of 与 completeness

    Scenario: 重复请求复用活动作业
      Given 同一调用者范围、订单和规则版本已有活动作业
      When 再次创建健康报告作业
      Then 返回同一个 job_id
      And 不重复提交后台计算

  Rule: 作业有界且失败关闭

    Scenario: 无权订单不创建作业
      Given 订单不在调用者范围
      When 创建健康报告作业
      Then 返回统一 ORDER_NOT_FOUND
      And 不保存报告作业

    Scenario: 过期或重启中的作业可重建
      Given 作业超过 deadline 或服务在 queued/running 时重启
      When 查询或重新创建作业
      Then 旧作业状态为 expired
      And 新请求可以创建新 job_id

Feature: 标准单问诊断与主体级历史查询
  标准调用方可以针对授权订单提交一次独立问题，并查询自身范围内的诊断资源。

  Rule: 诊断运行使用现有受限 Agent 路径

    Scenario: 自由文本问题完成一次诊断
      Given Bearer token 有诊断写入 scope
      And 订单属于调用者的不可变权限上下文
      When 提交一个自由文本问题
      Then 返回 202、opaque diagnosis_id 和 retry_after_ms
      And 后台使用现有受限诊断运行生成 completed 或 inconclusive 结果

    Scenario: 指标上下文不接受客户端伪造结果
      Given 调用方提交 indicator_code
      When 同时提交伪造的 score、curve 或完整 health report
      Then 伪造字段被拒绝或忽略
      And 诊断只使用服务端重新读取的受限证据

  Rule: 历史诊断按调用者范围隔离

    Scenario: 其他主体不能读取诊断
      Given 诊断由调用者 A 创建
      When 调用者 B 使用相同 diagnosis_id 查询或列出历史
      Then 返回统一 DIAGNOSIS_NOT_FOUND 或空列表
      And 不泄露诊断问题、状态或结果
