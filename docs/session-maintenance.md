# 已接入账号的会话维护

已保存 `refreshToken` 的账号可以通过 CLI 或 MCP 维护会话，不需要再次发送短信：

```sh
ezmcd account check personal
ezmcd account refresh personal
```

个人 Agent 调用 `account_refresh(account)`。它会变更认证状态，因此工具没有标注为只读。该操作不创建订单、不付款、不增加消费额度。

刷新协议来自已支持官方客户端：`POST /bff/passport/login/session/refresh`，使用当前会话签名。成功返回的新凭据先保存到本机私有文件，再通过钱包只读接口验证；通过后原子提交新会话，保留会员身份、消费策略和购买日志。

## 重复调用与恢复

- 在发送刷新请求前保存执行记录，整个流程持有与结算相同的账号锁。
- 收到新会话后余额核验失败：再次执行同一刷新命令，只核验已保存的新会话，不再次刷新。
- 刷新响应丢失、被拒绝或进程在发送阶段中断：不重发旧刷新令牌，要求本机重新登录。
- 最近一次成功刷新后 24 小时内再次调用，只查询当前会话余额。24 小时是本项目的维护间隔，不是官方会话有效期；查询失败也不会提前重复刷新。
- 最终保存前崩溃可使用保存的新会话继续核验；本地撤销同时移除未提交的新凭据。

单次真实联调已通过刷新、余额核验及保存；再次调用确认没有重复刷新。跨日连续运行与服务端过期后的恢复尚待验收。

## 给 Agent 的状态

`accounts_list` / `account_check` 分别报告：

- `device_context_available`：本地是否有设备字段，不能当作服务端仍接受这些字段的证明。
- `session_refresh_supported`：本地是否持有可尝试刷新的令牌，不保证服务器接受。
- `session_refresh_state`：未尝试、执行中、待核验、已完成或已停止。
- `readiness.can_browse`：本次核验通过后可尝试浏览，离线演示单独处理。
- `readiness.checkout_blockers`：尚未核验会话、运行字段缺失或过期、消费未授权、策略过期、刷新未完成等。

`checkout_prerequisites_met` 仅说明本地前置条件满足。下单仍必须检查实时门店、时段、购物车、报价、预算与已有订单。

当前适配器把 v5 运行样本与会话绑定。刷新若更换 `sid`，旧样本失效；即使未更换也保留原始采样时间，不延长其寿命。**会话维护已可用，不代表下单设备运行字段已经可以长期自动更新。** 字段来源见[设备上下文](device-context.md)。

## 检查现有运行字段

```sh
ezmcd account runtime-check personal
```

个人 Agent 可调用 `account_runtime_check(account)`。它对固定的订单列表第一页执行一次 v5 GET，结果只返回检查状态，不返回订单内容、签名或设备值。可以检查超过项目 24 小时限制的历史样本，用于区分本地期限与服务器当前是否接受这一次查询。

检查不修改账号文件、采样时间、消费策略或购买日志。`read_only_v5_verified=true` 只证明这次 GET 成功；`order_post_acceptance_verified` 和 `checkout_authorized_by_check` 均为 `false`。即使历史样本通过，也仍须更新官方运行证据才能使用当前下单适配器。401 不足以单独判断是登录还是运行字段有问题，可先用 `account check` 核对会话。
