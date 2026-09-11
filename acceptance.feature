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

    Scenario: 媒体上游以 HTTP 200 返回业务错误
      Given 视频下载接口返回 HTTP 200 和 code=102 的 JSON 错误包
      When AI-Ops 媒体代理回源
      Then 该媒体请求返回 404
      And 不把错误 JSON 当作视频字节返回

    Scenario: 上游忽略 Range 时代理仍正确切片
      Given 视频上游返回完整对象而忽略 Range 请求
      When 媒体代理收到 bytes=0-1023
      Then 返回 206 和仅对应范围的字节
      And Content-Range 反映完整对象长度

    Scenario: 媒体响应 MIME 与实际字节一致
      Given 图片授权声明为 image/png 但上游返回 JPEG 魔数
      When 媒体代理返回图片
      Then Content-Type 为 image/jpeg

Feature: 客服 QA RAG 单轮运行接入统一入口
  统一问答入口的 qa 路径按已发布客服智能体运行，异步完成后返回稳定 blocks[] 内容块与
  检索状态；FAQ 与订单诊断行为保持不变，媒体块只能引用本轮受授权检索结果。

  Rule: 分流行为保持回归

    Scenario: FAQ 命中仍同步返回固定答案
      Given 一个命中的固定问题
      When 用户通过统一入口提问
      Then 同步返回固定答案且不调用知识库或模型

    Scenario: 无订单业务问题创建 qa 作业
      Given 当前租户存在已发布客服智能体
      When 用户提出无订单号的业务问题
      Then 返回 202、qa_id 和轮询地址
      And 轮询完成后 result 含 blocks[] 和 retrieval_status

  Rule: blocks 内容块合同

    Scenario: 命中图片的分段返回 image 块
      Given 本轮检索命中携带 PNG 图片的分段
      When 智能体完成回答
      Then blocks 含 image 块及本轮签发的媒体资源描述
      And 不暴露 RAGFlow 标识、对象存储路径或外部 URL

    Scenario: 命中视频的分段返回 video 块
      Given 本轮检索命中 MP4 视频文档分段
      When 智能体完成回答
      Then blocks 含 video 块与可播放的媒体资源描述

    Scenario: 模型伪造媒体或引用被丢弃
      Given 智能体返回的 image/video 块引用本轮未签发的资源
      When harness 校验回答
      Then 该媒体块被移除且文本块保留
      And 引用未返回分段号的 reference 块同样被移除

  Rule: 检索状态诚实汇报

    Scenario: 无知识库命中返回 not_found
      Given knowledge_search 返回空分段
      When 智能体完成回答
      Then retrieval_status 为 not_found 且文本仍可交付

    Scenario: 知识库依赖不可用返回 unavailable
      Given kb-service 请求失败
      When 智能体完成回答
      Then retrieval_status 为 unavailable 且文本仍可交付

    Scenario: 模型声称 found 但无检索依据被降级
      Given 智能体未经检索直接声称 found
      When harness 校验回答
      Then retrieval_status 被修正为 not_found

  Rule: 漏检索时 harness 要求补检索

    Scenario: 业务问题漏检索触发一次补检索
      Given 用户提出业务知识问题且智能体首轮未请求 knowledge_search
      When harness 收到无检索依据的直接回答
      Then harness 要求补充一次检索后再接受回答
      And 单轮总检索次数不超过两次

    Scenario: 寒暄问题不触发补检索
      Given 用户提出简单寒暄
      When 智能体直接回答
      Then harness 不强制检索且正常完成

    Scenario: 模型返回空工具请求时仍交付检索状态
      Given 模型返回 tool_requests 但请求数组为空
      When harness 处理业务问题
      Then harness 使用当前问题执行一次受限检索
      And 无命中或依赖不可用时分别交付 not_found 或 unavailable 文本

Feature: 智能体会话与活跃订单上下文
  会话绑定用户主体、租户、入口和智能体版本，保存有限上下文并每轮重新校验；
  已确认活跃订单允许后续订单问题省略订单号，但聊天文本不能替代归属校验。

  Rule: 会话读取按主体/租户/入口/智能体隔离

    Scenario: 跨用户、跨租户或跨入口读取统一未找到
      Given 会话属于用户 A、租户 T1 和 consumer 入口
      When 用户 B 或另一租户或 operator 入口访问该会话
      Then 返回统一 404 且不泄露会话存在性

    Scenario: 会话创建绑定入口与智能体版本
      Given 已认证用户带 X-Business-Entry 与智能体版本标识
      When 创建会话
      Then 会话绑定该主体、租户、入口与智能体版本并返回 conversation_id

  Rule: 有限上下文与保留期

    Scenario: 上下文窗口取最近 8 轮或 8k token 中较小者
      Given 会话已积累超过 8 轮且部分轮次 token 很大
      When 读取会话上下文
      Then 只返回符合两个上限内最近完成的轮次
      And 中断或未完成的轮次不进入上下文

    Scenario: 会话默认保留 30 天
      Given 一条会话超过 30 天未活动
      When 后续访问该会话
      Then 返回统一未找到

  Rule: 活跃订单省略订单号但每轮重新校验

    Scenario: 已确认订单的后续订单问题省略订单号
      Given 会话已绑定归属校验通过的活跃订单
      When 用户提出明确涉及该订单的问题且未带订单号
      Then 按订单诊断语义路由并复用该订单
      And 本轮重新校验订单归属

    Scenario: 权限变化后活跃订单失效
      Given 活跃订单在上一轮归属校验通过
      And 本轮归属校验失败
      When 用户提出涉及该订单的问题
      Then 清除活跃订单并按普通知识问题回答
      And 不产生订单诊断

    Scenario: 普通知识问题即使存在活跃订单也走 qa
      Given 会话已绑定活跃订单
      When 用户提出不涉及订单的知识问题
      Then 按通用问答路径路由且不触发订单诊断

  Rule: 并发与取消语义

    Scenario: 同一会话并发生成返回忙
      Given 会话当前存在生成中的回合
      When 同一会话再次发起提问
      Then 返回 409 CONVERSATION_BUSY

    Scenario: 未完成的结果不作为完整回答保存
      Given 一个生成中的回合被停止或失败
      When 会话轮次落库
      Then 该轮次不保留答案且不进入后续上下文

  Rule: 版本切换语义

    Scenario: 新回合使用新版本且执行中回合保持原版本
      Given 智能体在回合执行期间发布新版本
      When 新回合开始
      Then 新回合按最新已发布版本运行
      And 执行中的回合继续使用其创建时版本快照

Feature: 后台智能体管理与草稿隔离调试
  管理员通过 API 管理智能体生命周期（T2 已交付），并在发布前用真实知识库状态
  校验绑定、用隔离调试预览草稿回答效果。管理页面本身（qumall-admin 菜单/角色）
  为范围外边界项，由接口级验收覆盖（2026-09-10 业务方决定）。

  Rule: 发布前知识库绑定必须按真实状态校验

    Scenario: 绑定知识库仍解析中时发布被拒并说明原因
      Given 租户草稿智能体绑定的知识库存在解析中文档
      When 管理员发布该智能体
      Then 发布被拒绝且错误信息包含解析中原因

    Scenario: 绑定知识库不存在或跨租户时发布被拒
      Given 租户草稿智能体绑定了不属于该租户或不存在的知识库
      When 管理员发布该智能体
      Then 发布被拒绝且错误信息包含不存在原因

    Scenario: kb-service 不可用时发布失败关闭
      Given kb-service 不可达
      When 管理员发布绑定了知识库的智能体
      Then 发布被拒绝且错误信息说明知识库服务不可用

  Rule: 草稿隔离调试不触达生产会话

    Scenario: 草稿调试返回内容块预览
      Given 管理员租户存在 customer 类型草稿智能体
      When 管理员发起草稿调试提问
      Then 返回 blocks[] 与 retrieval_status 且标记 debug 与草稿版本

    Scenario: 草稿调试不产生生产会话或订单访问
      Given 草稿调试运行
      When 调试完成后查询会话与订单接口
      Then 不存在由调试产生的会话轮次或订单读取

    Scenario: 非草稿或非客服智能体不能调试
      Given 智能体处于已发布状态或为运维类型
      When 管理员对其发起调试
      Then 返回 409 状态无效错误

    Scenario: 跨租户调试统一未找到
      Given 另一租户的管理员
      When 对其他租户的智能体发起调试
      Then 返回 404 且不泄露智能体存在性

Feature: 智能体运行监控与脱敏审计
  每次问答路由交互（faq/qa/diagnosis/debug）在完成点记录一行脱敏指标：
  租户、路由类型、结果、智能体与版本、会话、检索状态、检索数、媒体数、
  延迟、Token 与失败码。长期监控数据不含问题原文、回答原文、Prompt、
  知识库内容、媒体 URL 或任何内部凭据。

  Rule: 监控查询按租户与角色隔离

    Scenario: 租户管理员查询本租户聚合
      Given 管理员持有 agent 查看角色且其租户存在运行记录
      When 管理员查询监控汇总
      Then 返回本租户的调用量、成功/失败、延迟、Token、检索与媒体计数

    Scenario: 跨租户与无权限查询被拒绝
      Given 查询者无 agent 查看角色或查询其他租户
      When 查询监控接口
      Then 无权限返回 403，跨租户数据按租户隔离不可达且不泄露存在性

  Rule: 记录区分路由与失败形态

    Scenario: 失败码区分检索、模型、媒体、忙碌与取消
      Given qa 失败、诊断受阻、会话忙碌、kb 不可用各发生一次
      When 监控数据落库后查询
      Then QA_FAILED、DIAGNOSIS_BLOCKED、CONVERSATION_BUSY、KB_UNAVAILABLE
      And 检索未命中与检索不可用按 not_found/unavailable 区分

  Rule: 脱敏与保留

    Scenario: 监控行不含原文与内部标识
      Given 一次问答运行完成
      When 查看监控明细行
      Then 行内只有枚举与计数字段，不含问题、回答、Prompt、媒体 URL 或对象路径

    Scenario: 监控数据按保留期清理
      Given 监控行超过 30 天保留期
      When 新指标写入触发清理
      Then 过期行被删除且不影响其他数据
