# 95 SQLite目录兼容

基线0e9edd77，fix/sqlite-schema-compat。95候选启动报no such table: sqlite_schema，旧服务未停止。
共享schema读取改用sqlite_master，保持所有表/约束校验；用官方SQLite3.26隔离运行复现与验证，不修改宿主或业务库。
下一步合并制品后重建95；本片尚未完成业务验收。
