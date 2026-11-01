# 找回订单与查询取餐码

Agent 换了聊天、重启或丢失上一次回执后，可以从本机记录找回购买尝试。不要为了找回订单再次下单，也不要换幂等键。

```sh
# 查看该会员通过本项目产生的本地记录
ezmcd orders personal

# 按北京时间的取餐日期筛选；日期需替换为自己的安排
ezmcd orders personal --date 2026-11-02 --limit 20 --offset 0

# 使用返回的 attempt_id 查询原订单、收银台和钱包
ezmcd status personal RETURNED_ATTEMPT_ID
```

个人 Agent 使用 `orders_list`，随后对选中的原记录调用 `order_status`。列表返回商品、门店、取餐安排、金额、原订单编号与 `attempt_id`；预支付引用、签名、凭据和请求正文不会返回。如果旧报价记录不可用，仍显示购买尝试并标注 `meal_details_available=false`，不会把它当作从未下单。

列表只读取本机购买日志，明确标记 `live_status_checked=false`。其中的状态、取餐码与余额是历史值；`status_checked_at` 记录最后成功核对时间，旧记录可能为空。列表没有自动连接服务器，不能据此判断订单刚刚被取消或已经出餐。

如果实时查询返回 `PAID`，但 `pickup_code_pending=true`，表示已核验付款、取餐码尚未生成；临近取餐时再查询同一订单。`UNKNOWN` 表示尚未核对清楚，不能当成付款失败。没有 `order_id` 的未知尝试可以用下方官方查询工具找候选订单，或在 App 核对；项目不会猜测订单关联或自动补购。取消状态也不会自动释放购买额度或删除去重记录。

## 查看账号的官方订单

`official_orders_list(account, page=1, limit=10)` 读取官方账户的一页订单，包括未进入本项目日志的 App 订单。page 为1至100，limit 为1至20。满页只说明可能还有数据，可按 `next_page` 继续；`complete_history=false`，没有伪造总数或完整历史保证。列表的创建/成交时间不能替代预约取餐时间。

选定返回的 `order_id` 后，调用 `official_order_detail(account, order_id, attempt_id?)`。它只发送订单详情和收银台 GET，显示原餐品、门店地址、金额、优惠名称、预约时间、取餐方式和取餐码。`cashier_status` 是官方当前报告的状态，不能代替本项目 `order_status` 的订单、收银台和钱包差额核验；已关联的项目订单仍使用原 `attempt_id` 查询。

可选 `attempt_id` 用于核对结果不明的本地尝试。`local_attempt_comparison.order_fields_match` 只比较返回中列明的门店、取餐时间/方式、金额、商品和组件数量；没有验证优惠身份、全部定制或请求来源。另一客户端可能下了内容相同的单，因此匹配不会自动关联、修改本地状态、解除防重复记录或授权付款。已关联到其他订单的尝试不会改绑。

两个工具均需要有效会话，不要求启用消费，也不查询钱包支付令牌。付款引用、交易号、手机号、订单跳转链接和操作按钮不会返回给 Agent。遇到未知结构或身份冲突时停止，不把查询失败当作购买失败。

```sh
ezmcd orders-live personal --page 1 --limit 10
ezmcd order-view personal RETURNED_ORDER_ID
# 可选：只读比对本项目原尝试，绝不绑定或付款
ezmcd order-view personal RETURNED_ORDER_ID --attempt-id ORIGINAL_ATTEMPT_ID
```

`orders_list` 只包含本项目的尝试，不是账号在官方 App 的全部订单。空列表不证明没有官方订单；下单前的服务端重复订单检查仍会执行。

账号登录失效、刷新中断或本地撤销后，列表仍可使用。实时 `order_status` 需要先恢复同一会员的登录；使用原别名重新短信登录，或 `account connect ALIAS --update`。购买日志按会员身份保存，不要删除数据库、复制新数据目录或换账号来重试付款。

分页 `limit` 为1至100，`offset` 为0至10000。默认包含所有状态；可用 `--state UNKNOWN` 找待核对尝试，也可选择 `RESERVED`、`CREATED`、`PAYMENT_ATTEMPTED`、`PAID`、`CANCELLED`。日期按 Asia/Shanghai 的取餐日筛选，排序为取餐日倒序，同日按本地记录顺序倒序。
