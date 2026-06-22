# A 股 5 分钟仓位监控 Web V3：AKShare 免费验证版

这是一个“**规则触发提醒 + 人工决定交易**”的本地 Python 工程。程序使用 AKShare 读取沪深京 A 股近期 5 分钟行情，结合你录入的仓位和风控阈值生成提醒，并在浏览器中展示运行数据；支持企业微信群机器人和阿里云短信通知。

> 本工程不自动下单，也不构成投资建议。首次运行请保持 `dry_run: true`，先验证行情、规则和页面显示，再启用真实通知。

## V3 与之前版本的区别

| 项目 | Web V2 | Web V3（本版本） |
|---|---|---|
| 5 分钟行情来源 | Tushare `rt_min` | AKShare 新浪 `stock_zh_a_minute(period="5")`，失败自动回退东财 `stock_zh_a_hist_min_em` |
| 股票名称库 | Tushare `stock_basic` | AKShare `stock_info_a_code_name()` |
| 是否需要 `TUSHARE_TOKEN` | 需要 | **不需要** |
| 是否需要购买 Tushare 实时分钟权限 | 需要 | **不需要** |
| 使用定位 | 付费数据接入 | 免费验证提醒逻辑与界面 |

AKShare 读取公开财经网页数据，适合先验证策略和提醒流程；上游网页或接口结构发生调整时，公开接口可能临时不可用。确认需要长期稳定使用后，再考虑付费数据源或 MiniQMT。

## 主要功能

- 浏览器仪表盘：查看监控清单、最新价、持仓盈亏、市值仓位、触发信号与执行日志。
- 单票详情页：最近 5 分钟收盘价曲线、近期分钟行情表、信号记录、仓位和风控参数编辑。
- 网页添加股票：支持代码 `600000`、`600000.SH`；同步股票名称库后可输入名称，如 `浦发银行`。
- Excel 批量录入：`data/positions_template.xlsx` 可录入多只标的，然后在网页导入 SQLite。
- 定时监控：默认每 5 分钟、K 线周期结束后 20 秒检查一次。
- 通知通道：企业微信发送提醒；阿里云短信默认只发送 `HIGH` 风险信号。
- 局域网访问保护：可配置访问口令，供手机或同一 Wi-Fi 内其他设备查看。

## 数据流

```text
浏览器新增 / Excel 导入  →  SQLite 监控清单
                                   ↓
每 5 分钟定时任务       →  AKShare / 新浪优先 + 东方财富回退 5 分钟行情
                                   ↓
                               固定规则引擎
                                   ↓
      网页面板记录 + 企业微信通知 + HIGH 级短信通知
```

## 使用的 AKShare 接口

| 用途 | AKShare 接口 | 本工程配置 |
|---|---|---|
| 5 分钟行情 | `stock_zh_a_hist_min_em` | `period="5"`, `adjust=""`（不复权） |
| 股票代码和简称 | `stock_info_a_code_name` | 同步沪深京 A 股名称库 |

官方文档：

- `https://akshare.akfamily.xyz/data/stock/stock.html`
- `https://github.com/akfamily/akshare`

使用不复权行情，是为了让提醒中的价格尽量对应实际观察到的成交价格，避免复权价格与持仓成本口径混淆。

## 目录结构

```text
a_share_monitor_web_v3_akshare/
├── config/config.yaml                # 调度、行情、通知与网页配置
├── data/positions_template.xlsx      # Excel 批量录入模板
├── logs/                             # 运行日志
├── state/                            # SQLite 运行数据库
├── scripts/
│   ├── run_web_windows.bat
│   └── run_web_linux.sh
├── src/stock_monitor/
│   ├── webapp.py                     # Flask 网页面板
│   ├── runner.py                     # 定时监控服务
│   ├── state.py                      # SQLite 仓位/行情/信号存储
│   ├── market.py                     # AKShare 行情与名称库接口
│   ├── signal_engine.py              # 风控触发逻辑
│   └── templates/ + static/          # 页面模板与样式
├── .env.example                      # 通知凭据和网页口令模板
└── requirements.txt
```

## 安装步骤

### Windows PowerShell

```powershell
cd a_share_monitor_web_v3_akshare
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
Copy-Item .env.example .env
```

### Linux / Orange Pi

```bash
cd a_share_monitor_web_v3_akshare
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
cp .env.example .env
```

AKShare 官方仓库标注 Python 版本要求为 64 位 Python 3.9 或以上；本工程的 `pyproject.toml` 要求 Python 3.10 或以上。

## 首次启动：无需填写 Tushare Token

本版本的 `.env` 中**没有** `TUSHARE_TOKEN`，无需注册或购买 Tushare 权限。若先仅验证页面和行情，可以暂时不填写任何通知凭据。

确认 `config/config.yaml` 仍为：

```yaml
app:
  dry_run: true
```

启动网页：

```bash
stock-monitor web --config config/config.yaml
```

本机浏览器打开：

```text
http://127.0.0.1:8501
```

## 添加股票和验证行情

### 方式一：直接用代码添加

在网页“添加监控股票”输入：

- `600000` → 自动保存为 `600000.SH`；
- `000001` → 自动保存为 `000001.SZ`；
- `300750` → 自动保存为 `300750.SZ`；
- `600000.SH` → 直接保存完整代码。

随后点击“立即检查一次”。在非交易时段验证历史/近期分钟数据时，可勾选“忽略交易时段限制”。

### 方式二：按名称添加

1. 在首页点击“同步股票名称库”；
2. 在添加框输入名称，例如 `浦发银行`；
3. 在候选结果中点击“添加”。

名称库同样来自 AKShare，不再需要 Tushare `stock_basic` 权限。

### 方式三：Excel 批量导入

1. 打开 `data/positions_template.xlsx`；
2. 在“持仓录入”工作表填写代码、仓位和风控参数，将“启用”列设为“是”；
3. 保存 Excel；
4. 在网页点击“导入 Excel 持仓”。

Excel 只作为批量导入入口；导入之后，以网页详情页和 `state/stock_monitor.db` 中的数据为准。

## 企业微信与短信配置

### 企业微信群机器人

在 `.env` 中填写：

```dotenv
WECOM_BOT_WEBHOOK=你的企业微信群机器人Webhook
```

若群里包含微信用户或你在微信侧看到“暂不支持的消息类型”，请在 `config/config.yaml` 中将企业微信消息类型切换为文本：

```yaml
notifications:
  wecom:
    msg_type: "text"
```

`markdown` 在企业微信客户端展示更完整，但微信侧/互通场景可能不支持。

### 阿里云短信

在 `.env` 中填写 RAM 凭据：

```dotenv
ALIBABA_CLOUD_ACCESS_KEY_ID=你的AccessKeyID
ALIBABA_CLOUD_ACCESS_KEY_SECRET=你的AccessKeySecret
```

再在 `config/config.yaml` 中填写：

```yaml
notifications:
  aliyun_sms:
    phone_numbers: "你的手机号"
    sign_name: "已审核短信签名"
    template_code: "已审核短信模板CODE"
```

短信模板建议包含变量：`${stock}`、`${signal}`、`${price}`。默认只有 `HIGH` 级风险信号才发送短信，其他提醒只发送企业微信。

当网页数据与通知预览确认无误后，将：

```yaml
app:
  dry_run: false
```

然后测试通知：

```bash
stock-monitor notify-test --config config/config.yaml --channel both
```

## 手机或同一局域网设备访问

将 `config/config.yaml` 改为：

```yaml
web:
  host: "0.0.0.0"
  port: 8501
```

在 `.env` 中设置访问口令和会话密钥：

```dotenv
MONITOR_WEB_ACCESS_TOKEN=设置一个较长的访问口令
MONITOR_WEB_SECRET_KEY=设置一个随机长字符串
```

启动后，在手机浏览器输入：

```text
http://运行程序的电脑局域网IP:8501
```

本程序定位为本地/局域网工具，不要将网页端口直接暴露到公网。

## 提醒规则

| 规则 | 条件概述 | 默认级别 | 通知策略 |
|---|---|---:|---|
| 固定止损 | 当前价格相对成本跌幅达到设定比例 | HIGH | 企业微信 + 短信 |
| 盈利回撤保护 | 达到止盈触发线后，从观测最高价回撤超过阈值 | HIGH | 企业微信 + 短信 |
| 仓位超限 | 当前市值仓位超过最大仓位比例 | MEDIUM | 企业微信 |
| 趋势转弱 | 收盘价低于 MA20 且 MA5 低于 MA20 | MEDIUM | 企业微信 |
| 放量突破观察 | 放量突破近 20 根 K 线高点且均线满足条件 | LOW | 企业微信 |

同一股票同一规则的真实通知默认有 30 分钟冷却期。`dry_run: true` 时只输出和展示预览，不写入通知冷却记录。

## 常用命令

```bash
# 校验 Excel 模板
stock-monitor validate --config config/config.yaml

# 将 Excel 启用标的导入网页数据库
stock-monitor import-excel --config config/config.yaml

# 通过 AKShare 同步名称库
stock-monitor sync-stock-master --config config/config.yaml

# 执行一次行情检查；非交易时段调试可追加 --force-session
stock-monitor once --config config/config.yaml --force-session

# 启动网页并自动运行定时任务
stock-monitor web --config config/config.yaml

# 只启动网页，不启用后台定时任务
stock-monitor web --config config/config.yaml --no-scheduler

# 生成某板块的候选观察股；--strategy 见下方 strategy list
stock-monitor hot-sector candidates --sector 电网设备 --strategy short_term_resonance --config config/config.yaml
```

### 候选筛选策略（可导入/导出的配置文件）

策略以「一策略一文件」的形式放在 `config/strategies/*.yaml`，自包含 `id / display_name /
scorer / weights / thresholds / risk_penalties` 等字段。加载顺序：`config/strategies/` 目录优先
→ `config.yaml` 内联 `strategy_configs` 回退 → 内置默认兜底。新增策略 = 往目录里放一个文件，
网页下拉与 CLI 会自动识别；同因子换权重/阈值即可得到不同选股风格，无需改代码。

```powershell
# 列出已注册策略（id、显示名、打分引擎）
stock-monitor strategy list --config config/config.yaml

# 导出某策略为 YAML（备份/分享/二次调参）；省略 --out 则打印到 stdout
stock-monitor strategy export short_term_resonance --out my_strategy.yaml --config config/config.yaml

# 从 YAML 导入策略（落盘前做 schema 校验，校验失败不写入并返回非零退出码）
stock-monitor strategy import my_strategy.yaml --id my_variant --config config/config.yaml
```

## V3.1：行情断连修复与诊断

V3.1 针对东方财富接口可能出现 `RemoteDisconnected` 或 `ProxyError` 的情况进行了修改：

- 5 分钟分钟行情默认优先请求新浪接口 `stock_zh_a_minute`；
- 新浪失败时自动回退东方财富接口 `stock_zh_a_hist_min_em`；
- 每个来源默认最多重试 2 次；
- 程序自动把 `.sina.com.cn` 与 `.eastmoney.com` 加入 `NO_PROXY`，避免普通环境变量代理拦截行情请求；
- 若代理软件启用了 TUN/全局代理，仍需在代理规则中将相关域名配置为 `DIRECT`。

直接诊断两个行情来源：

```powershell
python scripts/diagnose_market_sources.py 600276.SH
```

可在 `config/config.yaml` 调整：

```yaml
market:
  primary_source: "sina"
  fallback_source: "eastmoney"
  retry_times: 2
  retry_backoff_seconds: 0.8
  bypass_proxy_for_market_hosts: true
```

## 开启 VPN / 代理时的分流配置

本工程读取的行情接口（东方财富、同花顺、新浪）都是**国内站点，会拒绝境外 IP 的连接**。开启 VPN 后日志里常见如下报错，且会一路重试到兜底源甚至完全失败：

```text
provider=akshare_hot_sector source=eastmoney_direct attempt=3/3 failed=ConnectionError: RemoteDisconnected('Remote end closed connection without response')
```

程序在代码层面已尽力规避代理：直连请求都设置了 `session.trust_env = False`（忽略环境变量与 Windows 系统代理），并把行情域名写入 `NO_PROXY`。这能绕过**代理模式**的 VPN，但**绕不过 TUN / 全局模式**——后者在操作系统网络层接管所有流量，应用层无法干预。此时必须在 VPN 客户端把这些域名配置为**直连（DIRECT / 绕过）**。

应用实际访问的全部域名：

```text
# 东方财富（行情 / 板块 / 资金流）
push2.eastmoney.com
push2his.eastmoney.com
17.push2.eastmoney.com
datacenter-web.eastmoney.com
quote.eastmoney.com

# 同花顺（板块 / 资金流 / 成分股 兜底源）
data.10jqka.com.cn
q.10jqka.com.cn

# 新浪（个股日线 / 分钟 兜底源）
hq.sinajs.cn
money.finance.sina.com.cn
vip.stock.finance.sina.com.cn
```

**Clash / Mihomo** 在配置的 `rules:` 段最前面加入（用后缀可覆盖所有子域名）：

```yaml
rules:
  - DOMAIN-SUFFIX,eastmoney.com,DIRECT
  - DOMAIN-SUFFIX,10jqka.com.cn,DIRECT
  - DOMAIN-SUFFIX,sina.com.cn,DIRECT
  - DOMAIN-SUFFIX,sinajs.cn,DIRECT
  # 也可直接让所有国内 IP 走直连（确认此规则在代理规则之前）：
  - GEOIP,CN,DIRECT
  # ……原有规则放在后面
```

**其它客户端**：找「绕过 / Bypass / 直连规则」或「分应用代理」，把上述域名（或直接把 `python.exe` 进程）加入直连白名单即可。配置后即使开着 VPN，这些国内行情站也会用本地国内 IP 直连，`eastmoney_direct` 即可成功，无需再依赖同花顺/新浪兜底源。

## 免费数据源的注意事项

- AKShare 本身无需你提供行情 Token，但它读取的公开网站数据并不是面向个人交易告警的商业 SLA 服务。
- 若上游网站限制访问、接口字段变化或网络异常，页面会显示执行失败或部分失败；此时不应依赖提醒做即时决策。
- 本版本适合验证：网页操作、仓位录入、五分钟规则触发、企业微信/短信发送流程。
- 你确认长期使用且对稳定性有要求后，再切换付费行情源或券商 MiniQMT 读取真实持仓与行情。

## 安全注意事项

- 不要把 `.env`、企业微信 Webhook 或阿里云 AccessKey 提交到 Git；
- 初期保持 `dry_run: true`；
- 此版本不接券商交易接口，也不会执行自动买卖；
- 网页开放局域网访问时设置访问口令，避免公网暴露；
- 实际交易由你本人结合风险承受能力和交易计划决定。
