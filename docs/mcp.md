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

## 工具

| 工具 | 作用 | 外部副作用 |
|---|---|---|
| `capabilities` | 版本、支持范围、缺口 | 无 |
| `account_connection_instructions` | 本地接入说明 | 无 |
| `accounts_list` | 别名、脱敏标签、策略 | 无 |
| `account_check` | 会话验证、余额 | 只读 |
| `stores_favorites` | 收藏门店 | 只读 |
| `store_get` | 按代码查店 | 只读 |
| `pickup_slots` | 可预约时段 | 只读 |
| `menu_list` | 门店/时段菜单、名称过滤 | 只读 |
| `product_detail` | 套餐轮次、组件、数量及加价 | 只读 POST |
| `cart_add_configured` | 按实时详情选择组件、单品或卡优惠 | 改空购物车，需幂等键 |
| `cart_get` | 已配置购物车 | 只读 |
| `cart_add_simple` | 空购物车加普通无规格单品 | 改购物车，需幂等键 |
| `checkout_quote` | 明细、优惠、上限、钱包验证 | 外部只读，本机保存报价 |
| `checkout_submit` | 下单＋麦钱包付款＋核对 | **真实消费**，受策略限制 |
| `order_pay` | 支付尚未开始付款的CREATED订单 | **真实消费**；已有尝试只查询 |
| `order_status` | 订单、付款、余额、取餐码 | 只读 |

[工具行为注解](https://py.sdk.modelcontextprotocol.io/servers/tools/)用于提示客户端，不代替本机策略和持久化去重。

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
