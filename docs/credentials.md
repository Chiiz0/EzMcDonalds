# 获取自己的凭据与开始使用

a35 的 `sampling-connect ALIAS` 改为实验性按进程钱包采样，系统代理保持原样；a37已有账号维护及a39首次接入已实机通过，空白机器安装仍待验收。旧版 `sampling-capture` 已停用，历史已完成样本与单请求导入仍保留。参见[网络修复说明](guarded-capture.md)。

本项目优先支持已经注册麦当劳中国会员的用户。凭据留在自己的电脑，个人 Agent 使用账号别名，不需要知道手机号、验证码或会话令牌。

## 先检查当前状态

```sh
ezmcd doctor
ezmcd account guide

# 已接入时，额外只读检查会话是否仍有效
ezmcd doctor --account personal --verify-session
```

`doctor` 会区分官方客户端是否匹配、有没有设备上下文、能否刷新会话、有没有下单运行字段、是否授权消费。默认不联网；`--verify-session` 只查询余额，不登录、不刷新、不下单。

诊断中的原采样时间表示已有运行证据的时间。重新计算签名中的随机数或计数不表示重新观测设备环境；目前仍需本人证据或已明确选择的限期复用。已定位的输入与验证范围见[环境观测与请求元数据](device-context.md#环境观测与请求元数据的区别)。

## 已有本人官方设备上下文

```sh
ezmcd account login personal --device-from existing-account
```

在本机终端依次输入自己的手机号与验证码。输入不回显，不要发到聊天。国内 11 位号码和 `+86` 前缀均可。成功后核验返回号码和钱包，再保存会话。

同一会员重新登录会保留原别名、消费策略和订单日志。缺少 `d` / `deviceToken` 时，不会尝试用随机字符串发送短信，先按下一节获取本人官方上下文。[详细登录与恢复规则](sms-login.md)。

重新登录后若 `runtime_recovery_available=true`，本人原设备的近期运行样本已作为待核验资料保留。可运行 `ezmcd account runtime personal --previous-device`，或让 Agent 调用 `account_runtime_restore`，通过固定只读请求后恢复使用，避免重复采集同一份资料。它不延长原采样日期；超过24小时、设备上下文变化或没有旧样本时仍须更新。[恢复步骤](session-maintenance.md#恢复近期的本人设备样本)。

## 从自己的官方 App 导入

需要反复采样的 Mac 用户可选择[持久采样证书](sampling-certificate.md)，首次授权后复用。下文的“移除临时证书”仅针对临时方案；项目新版不修改系统代理；外部调试工具若修改代理须自行恢复，停用时显式撤销证书。

目前真实适配依赖已验证的 iOS App on Mac 构建；具体版本和校验值见[环境要求](onboarding.md#环境要求)。`doctor` 显示构建不匹配时，不应假定 Android 或其他 App 版本导出的请求能够直接使用。

1. 在官方 App 登录自己的会员账号，停留在“我的”或麦钱包页面。
2. 使用本人控制、支持 HAR 导出的本地调试工具。只为 `api.mcd.cn:443` 启用 HTTPS 查看，记录原系统代理设置；若使用临时 CA，由本人在系统中确认信任。
3. 打开麦钱包，刷新余额。找到 **`GET https://api.mcd.cn/bff/member/mwallet/balance`**，确认响应 HTTP 200 且 JSON 中 `success` 为 `true`。
4. 仅保留这次余额请求及响应，导出 HAR。请求头、原始签名和响应正文必须保留；不要把整个浏览器历史或其他账号的流量混入文件。
5. 关闭采样，恢复先前代理设置，移除临时 CA 信任及其证书、私钥。确认正常联网后导入本地文件。

HTTPS 主机与证书配置可参考 Charles 的[按主机启用 SSL Proxying](https://www.charlesproxy.com/documentation/proxying/ssl-proxying/)和[证书配置](https://www.charlesproxy.com/documentation/using-charles/ssl-certificates/)文档。实际文件需导出为 HAR；若所用版本只提供其他格式，不要把扩展名改成 `.har`。mitmproxy 提供[保存 HAR 的选项](https://docs.mitmproxy.org/stable/concepts/options/)，也可使用支持 HAR 的其他本地工具；项目不要求购买特定软件。

```sh
chmod 600 /absolute/path/account.har
ezmcd account connect personal --har /absolute/path/account.har
ezmcd account check personal
```

导入器会验证官方域名、余额请求签名、单一会话及当前余额。它不重放 HAR 中的订单，也不保存其他流量。成功后原始 HAR 不再是日常查询所必需的，可按自己的保留策略移除；不要删除项目订单日志。

也可以跳过 HAR 导出：在第3步复制单次余额请求为 cURL/原始 HTTP 请求，在 Mac 私有终端运行 `ezmcd account connect personal --clipboard`。它只解析请求并重新查询余额，不执行复制的命令。非剪贴板方式为 `--request-file`，详见[复制请求导入](request-import.md)。随后同样恢复代理、移除临时证书并清理剪贴板中的凭据。

若工具只能导出单请求 JSON，可使用 `--balance-sample`；格式见[单请求接入](onboarding.md#从单请求样本或已有-profile-接入)。

## 为什么已登录还可能不能下单

| 检查结果 | 可以继续做什么 | 后续动作 |
|---|---|---|
| 会话有效，缺少下单运行字段 | 找店、查时段、看菜单、套餐选择、购物车与报价 | 提供有效官方 v5 样本，或显式核验同设备上下文的近期样本 |
| 有刷新令牌，会话需维护 | 尝试 `account refresh`，不需要短信 | 刷新若失败或结果未知，按提示本机重登 |
| 没有设备上下文 | 运行离线演示、查看接入指南 | 导入本人官方余额样本 |
| 消费策略关闭 | 继续查询和准备方案 | 用户在本机设置明确门店与限额 |
| 运行字段超过默认期限 | 继续浏览，保留已有订单和状态 | 更新本人官方证据，或本机显式选择[限期复用](runtime-use.md)；不能改采样时间 |

**登录凭据和下单运行字段不同。** 当前适配器接受同会话的官方 `POST /bff/order/orders` v5 运行样本，直接导入须在24小时内；已保存样本默认也按24小时限制，可由本机用户选择[显式限期复用](runtime-use.md)。可以使用本人已授权购买时保留的样本，但不要为了获得样本额外创建订单。当前还没有“打开 App 看一眼就自动更新全部运行字段”的功能，也不能承诺仅导入一次后长期无人值守购买。[设备字段来源](device-context.md)说明了已实现和待验证的部分。

## 单独更新下单运行字段

旧版 `sampling-capture` 已停用，不再提示点击提交订单。已有完整清理证据的历史目录仍可通过 `sampling-import` 只读核验后导入；新样本来源与限制见[采样状态](guarded-capture.md)。

已有本人近期官方样本时，无需重新导入整个账号：

```sh
ezmcd account runtime personal --sample /absolute/path/runtime.json
# 或仅包含当前会话官方 v5 请求的 HAR，无需再放入余额请求
ezmcd account runtime personal --har /absolute/path/runtime.har
# Mac 上复制单次已授权或已阻断的官方请求后，也可使用
ezmcd account runtime personal --clipboard
```

该操作验证样本、只读核验当前余额，再原子保存运行字段；不重放请求正文，保留原采样时间、刷新令牌、消费策略和订单日志。混合会话、过期或早于现有记录的样本会被拒绝。复制文本的格式与清理方法见[请求导入](request-import.md)。

### 已有请求头的本机导入

本人已有授权样本可在私有终端运行下列命令，输入不回显。样本须来自本人同会话的请求，不能拼接不同账号字段；不要为了取样提交测试订单。新版钱包采样不能提供下单运行字段。

```sh
ezmcd account runtime personal --paste-headers
ezmcd account runtime-check personal
```

导入只核验字段与余额，不发送订单或扩大消费策略。完成后清理复制内容；自己使用外部调试工具时也需自行停止采集与恢复它修改的设置。

如果短信账号沿用的是自己另一已接入账号的同一官方设备上下文，可以在本机显式运行：

```sh
ezmcd account runtime TARGET_ALIAS --device-from SOURCE_ALIAS
```

它要求两个账号的官方安装标识、SDK 设备字段和客户端构建一致，用目标账号自己的会话进行一次 v5 订单列表 GET；成功后才保存近期运行字段。不会复制来源账号的登录身份，也不会自动刷新采样日期。这个模式已有真实 GET 成功及坏签名拒绝的对照证据，之后在明确消费授权下完成一笔新会话的订单 POST 与麦钱包付款。该导入入口仍只接收近期样本；本地已保存的旧样本可另用[限期复用](runtime-use.md)，约10天样本已有单次消费证据，连续多日使用仍未验证。这不是免设备初始化方案。导入操作保留原消费策略。

## 交给自己的 Agent

```sh
ezmcd mcp-config
```

将生成的 stdio 配置放进支持 MCP 的个人 Agent 客户端。先让 Agent 调用 `account_diagnose`，再用 `stores_search`、`pickup_slots`、`menu_list`、`product_detail` 选择商品。无需在聊天中手工填写门店编码，Agent 可以按城市和店名查询。

可以对 Agent 说：“记住我刚选定的门店、套餐和饮品，工作日 08:30 自取，1 份，上限 20 元，需要卡优惠。”它可保存偏好并生成 `meal_plan`；保存偏好本身不会启用定时购买或消费权限。

确实要授权购买时，由用户在本机执行：

```sh
ezmcd account policy personal --enable-checkout \
  --store 七位门店代码 --max-order 20.00 --daily-budget 20.00 --max-orders-day 1
```

Agent 每次仍需核对实时购物车和报价。结果未知时查询原订单，不通过更换 key、别名或删除日志重下。可随时运行 `ezmcd account policy personal --disable-checkout` 关闭消费。
