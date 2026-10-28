Feature: C端客服跟随App语言设置
  Rule: 页面声明的语言不能阻断已经接通的客服能力
    Scenario Outline: 直接提问与订单快捷检测
      Given 用户正常登录并选择<language>，有本人可见订单
      When 用户直接查询或点击订单检测快捷指令
      Then 请求进入相应能力且答案使用所选语言
      And 不出现不支持语言提示，不扩大订单权限
      Examples:
        | language |
        | zh       |
        | zh-Hant  |
        | en       |
        | de       |
        | fr       |
        | es       |
        | pt       |
        | vi       |
        | mn       |
        | th       |
        | km       |
  Rule: 知识和实时回答都遵守显示语言
    Scenario: 选择蒙古语后用中文问知识
      Given 当前所选库有相关中文资料
      When 使用中文提问
      Then 回答用蒙古语并引用本轮来源
    Scenario: 切换语言后读取历史回答
      Given 已用泰语完成问答
      When 改为蒙古语读取历史回答
      Then 已保存的回答与其语言仍为泰语
    Scenario: 跨用户查询
      Given 用户没有另一张订单的权限
      When 在任意支持语言下请求该单
      Then 不交付订单数据
