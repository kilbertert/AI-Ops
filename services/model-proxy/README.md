# 固定目标模型代理

节点通过本代理访问配置中固定的模型供应商；节点代理令牌与供应商密钥分开保管。
仅允许模型列表和聊天端点，保留原TLS、响应帧化和拒绝规则。本次只迁移源码位置。

```bash
python services/model-proxy/model_proxy.py --help
uv run pytest tests/test_m2_live03_model_proxy.py tests/test_m2_live03_delivery_boundaries.py
```

[镜像构建脚本](../../deploy/m2-live-03/build-image.sh)将源码放到
`/opt/aiops-src/services/model-proxy/model_proxy.py`，
[正式节点Compose](../../deploy/node-runtime/compose.yaml)和[隔离Compose](../../deploy/m2-live-03/compose.yaml)
使用同一路径。新的Compose必须与包含该路径的新镜像一起交付，不能搭配旧镜像。
旧运行中的发布包保持自洽；单纯合并目录变动不重启生产。

部署参数、密钥挂载和网络边界见[正式节点手册](../../deploy/node-runtime/README.md)。
测试使用临时TLS和合成上游，只证明代理契约；不宣称真实模型或业务根因验收。
