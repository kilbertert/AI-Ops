# 控制台页面验收通道（开发机 → 36）

开发机到 `dify.ranlei.work:443` 的 TLS 握手会被重置（2026-11-04 实测 5 次全失败），
而 36 本机可达。要用**真实登录会话打开控制台页面**做验收时，走这条本地通道。

## 起

```bash
# 1) 隧道：只绑本机回环；关掉终端即断
ssh -N -L 18443:127.0.0.1:443 yidong-36 &

# 2) CONNECT 代理：只绑 127.0.0.1，只转发到上面那个隧道端口
uv run python tools/verification/dify-console-tunnel/proxy.py --port 18080 &

# 3) 截图（--expand 展开「复制并开通客户 AI 服务」面板）
uv run python tools/verification/dify-console-tunnel/capture.py --out /tmp/panel.png --expand
```

浏览器 URL 与 `Origin` 仍是 `https://dify.ranlei.work`，所以同源桥的校验照常生效。
**不要**直接访问 `https://dify.ranlei.work:18443` —— 那样 `Origin` 带端口，桥会拒绝
（「请从Dify控制台操作」）。这条差别是这套通道存在的理由。

## 关

```bash
pkill -f 'tools/verification/dify-console-tunnel/proxy.py'
pkill -f '18443:127.0.0.1:443'
pgrep -af 'proxy.py|18443:127.0.0.1:443'   # 必须无输出
```

## 纪律

- **它不修复网络**：隧道一关即失效。用它产出的证据必须写明「经取证通道」。
- 会话文件是库外 0600 资产，`capture.py` 只读不打印；不要把它复制进仓库。
- 代理无鉴权，只能绑回环（脚本会拒绝非回环地址）。
- 验收结束必须把隧道和代理都关掉再离开，别留给下一个人。
