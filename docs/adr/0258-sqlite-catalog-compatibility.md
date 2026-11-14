# 258. Schema检查采用SQLite历史兼容目录名

Status: Accepted

95的Python3.13链接SQLite3.26，sqlite_schema名称不可用，候选启动在建空库时失败。采用SQLite官方保留的sqlite_master别名读取同一系统目录，保持结构校验、版本迁移与事务不变。
替代方案是升级宿主SQLite或额外打包数据库运行时；只为一个目录别名增加运维依赖没有必要，先以目标SQLite版本验证现有schema合同。
依据：https://www.sqlite.org/schematab.html 的 Alternative Names，sqlite_master为兼容名称。
