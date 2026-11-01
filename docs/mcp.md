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
| `preferences_save` | 保存门店、餐品、配料和取餐偏好 | 本地写入；不授权消费、不创建调度 |
| `account_runtime_check` | 检查现有 v5 运行字段，包括历史样本 | 固定一次 GET；不续期、不授权下单 |
| `meal_plan` | 按实时菜单、时段与偏好准备草案 | 只读，不加购或购买 |
| `membership_assets` | 权益卡有效期、券分类数量 | 只读 |
| `cities_list` | 城市名称及代码 | 只读 |
| `stores_search` | 城市/店名或明确坐标查店 | 只读 |
| `account_check` | 会话验证、余额、下单前置条件 | 只读 |
| `account_refresh` | 已有账号会话维护、恢复核验 | 认证状态变更；不消费，不续期运行字段 |
| `stores_favorites` | 收藏门店 | 只读 |
| `store_get` | 按代码查店 | 只读 |
| `pickup_slots` | 北京时间具体可预约时间、餐段 | 只读 |
| `pickup_methods` | 非空车初始确认页的可用、默认已选及项目支持的取餐方式 | 只读；不代表已准备报价中的选择 |
| `menu_list` | 门店/时段菜单、名称过滤 | 只读 |
| `coupons_list` | 当前账号、门店和时段的券、适用状态与规则 | 只读 |
| `coupon_products` | 使用当前 coupon_ref 查询对应商品 | 只读 |
| `product_detail` | 套餐轮次、配料、数量及加价；可传 coupon_ref | 只读 POST |
| `cart_add_configured` | 按实时详情选择组件、单品、卡优惠或支持的单品券 | 改空购物车，需幂等键 |
| `cart_get` | 已配置购物车及120秒 cart_ref | 外部只读，本机保存引用 |
| `cart_clear` | 清空用户要移除的整份购物车 | 改购物车；需新鲜引用与幂等键，重复只查询 |
| `cart_add_status` | 找回指定或最新加购，并核对当前车 | 外部只读；本机记录核对结果，不重发 |
| `cart_operations_list` | 分页找回加购与取餐准备，默认只列未解决记录 | 本地只读，不需要登录；不含购买与清空操作 |
| `cart_operation_retire` | 用户放弃取餐时间已过的未解决意图 | 只结束本地意图，保留请求与写入记录；不清车、不取消、不退款、不消费 |
| `cart_add_simple` | 空购物车加普通无规格单品 | 改购物车，需幂等键 |
| `checkout_quote` | 明细、优惠、上限、钱包缺口及下单阻碍 | 外部只读，本机保存报价 |
| `checkout_quote_pickup` | 显式选择所需取餐方式并生成报价 | 一次切换 PUT，需新鲜 cart_ref 与稳定键；不创建订单或扣款 |
| `checkout_quote_pickup_status` | 找回指定或最新取餐报价准备 | 外部只读；核对已保存回执，不重发、不续期 |
| `checkout_submit` | 下单＋麦钱包付款＋核对 | **真实消费**，受策略限制 |
| `order_pay` | 支付尚未开始付款的CREATED订单 | **真实消费**；已有尝试只查询 |
| `order_status` | 订单、付款、余额、取餐码 | 只读 |
| `orders_list` | 按账号、取餐日期、状态分页找回本地购买记录 | 本地只读，无网络；不是实时或完整官方订单列表 |

[工具行为注解](https://py.sdk.modelcontextprotocol.io/servers/tools/)用于提示客户端，不代替本机策略和持久化去重。

需要重新选餐时，先 `cart_get` 查看原内容，再按用户改餐意图用同一请求、`cart_ref` 与稳定键调用 `cart_clear`。确认空车后重新加购并报价。清空不会取消订单；不与官方 App 并发改车。[购物车改餐与重复调用](cart.md)。

加购工具现在返回 `operation_id` 和 `state`。只有 `MATCHED` 才建议获取新报价；`EMPTY`、`DIFFERENT`、`UNAVAILABLE` 都不是换键重试的理由。断线或重启后用 `cart_add_status(account, operation_id?)` 恢复，省略引用时查询该会员最新记录。重复原加购请求只读取当前车并比较原草稿；当前车已被移除也不会自动补加。

跨日后无法用原取餐时间核对时，先 `cart_operations_list` 找回未解决意图。用户放弃这顿餐且 `retirement_available=true`，才可明确 `cart_operation_retire`。旧键和状态查询返回 `RETIRED`，不重发；这不代表原请求未执行。新餐必须查看当前购物车、重新报价并遵守原消费策略。未知购买记录不会因此解除或取消。

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

门店搜索结果和可预约时段不代表支持用户所需的方式。非空车调用 `pickup_methods`，分别读取 `requested_method_available`、`requested_method_selected` 和每项的 `checkout_supported`。支持 `locker-in`（取餐柜）和 `take-in-store`（柜台自取）；堂食和得来速不能替代。所需方式可用但未选中时，先以同一 request 调用 `cart_get`，再把新鲜 cart_ref 与稳定键交给 `checkout_quote_pickup`。其 READY 结果就是待审阅的报价，可交给 checkout_submit，无需再次调用初始 checkout_quote。

切换选择保存在对应报价中，新的初始 GET 可能仍显示默认方式。断线后用 `checkout_quote_pickup_status(account, operation_id?)` 恢复，不据默认 GET 重复切换。查询不延长120秒期限；缺失回执、内容变化或过期时不能提交。空车确认页可能拒绝查询，也不能据此断定门店没有该方式。[详细说明与恢复流程](pickup.md)。

用户已授权消费且本地策略允许时，调用`checkout_submit`：

```json
{"account":"personal","quote_id":"returned-quote-id","idempotency_key":"user-breakfast-20261102"}
```

每次用户购买意图使用固定key。相同key不同内容拒绝；同账号同店同时间同商品换key，仍关联原尝试。未知结果不能通过改key、删数据库、重建订单来重试。

## 结果

`PAID`要求订单、收银台及预期钱包差额一致。`UNKNOWN`不是付款失败，只查询；`CANCELLED`优先于历史收银台`PAID`。取消后本地额度和去重不自动解除。密码/安全验证交由官方入口。

消费接口成功后，最多核对四次同一订单，中间等待1、2、4秒；一旦成功、取消或查询失败即停止等待。等待只查询订单、收银台和余额，不重发订单、预支付或消费。结果给出 `next_action`：`query_order_status` 继续查原订单；`query_pickup_code_later` 表示付款已核验但取餐码待生成；`review_in_official_app` 用于缺少订单回执、无法关联的情况。手动 `order_status` 每次只核对一次。

下单回执丢失时`order_id`可能为空，保留`attempt_id`，通过官方订单列表人工核对，当前版本不自动猜测关联。`pickup_code=null`表示尚未生成。

聊天或进程重启后可用 `orders_list(account, pickup_date?, state?, limit=20, offset=0)` 找回原尝试。日期为北京时间 `YYYY-MM-DD`，状态为大写原始状态，limit 范围1至100，offset 范围0至10000。列表按取餐日期降序、同日按记录顺序倒序；包含未知及取消记录。`source=local_project_journal`、`live_status_checked=false` 表示所有状态、取餐码和余额都是历史记录，下一步必须查询原订单的 `order_status`。`status_checked_at` 是最后成功核对的 Unix 秒时间，旧记录可为空。

本地列表不包含仅在官方 App 创建的订单，返回空列表不构成重新购买的依据。找回记录不需要有效登录；实时查询仍需要同一会员的有效会话。[订单找回与取餐](orders.md)。

`simulation=true`始终表示离线演示，演示店号和取餐号不能实际使用。

`product_detail` 会返回 `modifiers` 支持标记。免费保留/去除配料给出 `code` 与 `quantity`（0 或 1）；单选组给出一个选项的 `code`、`quantity=1` 及实时 `key`。传入时不能漏掉活动组。加购回读未确认配料时停止，不能换 key 重试。规格聚合页的 `variants[].menu_offers` 用于继续查询具体商品，不能直接购买聚合页。详见[商品选择](products.md)。

`coupons_list → coupon_products → product_detail` 使用同一 `request`，详情与选择带上返回的 `coupon_ref`。加购前重新核验，券失效时不回退原价；加购后必须回读确认券。详见[优惠券选择](coupons.md)。

凭据维护仅在本机 CLI 进行：`account connect` 和 `account runtime` 都支持复制的单次 cURL/原始请求文本，Mac 用 `--clipboard`，文件用 `--request-file`；只解析，不执行复制命令。`account runtime` 也可显式检查同设备上下文复用。MCP 不接收运行样本、手机号或验证码，不读取剪贴板，也不提供修改消费策略的工具。[本机导入指引](request-import.md)。
