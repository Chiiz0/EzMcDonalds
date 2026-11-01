# EzMcDonald's

让个人 Agent 使用用户自己授权的麦当劳中国账号，选择门店和餐品，核对优惠，完成到店取餐下单与麦钱包付款。

[![Tests](https://github.com/Chiiz0/EzMcDonalds/actions/workflows/ci.yml/badge.svg)](https://github.com/Chiiz0/EzMcDonalds/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**当前版本 `0.1.0a18`：本地 Python CLI + 36 个 stdio MCP 工具。** 公开核心已通过 MCP 完成一笔真实早餐卡自取订单和 ¥12.60 麦钱包付款；重启后重复调用返回同一笔订单，没有再次扣款。项目提供多账号隔离、本地短信登录、会话维护、城市/店名搜索、门店时段可用券、套餐与配料偏好、实时选餐方案、消费策略和防重复日志。

这是开发者 alpha。**本地短信登录已实测成功，但依赖用户自己已接入的官方设备上下文**；空白机器首次接入仍需官方 App 登录及授权采样，已有账号支持按需会话刷新；已有 Mac 账号可用 `sampling-capture` 无订单采集运行字段，仍需操作官方 App，尚不能自动跨日更新。支持普通单品、部分套餐组件选择，以及官方 App 已配置的购物车；不是所有门店、商品和优惠均已验收。真实适配当前依赖指定 macOS 官方客户端构建。详见[能力矩阵](docs/capabilities.md)。本项目独立开发，非麦当劳官方项目。

前一天的加购或取餐准备结果不明时，可以[找回并结束已过时的本地意图](docs/cart.md#过了取餐时间仍有未核对记录)，再查看新餐的购物车。旧请求始终留在日志中，不会因换日或重启自动重发；结束本地意图不会清空官方购物车或取消订单。

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

## 开始使用

已有会员可先运行 `ezmcd doctor` 检查环境与账号。个人 Agent 可以按城市和店名找店，读取菜单、卡券概况，保存套餐偏好并调用 `meal_plan` 生成实时选餐方案。

[获取自己的凭据](docs/credentials.md) · [无订单更新运行字段](docs/guarded-capture.md) · [偏好与选餐方案](docs/meal-planning.md) · [会话维护](docs/session-maintenance.md)

## 连接自己的账号

先在官方 App 登录自己的手机号，再按[接入文档](docs/onboarding.md)准备本地采样。Mac 用户可复制一条余额请求后运行下面命令，无需整理 HAR；也可保留原有文件导入方式。命令验证签名、查询当前余额，仅保存必要会话字段。手机号标签不是认证凭据。

```sh
# 在本机调试工具中复制余额请求为 cURL/原始 HTTP 请求后运行
.venv/bin/ezmcd account connect personal --clipboard
```

复制的命令只作为文本解析，不执行、不发给 Agent；结束后清理剪贴板、恢复代理并移除临时证书。需要重复采样的 Mac 用户可以显式安装[仅限官方 API 主机的持久采样证书](docs/sampling-certificate.md)，后续复用，停用时撤销。[复制请求导入与更新说明](docs/request-import.md)。文件方式如下：

```sh
# HAR 文件只保留自己明确授权的会话，权限必须为 0600
chmod 600 /absolute/path/account.har
.venv/bin/ezmcd account connect personal --har /absolute/path/account.har
.venv/bin/ezmcd account check personal
```

已有验证过的会话可用 `account import --profile ...`。真实下单还要求 24 小时内的官方 v5 运行字段；可导入同会话样本，或在本机显式复用同一官方设备字段的近期样本并做 v5 只读核验。缺失时仍可找店、选餐、加购和报价。**当前无法承诺一次接入后长期无人值守点餐。** 不应为取得采样而额外创建订单；接入格式、依赖和更新流程见[账号接入](docs/onboarding.md)。

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

1. `capabilities` → `accounts_list` → `account_diagnose`；已有刷新令牌时可调用 `account_refresh` 维护会话。
2. `stores_search` / `stores_favorites` → `pickup_slots` → `membership_assets` / `menu_list`；指定优惠券可用 `coupons_list` → `coupon_products` 查看对应商品。
3. `product_detail` 查看套餐轮次和组件；`cart_add_configured` 添加明确选择的商品到空购物车。已有餐品先 `cart_get` 查看，用户要重新选餐时可 `cart_clear` 后再加购；复杂定制仍可在官方 App 配置。
4. 加购返回 `MATCHED` 后继续；断线或重启用 `cart_add_status` 找回原请求，不换键重加。非空车用 `pickup_methods` 核对取餐柜或柜台自取。所需方式已选中时用 `checkout_quote`；可用但未选中时，用新鲜 `cart_ref` 和稳定键调用 `checkout_quote_pickup`，其 `READY` 结果就是待审阅报价。常用餐品可保存偏好并用 `meal_plan` 生成草案。报价最多有效120秒；余额不足仍展示金额和付款缺口。
5. 本地策略已授权时，`checkout_submit` 创建并支付一单；同一购买使用稳定的 `idempotency_key`。
6. `order_status` 查询订单与取餐号。支付成功响应后会短暂等待状态同步；仍为 `UNKNOWN` 时只查询，不换 key 重下。`pickup_code_pending=true` 表示付款已核验、取餐码待生成。

换了聊天或丢失购买回执时，先调用 `orders_list(account, pickup_date)` 找回本项目的购买记录，再用其中的 `attempt_id` 调用 `order_status`。CLI 对应 `ezmcd orders personal --date YYYY-MM-DD`；登录失效或本地撤销后也能先查看记录。列表是历史快照，不包含仅通过官方 App 创建的订单；没有本地记录不等于没有官方订单。详见[订单找回与取餐](docs/orders.md)。

`account_runtime_check` 可单独检查现有运行字段是否通过一次 v5 只读查询，包括历史样本；它不会延长样本期限或授权下单。凭据指引提供请求断点采样方法，运行字段支持 `--clipboard`、`--request-file` 及逐项 `--paste-headers` 导入；订单正文被丢弃，不需要把凭据交给 Agent。

[MCP 工具及调用示例](docs/mcp.md) · [套餐配置](docs/products.md) · [架构](docs/architecture.md)

[优惠券选择](docs/coupons.md)会重新核对券、门店和时段；券失效不自动改成原价。普通冰美式券＋少冰已完成真实加购、¥9.90报价与清空；普通券付款仍待验收。[修改购物车](docs/cart.md)。

普通单品已真实验证两份脆薯饼，以及脆薯饼＋脆香油条的加购、各 ¥17.00 报价和清空。另一门店的柜台自取也已完成显式切换、¥8.50报价及新进程恢复，重复调用没有再次切换。这些测试没有新增购买，柜台自取和跨店真实付款仍待验收。[取餐方式与报价恢复](docs/pickup.md)。

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
