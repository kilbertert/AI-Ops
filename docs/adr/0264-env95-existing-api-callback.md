# 264. 95在既有API域名补齐精确Dify回调

Status: Accepted

95旧/v1代理覆盖Authorization为App服务身份，Dify一次性数据授权因此丢失。使用现役回调片段的同一条路径/方法白名单，
仅把上游渲染为95回环Gateway；精确location优先于旧/v1规则，不改变公司其他接口或新增域名。
替代方案是直接放行全部/v1/internal或新建公网入口，扩大范围且无必要；保留现有精确片段避免两套回调定义漂移。
