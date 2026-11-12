# 2026-11-12：管家运行日志补明细（ADR-0249）

- 用户反馈 Codex 实现的 Java 运行日志详情过简；批准把 0242 白名单拓宽到
  「执行元数据 + 问题全文 + 订单号」，答案/证据/参数/凭据仍不投影。
- 实现：trace 事件加 `detail` 机器码（拒绝/复核/终态原因），投影扩入
  reason/question/order_no/节点/时间线/版本核对/结果指纹/trial/证据工具名；
  问题用同一只读连接按指纹批量读，不碰 `get_assistant_question` 的写入收敛。
- 前端：列表加问题/订单号，详情分块终态原因+执行节点+时间线+版本核对+
  证据工具+逐事件拒绝码；全字段可选兜底，兼容旧后端。
- 独立 Hack 一轮 4 发现全修（evidence 键注入→过滤声明集、trial 判据→
  chat_binding 前缀、reason 上限→128、detail 截断→store 层），复验轮进行中。
- 套件全绿 + verify-aiops-gateway 通过 + ADR 门禁通过；待 Hack 复验回执与
  用户授权后提交/PR。
