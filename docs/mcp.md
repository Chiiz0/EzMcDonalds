# 个人Agent接入

使用[官方MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)，固定依赖2.3.0。当前只开放stdio，由个人Agent启动本机进程，不监听网络端口。

## 配置

安装后运行`ezmcd mcp-config`生成实际路径配置。使用自定义数据目录时，生成配置也指定同一目录。

```json
{
  "mcpServers": {
    "ezmcdonalds": {
      "command": "/absolute/path/to/project/.venv/bin/python",
      "args": ["-m", "ezmcd.mcp_server"],
      "env": {"EZMCD_DATA_DIR": "/absolute/path/to/private/data"}
    }
  }
}
```

配置不含凭据。本机进程可以使用该目录中全部账号，只交给用户信任的个人Agent；没有跨用户HTTP鉴权。

`capabilities.sms_login=true` 表示本地 CLI 已支持短信登录，不表示 MCP 能发送短信。个人 Agent 可用 `account_connection_instructions` 提示用户运行本地命令；手机号和验证码只在终端输入。完成后通过 `accounts_list` / `account_check` 使用新账号。

## 工具

| 工具 | 作用 | 外部副作用 |
|---|---|---|
| `capabilities` | 版本、支持范围、缺口 | 无 |
| `account_connection_instructions` | 本地接入说明 | 无 |
| `accounts_list` | 别名、脱敏标签、策略 | 无 |
| `account_diagnose` | 本地环境、账号状态及后续步骤 | 可选只读余额检查 |
| `preferences_get` | 读取偏好 | 本地只读 |
| `preferences_save` | 保存门店、餐品和取餐偏好 | 本地写入；不授权消费、不创建调度 |
| `meal_plan` | 按实时菜单、时段与偏好准备草案 | 只读，不加购或购买 |
| `membership_assets` | 权益卡有效期、券分类数量 | 只读 |
| `cities_list` | 城市名称及代码 | 只读 |
| `stores_search` | 城市/店名或明确坐标查店 | 只读 |
| `account_check` | 会话验证、余额、下单前置条件 | 只读 |
| `account_refresh` | 已有账号会话维护、恢复核验 | 认证状态变更；不消费，不续期运行字段 |
| `stores_favorites` | 收藏门店 | 只读 |
| `store_get` | 按代码查店 | 只读 |
| `pickup_slots` | 北京时间具体可预约时间、餐段 | 只读 |
| `menu_list` | 门店/时段菜单、名称过滤 | 只读 |
| `product_detail` | 套餐轮次、组件、数量及加价 | 只读 POST |
| `cart_add_configured` | 按实时详情选择组件、单品或卡优惠 | 改空购物车，需幂等键 |
| `cart_get` | 已配置购物车 | 只读 |
| `cart_add_simple` | 空购物车加普通无规格单品 | 改购物车，需幂等键 |
| `checkout_quote` | 明细、优惠、上限、钱包缺口及下单阻碍 | 外部只读，本机保存报价 |
| `checkout_submit` | 下单＋麦钱包付款＋核对 | **真实消费**，受策略限制 |
| `order_pay` | 支付尚未开始付款的CREATED订单 | **真实消费**；已有尝试只查询 |
| `order_status` | 订单、付款、余额、取餐码 | 只读 |

[工具行为注解](https://py.sdk.modelcontextprotocol.io/servers/tools/)用于提示客户端，不代替本机策略和持久化去重。

账号刷新及重复调用规则见[会话维护](session-maintenance.md)。`readiness` 区分可浏览与下单前置条件，不替代实时订单检查。

## 报价请求

`menu_list`、`cart_get`和`checkout_quote`等使用统一的`request`：

```json
{
  "request": {
    "account": "personal",
    "store_code": "1234567",
    "pickup_at": "2026-11-02T10:15:00+08:00",
    "max_amount_cny": "20.00",
    "eat_type": "locker-in"
  }
}
```

门店、日期仅为格式示例，必须使用真实门店与可预约时段。金额为元字符串，内部使用整数分。报价返回商品组件、优惠、最终金额、`quote_id`及过期时间。

用户已授权消费且本地策略允许时，调用`checkout_submit`：

```json
{"account":"personal","quote_id":"returned-quote-id","idempotency_key":"user-breakfast-20261102"}
```

每次用户购买意图使用固定key。相同key不同内容拒绝；同账号同店同时间同商品换key，仍关联原尝试。未知结果不能通过改key、删数据库、重建订单来重试。

## 结果

`PAID`要求订单、收银台及预期钱包差额一致。`UNKNOWN`不是付款失败，只查询；`CANCELLED`优先于历史收银台`PAID`。取消后本地额度和去重不自动解除。密码/安全验证交由官方入口。

下单回执丢失时`order_id`可能为空，保留`attempt_id`，通过官方订单列表人工核对，当前版本不自动猜测关联。`pickup_code=null`表示尚未生成。

`simulation=true`始终表示离线演示，演示店号和取餐号不能实际使用。
