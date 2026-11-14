# 95升级服务身份

分支fix/env95-upgrade-service-identity，基线bf3bf80f。真实升级在停机前被配置属主保护拒绝，旧服务未停。
补齐系统安装与应用检查身份分工；只改check_state执行账号，合并后重新构建完整制品复验。
