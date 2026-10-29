# 管家 H5 制品部署与恢复

适用于已合并的公司管家 H5 与本仓窄代理。方法为本轮退役验收补齐，目录迁移仍等待真实页面通过。

## 制品准备

1. 读公司仓 `docs/operator-h5/HANDOFF.md`，核干净 checkout、实际目标分支和当前远端修订。
2. 从受控配置运行 `python tools/build-h5.py --config-file <私有JSON>`，随后运行
   `python tools/verify-h5.py --output <私有证据目录>`。该浏览器检查使用合成业务响应。
3. 清单必须 `dirty=false`、`source_commit` 等于所选合并修订。包内保留完整 H5 与
   `build-manifest.json`；本仓脚本、两个代理片段从合并 main 打包，均计算 SHA-256。
4. 用 `.dev-host.toml` 声明的 `41` 与 `dev-host cp --artifact-sha256` 传送到备份/制品区域；
   不复制私有构建配置、账号或令牌。远端先复核摘要，再解包受控制品。

## 安装与验收

安装前记录站点真实访问流量、现有静态根、Nginx配置与回滚位置；不用监听状态推断使用情况。
主机路径由运维清单确定，不从临时目录猜测。读取vhost和其include/rewrite片段，确认没有优先的`location ^~ /v1/`；安装后逐条验证7种方法/路径。保留原vhost的TLS、已有安全规则与非助手入口。
执行形式（制品目录同时包含 `deploy/` 与 `ops/nginx/`）：

```bash
python3 deploy/operator-h5.py \
  --build <解包的H5目录> --manifest <构建清单> --revision <公司合并SHA> \
  --release <新的静态发布目录> --vhost <既有站点配置> \
  --extension <既有extension目录下的助手conf> --proxy <独立proxy.inc> \
  --backup <新的私有备份目录> --nginx <既有nginx二进制>
```

脚本先核制品集合和每个摘要，保存原配置，再切换静态根。`nginx -t` 后 reload，
失败恢复原配置。旧静态根不删除；仅旧static/js、static/css下带8位十六进制哈希的JS/CSS分块复制到新根的缺失位置，支持已打开页面，新制品同名文件优先。配置里的 company gateway 保持既有本地入口；
个人 Bearer 原样传递，入口固定 operator，外部来源密钥/两种第三会话头清空。
仅7种方法/路径组合可转发，管理端点不开放；失败不重试，响应不缓存。
此映射依据 [Nginx 官方代理规范](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)：
正则location里的无URI `proxy_pass` 保留路径；`proxy_set_header` 空值删除头。

安装后按 [QA计划](../acceptance/operator-h5-deploy/qa-plan.md) 由正常管家登录驱动首页和助手，
保存页面、POST/GET、同一QA/run和独立CPMS对照。仅健康成功时仍写“未完成业务验收”。
恢复命令：

```bash
python3 deploy/operator-h5.py --restore --backup <本次备份目录> --nginx <既有nginx二进制>
```

恢复不删除新制品，以便取证；静态版本清理另作退役决定。公司仓按其自身MR规则交付，
本仓不改写外部仓库的保护、钩子或分支策略。
