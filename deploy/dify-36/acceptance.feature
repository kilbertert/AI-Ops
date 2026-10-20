Feature: 36 上 Dify 栈的知识面与库卫生
  运营在控制台里既可能自建 vendor 数据集，也可能挂 external 知识库；两者都必须能用同一个
  检索端点问到，且 API 模板的密钥与已删数据集的绑定不得留下可被误用的残留。

  Rule: vendor 数据集的索引必须落在某个向量库上
    Scenario: 上传文档后索引完成
      Given 本实例设置了 VECTOR_STORE 且 weaviate 就绪
      And 一个 vendor 数据集配置了可用的 embedding 模型
      When 运营上传一份文档并等待索引
      Then 文档状态变为 completed
      And embeddings 表出现对应行
      And 不存在 “Vector store must be specified.” 的文档 error

    Scenario: 只起容器而没有配置向量库类型
      Given weaviate 容器在运行
      And 没有设置 VECTOR_STORE
      When 运营上传文档
      Then 文档停在 indexing 并报 “Vector store must be specified.”
      And 部署自检在起容器之前就应失败

  Rule: 同一检索端点同时服务 vendor 与 external 数据集
    Scenario: 用服务 API 检索 external 数据集
      Given 一个 provider 为 external 的数据集绑定了可用的 External Knowledge API
      When 调用 POST /v1/datasets/{id}/retrieve 并给出 query
      Then 返回 200 且 records 来自该外部知识库
      And 每条记录带 content 与 metadata，而不是空的 segment 列表

    Scenario: 外部知识库不可达
      Given external 数据集的绑定存在但上游返回非 200
      When 调用检索端点
      Then 返回错误而不是“命中为空”的成功响应

    Scenario: vendor 数据集的响应形状不变
      Given 一个 vendor 数据集已索引完成
      When 调用检索端点
      Then records 仍带 segment 结构

  Rule: API 模板的密钥不以明文落库
    Scenario: 新建或更新 API 模板
      Given 运营在控制台填入 endpoint 与 api_key
      When 保存成功
      Then 数据库中的 settings 不含该密钥明文
      And 控制台再次打开该模板仍能看到原值（可原样回填）

    Scenario: 检索时使用解密后的密钥
      Given API 模板的密钥已加密存储
      When 检索打到外部端点
      Then 请求头携带的是明文密钥

  Rule: 删除数据集不留孤儿绑定
    Scenario: 删除一个 external 数据集
      Given 该数据集有一条 external_knowledge_bindings 记录
      When 删除该数据集
      Then 对应的绑定记录一并消失
      And API 模板的 dataset_bindings 不再列出它
