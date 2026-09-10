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

Feature: 公司 cloud-auth Bearer 适配
  标准 API 使用公司现有 cloud-auth 签发的 Bearer token，不复制签名密钥。

  Scenario: cloud-auth token 通过 UPMS 建立范围
    Given Bearer token 可被 UPMS /user/info 与 /user/ds 验证
    When 调用标准订单接口
    Then AI-Ops 建立不可变 ScopeContext
    And 订单查询使用该上下文的受限范围

  Scenario: 伪造 user_id 不能替代 token
    Given 调用方只有裸 user_id 或 tenant_id，没有有效 Bearer token
    When 调用标准订单接口
    Then 请求被拒绝
    And 不触发订单查询

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

Feature: 标准健康报告曲线与来源摘要
  健康报告使用完整受限时序数据计算，并返回有界、可追溯的曲线响应。

  Rule: 曲线响应有界且不伪造缺失数据

    Scenario: 大量时序数据降采样
      Given 订单有超过 300 个有效时序点
      When 获取健康报告
      Then 每条曲线最多返回 300 个有序点
      And 保留首点、末点与极值
      And 指标计算使用完整输入点

    Scenario: 遥测不可用时报告仍可部分完成
      Given 订单授权有效但遥测源不可用
      When 获取健康报告
      Then 报告仍可 completed
      And 曲线为空且 source_summary.telemetry 为 unavailable
      And 不使用伪造点替代缺失遥测

Feature: 完整充电健康指标
  健康报告按已确认确定性公式返回雷达评分和 SOH，缺少权威输入时逐项不可用。

  Rule: 评分和单位由服务端确定

    Scenario: 完整输入返回五维评分
      Given 订单有完整温度、SOC、电压和权威容量输入
      When 获取健康报告
      Then 返回五个稳定 radar code
      And 每个 score 在 0 到 100 之间
      And rule_version 为当前公式版本

    Scenario: 无权威容量不生成 SOH
      Given 订单有遥测但没有权威标称容量
      When 获取健康报告
      Then capacity 评分和 SOH 为 unavailable
      And 不使用 VIN 后缀或车型模糊匹配推断容量

Feature: 统一标准 API 契约
  健康报告和单问诊断使用同一 Bearer、错误和资源隔离约定，但彼此独立。

  Scenario: 两类资源独立返回
    Given 调用方具备对应 scope
    When 分别创建健康报告作业和单问诊断
    Then 两者返回各自 opaque ID、状态和 retry_after_ms
    And 任一资源失败不修改另一资源

Feature: C/B 平台隔离固定问答
  固定问答按服务端判定的平台内容域返回，不依赖订单或模型。

  Rule: 平台身份来自可信上下文

    Scenario: C 端入口返回客户端推荐
      Given 服务 Bearer 和有效 thirdSession 已通过认证
      And 入口上下文为 consumer
      When 调用固定问答推荐接口
      Then 返回 platform consumer 和 question_id/title/sort 展示字段
      And 推荐响应不包含答案或身份详情

    Scenario: 管家入口需要唯一 B 端主体
      Given C 端身份关联一个同租户且具备管家端角色的 B 端主体
      And 入口上下文为 operator
      When 调用固定问答目录接口
      Then 返回 platform operator 的正式目录
      And 不返回客户端目录内容

    Scenario: 多个 B 端主体拒绝歧义请求
      Given C 端身份关联多个具备管家端角色的 B 端主体
      When 未提供入口或请求 operator 内容
      Then 返回 409 PLATFORM_AMBIGUOUS
      And 不随机选择 B 端主体

  Rule: 固定答案与诊断链路隔离

    Scenario: 点击推荐同步返回确定性答案
      Given 当前平台推荐 question_id 有正式固定答案
      When POST 固定问答答案接口
      Then 返回 200、纯文本答案和 faq_version
      And 不创建 job_id 或 diagnosis_id
      And 不调用模型、不查询订单

    Scenario: 跨平台问题标识隐藏内容
      Given 当前平台为 consumer
      When 提交 operator 前缀的 question_id
      Then 返回 404 FAQ_NOT_FOUND

Feature: 智能体草稿、发布、停用与不可变版本
  管理员在已授权租户内维护智能体草稿；已发布版本是运行时不可变快照。

  Rule: 草稿只能由当前租户的智能体管理员维护

    Scenario: 管理员创建并编辑客服草稿
      Given 当前调用者属于租户 tenant-a 且拥有 ROLE_AGENT_ADMIN
      When 创建客服智能体草稿并提交业务 Prompt、知识库绑定、模型和 blocks-v1 输出合同
      Then 返回 opaque agent_id、draft 状态和 revision
      And 编辑草稿后 revision 增加
      And 草稿内容不会出现在其他租户的列表中

    Scenario: 非管理员或跨租户读取被拒绝
      Given 智能体属于 tenant-a
      When tenant-b 调用者或没有 ROLE_AGENT_ADMIN 的调用者读取智能体
      Then 返回统一的未授权结果
      And 不泄露智能体是否存在

  Rule: 发布校验并生成不可变快照

    Scenario: 发布生成可追溯版本快照
      Given 草稿的知识库属于当前租户且状态为 ready
      And 模型和输出合同在后端 allowlist 中
      When 管理员按当前 revision 发布草稿
      Then 返回 published 状态和 version_no=1
      And 快照包含 Prompt、知识库、模型、输出合同、发布者和发布时间
      And 后续编辑草稿不会改变 version_no=1 的快照

    Scenario: 从已发布版本派生新草稿
      Given 智能体当前发布 version_no=1
      When 管理员按当前 revision 创建新草稿并修改知识库绑定
      Then 旧版本仍保持可运行且内容不变
      And 新草稿发布后生成 version_no=2

    Scenario: 发布拒绝无效依赖或过期 revision
      Given 草稿绑定的知识库仍在解析或模型不在 allowlist
      When 管理员发布草稿
      Then 返回明确的发布校验错误且不生成新版本
      When 使用旧 revision 再次保存或发布
      Then 返回版本冲突且不覆盖其他人的修改

  Rule: 停用阻止新运行但保留历史快照

    Scenario: 停用智能体
      Given 智能体已有已发布版本
      When 发布管理员停用智能体
      Then 智能体状态为 disabled 且不能再发布新回合
      And 已发布版本快照仍可按 version_no 查询

    Scenario: 只能删除未发布草稿
      Given 一个智能体从未发布过版本
      When 管理员删除该草稿
      Then 后续读取返回统一未找到
      And 已发布智能体不能被删除
Feature: 受限知识检索与媒体资源协议
  Codex 只能通过 AI-Ops harness 的受限知识检索工具获取当前已发布智能体允许的知识库资料，
  图片和视频必须通过短时授权的媒体资源协议交付，不能暴露 RAGFlow 内部标识。

  Rule: 检索范围与调用次数由 harness 控制

    Scenario: 检索只使用已发布智能体绑定的知识库
      Given 当前租户的客服智能体版本只绑定知识库 KB-A
      And 检索响应同时包含 KB-A 和其他租户知识库的分段
      When Codex 请求 knowledge_search
      Then 只返回 KB-A 的脱敏分段和引用
      And 请求方不能通过参数选择其他知识库或 top_k

    Scenario: 单轮检索达到上限后返回受限状态
      Given 当前回合已执行两次 knowledge_search
      When Codex 再次请求 knowledge_search
      Then 返回 retrieval_status limited
      And 不再调用知识库服务

  Rule: 媒体资源可授权渲染且不越权

    Scenario: 命中图片和视频生成短时资源引用
      Given 检索分段关联一个 PNG 图片和一个 MP4 文档
      When harness 规范化检索结果
      Then 返回 image/video 内容资源引用和 BFF 可转发的相对地址
      And 不返回 RAGFlow image_id、对象存储路径或租户 token

    Scenario: 视频代理支持浏览器 Range 请求
      Given 当前用户持有仍有效的视频资源授权
      When 媒体代理收到 bytes=2-5 的 Range 请求
      Then 返回 206、video/mp4、Content-Range 和对应字节
      And 返回 inline 与 Accept-Ranges 响应头

    Scenario: 媒体授权跨租户或过期后失效
      Given 媒体资源属于租户 A 或已超过 TTL
      When 租户 B 或过期会话请求该资源
      Then 返回 403 且不读取媒体内容

  Rule: 依赖失败不伪造知识依据

    Scenario: 知识库服务不可用
      Given kb-service 请求超时或返回非法响应
      When harness 执行 knowledge_search
      Then 返回 retrieval_status unavailable
      And 不产生媒体资源引用
