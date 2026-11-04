Feature: 并行构建不争写仓库锁文件
  Rule: 构建输入受版本控制，构建记录随制品交付
    Scenario: 两个独立源码改动分别构建
      Given 两个任务基于相同输入锁且修改不同源码
      When 分别构建Gateway镜像并校验各自的identity.json
      Then 两个构建均成功且仓库锁文件逐字不变
      And 每个记录只匹配对应源码与镜像

    Scenario Outline: 错配制品必须阻止启动
      Given 本次构建记录与已批准的基础镜像输入
      When 部署预检遇到<损坏>
      Then 预检返回非零且不启动栈
      Examples:
        | 损坏 |
        | 缺失或非法构建记录 |
        | 镜像ID不符 |
        | 源码摘要标签不符 |
        | 依赖摘要标签不符 |
        | 基础镜像不符 |

    Scenario: 旧构建记录不能验证新源码
      Given 已构建镜像的identity.json
      When 源码或uv.lock改变后对旧记录执行context_digest.py --check
      Then 校验失败并要求重新构建
