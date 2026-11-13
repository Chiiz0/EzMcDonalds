# 钱包 GET 运行字段实验

这是供维护者接手的源码原型，**真实凭据导入尚未通过**。不包含在 wheel/sdist，不注册 `ezmcd` 或 MCP 子命令，不会随安装启动。标准接入仍使用主项目的[凭据指南](../../docs/credentials.md)。项目交接总览见[handoff](../../docs/handoff.md)。

## 已知结果

2026-11-11 在已授权的官方 Mac App 上进行三轮实验。第三轮观察到4个带 `x-mcd-sign` 的 v5 钱包 GET，均被本地上下文校验拒绝，未转发至上游；没有保存可导入的运行样本，也没有更新账号。三轮最终均取得有效的同会员 v4 钱包响应，验证原配置恢复；采样进程及临时叶证书私钥清理完成，系统代理、项目账号、偏好、购买日志未改变，金融请求转发数为0。

拒绝时日志没有具体字段名，**尚不能判定是哪个字段或哪个 SDK 造成**。之后增加了仅包含字段名的错误码，并在首次上下文不匹配时立即进入恢复，避免继续等待客户端重试。新增诊断还没有重新进行实机采样。期间 App 有时显示查询失败，一次用户重新登录后恢复正常，尚未证明统一根因。

原型的68项隔离测试通过；交接导出另外补充2项 PID 复用/读取失败回归。测试只证明这些被测分支，不证明真实导入、订单 POST 或长期凭据维护成功。

## 机制与代码地图

| 文件 | 职责 |
|---|---|
| `transaction.py` | 保存原配置与候选配置、先记录下发意图、验证官方签名、恢复时序与同会员身份 |
| `handler.py` | 只转发有限 GET，阻断写请求及候选 v5 钱包请求，记录无凭据诊断 |
| `transport_fence.py` | 两个固定域名的 DNS/TLS 边界、正向 App 进程选择器、存活期限 |
| `runner.py` / `addon.py` | 独立采样/恢复子进程、预检、App 生命周期、清理及可选导入 |
| `process_receipt.py` / `resources.py` | 按 PID/启动身份确认旧进程结束，仅清理有指纹记录的临时私钥 |
| `config_certificate.py` | 独立的 `img.mcd.cn` 当前用户证书；来自项目 CA 实现的实验副本 |
| `import_evidence.py` | 恢复、清理与上下文核验后做固定只读请求，保留旧资料并原子更新账号 |
| `test_*.py` | 虚构账号、虚构密钥、隔离文件和网络替身 |

原型只在本机收到的 `https://img.mcd.cn/app/main/assets/requestConfig101.json` 中追加精确的 `GET /bff/member/mwallet/balance` v5 规则。原 POST 规则和其他配置保持原值，不修改官方服务器配置。

**App 会持久保存配置。** 退出采样器不等于恢复。采样和恢复使用不同进程；恢复只转发未修改的官方配置，随后要求有效的 v4 钱包请求、同一会员、请求在配置响应之后发起。任一记录恢复未验证时，禁止创建新实验，换别名或记录编号也不能绕过。

配置恢复和凭据可导入分别判断。用户自己的会话发生变化仍可验证配置恢复，但候选运行字段导入继续要求严格上下文匹配；新 `d` 仅在两阶段实际请求一致并通过只读核验后才可替换。采样日期来自实际签名时间，不能通过重放或恢复延长。导入不会增加消费权限，订单 POST 接受情况仍未验收。

## 先运行离线检查

在仓库根目录执行，Python 3.11+，无官方 App、真实账号或采样依赖也可运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s experiments/readonly_runtime -p 'test_*.py'
.venv/bin/python experiments/readonly_runtime/runner.py --help
```

## 实机入口与恢复

以下是维护者参考，不是推荐新用户直接运行的接入流程。每次实机实验需要本机用户授权其账号、两个域名的受限证书及配置变化。采用主项目支持的固定 Mac 构建，准备本人已接入的账号；不读取受保护的 App 容器/钥匙串，不启用系统代理，不提交测试订单。

采样 Python 环境需要 Python 3.12+ 和项目固定的 mitmproxy 版本；安装方法见[首次接入](../../docs/first-connection.md)。例如独立建立 `.tools/capture-venv` 并在其中安装 `-e '.[sampling]'`。主进程用安装过本项目的 `.venv/bin/python`；`--mitmdump` 指定采样环境的可执行文件，子进程所需源码路径由 runner 传入。

下面的 `personal` 是示例别名，`.private/research` 必须替换为本人实际账号所在目录；不能换目录丢弃购买日志。原 `api.mcd.cn` 证书按[标准证书流程](../../docs/sampling-certificate.md)准备；新增配置域名证书独立管理：

```sh
# 仅生成本机材料，不添加系统信任
.venv/bin/python experiments/readonly_runtime/runner.py \
  --root .private/research prepare-cert

# 以下安装由用户明确授权并在 macOS 系统窗口验证
.venv/bin/python experiments/readonly_runtime/runner.py \
  --root .private/research install-cert

.venv/bin/python experiments/readonly_runtime/runner.py \
  --root .private/research cert-status

# 维护者经授权才启动；初次调查省略 --update-account
.venv/bin/python experiments/readonly_runtime/runner.py \
  --root .private/research --mitmdump .tools/capture-venv/bin/mitmdump \
  run personal --allow-local-config-change

# 中断时用输出中的原 CAPTURE_DIRECTORY，只恢复，不重新采样
.venv/bin/python experiments/readonly_runtime/runner.py \
  --root .private/research --mitmdump .tools/capture-venv/bin/mitmdump \
  recover personal CAPTURE_DIRECTORY
```

两阶段各最多120秒。看到 `COLLECT_READY` 或 `RESTORE_READY` 后进入钱包；`PHASE_ENDING` 表示工具主动退出 App，随后停止采样进程。结束时读取 `FINISHED` 的 `restoration_verified`、`restoration_required` 和完整 `cleanup`。仅恢复模式不要求已取得运行字段即可成功退出。不要把窗口突然关闭当成崩溃或成功证据。

`run`/`recover` 的 `--update-account` 是可选的真实本地账号改写，先前没有成功的实机导入证据。单独 `import ALIAS CAPTURE_DIRECTORY` 也会先做只读联网核验；“只导入文件”并非离线操作。重复导入保留原文件和观察时间，不重新授予消费权限。

用户不再需要该配置域名证书时，可显式运行 `--root <原目录> remove-cert`；不影响原 API 证书。持久根证书和私钥按用户授权留在本机，临时服务端私钥按每轮资源清单清理。父进程被强制终止时不能假定清理已经执行；未知来源文件不得删除。

## 接下来要做的工作

1. 用新的字段名诊断确认首个真实 v5 上下文差异，保留失败，不输出头值；不要先删除校验或伪造字段。
2. 核验新会话下两阶段请求一致性、恢复及清理，再执行候选导入的固定钱包/查单 GET。保存真实来源和原日期，验证策略、偏好、购买日志不变。
3. 分析 App 查询失败、采样阶段之间连接/会话变化及过早打开 App 的预检竞态；不要通过放宽全网路由或系统代理解决。
4. 整理重复的证书生命周期代码和源码入口，再设计正式 CLI/MCP 集成。验证新运行字段下的真实订单需要另有一笔完整消费授权，不能重用历史已付款订单的授权。

原始请求、账号文件、官方二进制、证书/私钥、真实日志与本机路径均不在此目录。需要真实证据时，只能由本人另行授权在本机使用。
