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

## 慢请求期间报价或授权到期

报价的120秒期限限制创建新订单；本地消费策略的 `expires_at` 限制后续金融写请求。查单、签名核验、余额查询或付款准备较慢时，项目会在创建购买记录前，以及每次下单、预支付、麦钱包扣款的日志占位前后重新检查。不会因准备开始时尚未到期，就继续使用已过期的报价或授权。

若在创建购买记录前发现过期，不会占用购买次数或预算。若已经创建订单或写入请求占位，则保留原记录，停止后续写请求；结果通过原尝试查询，不删除记录、换键重购或自动恢复付款。即使本次明确停在发送前，占位也不会释放，以保持重启后的保守去重。需要时在官方入口核对或处理原单。

订单已创建后，原报价到期不会单独阻止仍获授权的付款；付款必须继续符合本地策略、订单内容、可支付状态和余额核验。重新启用策略不会自动重发此前中断的付款。上述截止时间是本机发送前检查，不保证上游收到或完成处理的时刻。

## 按一份报价授权

本机 `account policy ALIAS --enable-checkout --quote-id QUOTE_ID` 可以只授权已经核对的一餐，仍接受 `--max-order`、`--daily-budget`、`--max-orders-day`、`--store` 和 `--expires-at`。必须使用接入账号时的同一数据目录。报价须属于该会员且尚未过期；授权不联网、不重新取价、不创建订单或付款。返回的 `authorized_quote` 是已保存报价快照，不表示刚做过会话、余额或运行字段检查。

授权保存该会员及报价内容的指纹，覆盖门店、北京时间、取餐方式、商品组件和配料、优惠、金额以及订单正文。默认期限最多15分钟；相同内容和额度重复运行且未指定新期限时，保留原期限，不从执行命令时重新计时。已过期时需本人明确提供新期限与新鲜报价，不会自动续期。

新的报价编号可以沿用授权，但所有被绑定的内容必须完全一致；钱包余额、报价编号和报价有效期变化不会扩大消费范围。价格下降、同价换餐或换优惠也不会自动获准。每次金融请求仍复查该绑定和有效期；预算继续累计原日志，不因按报价授权而重置。

付款后相同内容只对应原购买记录，不能继续买第二单。任何状态的既有同店同取餐时间记录都会阻止重新授权；未知结果只查原单，不换键或删记录。授权期限过后仍可查询原单，关闭消费可用 `account policy ALIAS --disable-checkout`。此功能没有新增MCP授权工具；Agent不能自行开启或改写权限。

该模式已完成隔离CLI/MCP模拟消费和重启验证；保存的真实历史报价与当时同内容重报价能匹配同一范围。尚未用这个新增授权模式发送真实金融请求，不能把模拟验收当作真实付款证据。

## 订单状态、付款与实际取餐

`official_order_detail` 的 `status_title` 优先采用官方 `orderStatus`，`status_source` 标明来源。详情顶部的 `orderStatusTitle` 可能是取餐码；它不会覆盖正式状态。缺少正式状态时，只回退已观察到的“待支付／预约完成／已完成／已取消”文字；未知数字、标题类型及状态码不会被猜测翻译。

`official_order_state=COMPLETED` 表示接口报告“已完成”，`CANCELLED` 表示“已取消”，其余为 `UNKNOWN`。`status_description` 保留官方说明。已观察到“已完成”与“请至人工窗口取餐”同时出现，因此不能把订单状态等同于用户已经取走餐品；`collection_verified` 始终为 false。

本项目的 `order_status` 返回 `official_status`、`official_order_state` 和 `official_status_detail`。这些字段与本地 `state`、`paid_verified` 分开：已完成不能单独证明麦钱包付款成功，未知付款仍只查询；取消仍覆盖历史付款状态，但不证明退款到账。已有付款核验且订单已完成时，`next_action=review_completed_order` 提醒 Agent 查看官方说明，不代表补购或确认已取餐。查询失败会标记 `status_query_failed=true` 并要求重新查询，旧状态不能作为最新结论。

这些状态在 `orders_list` 中仍是本地快照，需结合 `status_checked_at` 与实时 `order_status` 阅读。旧购买键的重复调用只查原订单，官方完成不会释放购买记录以便重新下单。

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
