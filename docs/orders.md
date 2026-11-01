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

如果实时查询返回 `PAID`，但 `pickup_code_pending=true`，表示已核验付款、取餐码尚未生成；临近取餐时再查询同一订单。`UNKNOWN` 表示尚未核对清楚，不能当成付款失败。没有 `order_id` 的未知尝试需要在官方入口核对，项目不会猜测订单关联或自动补购。取消状态也不会自动释放购买额度或删除去重记录。

`orders_list` 只包含本项目的尝试，不是账号在官方 App 的全部订单。空列表不证明没有官方订单；下单前的服务端重复订单检查仍会执行。

账号登录失效、刷新中断或本地撤销后，列表仍可使用。实时 `order_status` 需要先恢复同一会员的登录；使用原别名重新短信登录，或 `account connect ALIAS --update`。购买日志按会员身份保存，不要删除数据库、复制新数据目录或换账号来重试付款。

分页 `limit` 为1至100，`offset` 为0至10000。默认包含所有状态；可用 `--state UNKNOWN` 找待核对尝试，也可选择 `RESERVED`、`CREATED`、`PAYMENT_ATTEMPTED`、`PAID`、`CANCELLED`。日期按 Asia/Shanghai 的取餐日筛选，排序为取餐日倒序，同日按本地记录顺序倒序。
