Feature: 知识库图文入库与交付
  Rule: 配置必须在使用它的租户内解析
    Scenario: 修复跨租户的视觉模型默认值
      Given 租户默认视觉模型指向另一租户的模型
      When 使用该租户自己的 provider 和模型设置默认值
      Then 该租户的真实模型解析返回 OpenAI 的 gpt-4o-mini
      And 图片识别请求返回与图片一致的内容

  Rule: 图片的类型不由父文档扩展名决定
    Scenario: Word 文档内嵌图片进入回答
      Given 授权库的检索片段来自 docx 并携带 image_id 且未声明 MIME
      When Gateway 转换知识检索结果并读取签名媒体
      Then 返回图片资源与经过字节识别的图片响应
      And 未授权库或明确不支持的 MIME 仍不能交付媒体

  Rule: 解析数量不能替代内容证明
    Scenario: 检验上传文档的图文产出
      Given 原文存在可解码图片和损坏图片引用
      When 解析任务进入终态并逐页读取切片
      Then 保留图片应能下载且识别内容可检索
      And 损坏引用必须单独列出，不能声称全部图片已恢复
