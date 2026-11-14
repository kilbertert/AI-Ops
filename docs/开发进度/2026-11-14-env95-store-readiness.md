# 95内部元数据库就绪修复

任务fix/env95-store-readiness，基线44a136a8。现场网络阻断已定位到firewalld内部网桥登记；
网络通后发现MySQL临时socket阶段被报healthy，补齐TCP身份查询并限制小元库内存，保持原执行链。
当前95Gateway1.10.3已上线；真实准入、独立知识开户与合成检索/跨环境拒绝通过，三业务及原客户端尚未验收。
