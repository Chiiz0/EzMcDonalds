# EzMcDonald's

让个人 Agent 使用用户自己授权的麦当劳中国账号，选择门店和餐品，核对优惠，完成到店取餐下单与麦钱包付款。

[![Tests](https://github.com/Chiiz0/EzMcDonalds/actions/workflows/ci.yml/badge.svg)](https://github.com/Chiiz0/EzMcDonalds/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**当前版本 `0.1.0a3`：本地 Python CLI + 17 个 stdio MCP 工具。** 原型已完成一笔真实的纯脚本早餐卡下单付款；项目提供多账号隔离、本地短信登录、消费策略、通用结算、防重复日志及无需账号的离线演示。

这是开发者 alpha。**本地短信登录已实测成功，但依赖用户自己已接入的官方设备上下文**；空白机器首次接入仍需官方 App 登录及授权采样，已有账号支持按需会话刷新；下单运行字段尚不能自动更新。支持普通单品、部分套餐组件选择，以及官方 App 已配置的购物车；不是所有门店、商品和优惠均已验收。真实适配当前依赖指定 macOS 官方客户端构建。详见[能力矩阵](docs/capabilities.md)。本项目独立开发，非麦当劳官方项目。

## 快速开始

Python 3.11+，macOS 或 Linux。真实付款目前仅在指定 macOS 环境有验证证据；Linux 可运行演示和测试。

```sh
git clone https://github.com/Chiiz0/EzMcDonalds.git
cd EzMcDonalds
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 完全离线：模拟报价、下单、付款，再次调用验证未重复消费
.venv/bin/ezmcd --data-dir .private/demo demo --run

# 生成可粘贴到个人 Agent 的 stdio MCP 配置
.venv/bin/ezmcd --data-dir .private/demo mcp-config
```

尚未发布到 PyPI。演示始终返回 `simulation=true`，不会连接麦当劳或真实扣费。

## 连接自己的账号

先在官方 App 登录自己的手机号，再按[接入文档](docs/onboarding.md)准备本地采样。可导入 HAR 或单次余额请求样本；命令验证签名、查询当前余额，仅保存必要会话字段。手机号标签不是认证凭据。

```sh
# HAR 文件只保留自己明确授权的会话，权限必须为 0600
chmod 600 /absolute/path/account.har
.venv/bin/ezmcd account connect personal --har /absolute/path/account.har
.venv/bin/ezmcd account check personal
```

已有验证过的会话可用 `account import --profile ...`。真实下单还要求同会话、24 小时内的官方 v5 运行字段；缺失时只能查询。**当前无法承诺一次接入后长期无人值守点餐。** 不应为取得采样而额外创建订单；接入格式、依赖和更新流程见[账号接入](docs/onboarding.md)。

已有本人官方设备上下文后，可为自己控制的已注册会员账号执行纯脚本短信登录：

```sh
# 手机号和验证码在本机终端输入，不回显、不写入命令参数
.venv/bin/ezmcd account login another-account --device-from personal
```

设备字段的来源、可独立生成的部分和仍需验证的依赖见[设备上下文](docs/device-context.md)。已有刷新令牌的账号可运行 `ezmcd account refresh personal`，详见[会话维护](docs/session-maintenance.md)。

这里只沿用设备字段，不使用旧账号会话；新会话通过号码绑定和钱包只读核验后保存。新账号消费默认关闭，同一会员重新登录保留原策略和日志，旧下单运行字段不复制。详见[短信登录](docs/sms-login.md)。

消费默认关闭。用户在本机授权个人 Agent 自动购买时，设置明确限额：

```sh
.venv/bin/ezmcd account policy personal \
  --enable-checkout --max-order 20.00 --daily-budget 20.00 --max-orders-day 1

# 可添加 --store 七位门店代码，以及 --expires-at 带时区的有效期
.venv/bin/ezmcd account policy personal --disable-checkout

# 移除本地账号凭据并关闭消费，保留订单日志
.venv/bin/ezmcd account revoke personal
```

额度按取餐日（Asia/Shanghai）统计本工具保留的尝试，未知结果也占用额度。其他入口的消费不在本工具预算内。`revoke` 不撤销官方服务器上的登录，也不删除用户原始 HAR 文件。

## 给个人 Agent 使用

运行 `.venv/bin/ezmcd mcp-config`，将输出配置到支持 stdio MCP 的客户端。入口也可使用 `ezmcd-mcp` 或 `python -m ezmcd.mcp_server`。账号、凭据和日志默认存于 `~/.local/share/ezmcdonalds`，可用 `EZMCD_DATA_DIR` 指定。

推荐调用顺序：

1. `capabilities` → `accounts_list` → `account_check`；已有刷新令牌时可调用 `account_refresh` 维护会话。
2. `stores_favorites` / `store_get` → `pickup_slots` → `menu_list`。
3. `product_detail` 查看套餐轮次和组件；`cart_add_configured` 添加明确选择的商品到空购物车。复杂定制可先在官方 App 配置，再调用 `cart_get`。
4. `checkout_quote` 核对商品、优惠和最终金额，报价有效期 120 秒。
5. 本地策略已授权时，`checkout_submit` 创建并支付一单；同一购买使用稳定的 `idempotency_key`。
6. `order_status` 查询订单与取餐号。未知结果只查询，不换 key 重下。

[MCP 工具及调用示例](docs/mcp.md) · [套餐配置](docs/products.md) · [架构](docs/architecture.md)

## 实现特点

- 同一会员身份共享订单日志，不能通过更换别名重置预算。
- 门店、餐段、日期、商品、份数和上限均由参数与实时菜单决定。
- 核对购物车和确认页的门店、取餐方式、组件、卡券及金额；展示价不是付款金额。
- 预支付后获取新钱包令牌，付款后核对订单、收银台和余额；官方验证要求交回官方入口。
- 账号锁、幂等键、购买内容指纹、服务端已有订单检查、发送前持久化日志共同限制重复执行。
- 凭据和消费策略只通过本地 CLI 管理；MCP 不提供导入凭据或提高额度的工具。

## 开发

```sh
.venv/bin/python -m pip install build
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m build
```

`src/ezmcd/` 包含协议适配、账号、商品、状态机、存储、CLI 与 MCP；`tests/` 为公开测试；`examples/` 提供配置示例。测试默认离线，不执行真实购买。需要私有历史证据的两项对照测试在干净 checkout 中明确跳过。

公开仓库和发行包不含账号、订单记录、HAR、安装包、官方二进制或提取出的签名密钥。参见[验证记录](docs/validation.md)、[路线](docs/roadmap.md)和[贡献说明](CONTRIBUTING.md)。

## License

[MIT](LICENSE) © 2026 Chiiz0
