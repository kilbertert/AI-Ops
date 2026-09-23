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

    Scenario: 视频文档短暂 not found 后恢复
      Given RAGFlow 对已存在的视频文档第一次返回 code=102
      And 后续请求返回真实视频字节
      When AI-Ops 媒体代理回源
      Then 代理有限重试并返回真实视频字节
      And 不把瞬时错误暴露给前端

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

Feature: 41 环境演示数据源只读门禁
  41 环境用于演示数据读取时，必须保持环境标识正确、数据面一致和只读凭据边界。

  Rule: 41 环境的业务数据面必须可验证

    Scenario: 41 环境提供诊断所需的 MySQL、Redis 和部分 TDengine 数据
      Given 41 环境地址为 47.97.160.153
      When 通过只读事务检查 cloud_charging_pile 及其依赖数据面
      Then ch_order_info、ch_fee_template_record、iot_charging_device 和 ch_site 的所需字段完整
      And Redis 白名单 Stream 可认证读取其类型与长度元数据
      And charging-gun_property 超表存在且可返回聚合计数
      And 缺失 charging-pile_comm 时必须标记协议证据不可用

  Rule: 不安全凭据或不完整数据面不得切换生产服务

    Scenario: 41 的高权限账号不能作为 AI-Ops 运行时账号
      Given 数据库账号拥有写权限或全库权限
      When 操作者准备将 AI-Ops 数据源切换到 41
      Then 切换被阻止并要求独立的最小只读账号
      And 不修改 41 环境中的服务、配置或数据
Feature: 41 环境 Gateway 切换

  Rule: 切换后公网标准 API 保持可用且只读

    Scenario: Gateway 健康检查
      Given 41 的 aiops-gateway-41.service 已启用
      When 调用 41 Gateway 的 GET /health
      Then 返回 HTTP 200
      And business_mutations 为 disabled

    Scenario: 公网入口保持认证错误合同
      Given 120 Nginx /v1/* 已通过受限隧道指向 41 Gateway
      When 使用无效 X-Third-Session 调用 GET /v1/faq/recommendations
      Then 返回 HTTP 401
      And 错误码为 INVALID_ACCESS_TOKEN

  Rule: 41 复用共享 KB 栈

    Scenario: 41 Gateway 通过受限回环隧道访问共享 kb-service
      Given 36 的 kb-service 监听 127.0.0.1:9380
      And 41 的 aiops-36-kb-tunnel.service 已启用
      When 从 41 调用本地 KB 健康端点
      Then 127.0.0.1:29380/healthz 返回 HTTP 200
      And 隧道只允许转发到 36 的 127.0.0.1:9380

    Scenario: 41 公网 FAQ 业务链路可用
      Given H5 登录接口返回与 41 会话库匹配的有效 thirdSession
      When 通过 https://api.mall.qushiyun.com/v1/faq/recommendations 调用 FAQ
      Then 返回 HTTP 200 且包含 28 条 consumer 推荐
      And 请求不返回 Nginx 404

    Scenario: 41 自由问答使用可用模型 provider
      Given 41 Gateway 默认 provider 已配置为可用的百炼兼容 provider
      And H5 登录接口返回与 41 会话库匹配的有效 thirdSession
      When 通过 https://api.mall.qushiyun.com/v1/assistant/questions 提交不命中 FAQ 的问题
      Then 返回 HTTP 202
      And 轮询终态为 completed 且 result.text 非空

    Scenario: 41 多语言 FAQ 主链路按请求语言返回
      Given 41 运行副本已部署 Accept-Language、FAQ 五语目录和短路匹配实现
      And H5 登录接口返回与 41 会话库匹配的有效 thirdSession
      When 使用 zh-CN、en-US、de、fr、es、pt-BR 分别请求 FAQ 推荐、固定答案和统一入口快捷问
      Then 每次响应均返回 HTTP 200
      And language 字段分别解析为 zh、en、de、fr、es、pt
      And 标题和答案使用对应语言，未跨环境读取会话或数据

    Scenario: 41 客服问答返回多媒体检索块
      Given 41 租户存在已发布且绑定图片和视频知识库的客服智能体
      And RAGFlow embedding provider 可用
      When 通过 https://api.mall.qushiyun.com/v1/assistant/questions 提交媒体相关问题
      Then 返回或轮询结果包含 blocks[] 的 image 或 video 块
      And 每个媒体块携带短时签名 URL
      And 视频签名 URL 支持 HTTP 206 与超范围 416

    Scenario: 95 与 41 公网入口保持数据面隔离
      Given api.qumall.qushiyun.com 是 95 环境入口
      And api.mall.qushiyun.com 是 41 环境入口
      When 分别使用对应环境的无效 thirdSession 调用 FAQ
      Then 两个入口均返回 HTTP 401 INVALID_ACCESS_TOKEN
      And 95 的请求不会进入 41 Gateway 或 41 会话库
      And 41 的请求不会进入 95 Gateway 或 95 会话库

    Scenario: 41 没有用户订单时不伪造诊断成功
      Given 当前 H5 会话的订单列表 total 为 0
      When 使用该会话创建健康报告
      Then 返回 HTTP 404 且错误码为 ORDER_NOT_FOUND

Feature: 环境清单驱动的 Agent 收敛

  Rule: aiops admin reconcile 走生产代码路径幂等收敛

    Scenario: 首次按清单创建并发布 agent
      Given 一个包含已发布客服 agent 的环境清单
      When 管理员运行 aiops admin reconcile
      Then agent 通过 AgentManager 创建并发布
      And 发布前对 kb-service 执行知识库活性校验
      And created_by 与 published_by 记录为 aiops-admin

    Scenario: 重复执行相同清单是幂等无操作
      Given 清单已成功收敛一次
      When 管理员再次运行 aiops admin reconcile
      Then 所有 agent 报告 unchanged
      And 不产生新的发布版本

    Scenario: 配置漂移通过 fork/update/publish 收敛
      Given 已发布 agent 的 prompt 与清单不一致
      When 管理员运行 aiops admin reconcile
      Then agent 产生新的不可变版本且内容与清单一致

    Scenario: 非白名单模型在任何写入前失败
      Given 清单中的模型不在 Settings 允许列表
      When 管理员运行 aiops admin reconcile
      Then 命令以错误退出
      And 数据库未发生任何变更

    Scenario: prune 仅禁用清单租户内的多余 agent
      Given 清单租户存在清单外 agent 且另一租户也存在 agent
      When 管理员运行 aiops admin reconcile --prune
      Then 清单租户的清单外已发布 agent 被禁用
      And 清单外租户的 agent 保持不变

    Scenario: 预演模式零写入
      Given 尚未收敛的环境清单
      When 管理员运行 aiops admin reconcile --dry-run
      Then 输出与实收敛相同的计划报告
      And gateway 数据库零变更


  Rule: 客服提示词的业务行为契约随清单演进

    Scenario: 知识库命中时以知识库为唯一事实来源
      Given 提示词已收敛到清单版本且用户问题在知识库命中
      When 客服智能体完成回答
      Then 回答基于知识库内容组织且不添加知识库没有的政策数字或承诺
      And 自身认知与知识库冲突时以知识库为准

    Scenario: 未命中的通用常识回答声明非官方政策
      Given 用户提出充电领域通用公开常识问题且知识库未命中
      When 客服智能体完成回答
      Then 回答先说明通用常识参考且非平台官方政策
      And 不编造具体数据政策或参数

    Scenario: 超出能力边界的问题按固定话术拒答
      Given 用户提出金融医疗法律等非领域问题或本人订单扣费问题
      When 客服智能体完成回答
      Then 使用提示词中的固定拒答话术或引导诊断人工客服
      And 不猜测订单状态或扣费原因

Feature: 意图相关的诊断置信阶梯与环境预检

  Rule: 降级由所问问题需要的证据决定，而非数据源失败本身

    Scenario: 外围数据源失败但订单费用证据完整时给出诊断
      Given MySQL 订单/费用/设备证据完整且自洽
      And TDengine 遥测源查询失败
      When Agent 完成诊断
      Then 结果状态为 diagnosed 且置信度为 medium
      And 失败源进入 failed_sources 与 limitations
      And 不因外围源失败扣留结论

    Scenario: 问题本身依赖缺失的遥测时保持 inconclusive
      Given 订单电表起止值矛盾且需要枪遥测裁决
      And 枪遥测稳定表在本环境不存在
      When Agent 完成诊断
      Then 结果状态为 inconclusive 且在 limitations 中说明缺失通道

    Scenario: 订单主数据失败时结果 blocked 且 low
      Given Agent 诊断运行中订单主数据源（order_snapshot）查询失败
      When Agent 完成诊断
      Then 结果状态为 blocked 且置信度为 low
      And 不会以 inconclusive 或 diagnosed 掩盖主数据不可用

    Scenario: 预检缺口记入证据日志并出现在初始提示
      Given 预检发现 charging-gun_property 缺少列 batteryMinTemperature
      When 诊断运行开始
      Then 证据日志记录该工具的 blocked 条目且错误文本含 预检
      And 初始提示包含 环境能力预检 说明
      And blocked 条目不进入 failed_sources

    Scenario: 预检不拦截模型工具请求
      Given 预检已记录 blocked 条目
      When 模型仍请求该工具
      Then 工具真实执行并按实际结果记录
      And 预检不代答、不短路执行器

Feature: 统一助手业务意图路由与产品快捷动作

  Rule: 低风险输入不进入订单诊断或业务知识检索

    Scenario: 普通问候不创建重型作业
      Given 用户已通过消费者入口认证
      When 用户提交“你好”
      Then 返回普通问答或轻量回答结果
      And 不创建订单诊断作业
      And 不调用业务知识库检索

    Scenario: 无实时工具时天气问题诚实说明能力边界
      Given 当前服务未配置实时天气工具
      When 用户提交“今天天气怎么样”
      Then 不调用业务知识库检索
      And 回答明确说明无法查询实时天气
      And 不伪造当前天气数据

    Scenario: FAQ 问题保持同步确定性路径
      Given 固定问答目录包含“充电枪拔不出来怎么办”
      When 用户通过统一助手入口提交该问题
      Then 返回 HTTP 200 且 type 为 faq
      And 不创建异步 QA 或诊断作业

  Rule: 高风险订单问题必须先完成业务上下文校验

    Scenario: 已授权订单问题进入诊断
      Given 用户拥有订单 123 的访问权限
      When 用户提交“订单 123 为什么提前结束”
      Then 返回 HTTP 202 且 type 为 diagnosis
      And 返回 diagnosis_id 供诊断轮询

    Scenario: 缺少订单号时返回澄清
      Given 用户未在请求中提供订单号
      When 用户提交“是不是扣错钱了”
      Then 返回 HTTP 200 且 type 为 clarification
      And missing_fields 包含 order_no
      And 不创建诊断作业

    Scenario: 扣费争议的判定与澄清文案都不限中文
      Given 用户未在请求中提供订单号
      And 请求的 Accept-Language 分别为 en/de/fr/es/pt
      When 用户分别提交该语言的扣费争议问题
      Then 均返回 HTTP 200 且 type 为 clarification
      And missing_fields 包含 order_no
      And message 为该语言文案而非中文原文
      And language 回显与请求语言一致

    Scenario: 单纯的主题词不构成扣费争议
      Given 用户提交独立的“refund”或“Why did charging stop unexpectedly?”
      When 服务端处理该请求
      Then 不返回 type 为 clarification
      And 该问题按 FAQ 或通用问答路径处理

  Rule: 文本内嵌的订单号必须按语言无关的方式识别

    Scenario: 中文提问内嵌订单号进入诊断
      Given 用户拥有订单 123 的访问权限
      When 用户提交“帮我检测（123）这个订单的充电异常”
      Then 返回 HTTP 202 且 type 为 diagnosis
      And order_no_extracted 为 123

    Scenario: 英文提问内嵌订单号同样进入诊断
      Given 用户拥有订单 123 的访问权限
      When 用户提交“Please check charging anomalies for order (123)”
      Then 返回 HTTP 202 且 type 为 diagnosis
      And order_no_extracted 为 123

    Scenario: 普通单词不得被当作订单号
      Given 提问文本中不含任何订单号
      And 提问为英文自然语言（如“Show me a customer case”）
      When 服务端进行文本内嵌订单号识别
      Then 不识别出订单号
      And 请求不被路由为订单诊断

  Rule: 产品快捷动作只声明入口，执行复用统一助手协议

    Scenario: 平台发布的动作对新租户默认可见
      Given 平台管理员已在 consumer 入口发布全局快捷动作 case_exploration
      And 租户没有该 code 的本地覆盖
      When 该租户通过 GET /v1/shortcuts 读取快捷动作
      Then 返回已发布的 case_exploration 稳定 code 和本地化文案
      And 结果不要求为该租户复制一条默认数据库记录

    Scenario: 全局动作仍按当前租户解析执行
      Given 平台已发布 smart_diagnosis 快捷动作
      And 用户来自已验证的租户上下文
      When 用户提交 shortcut_code=smart_diagnosis
      Then 服务端使用当前会话租户和入口解析动作
      And 仍要求该用户提供并通过归属校验的订单上下文

    Scenario: 平台入口与租户入口隔离
      Given 平台仅在 consumer 入口发布 case_exploration
      When 用户从 operator 入口读取 GET /v1/shortcuts
      Then case_exploration 不出现在 operator 有效动作列表
      And 修改 tenant-id 或入口请求头不能改变有效租户和入口

    Scenario: 租户覆盖优先于平台默认
      Given 平台已发布 case_exploration 且租户尚无覆盖
      When 租户管理员发布同 code 的本地文案覆盖
      Then 该租户读取列表返回本地覆盖
      And 其他租户仍返回平台默认文案

    Scenario: 租户停用只抑制本租户动作
      Given 平台已发布 report_fault 且租户尚无覆盖
      When 租户管理员发布该 code 的租户级停用
      Then 该租户有效列表不返回 report_fault
      And 其他租户仍可看到 report_fault

    Scenario: 草稿覆盖不改变线上有效结果
      Given 平台已发布 smart_diagnosis
      When 租户管理员创建同 code 的覆盖草稿但未发布
      Then 该租户仍返回平台默认版本
      And 草稿不出现在有效列表

    Scenario: 智能检测要求先选择订单
      Given 已发布 smart_diagnosis 快捷动作且 requires_order 为 true
      When 前端读取 GET /v1/shortcuts
      Then 返回该动作的稳定 code 和 requires_order 元数据
      And 前端选择订单后复用 POST /v1/assistant/questions

    Scenario: 故障上报第一版只收集故障描述
      Given 已发布 report_fault 快捷动作
      When 用户点击该动作但未提供故障描述
      Then 返回需要 fault_description 的 clarification
      And 不创建工单或其他业务写入

  Rule: 宣传内容与客服 FAQ 隔离

    Scenario: 客户案例走宣传知识库卡片
      Given 已发布 case_exploration 快捷动作和宣传 Agent
      When 用户点击案例入口或提交客户案例问题
      Then 返回结构化案例卡片或可轮询的宣传 QA 结果
      And 不进入客服 FAQ
      And 结果不泄露知识库内部标识

    Scenario: 全局宣传动作按租户绑定执行
      Given 平台已发布 case_exploration 且租户 A 有自己的已发布宣传 Agent
      And 租户 B 没有宣传绑定
      When A 和 B 使用同一 shortcut_code 提交客户案例问题
      Then A 只使用租户 A 的 Agent/知识库
      And B 返回本地化诚实空卡片
      And B 不回退到租户 A 的 Agent、知识库或媒体

    Scenario: 错误宣传绑定不跨租户兜底
      Given 租户 A 的覆盖引用了只属于租户 B 的 Agent/version
      When 租户 A 执行全局 case_exploration
      Then 服务端返回诚实空卡片或不可用结果
      And 不返回租户 B 的内容、媒体、指标或内部标识

Feature: 41 快捷动作数据迁移
  迁移将既有租户复制动作收敛为平台默认与租户覆盖，保留审计和回滚能力。

  Rule: 迁移安全且可重复

    Scenario: 迁移前备份并预演
      Given 41 Gateway 使用已确认的 SQLite 数据库
      When 管理员先创建精确数据库备份并运行 aiops admin migrate-shortcuts --dry-run
      Then 备份文件可恢复且预演只输出待创建动作
      And SQLite 数据库内容与文件校验和不变

    Scenario: 已发布租户动作收敛为平台默认
      Given 两个租户均有同一 consumer code 的已发布复制动作
      And 至少一个复制动作绑定了租户专属宣传 Agent/version
      When 管理员运行 aiops admin migrate-shortcuts
      Then 创建一个已发布平台默认且平台版本不携带租户 Agent/version
      And 原租户动作及其历史发布版本保持不变

    Scenario: 迁移失败不留下半完成平台草稿
      Given 平台默认尚不存在且发布操作失败
      When 管理员运行迁移
      Then 迁移报告失败且平台作用域不留下 draft 或 disabled 残留
      And 原租户动作保持可读

    Scenario: 重复迁移与恢复
      Given 一次迁移已成功完成
      When 管理员再次运行迁移并按备份执行恢复演练
      Then 二次运行不产生新平台动作、版本漂移或租户记录
      And 恢复后既有租户动作和发布版本仍可通过有效列表读取

    Scenario: 41 双租户公网验收
      Given 迁移提交已部署到 41 且存在有覆盖与无覆盖的真实会话租户
      When 分别读取 consumer/operator 快捷动作并执行宣传与 smart_diagnosis 入口
      Then 无覆盖租户看到平台默认，有覆盖租户覆盖优先，停用只影响自身
      And 宣传 Agent/知识库不跨租户，smart_diagnosis 仍执行订单归属校验

Feature: 快捷动作跳转站内页面
  一条产品快捷动作可以携带可选的站内跳转路径。路径非空即跳转动作：由客户端导航到
  对应页面，不经过统一助手入口、不创建任何作业；路径为空即既有提示动作。

  Rule: 跳转路径是跳转动作的唯一判别依据

    Scenario: 列表对跳转与提示动作返回同一形状
      Given 租户已发布一条带跳转路径的 report_fault 动作
      And 租户已发布一条不带跳转路径的 case_exploration 动作
      When 客户端读取 GET /v1/shortcuts
      Then report_fault 的 jump_path 等于配置的站内路径
      And case_exploration 的 jump_path 字段存在且为 null
      And 客户端无需区分“字段缺失”与“字段为空”

    Scenario: 既有动作不因新字段改变行为
      Given 41 上已发布四条不含 jump_path 的历史动作
      When 客户端读取 GET /v1/shortcuts
      Then 每条动作的 jump_path 均为 null
      And 其余字段、多语言文案与 language 回显与变更前一致
      And 不需要重新发布这些动作

    Scenario: 跳转路径在发布后不可变
      Given 一条已发布动作的 jump_path 为路径 A
      When 管理员派生根草稿后将其改为路径 B 并发布
      Then 有效列表返回路径 B
      And 历史发布版本快照仍记录路径 A

  Rule: 跳转路径格式受限但不做存在性校验

    Scenario: 路径必须以斜杠开头
      Given 管理员提交 jump_path 为 charge/pages/faultReport
      When 调用创建或更新快捷动作接口
      Then 请求被拒绝且错误码为 SHORTCUT_VALIDATION_FAILED

    Scenario: 路径留空表示提示动作
      Given 管理员创建动作时省略 jump_path
      When 读取该动作
      Then jump_path 为 null 且该动作按提示动作工作

    Scenario: 跳转路径与宣传绑定互斥
      Given 管理员在创建请求中同时提交 jump_path 与 target_agent_version
      When 调用创建快捷动作接口
      Then 请求被拒绝且错误码为 SHORTCUT_VALIDATION_FAILED
      And 单独提交其中任意一个仍然成功

    Scenario: 后端不校验页面是否存在
      Given 管理员提交一个格式合法但客户端并不存在的站内路径
      When 读取 GET /v1/shortcuts
      Then 服务端原样返回该路径且不报错
      And 页面可达性由客户端验收，不属于后端验收范围

  Rule: 跳转动作永不经过统一助手入口

    Scenario: 跳转动作误打入口时同步澄清且不建作业
      Given 用户点击了一条跳转动作
      And 客户端错误地把该动作提交到 POST /v1/assistant/questions
      When 服务端处理该请求
      Then 返回 HTTP 200 且 type 为 clarification
      And 不创建问答作业也不创建诊断作业

    Scenario: 跳转动作不会因为带订单上下文而启动诊断
      Given 请求同时携带跳转动作的 shortcut_code 和一个属于本人的订单号
      When 服务端处理该请求
      Then 仍返回 type 为 clarification
      And 不启动诊断作业

    Scenario: 提示动作行为无回归
      Given 用户点击智能检测（requires_order 的提示动作）
      When 请求缺少订单上下文
      Then 仍返回 type 为 clarification 且 missing_fields 包含 order_no
      And 当订单上下文齐备且属于本人时仍返回 type 为 diagnosis

    Scenario: 未知或未发布的 code 保持既有回退
      Given 用户提交一个未发布的 shortcut_code
      When 服务端处理该请求
      Then 该 code 被忽略且问题按普通提问处理

Feature: 诊断答案的输出语言合同

  诊断答案面向终端用户阅读。请求了非中文语言时，答案里不得残留中文——
  尤其是原样附带的中文枚举值。该合同由确定性校验器强制执行，不依赖模型自觉。

  Rule: 非中文请求的答案不含中文

    Scenario: 原样附带的中文停因被判为不合格
      Given 一次诊断请求的 Accept-Language 为 en
      And 模型把停因译为 "balance exhausted, order stopped"
      And 答案同时原样引用了存储值 "余额耗尽停止订单"
      When 结果校验器校验该答案
      Then 返回校验错误，指出结果语言为 en 但仍含中文字符
      And 该答案不被当作最终结论交付

    Scenario: 只翻译、不附带原文的答案通过
      Given 一次诊断请求的 Accept-Language 为 en
      And 答案把停因译为 "balance exhausted, order stopped"
      And 订单号、证据 ID、字段名、状态码与时间戳按存储原样保留
      When 结果校验器校验该答案
      Then 不产生语言相关校验错误

    Scenario: 中文请求不受影响
      Given 一次诊断请求的 Accept-Language 为 zh
      When 结果校验器校验一份中文答案
      Then 不产生语言相关校验错误

  Rule: 校验失败走既有的合同修复回路

    Scenario: 语言不合格时要求模型修正而非直接交付
      Given 模型返回了一份含中文枚举的英文诊断
      When 校验失败且重试次数未达上限
      Then 将校验错误回送给模型并在同一会话内要求修正
      And 修正后仍不合格则按 blocked 处理，不输出中文残句

Feature: 模型端点兼容适配器

  统一模型端点对 Codex 发出的 Responses 请求有两处已知拒绝，客户端（Codex）
  无法感知也无法自我修正。适配器是唯一的修复点：它在转发前重写请求，
  客户端全程无感。

  Rule: 有工具时不得同时要求受约束输出

    Scenario: 工具与语法同时出现时丢弃语法
      Given 一个 /responses 请求同时声明了 tools 和 text.format 受约束语法
      When 适配器转发该请求
      Then 该请求不再携带 text.format
      And tools 原样保留
      And 其余 text 字段（如 verbosity）不受影响

    Scenario: 只有语法没有工具时保持原样
      Given 一个 /responses 请求声明 text.format 但没有 tools
      When 适配器转发该请求
      Then 请求体逐字节不变

    Scenario: 空工具列表不算声明工具
      Given 一个 /responses 请求的 tools 为空数组且带受约束语法
      When 适配器转发该请求
      Then 请求体逐字节不变

  Rule: 两处修补在同一请求上互不干扰

    Scenario: 历史项缺 status 且同时有语法冲突
      Given 一个请求既有缺少 status 的 input 历史项又同时带 tools 与语法
      When 适配器转发该请求
      Then 历史项补上 status
      And 受约束语法被丢弃
      And 两处修补都计入修补计数

Feature: 回答面输出语言契约收口

  非中文请求的回答不得包含中文字符。该判定是**共享的单一实现**，由各回答面在
  自己的定稿点调用；此前只有标准诊断一个面有校验，因此同类缺陷被逐面发现、
  逐面修复，兄弟面长期破损。

  校验只能证明"没漏中文"，**不能**证明"译得对"——后者不可由模式判定。

  Rule: 泄漏的回答不交付，改发本地化兜底

    Scenario: 英文请求的客户案例回答仍含中文
      Given 一次英文请求命中客户案例路径
      And 模型输出的卡片正文含中文小标题
      When 该回答在定稿点被校验
      Then 不交付该正文
      And 返回英文的不可用文案
      And 检索状态标记为 unavailable

    Scenario: 干净的非中文回答原样交付
      Given 一次英文请求
      And 模型输出的正文为纯英文
      When 该回答在定稿点被校验
      Then 原样交付，不触发兜底

    Scenario: 中文回答不被判为泄漏
      Given 一次中文请求
      When 回答含中文
      Then 不触发兜底（中文是默认语言，中文回答是正确结果）

    Scenario: 每种受支持的非中文语言都被覆盖
      Given 请求语言分别为 en、de、fr、es、pt
      When 模型输出含中文
      Then 五种语言各自触发兜底，且兜底文案为对应语言

  Rule: 资源文件名按形态豁免，不按字段放行

    Scenario: 媒体标题中的中文文件名不判为泄漏
      Given 英文回答引用媒体资源
      And 该资源的标题为中文文件名（如 新加坡无人电动巴士.mp4）
      When 校验该回答
      Then 不判为泄漏

    Scenario: 文件名旁边的中文散文仍判为泄漏
      Given 英文回答同时含中文散文与中文文件名
      When 校验该回答
      Then 判为泄漏（豁免按值生效，不按整条回答放行）

  Rule: 快捷动作缺翻译不再静默

    Scenario: 存量行缺某语言
      Given 一条已发布快捷动作的某语言文案缺失
      When 以该语言读取列表
      Then 仍返回中文回退值（用户按钮不空白）
      And 记录结构化告警，含租户/动作/字段/缺失语言
      And 告警不记录文案正文

    Scenario: 覆盖度缺口是确定性失败
      Given 一条快捷动作缺某受支持语言
      When 运行覆盖度检查
      Then 失败并逐条列出缺失的字段与语言
      And 中文（默认语言）永不报告为缺口

  Rule: 草稿预览按 agent 语言运行

    Scenario: 英文 agent 的草稿预览
      Given 一个英文 agent 的草稿
      And 请求未声明语言
      When 运行草稿预览
      Then 预览以已解析的语言运行（而非隐式落到中文）

Feature: 持续部署到服务主机

  Rule: 部署只在合并到 main 且改动影响运行时时发生

    Scenario: 纯文档合并不触发部署
      Given 一个只改 docs/ 的提交合并到 main
      When CD workflow 评估触发条件
      Then 不产生部署运行

    Scenario: 影响运行时的合并触发一次受门控的部署
      Given 一个改动 src/ 的提交合并到 main
      When CD workflow 运行
      Then 该运行绑定到 production-41 环境
      And 在有人批准该环境之前不写入 41

  Rule: 部署可自证跑了哪个 commit

    Scenario: 部署后 /health 报出本次 commit
      Given 已完成一次 `--commit <sha>` 部署
      When 读取 41 本机的 /health
      Then version 等于 `<semver>+<short-sha>`
      And 服务为 active

    Scenario: 回滚后 /health 报出被回滚到的 commit
      Given 已对上一个已知良好 commit 执行 `--rollback-to`
      When 读取 41 本机的 /health
      Then version 含该 commit 的 short sha

  Rule: 身份与产物受策略门约束

    Scenario: 写入服务主机必须携带产物身份
      Given 一次对 41 的产物上传
      When 声明的 artifact-sha256 与载荷实际 sha 不符
      Then dev-host 拒绝且不写入（退出 77）

    Scenario: CD 用专用身份而非人工身份
      Given CD 执行一次部署
      When 解析其使用的 ssh 别名
      Then 别名指向 CD 专用密钥
      And 人工别名仍指向人工密钥

  Rule: 拒绝即无副作用

    Scenario: 环境前置不满足时以专用退出码失败
      Given 缺少 tomllib 的解释器（python3 < 3.11）
      When 运行 deploy-41.sh
      Then 以 65（环境问题）退出，区别于部署失败
      And 不上传、不写入、不重启

    Scenario: 解包目录残留不得进入生产
      Given /tmp/sync-check 留有上次中断部署的残留文件
      When 执行部署
      Then 残留文件不出现在 41 的源码目录
      And 文件数与本地一致

  Rule: 部署不等于业务验收

    Scenario: CD 通过不宣称业务验收
      Given 一次成功的 CD 部署
      When 报告交付状态
      Then 最多为 merged_waiting_deploy
      And 真实端到端仍须按 runbook §5 由人用业务方 thirdSession 执行
