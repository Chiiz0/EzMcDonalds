# 采样状态及替代入口

## a35：停用旧版系统代理采样

旧版通过系统 HTTPS 代理接管整机流量，而保护器只允许指定麦当劳请求。一次会话维护采样导致其他应用无法联网；采样未完成，也没有留下清理完成记录，用户手动关闭代理才恢复。证书只信任一个域名，不等于代理只影响一个 App。

从 a35 起，项目删除系统代理写入路径。`sampling-capture` 返回 `sampling_runtime_capture_suspended`，在读取账号、操作证书、启动采样或 App 之前停止；保留参数只是为了给旧脚本明确错误。不要使用旧版本或修改源码绕过此限制。

## 钱包会话接入与维护

`sampling-connect` 改用 mitmproxy 的 [Local Capture](https://docs.mitmproxy.org/stable/concepts/modes/#local-capture)，固定正向选择器 `local:/McdApp.app/McdApp`。它匹配可执行路径，包括 macOS 转移后的 App；不是精确 PID 隔离。不会选择所有进程，也不会回退到 HTTPS/PAC/SOCKS 系统代理、DNS、路由或防火墙改写。

该入口仅采集钱包会话，不让用户提交测试订单；本机监听端口的阻断自检不代表原生 App 所有请求已被捕获。因此在原生路径完成验收前，**不把它用作提交订单的安全保护器**。已有运行字段导入仍保留。

启动时分别验证本机路由和自检监听器就绪，再允许打开 App。准备和采集合计最多120秒；收尾有固定次数的退出检查，系统状态查询另有限时。退出失败停止采样并返回未完成，不能导入部分结果。采样子进程监视父进程与独立时间上限。系统代理仅查询前后状态；用户中途改变配置也不会被自动覆盖。

网络扩展可能要求首次 macOS 授权。没有安装或未就绪时，本轮停止；后续手动重新运行才会再尝试。证书仍按原有显式授权管理，不自动扩大信任范围。步骤见[首次接入](first-connection.md)和[会话维护](session-maintenance.md)。

## 只读诊断

```sh
ezmcd sampling-status
# 网络服务不是 Wi-Fi 时：
ezmcd sampling-status --network-service Ethernet
```

报告系统代理是否启用、是否可能存在旧版127.0.0.1:18089配置、依赖版本及验收状态。不启动采样、不修改网络、不发起麦当劳请求。禁用但仍保留旧端口不是活动代理；其他代理配置不会被删除。

若旧版曾异常终止且网络仍有问题，在系统网络设置核对并关闭那份遗留代理；不要自动覆盖后来设置的代理。没有清理记录的旧采样目录不能假装完成后导入。

## 已有凭据继续使用

- 已完成且有完整清理证据的历史目录可继续 `sampling-import`，只读核验后导入，不重新打开 App 或代理。
- 本人已有会话请求可用 `account connect ALIAS --request-file /private/balance.txt --update`；运行请求用 `account runtime ALIAS --request-file /private/runtime.txt`，见[复制请求导入](request-import.md)。它只解析必要字段，不执行复制的命令或重放订单。
- 有本人设备上下文的短信登录、已有会话刷新、MCP浏览/报价与防重复购买保持原有边界。不要为取得样本额外购买，不修改采样日期延长有效期。

## 验收边界

新路径已用隔离测试检查就绪范围、启动失败、App卡住、子进程终止、父进程消失、超时、用户修改代理及导入清理证据。a36 原生尝试未取得余额请求，失败证据保留。a37 修复后，已有账号的钱包采样、退出清理、实时核验、隔离副本更新及重复导入已通过；首次创建账号和跨日维护仍未验收，没有新增消费。

## 看懂进度与诊断

- `GUARD_ACTIVE`：采样器和自检已就绪，不证明 App 请求经过它。
- `APP_RUNNING`：观察到麦当劳进程，不证明已进入钱包或取得余额。
- `BALANCE_CAPTURED` / `CAPTURE_FINISHED`：样本阶段，仍需等待导入核验。
- `ACCOUNT_CONNECTED` / `ACCOUNT_UPDATED` / `ACCOUNT_REVERIFIED`：分别表示首次导入、更新或重复只读核验完成。
- `ACCOUNT_NOT_CONFIRMED`：本轮未确认成功，按最终错误与已有证据处理，不依据中间提示重复运行。

最终结果的 `routing_diagnostics` 只包含固定计数。已观察到原生连接但没有API请求时返回 `native_traffic_without_api`，便于区分与完全未观察到请求的情况；计数不会自动认定具体故障原因。旧目录没有计数时 `available=false`，不能把缺失当作零。网络探测需先有有效基线，外部站点本身不可达时保持结论未定。

### 本机传输的范围

采样开始前用系统解析器查询固定的 `api.mcd.cn`，本轮只本地回答该名称及系统返回的规范别名的 A/AAAA 查询；HTTPS 类型返回无记录，不透传请求。解析器在采样期间不运行，API 上游连接使用本轮地址并保留 TLS 域名校验。其他 DNS、UDP 和非 API 上游连接仍拒绝。

每次生成仅含 `api.mcd.cn` 的临时叶证书，正常收尾后移除叶私钥；复用的受限 CA 和其信任范围不变。本机模式使用 HTTP/1.1，只有 TLS SNI 与 HTTP authority 都匹配 API 才识别透明 IP 请求。没有关闭服务器证书验证。

终端每条提示带 `[RUN …]` 本轮编号。`native_acceptance=wallet_maintenance_verified_a37` 表示版本的历史实机证据；本轮是否成功仍以最终账号导入结果和清理记录为准。
