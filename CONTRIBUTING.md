# Contributing

欢迎提交问题、协议兼容修复、测试及文档改进。代码按 [MIT](LICENSE) 发布。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e . build
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m unittest discover -s experiments/readonly_runtime -p 'test_*.py'
.venv/bin/python -m build
```

测试使用虚构账号和离线适配器。不要在 CI 中放入真实账号或执行真实购买。针对支付的变更应覆盖发送前日志、回执丢失、重复调用、报价变化与身份隔离；真实消费验收须另有具体授权。

新增商品字段或接口时，请说明来源、支持范围和证据级别。仅发现 URL 或模拟通过不等于真实兼容。保留未知写结果只查询的行为，不能通过刷新 key、清空日志或修改时间戳绕过停止条件。

Issue 和 PR 不应包含手机号、sid、token、卡券标识、订单标识、HAR、官方二进制或签名密钥。协议夹具应手工构造；必要的本机证据不提交。

接手本轮工作请先读[交接说明](docs/handoff.md)。`experiments/readonly_runtime` 只提供源码研究入口，CI 只运行离线合成测试，不安装系统证书、不启动官方 App、不连接真实账号；不要把实验测试通过写成真实凭据导入或付款验收通过。
