# 知识选库权限缺陷已部署

日期：2026-10-27；实现#1108/69409638/v0.6.1，本记录分支docs/consumer-editor-live。
修复Dify工作区editor打开知识节点403；保持账号权限及原V5不变。
现役同一system/editor端点从403变200并返回3库，隔离editor浏览器选库/保存通过，整链95/95，CI全绿。
首次探活超时已自动恢复，重跑成功；下一步用户刷新原页面确认，不需重发工作流或改知识文件。
详见[线上验收](../validation/2026-10-27-consumer-picker-editor-production.md)。
