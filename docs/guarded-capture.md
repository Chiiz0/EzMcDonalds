# 不创建订单的本机运行字段采样

已经接入的 Mac 用户可以运行 `sampling-capture`，在官方 App 发送订单前截取并阻断请求，更新自己的下单运行字段。无需为了取得样本购买一单。当前适配指定的官方客户端构建；它仍需要人在官方 App 操作，不是跨日自动续期。

## 准备

采样附加组件需要 Python 3.12+，核心查询与 MCP 仍支持 Python 3.11。项目目录中安装：

```sh
.venv/bin/python -m pip install -e '.[sampling]'
.venv/bin/ezmcd sampling-cert install
.venv/bin/ezmcd sampling-cert status
```

首次证书安装需要在 macOS 完成授权，后续复用，不在每轮移除。所有命令须使用同一数据目录；自定义目录时给每条命令加 `--data-dir /absolute/path/data`。

先在官方 App 登录目标会员，准备一份可以进入确认页的购物车，然后退出 App。只加购物车，不提交订单；记录测试前的订单和余额。正常退出卡住时，在活动监视器中只强制退出 `McdApp`。已有连接必须关闭，不能让 App 保持运行后才启用代理。当前命令要求这个会员已有本地别名；首次接入仍按[凭据指南](credentials.md)处理。

## 开始采样

```sh
.venv/bin/ezmcd sampling-capture personal --network-service Wi-Fi --update-account
```

`--network-service` 是 macOS 的当前联网服务名。命令先检查持久证书、App 已退出和原代理状态，再做一次合成阻断测试。已有 HTTP、HTTPS、SOCKS 或自动代理时停止；不会覆盖活动代理。必要时用 `--mitmdump /absolute/path/mitmdump` 指定自己安装的程序。

只有看到 `GUARD_ACTIVE` 后才打开官方 App：

1. 先进入麦当劳钱包的余额页。终端出现 `BALANCE_CAPTURED`，表示已通过签名和会员身份校验。余额尚未校验时先不要进入提交操作。
2. 打开已准备购物车对应的门店和取餐时间，进入订单确认页。
3. 在这一个保护窗口中触发一次提交；若 App 再确认餐厅，只确认一次。订单请求会在请求头阶段终止，App 可能显示“提交失败”。不要重试。
4. 看到 `CLOSE_OFFICIAL_APP_NOW` 后立即退出 App；卡住时只强制退出麦当劳。采样器会一直保留阻断，直到确认 App 进程结束，才恢复原 HTTPS 代理并退出。
5. 等待 `CLEANUP_RESULT` 中 `proxy_restored=true`、无错误。证书保留。使用 `--update-account` 时，随后自动核验并导入，最终 `account_updated=true` 表示已保存。再核对官方订单和余额，查看并按自己的需要处理测试购物车；导入不会清空购物车。

默认采样120秒，`--seconds` 可设30至120秒。超时后停止读取和收集，但继续阻断，不能靠超时自动放行客户端的重试。命令不打开 App、不自动点击、不创建购物车、不生成账号授权、不修改消费策略；采样期间锁住本地账号，防止同一数据目录的 MCP 同时购买。

系统 HTTPS 代理会在这段时间临时指向本机。只解密 `api.mcd.cn`；其他 HTTPS 隧道（包括钱包支付主机）被阻止，其他软件联网可能暂时受影响。订单、购物车写入、预支付及消费请求不转发。官方原生会话刷新可放行至多一次，且必须与本地账号或 `--refresh-from` 指定的本人签名余额样本的 `sid` 和 `token` 完全匹配；结果未知不重发。其余 POST 等写请求继续阻断。

## 导入结果

加 `--update-account` 后，无需再复制两个样本文件的路径。只有完整余额/运行样本、采样时账号绑定及代理清理记录均通过检查，才会使用新凭据执行余额 GET 与固定 v5 订单列表 GET；两个查询通过后，在账号锁内一次保存凭据与导入记录。消费策略、偏好及购买日志保留，观察日期仍取原官方签名时间。

网络失败、验证被拒绝或保存前中断时，旧本地账号保持不变。可在恢复正常网络后，使用 `CLEANUP_RESULT` 输出的同一个 `capture_directory` 继续导入，不必重新采样：

```sh
.venv/bin/ezmcd sampling-import personal /absolute/path/data/captures/采样编号
```

如果先前已保存但终端输出丢失，再次运行只做两次 GET 核验，返回 `already_imported=true`，不重写凭据。采样后另行登录、撤销或修改账号，旧采样不能覆盖新状态；重复导入也不会延长24小时期限。`sampling-import` 不启动 App、证书或代理操作，不发送短信、刷新会话、订单或支付请求。它只接受同一数据目录内新版采样生成的私有目录；不要手工补写绑定记录。

自动导入核对的是凭据和本地采样记录，不是“其他客户端没有购买”的独立证明，也不会将订单列表里的候选自动关联到本地购买。仍应核对原订单、余额和购物车。官方 App 刷新出的新会话可能已使原会话失效；导入失败时“本地文件未变”不代表旧登录在服务器仍有效。

不加 `--update-account` 时仍只采样不导入。旧版采样缺少账号绑定，继续按以下显式入口处理：

成功输出包含 `balance_sample` 与 `runtime_sample` 的本机绝对路径。采样本身不覆盖账号；确认无新增订单、余额未变化并清理草稿后，用输出路径替换下面占位值：

```sh
.venv/bin/ezmcd account connect personal --update \
  --balance-sample /absolute/path/capture/balance.json \
  --runtime-sample /absolute/path/capture/runtime.json
.venv/bin/ezmcd account check personal
.venv/bin/ezmcd account runtime-check personal
```

导入只重新查询余额、验证同会员身份、校验官方签名，不重放订单正文；保留消费策略和订单日志。若 App 与原短信登录会话不同，会替换为当前 App 会话，不保留旧会话的刷新令牌；之后需要时重新短信登录或采样。运行字段仍保留真实观察时间，当前24小时限制不变。

样本存于数据目录的 `captures/<随机编号>`，目录0700、文件0600；只保留必要请求头、校验结果和脱敏审计，不保存订单正文、Cookie、完整余额响应或原始代理日志。样本仍包含登录凭据，不要上传仓库或发给 Agent。导入成功后可在本机删除不再需要的采样目录；不要删除账号数据目录中的购买日志。

## 未完成与恢复

- `balance_saved=false`：查看输出的缺失字段名称。曾实测原生刷新被阻断后缺少 `meddyid`；不要手工补字段。如果已经取得同会员的近期有效余额样本，可用 `--refresh-from /absolute/path/balance.json` 为下一轮匹配官方刷新。不同会话不能随意混用。
- `runtime_saved=false`：本轮没有通过运行样本校验，命令退出码为2。先完成 App 退出和代理恢复，不把空结果当成可下单状态。
- `sampling_effective_proxy_mismatch`：所选联网服务未使代理生效；不会发出可开始操作的指令。
- 清理报错、终端被强制结束或电脑重启：先关闭麦当劳，再根据本轮 `proxy-before.json` 在系统设置恢复该网络服务原 HTTPS 代理。不要在 App 仍等待重试时直接关代理。持久证书按原授权保留；停用时运行 `sampling-cert remove`。

## 验收范围

采样核心已在支持的官方 App 完成真实余额请求、发送前订单阻断、运行签名校验和新签名的只读 GET 核验；独立查询确认订单列表与钱包余额未变化，测试购物车已清理。合成 TLS 测试确认订单、预支付和购物车请求不触达上游，钱包隧道被阻断。不是一次新的支付验收，也不证明跨日无人值守或其他客户端构建可用。

a22 的自动导入与恢复有22项隔离测试：账号变化、撤销、清理失败、部分采样、签名/会话不匹配、到期、核验失败、保存失败、保存后回执丢失及重复导入。既有本人官方样本在独立测试副本中通过新 CLI 的真实两次 GET 核验，第二个 CLI 进程只读复核，新 MCP 进程也可查会话与 v5；原账号、日志、官方订单和余额未变化。该验证使用原有样本及隔离测试绑定，没有重新采样，不等于新 `--update-account` 全程已经在 App 实操验收。
