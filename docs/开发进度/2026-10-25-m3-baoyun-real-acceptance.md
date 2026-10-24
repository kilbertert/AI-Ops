# M3 baoyun 本人订单成功链

基线1247dc65，分支docs/m3-baoyun-real-acceptance。本片仅记录实际验收，不修改实现。

真实身份→本人订单授权→发布版本→Snail平台→Java节点→baoyun→CPMS快照→原GET已跑通。
跨用户查询404且零run；本人POST 233ms返回202，run约31.47秒完成，succeeded/matched；
独立SQL/CPMS与快照一致。跨用户QA回读404，撤回后本人回读410。两条业务POST无重发。

临时应用、输入、容器、网络与定时器清理通过；生产配置和现役客服内容未改，生产入口仍关闭。
详见[本次验收](../validation/2026-10-25-m3-baoyun-real-acceptance.md)。

下一步为登录公司App的页面验收与正式发布配置；未完成业务验收和常驻开放，不能把API脚本
成功写成App真机、故障诊断准确率或M1–M4整线完成。
