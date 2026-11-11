# EzMcDonald's

让个人 Agent 使用用户自己授权的麦当劳中国账号，选择门店、餐品和优惠，核对报价，在消费授权与预算内完成到店自取和麦钱包付款。

[![Tests](https://github.com/Chiiz0/EzMcDonalds/actions/workflows/ci.yml/badge.svg)](https://github.com/Chiiz0/EzMcDonalds/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**当前版本 `0.1.0a47`：本地 Python CLI + 38 个 stdio MCP 工具，面向已注册会员的开发者 alpha。** 已通过 MCP 完成真实早餐卡自取订单和 ¥12.60 麦钱包付款，包括一笔使用约10天本人运行字段的预约单；重启重复调用未再次消费。更多商品、规格和普通券已有真实加购报价证据；不代表所有门店、商品和优惠都已支持。[能力矩阵](docs/capabilities.md) · [验证记录](docs/validation.md) · [更新日志](CHANGELOG.md)

**开始前先了解三个条件：**

- 真实适配依赖[指定的官方 Mac 客户端构建](docs/onboarding.md#环境要求)。核心需要 Python 3.11+；项目内钱包采样另需 Python 3.12+。Linux 可运行演示和测试，真实付款未验收。
- 首次接入仍需本人官方 App 登录及本机授权。项目内短信登录已实测，但需要本人已有的官方设备上下文，不能只输入手机号就取得账号权限。
- **登录成功不等于可以下单。** 下单还需要本人有效的运行字段和独立消费授权；运行字段尚不能自动续期，无法承诺一次接入后长期无人值守点餐。凭据获取方式见[凭据指南](docs/credentials.md)。

本项目独立开发，非麦当劳官方项目。没有启用定时购买。

## 快速开始

```sh
git clone https://github.com/Chiiz0/EzMcDonalds.git
cd EzMcDonalds
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 完全离线：模拟报价、下单、付款及重复调用
.venv/bin/ezmcd --data-dir .private/demo demo --run
```

尚未发布到 PyPI。演示返回 `simulation=true`，不会连接麦当劳或真实扣费。若要让 Agent 体验演示，可运行 `.venv/bin/ezmcd --data-dir .private/demo mcp-config`；这份配置只连接演示目录。

## 连接自己的账号

先检查本机环境，再按自己的情况选择入口：

```sh
.venv/bin/ezmcd doctor
.venv/bin/ezmcd account guide
```

| 你已有的条件 | 接下来做什么 |
|---|---|
| 官方 Mac App 已登录，项目还没有账号 | 按[首次接入引导](docs/first-connection.md)准备采样依赖、证书和网络扩展，再运行 `sampling-connect personal --manage-app`，只进入麦钱包 |
| 已有同会员的项目账号，需要更新会话 | 在**原数据目录**运行 `sampling-connect personal --update --manage-app`；保留策略和购买日志，见[会话维护](docs/session-maintenance.md) |
| 已有本人官方设备上下文，想通过短信登录 | 在本机运行 `account login ALIAS --device-from EXISTING_ALIAS`，输入手机号和验证码；见[短信登录](docs/sms-login.md) |
| 已有本人单条余额请求或 HAR | 使用 `account connect personal --clipboard`、`--request-file` 或 `--har`；见[复制请求导入](docs/request-import.md)和[账号接入](docs/onboarding.md) |

表中子命令均通过 `.venv/bin/ezmcd` 运行。`personal` 是本地别名，不是手机号或认证凭据。首次保存账号时消费默认关闭；MCP 不接收手机号、验证码或会话令牌。

项目内采样仅匹配麦当劳进程，不修改系统代理；环境不支持就停止。**旧版 `sampling-capture` 已停用，不要提交测试订单取得样本。** 首次证书/扩展授权以及最终接入成功的判断，按本机终端和[首次接入引导](docs/first-connection.md)操作。需要重复采样可使用本人显式授权的[受限持久证书](docs/sampling-certificate.md)。

## 检查能否下单

```sh
.venv/bin/ezmcd doctor --account personal --verify-session
.venv/bin/ezmcd account check personal
```

这两个命令只核验账号和余额，不下单或付款。按返回的阻碍继续处理：

| 检查结果 | 可继续的操作 | 需要补齐的条件 |
|---|---|---|
| 会话有效，缺少下单运行字段 | 找店、看菜单、选券、准备购物车和报价 | 导入本人已有的有效运行样本，见[凭据指南](docs/credentials.md#为什么已登录还可能不能下单) |
| 原运行样本超过默认24小时 | 保留账号和订单，继续可用的查询 | 更新本人证据，或由本机用户选择[显式限期复用](docs/runtime-use.md)；不改原采样日期；约10天样本有单次真实下单付款证据，其他年龄和连续多日使用仍待验证 |
| 会话失效或刷新结果未知 | 离线找回本地购买记录 | 按[会话维护](docs/session-maintenance.md)恢复，不重复发送未知刷新请求 |
| 消费关闭、授权到期或余额不足 | 核对选餐方案、最终报价和付款缺口 | 由本人决定是否授权或在官方入口充值；Agent不能提高额度 |

已注册会员的首次钱包接入和已有账号维护均有实机证据；空白机器安装及长期凭据续期仍有缺口。设备字段的来源和支持范围见[设备上下文](docs/device-context.md)。

`doctor` 和 Agent 的 `account_diagnose` 会给出 `runtime_guidance`：原样本何时到期、当前还剩多久，以及需要恢复账号、核验原设备样本、由本人选择限期复用，还是必须取得新证据。复用选项只描述本机条件，不自动选择最长时限或开启权限。消费策略已到期时，诊断会先引导查询原购买记录。

## 给个人 Agent 使用

真实账号接入后，重新生成使用**真实账号数据目录**的配置：

```sh
.venv/bin/ezmcd mcp-config
```

把输出交给支持 stdio MCP 的个人 Agent。配置包含本机可执行文件及数据目录的绝对路径，不含凭据。默认目录是 `~/.local/share/ezmcdonalds`；如果接入时指定了 `--data-dir` 或 `EZMCD_DATA_DIR`，这里也必须使用同一目录。不要把演示配置误当成真实账号配置，也不要换目录绕过购买日志。[MCP 配置与完整工具列表](docs/mcp.md)

可以这样向 Agent 表达需求：

> 使用 personal 账号，先确认能否下单，再按我保存的早餐偏好准备下一餐。核对门店、具体北京时间、取餐方式、优惠和总额；在我已授权的消费范围内付款，并返回订单状态和取餐码。已有同餐记录时查询原单；缺货、优惠不可用、超预算或需要官方验证时停止并说明。

还没有偏好时，先让 Agent 用 `stores_search`、`pickup_slots`、`menu_list` 和 `product_detail` 核对门店、时段、套餐组件及配料，再调用 `preferences_save` 保存。[偏好与选餐方案](docs/meal-planning.md)说明了早餐卡、指定券名称、组件份数和饮品选择的写法。**保存偏好不会授权消费或创建定时任务。**

## 核对报价并授权购买

Agent 的主要调用顺序：

1. `capabilities` → `accounts_list` → `account_diagnose`，核对当前能力与账号。
2. `meal_plan` 使用已保存偏好，或从实时门店、菜单和卡券查询开始选餐。商品展示价和预估不是付款金额；指定券使用 `coupons_list` → `coupon_products`。
3. `cart_get` 查看现有餐品；空车使用 `cart_add_configured` 加入明确选择的商品。已有内容需要用户决定是否移除，不能直接覆盖。加购结果必须为 `MATCHED`；断线后用 `cart_add_status` 找回原操作。
4. `pickup_methods` 核对取餐方式。所需方式已选中时用 `checkout_quote`；可用但未选中时用新鲜 `cart_ref` 和稳定键调用 `checkout_quote_pickup`。检查商品、组件、优惠、最终金额及 `checkout_blockers`，报价有效120秒。
5. 用户授权范围和本地策略均允许时，`checkout_submit` 创建并支付一单；同一购买始终使用同一个 `idempotency_key`。
6. `order_status` 查询原订单和取餐说明。`UNKNOWN` 时只查询，不换键重下；`pickup_code_pending=true` 表示付款已核验但取餐码未生成。官方“已完成”不单独证明用户已取餐。[订单查询](docs/orders.md)

消费默认关闭。需要交给 Agent 自动购买时，由用户在本机设置限额；下面的命令会开启消费，请按自己的授权范围设置门店和期限：

```sh
.venv/bin/ezmcd account policy personal \
  --enable-checkout --max-order 20.00 --daily-budget 20.00 --max-orders-day 1

# 可添加 --store 七位门店代码，以及 --expires-at 带时区的有效期
# 随时关闭消费，保留查询和记录
.venv/bin/ezmcd account policy personal --disable-checkout
```

额度按北京时间的取餐日统计本项目保留的尝试，未知结果也占用额度；其他入口的消费不计入本项目预算。新订单发送前报价到期，或金融写请求前消费授权到期，会[停止相应请求](docs/orders.md#慢请求期间报价或授权到期)，不会删除原记录或自动重试。麦钱包充值、支付密码或风险验证由本人在官方入口完成。

## 日常维护与恢复

- **换聊天或丢失回执：** `orders_list` 找本地尝试，再 `order_status` 核对原单。官方入口的订单可用 `official_orders_list` / `official_order_detail` 查询；本地列表不是完整官方历史。[找回订单](docs/orders.md)
- **加购或取餐准备中断：** 恢复原操作，不换键重发。取餐时间已过且用户放弃该餐时，可[结束本地过时意图](docs/cart.md#过了取餐时间仍有未核对记录)；这不会清空官方购物车、取消订单或释放未知购买记录。
- **优惠不可用或出现未知商品结构：** 保留用户的选择，不静默改原价、少配料或换餐；按[优惠券](docs/coupons.md)、[套餐配置](docs/products.md)和[取餐方式](docs/pickup.md)说明处理。
- **会话或运行字段到期：** 按[会话维护](docs/session-maintenance.md)和[限期复用](docs/runtime-use.md)处理；一次只读签名查询成功不会自动授权消费或延长样本日期。
- **停用：** `account revoke personal` 移除本地账号凭据并关闭消费，保留身份和日志；它不注销官方服务器会话，也不删除原始 HAR。采样证书由本人显式撤销，见[证书管理](docs/sampling-certificate.md)。

## 实现特点

- 同一会员共享购买日志，不能通过更换别名重置预算。
- 门店、日期、商品和数量来自参数及实时查询，核心不固定某家店或某套早餐。
- 报价核对购物车与确认页的取餐方式、组件、卡券和金额；付款后核对订单、收银台和余额。
- 账号锁、幂等键、购买内容指纹、服务端已有订单检查及发送前持久化日志共同限制重复执行。
- 凭据与消费策略在本机管理；MCP 不提供导入凭据或提高额度的工具。

## 开发

```sh
.venv/bin/python -m pip install build
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m build
```

源码在 `src/ezmcd/`，公开测试在 `tests/`，配置示例在 `examples/`。测试默认离线，不执行真实购买；需要本机私有证据的两项对照测试在干净 checkout 中跳过，Linux 另跳过 macOS 的加密对照。

公开仓库和发行包不含账号、订单记录、HAR、官方安装包、二进制或提取出的签名密钥。[验证记录](docs/validation.md)保留成功、失败及尚未验收的边界；开发历史见[更新日志](CHANGELOG.md)，剩余工作见[路线](docs/roadmap.md)。[贡献说明](CONTRIBUTING.md)

## License

[MIT](LICENSE) © 2026 Chiiz0
