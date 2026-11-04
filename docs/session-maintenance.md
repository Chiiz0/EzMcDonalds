# 已接入账号的会话维护

**a35：此入口使用实验性按进程采样，不修改系统代理；新版官方 App 维护仍待验收。旧版整机代理方案已移除。**

## 只进入钱包更新登录

已有账号现在可复用首次接入的本机引导，无需复制请求，也无需准备测试购物车。先在支持的 Mac 官方 App 登录**原账号的同一会员**并确认麦钱包正常显示，然后使用原数据目录：

```sh
ezmcd sampling-connect personal --update --manage-app
```

在本机终端按回车后，工具核验并重新打开 App；用户只进入麦钱包，等待最终 `[ACCOUNT_UPDATED]` 或 `[ACCOUNT_REVERIFIED]` 及对应JSON结果；`GUARD_ACTIVE` 和余额样本已校验均不代表更新完成。复用本人已授权的受限证书；保护器拒绝捕获到的写请求；原生路由未验收，不要测试提交、短信或付款，退出 App、停止采样且清理核对通过后才查询一次实时余额、原子更新会话。首次接入及证书准备见[首次接入指南](first-connection.md)。

该模式绑定原会员、客户端和采样开始时的账号状态。其他会员、已撤销账号、期间修改过的账号或清理未完成的采样不能覆盖。它保留原消费策略、偏好和购买日志，不新增消费授权，也不发送旧刷新令牌。没有采集新的下单运行字段：完全相同会话及设备的原字段保留原始日期；换会话后符合条件的近期本人样本只能成为待核验资料，通过下文的 `account runtime --previous-device` 才能恢复。缺失、过期或换设备时仍须另行取得有效运行样本。

若采样完成但余额核验或保存失败，命令输出本机目录与 `resume_command`，用同一目录继续即可，不重开 App 或代理：

```sh
ezmcd sampling-import personal /absolute/path/data/captures/采样编号 --connect --update
```

成功后的重复导入只查询一次余额，不重写账号、不恢复旧策略、不延长运行字段。已另行改变凭据时旧目录会被拒绝。该路径的会话切换、过期、身份绑定、失败恢复及去重已由隔离测试覆盖；新的官方 App 维护采样和跨日自动续期仍待实机验收。

## 使用已有刷新令牌

已保存 `refreshToken` 的账号可以通过 CLI 或 MCP 维护会话，不需要再次发送短信：

```sh
ezmcd account check personal
ezmcd account refresh personal
```

本机 [受保护采样](guarded-capture.md#采样期间的会话刷新) 也可保存一次已允许的官方刷新响应中的凭据，前提是与本人签名余额样本匹配。导入只做GET核验，不会再次刷新；是否采到令牌单独返回，不能把会话可用当作刷新令牌已经兑换成功。该新增采样分支的真实刷新联调仍待验收。

个人 Agent 调用 `account_refresh(account)`。它会变更认证状态，因此工具没有标注为只读。该操作不创建订单、不付款、不增加消费额度。

刷新协议来自已支持官方客户端：`POST /bff/passport/login/session/refresh`，使用当前会话签名。成功返回的新凭据先保存到本机私有文件，再通过钱包只读接口验证；通过后原子提交新会话，保留会员身份、消费策略和购买日志。

## 重复调用与恢复

- 在发送刷新请求前保存执行记录，整个流程持有与结算相同的账号锁。
- 收到新会话后余额核验失败：再次执行同一刷新命令，只核验已保存的新会话，不再次刷新。
- 刷新响应丢失、被拒绝或进程在发送阶段中断：不重发旧刷新令牌，要求本机重新登录。
- 最近一次成功刷新后 24 小时内再次调用，只查询当前会话余额。24 小时是本项目的维护间隔，不是官方会话有效期；查询失败也不会提前重复刷新。
- 最终保存前崩溃可使用保存的新会话继续核验；本地撤销同时移除未提交的新凭据。

单次真实联调已通过刷新、余额核验及保存；再次调用确认没有重复刷新。跨日连续运行与服务端过期后的恢复尚待验收。

另一个旧会话真实刷新被官方拒绝后，项目保留 `STOPPED` 状态。`doctor` 会提示本机重新登录，不能通过反复刷新恢复该旧令牌。若账号仍有本人官方设备字段，可运行 `ezmcd account login personal --device-from personal`，在本机输入原会员的手机号和验证码；或用 `account connect personal --clipboard --update` 重新导入本人官方会话。两种方式均保留原会员的购买日志。

## 给 Agent 的状态

`accounts_list` / `account_check` 分别报告：

- `device_context_available`：本地是否有设备字段，不能当作服务端仍接受这些字段的证明。
- `session_refresh_supported`：可尝试刷新或继续核验的本地令牌；`CLAIMED` / `STOPPED` 返回 `false`，不保证服务器接受。
- `session_refresh_state`：未尝试、执行中、待核验、已完成或已停止。
- `session_status` / `session_recovery`：待核验提示 `resume_refresh_verification`，通过 `account_refresh` 继续只读核验；已中断或停止提示 `relogin_or_reconnect_locally`。这两种状态的 `can_browse` 均为 `false`。
- `readiness.can_browse`：本次核验通过后可尝试浏览，离线演示单独处理。
- `readiness.checkout_blockers`：尚未核验会话、运行字段缺失或过期、消费未授权、策略过期、刷新未完成等。

`checkout_prerequisites_met` 仅说明本地前置条件满足。下单仍必须检查实时门店、时段、购物车、报价、预算与已有订单。

当前适配器不会直接把旧 v5 样本当作新会话的可用字段。刷新若更换 `sid`，原样本先退出可用状态；同一会员、完全相同的设备上下文且原样本仍在24小时内时，保留为待核验资料。未更换会话时也保留原始采样时间，不延长寿命。**会话维护已可用，不代表下单设备运行字段已经可以长期自动更新。** 字段来源见[设备上下文](device-context.md)。

## 恢复近期的本人设备样本

重新短信登录、重新导入会话或会话刷新后，`accounts_list` / `account_check` 的 `runtime_recovery_available=true` 表示本人近期样本可尝试只读恢复。`doctor` 会提示 `restore_previous_own_runtime_readonly`，无需再次从另一个账号导入或重新采样：

```sh
ezmcd account runtime personal --previous-device
```

个人 Agent 对应调用 `account_runtime_restore(account)`。它只使用该会员本地保留的资料，检查客户端构建、版本、语言、安装标识和 SDK 设备字段完全一致，再以**当前登录会话**计算新签名，对固定订单列表第一页执行一次 v5 GET。成功且响应结构正确后才启用样本；不发送刷新令牌、短信、订单或付款请求。

原采样日期不变，仍从最初观察时刻计算24小时。新登录不是新采样，`verified_at` 也不是续期日期。资料过期、设备字段变化、登录未完成或验证失败时停止；失败保留待核验资料，可修复登录/网络后再次做只读检查。重启后重复已成功的恢复只核验同一份资料，不延长寿命。已有其他近期可用样本时不覆盖它。

待核验资料保存在同一个本机私有账号文件，不包含旧会话令牌；本地撤销同时删除它。消费策略、购买日志和防重复记录不变。没有历史样本时不会构造设备值，也不会从另一会员自动复制。资料已超过24小时，仍需通过官方 App 更新。

重新登录/刷新转移与失败恢复已通过隔离测试。真实 stdio MCP 在当前有效会话的隔离副本上完成恢复及重启后的只读复核，原账号和日志未变；这次没有真实切换登录会话，也未验证超过24小时或新订单 POST。

## 检查现有运行字段

```sh
ezmcd account runtime-check personal
```

个人 Agent 可调用 `account_runtime_check(account)`。它对固定的订单列表第一页执行一次 v5 GET，结果只返回检查状态，不返回订单内容、签名或设备值。可以检查超过项目 24 小时限制的历史样本，用于区分本地期限与服务器当前是否接受这一次查询。

检查不修改账号文件、采样时间、消费策略或购买日志。`read_only_v5_verified=true` 只证明这次 GET 成功；`order_post_acceptance_verified` 和 `checkout_authorized_by_check` 均为 `false`。即使历史样本通过，也仍须更新官方运行证据才能使用当前下单适配器。401 不足以单独判断是登录还是运行字段有问题，可先用 `account check` 核对会话。
