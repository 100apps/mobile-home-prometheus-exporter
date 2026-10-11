# 移动爱家 / AndLink 原始遥测 Prometheus exporter

一个 Python 文件，标准库实现、无第三方依赖。读取**明确选定**的插座，将云端 `power`、`volts`、`current`、`powerConsumption` 转为 Prometheus 原始值指标。只提供 `/metrics` 和 `/healthz`，没有看板、开关、清零或设备配置接口。

**这些字段的单位、倍率和设备上报周期尚未实证确认。** 不把它们标为已验证 W/V/A/kWh，不用电压乘电流替代有功功率，不把缺失值填成 0。此前实机返回型号名为 S30m、类型为 `590513`；APK 中 S30c 类型 `30944` 不足以证明两者单位一致。

## 启动

需要已安装的 [uv](https://docs.astral.sh/uv/) 和 Python 3.11+。文件含 PEP 723 元数据，无依赖安装；也可直接 `python socket_exporter.py`。

自行创建 `config.toml`（程序不会生成它）：

```toml
# 建议用户名/密码通过环境变量提供；也可在此填写以下两项：
# username = "替换为移动爱家登录手机号"
# password = "替换为密码"
listen = "127.0.0.1"
port = 9188
cache_ttl = 60
timeout = 10

[devices]
plug1 = "name:替换为App内插座的精确名称"
# plug2 = "id:替换为你已知的设备ID"
```

使用本地终端提示输入，避免密码写进命令历史（不要把真实密码发到聊天或问题反馈中）：

```powershell
# PowerShell：环境变量只对当前终端及其子进程生效。
$env:SOCKET_EXPORTER_USERNAME = Read-Host '移动爱家手机号'
$credential = Get-Credential -UserName $env:SOCKET_EXPORTER_USERNAME -Message '输入移动爱家密码'
$env:SOCKET_EXPORTER_PASSWORD = $credential.GetNetworkCredential().Password
uv run socket_exporter.py --config config.toml
# 结束后清理本终端的变量（程序运行时不会自动清除父终端变量）。
Remove-Item Env:SOCKET_EXPORTER_USERNAME, Env:SOCKET_EXPORTER_PASSWORD
```

```bash
# Bash
read -r -p '移动爱家手机号: ' SOCKET_EXPORTER_USERNAME
read -r -s -p '移动爱家密码: ' SOCKET_EXPORTER_PASSWORD; echo
export SOCKET_EXPORTER_USERNAME SOCKET_EXPORTER_PASSWORD
uv run socket_exporter.py --config config.toml
unset SOCKET_EXPORTER_USERNAME SOCKET_EXPORTER_PASSWORD
```

环境变量也可能被同用户/管理员进程读取；配置文件如含密码，请限制文件权限。程序不自动保存配置、密码或 token。仓库忽略本地配置，仍应在提交前人工检查。

离线检查配置，不登录、不联网：

```sh
uv run socket_exporter.py --config config.toml --check-config
```

如已安装合适的 Python、且希望禁止 uv 下载解释器，可使用：

```sh
uv run --offline --no-cache --no-managed-python socket_exporter.py --config config.toml
```

默认监听 `127.0.0.1:9188`，不与旧看板的 9187 端口冲突。`/healthz` 返回 `ok` 只表示进程可响应，**不表示账号、设备或云端正常**。首次 `/metrics` 会启动一次后台登录和读取；该次响应可能只有状态指标，下一次 scrape 才能看到读数。

## 参数、环境变量和设备选择

优先级：**CLI > `SOCKET_EXPORTER_*` 环境变量 > 指定 TOML > 默认值**。未知配置项或无效值会拒绝启动，错误信息不回显参数值。

| CLI | 环境变量后缀 | TOML | 默认值 / 限制 |
|---|---|---|---|
| `--username` | `USERNAME` | `username` | 必填，手机号 |
| `--password` | `PASSWORD` | `password` | 必填；不推荐 CLI，可能暴露在进程列表/历史中 |
| `--listen` | `LISTEN` | `listen` | `127.0.0.1`；IPv4 |
| `--port` | `PORT` | `port` | `9188` |
| `--cache-ttl` | `CACHE_TTL` | `cache_ttl` | `60` 秒，10–3600 |
| `--timeout` | `TIMEOUT` | `timeout` | `10` 秒，1–60；网络 I/O 超时，DNS 仍受系统解析器影响 |
| `--device ALIAS=SELECTOR`（可重复） | `DEVICES` | `[devices]` | 1–8 个设备 |
| `--config` | `CONFIG` | — | 无默认配置路径 |

`--device` 整体替换配置/环境变量的设备映射，不合并。`SOCKET_EXPORTER_DEVICES` 是 JSON 对象，例如 `{"plug1":"name:替换为精确名称"}`。TOML `[devices]` 的键或 CLI 的 `ALIAS` 是公开指标标签，建议用不包含账号、真实姓名或设备 ID 的别名。

- `name:名称`：登录后读取一次设备列表，仅精确匹配 `name` 或 `desc`、类型 `30944` / `590513` 的指定设备；名称不唯一或找不到则停止，不自行选择其他设备。
- `id:设备ID`：使用你已知的设备 ID，跳过列表查询。样例没有真实 ID；程序不会扫描全部设备的遥测，也不会打印设备列表。
- 重复指向同一设备的配置会拒绝采集。名称/选择有误时修正配置并重启。

## Prometheus

Prometheus 与 exporter 在同一主机时：

```yaml
scrape_configs:
  - job_name: mobile_home
    scrape_interval: 30s
    scrape_timeout: 5s
    static_configs:
      - targets: ['127.0.0.1:9188']
```

例如 `andlink_raw_parameter{device="plug1",parameter="power",unit_status="unverified"}`。四个原始字段都是 **gauge**；`powerConsumption` 的累计周期和重置语义未知，不能直接当作 kWh counter 或对其使用 `rate()` 解释耗电功率。

| 指标 | 含义 |
|---|---|
| `andlink_raw_parameter` | 原始数值，无单位换算；保留真实 0，缺失/非有限值不输出 |
| `andlink_device_sample_available` | 当前缓存至少有一个可用原始字段；不代表设备刚测量 |
| `andlink_cloud_connected` | 云端连接标记；未知时省略 |
| `andlink_last_fetch_timestamp_seconds` | HTTP 读取时间，不是设备测量时间 |
| `andlink_exporter_up` | 最近整轮读取成功且各响应缓存未过期 |
| `andlink_exporter_refresh_in_progress` | 后台正在读取；全局只有一轮、设备请求串行 |
| `andlink_exporter_stopped` | 已停止，需检查原因并手动重启 |
| `andlink_exporter_error{reason=...}` | 固定错误码，不包含上游错误正文 |
| `andlink_exporter_refresh_errors_total` | 本进程失败读取轮数 |
| `andlink_exporter_retry_after_seconds` | 剩余退避时间；重启不能免除服务端限制 |

读取由 scrape 触发，不运行独立定时采集。成功后至少等待 `cache_ttl` 才会发起下一轮；并发 scrape 共享同一个后台任务，不重复登录/读取，也不阻塞等待上游。后台刷新时短暂保留原缓存，超过 `2 × cache_ttl` 后省略原始读数；任一刷新失败立即清空本轮缓存，避免失败后继续显示旧值。明确离线的设备不输出原始值。Prometheus 的 `up` 仅表示 HTTP scrape 成功，请同时告警 `andlink_exporter_up`。

默认 60 秒是保守本地设置，**不是已知的官方频率限制**；缩短 scrape 周期不会绕过缓存。云端值改变也不证明设备每秒采样；同一旧值可能在多次成功请求中重复。

## 认证、安全和故障排查

登录沿用已验证的两步语义：移动爱家密码登录返回 `JSESSIONID`，再通过 AndLink `loginByApp` 换取内存中的 `API_KEY`。密码哈希仅用于服务端既有协议，不是推荐的本地密码存储方式。所有上游请求验证 HTTPS 证书，不跟随重定向、不使用代理环境变量，也不允许指定其他上游地址。详情 GET 的 `checkConnected=True` 与已有协议一致，可能触发云端连接检查，但不调用设备开关或配置接口。

每个进程最多进行**一次密码登录尝试**。登录失败、token 失效或设备选择异常时停止，不无限重试密码；检查账号/配置后手动重启。密码的程序引用在首次尝试后释放，但不承诺从进程内存中安全擦除；环境变量及用户自己的配置仍由用户管理。

- `rate_limited`：尊重 `Retry-After`（秒数或 HTTP 日期）；无有效值时至少等待 900 秒。不要反复重启绕过等待。
- `timeout` / `connection_failed` / `http_error`：网络、TLS 或非 200 响应；指数退避，通常最多 900 秒，服务端更长等待优先。
- `authentication_failed`：停止，检查登录状态/密码；没有自动重新登录。
- `device_selection_failed`：精确名称未找到、重名或重复设备，检查配置；不输出私密设备列表。
- `invalid_response` / `upstream_rejected`：接口结构或业务响应不符合当前协议。不要把含 token、账号或原始上游正文的日志公开。

`/metrics` **没有服务端鉴权**。上游账号密码不是 HTTP 服务密码。若配置 `--listen 0.0.0.0`，就会监听所有 IPv4 网卡；程序不修改防火墙。远程/公网部署的 TLS、鉴权、网络访问限制由用户自己配置，推荐只允许 Prometheus 访问。

程序**不写本地遥测、历史、日志、数据库或会话文件**；读数和 token 仅在内存中，进程退出后丢失，重启需重新登录。用户自备 TOML 是允许的输入；uv/Python 自身可能使用运行时缓存，操作系统也可能使用交换空间或转储，这不属于应用的遥测持久化。历史只交给 Prometheus 保存。

本仓库不含任何真实配置、凭据、设备 ID 或遥测历史。发布验证使用合成响应，不以真实密码运行新程序；真实单位和上报周期仍需独立确认。
