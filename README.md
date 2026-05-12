# AetherSwap

AetherSwap 是一个面向 CS2 / Steam 饰品市场的数据采集、套利雷达和半自动交易执行系统。项目当前以本地运行为主，采用 **FastAPI + SQLAlchemy + SQLite + 原生 HTML/JS + DataEngine**，重点解决多平台行情抓取、实时复验、平台登录态保持、代理池调度和交易记录沉淀。

> 本项目仅用于学习、研究和个人风控实验。第三方平台可能限制自动化请求、下单和批量访问，请务必遵守平台规则并自行承担账号、资金、网络和合规风险。

## 当前能力

- 多平台行情：Steam、Buff、UUYP、ECO，并预留 C5Game Provider 扩展位。
- 套利雷达：支持利润率、成交量、价格区间等多维筛选，并展示平台价、Steam 价和差价。
- Smart JIT：直接购买前按数据新鲜度决定是否跳过实时测价，求购单默认跳过 JIT，降低风控压力。
- Graceful Degradation：下单失败后不立即穿透重爬，避免被秒杀或风控时触发请求风暴。
- 平台会话管理：统一 Provider / Preflight 架构，集中处理 Cookie、Token、CSRF、设备头、冷却与健康状态。
- 代理池调度：Steam / Buff 等请求会从代理池动态选择出口，并在日志中标记 `(Direct)` 或脱敏代理。
- SteamDT 补充数据源：可选启用高频行情补充，并遵守时间戳覆盖风控。
- 时间加权写入：只有新数据时间不早于数据库现有 `updated_at` 时才覆盖价格，防止旧数据污染新行情。
- SQLite WAL：数据库开启 WAL 与 `synchronous=NORMAL`，改善 WebUI 查询和主引擎写入并发。
- 交易记录与通知：手动/自动执行结果写入 SQLite；下单成功可触发 Webhook 通知。
- 源头币种锁定：请求层强制 CNY，避免代理地区导致 USD/CNY 混价。
- 平台能力注册表：`BUFF` / `UUYP` / `ECO` / `C5Game` / `Steam` 的买入、求购、上架、改价、撤单、发货、报价接收能力集中声明，可通过 API 自检。
- PlatformAction 自动化账本：自动买入、动态求购、Steam 求购、Steam/第三方上架、改价、撤单、发货、接受报价统一进入 `platform_action` 状态机。
- 持久化 worker：后台按 `PlatformAction.next_check_at` 轮询执行，支持租约、重试、等待平台、等待报价、等待 Steam 确认和资金占用释放。
- 风控预算：当前默认记录 20% 短线回撤容忍，并执行单饰品 ¥3000、单品类 ¥5000、单平台日自动成交 ¥5000、Steam 余额锁定 5 天上限等约束。
- 卖侧自动化底座：库存、Steam 在架和 C5 订单快照可规划上架、改价、撤单、发货与报价接收动作；默认只规划不提交。
- SAFE_MODE 烟测：平台能力和 worker 闭环默认不实例化真实平台客户端，便于先验证状态机、风控和队列行为。

## 快速启动

Windows 环境推荐使用项目内虚拟环境：

```powershell
cd I:\cs\AetherSwap
.\.venv\Scripts\activate
python run.py
```

`run.py` 会启动 FastAPI 后端并打开本地控制台：

```text
http://127.0.0.1:28472
```

也可以手动启动 Web API：

```powershell
.\.venv\Scripts\activate
python -m uvicorn app.api:app --host 127.0.0.1 --port 28472
```

单独启动数据引擎：

```powershell
.\.venv\Scripts\activate
python DataEngine\master_loop.py
```

## 常用命令

```powershell
# 基础语法检查
python -m compileall DataEngine app utils

# 同步第三方静态基准数据和本地平台 ID 映射
python DataEngine\sync_baseline.py

# 运行交易执行器，默认建议先保持 Safe Mode
$env:SAFE_MODE_ENABLED="true"
python DataEngine\trade_executor.py

# 测试 SteamDT 补充源；未配置 api_url 时会安全跳过
python DataEngine\steamdt_fetcher.py

# 应用正式数据库迁移；默认目标是 config/market_data.db
python -m alembic upgrade head

# 交易自动化核心测试
python -m pytest tests/test_platform_action.py tests/test_trading_worker_runtime.py tests/test_trading_platform_adapters.py tests/test_trading_safe_mode_loop.py
```

## 目录结构

```text
AetherSwap/
├─ app/                         FastAPI 后端、API 路由、WebUI 挂载
│  ├─ api.py                    主 API 入口
│  └─ services/
│     ├─ platform_sessions.py   平台登录态 Provider 与健康状态
│     ├─ trading/               PlatformAction、adapter、worker、风控和卖侧自动化
│     └─ notifier.py            Webhook 通知
├─ DataEngine/                  行情抓取、基准同步、JIT、交易执行
│  ├─ master_loop.py            主调度循环
│  ├─ main_engine.py            批量行情刷新与机会生成
│  ├─ trade_executor.py         自动/手动交易执行逻辑
│  ├─ proxy_pool.py             请求级代理选择与日志标签
│  ├─ sync_baseline.py          CSGOTrader / 本地 mapper 同步
│  └─ steamdt_fetcher.py        SteamDT 高频补充源
├─ alembic/                     正式数据库迁移，包含 platform_action 表
├─ buff/                        Buff 买入/求购与平台接口
├─ uuyp/                        UUYP 买入/求购与平台接口
├─ eco/                         ECO 平台接口
├─ c5game/                      C5Game OpenAPI 发货、订单和报价查询客户端
├─ steam/                       Steam 平台辅助接口
├─ utils/                       配置、代理、日志等公共工具
├─ web/                         原生 HTML / JS / CSS 前端
├─ config/                      本地配置与状态文件
├─ logs/                        运行日志
└─ run.py                       本地一键启动入口
```

## 核心架构

### 数据流

```text
静态底座(CSGOTrader / mapper)
        ↓
批量行情抓取(Steam / Buff / UUYP / ECO / SteamDT)
        ↓
时间戳覆盖防御(upsert_market_price_if_fresh)
        ↓
套利雷达筛选与监控池
        ↓
Smart JIT 复验
        ↓
平台 Provider 预检与交易执行
        ↓
PlatformAction 账本 / 状态机 / 风控预算
        ↓
持久化 worker 执行、轮询与重试
        ↓
交易记录 / Webhook 通知 / 自动化面板
```

### 自动化交易底座

`PlatformAction` 是本期开始的交易执行唯一事实源。旧的 `TradeExecutionRecord` 仍用于兼容历史记录和 UI 查询，但新的自动化动作都应进入 `platform_action`，由状态机、风险预算和 worker 统一推进。

支持的动作类型：

- 买入侧：`direct_buy`、`purchase_order`、`steam_buy_order`、`poll_order`。
- 卖出侧：`steam_listing`、`platform_listing`、`reprice_listing`、`cancel_order`。
- 交割侧：`deliver_order`、`accept_trade_offer`。

核心状态：

```text
queued -> processing -> submitted / waiting_platform / waiting_trade_offer
       -> waiting_steam_confirm / waiting_settlement / retry_wait
       -> succeeded / failed / cancelled / expired / risk_blocked
```

关键行为：

- `idempotency_key` 防止同一机会重复创建动作。
- `filled_quantity`、`remaining_quantity`、`filled_amount_cny` 和 `released_budget_cny` 记录求购部分成交，worker 会释放未成交部分的活跃占用。
- `risk_category` 用规范化饰品品类聚合风险，降低同一类饰品多磨损、多外观分散穿透限额的概率。
- `TradeOfferService` 会先校验收货报价，不接受需要本账号额外给出物品的报价；不安全报价会被标记为 `unsafe_offer`。

常用 API：

```text
GET  /api/trade/platform_capabilities
GET  /api/trade/platform_actions
GET  /api/trade/platform_action_summary
GET  /api/trade/automation_overview
POST /api/trade/platform_actions
POST /api/trade/platform_actions/run_once
POST /api/trade/platform_actions/smoke
POST /api/trade/platform_actions/worker_start
POST /api/trade/platform_actions/worker_stop
POST /api/trade/platform_actions/worker_wake
POST /api/trade/seller_actions/plan
POST /api/trade/seller_actions/scan
POST /api/trade/seller_actions/scanner_run_once
```

### 安全默认值

自动化执行默认关闭，并且 SAFE_MODE 默认开启：

```json
{
  "trading_worker": {
    "enabled": false,
    "safe_mode": true,
    "poll_interval_seconds": 10,
    "batch_size": 10,
    "lease_seconds": 60
  },
  "seller_snapshot_scanner": {
    "enabled": false,
    "commit": false,
    "interval_seconds": 3600
  }
}
```

`POST /api/trade/platform_actions/run_once` 默认按 SAFE_MODE 执行。`POST /api/trade/platform_actions/smoke` 可检查平台能力矩阵；当 `safe_mode=true` 时，即使传入 `live_preflight=true` 也不会实例化真实平台客户端。

### 登录态与风控

平台请求不再散落在业务代码中拼 Header，而是由 Provider 统一完成：

- `BuffSessionProvider`：Cookie、CSRF、CNY Cookie、业务冷却。
- `UuypAppSessionProvider`：App Token、设备头、`uk`、直连策略、登录/风控错误识别。
- `EcoSessionProvider`：ECO Cookie / ID 映射预检。
- `C5OpenApiProvider`：预留 C5Game OpenAPI 接入位。

健康状态和冷却信息保存在：

```text
config/platform_session_state.json
```

前端或脚本可通过接口查看平台状态：

```text
GET /api/platform/session_state
```

## Smart JIT 与降级交易

配置项：

```text
JIT_BYPASS_MINUTES=5
```

行为规则：

- `direct_buy` / `buy_listing` / `instant_buy`：如果数据库行情在绕过窗口内，直接使用现有数据下单。
- `purchase_order` / `platform_order`：挂求购单默认跳过 JIT，使用最新大盘数据。
- 免检下单失败后不立刻触发重爬或连续重试，避免接口压力和风控放大。
- 登录态缺失、平台鉴权熔断或平台 ID 缺失时，机会会进入 `verifying` / `mapping_missing` 等可观察状态。

## 代理池与负载均衡

代理池配置位于 `config/app_config.json` 的 `proxy_pool`：

```json
{
  "proxy_pool": {
    "enabled": true,
    "strategy": 1,
    "test_url": "https://ipv4.webshare.io/",
    "timeout_seconds": 10,
    "proxies": [
      {
        "host": "127.0.0.1",
        "port": 7890,
        "username": "",
        "password": ""
      }
    ]
  }
}
```

说明：

- 多个代理会按权重/随机策略选择，请求日志会输出脱敏代理标签。
- 如果只配置 `127.0.0.1`，AetherSwap 会提示这是本地单出口。建议在 Clash / v2ray 侧启用负载均衡组，让本地端口背后真正分流。
- UUYP 下单链路默认倾向直连或专门的 bypass 策略，减少代理 IP 触发云盾拦截。

## SteamDT 补充源

SteamDT 默认关闭，需要显式配置：

```json
{
  "steamdt": {
    "enabled": false,
    "api_url": "",
    "interval_seconds": 600,
    "timeout": 15,
    "limit": 300
  }
}
```

也可使用环境变量覆盖：

```powershell
$env:STEAMDT_ENABLED="true"
$env:STEAMDT_API_URL="https://example.com/api"
$env:STEAMDT_INTERVAL_SECONDS="600"
python DataEngine\master_loop.py
```

SteamDT 写入时会标记 `data_source='steamdt'`，并通过 `upsert_market_price_if_fresh()` 执行时间戳覆盖防御。

### SteamDT OpenAPI

本期同时加入 SteamDT OpenAPI 基础映射同步和中频价格同步：

- `DataEngine\steamdt_openapi.py` 同步 SteamDT 基础映射，写入 `platform_mapping`，并回填 `item_base` 中常用热字段。
- `DataEngine\steamdt_openapi_price.py` 按 P2/P3 优先级同步 Steam / Buff / UUYP / ECO 行情，写入 `market_price`，`data_source='steamdt_openapi'`。
- OpenAPI 价格写入使用真实 `updateTime`，仍走 `upsert_market_price_if_fresh()`，不会用旧数据覆盖新行情。
- 行情同步带有 batch/single 配额状态、代理池失败冷却、条件型异常求购价过滤，以及对优先级调度和雷达快照的增量刷新。

手动运行：

```powershell
$env:STEAMDT_OPENAPI_API_KEY="..."
python DataEngine\steamdt_openapi.py --force

$env:STEAMDT_OPENAPI_PRICE_ENABLED="true"
python DataEngine\steamdt_openapi_price.py --once
```

Web 设置页可配置 OpenAPI key、中频行情同步配额、P2/P3 目标周期和 SteamDT 会话胶囊池。平台连通性自检会展示 SteamDT OpenAPI / OpenAPI Price 最近状态。

## 配置文件

常见配置文件：

```text
config/app_config.json              系统运行参数、代理池、SteamDT、风控阈值
config/credentials.json             平台 Cookie、Token、账号相关敏感配置
config/platform_session_state.json  Provider 健康状态、冷却和最近错误
config/config.json                  轻量通知等通用配置
config/session_capsules.json        SteamDT 等浏览器会话胶囊池
config/platform_runtime_state.json  平台运行态健康摘要
```

敏感信息包括 Cookie、Token、`shared_secret`、`identity_secret`、代理账号密码等。不要提交到公开仓库，也不要贴到日志或 issue 中。

## 平台要点

### Buff

- 请求层强制 `locale=zh-Hans` 与 `currency=CNY`。
- 购买/求购请求必须携带 Cookie 中解析出的 CSRF，并设置请求头。
- 平台 ID 优先来自数据库和本地 mapper，不在执行链路中做低价值在线搜索。

### UUYP

- 请求头固定 `Accept-Language: zh-CN,zh;q=0.9`，Cookie 中保留 CNY 语义。
- POST 下单依赖 App Token、设备头、`uk` 等业务鉴权字段。
- 登录异常、风控拦截、业务鉴权失败会触发冷却，避免同一批次继续请求。
- 价格为 `0` 且成交量为 `0` 的报价视为无效行情，不参与套利计算。

### Steam

- Market 请求强制 `currency=23` 或 Cookie `steamCurrencyId=23`。
- 通过代理访问时使用更宽松的超时和指数退避。
- 熔断开启后批次会快速失败，避免每个机会都等待外层超时。

### ECO / C5Game

- ECO 缺少平台 ID 时直接跳过并记录原因，不做阻塞式在线搜索。
- C5Game 目前保留 Provider 扩展位，适合后续接入 OpenAPI / 签名式调用。

## 平台能力矩阵

能力注册表位于 `app/services/trading/capabilities.py`，状态分为 `ready`、`partial`、`planned` 和 `missing`。当前能力概览：

| 平台 | 已可用能力 | 部分可用 / 规划中 |
| --- | --- | --- |
| BUFF | 行情、卖单簿、直接买入、报价轮询、接受 Steam 报价 | 求购、改价、撤单、第三方上架 |
| UUYP | 行情、卖单簿、直接买入、求购 | 改价、下架/撤单；第三方上架和报价接收仍规划中 |
| ECO | 行情、卖单簿、直接买入、求购、订单状态 | 上架、改价、下架为 sale-only 底座 |
| C5Game | 订单状态、发货、报价 ID 查询 | 行情、买入、求购和报价接收仍按 OpenAPI 继续补齐 |
| Steam | 行情、卖单簿、Steam 上架、Steam 求购、接受报价、移动确认 | Steam listing 下架仍走既有 delist 路径 |

这些状态会被 SAFE_MODE 烟测和自动化面板读取，便于后续按平台逐项补齐，而不是把接口散落到业务流程里。

## 数据库策略

- SQLite engine 初始化启用 WAL：
  - `PRAGMA journal_mode=WAL`
  - `PRAGMA synchronous=NORMAL`
- 正式迁移使用 Alembic，当前修订会创建 `platform_action` 并补齐部分成交与风险品类字段：
  - `20260510_0001_platform_action`
  - `20260510_0002_platform_action_partial_fill`
  - `20260510_0003_platform_action_risk_category`
- 所有价格写入应携带真实数据时间：
  - JIT 实时爬取：当前时间。
  - CSGOTrader：HTTP `Last-Modified`。
  - SteamDT：页面或接口返回的真实行情时间。
- 只有 `new_timestamp >= current.updated_at` 时才覆盖价格。

## 通知

下单成功后可通过 Webhook 推送通知，配置示例：

```json
{
  "notify": {
    "enabled": true,
    "webhook_url": "https://example.com/webhook"
  }
}
```

通知内容包含饰品名、平台、动作类型、价格和执行结果。可用于 Server 酱、PushPlus、Telegram Bot 或自建 Webhook 网关。

## 故障排查

### 启动后页面 500 或模板找不到

确认 `app/api.py` 中前端目录指向项目根目录下的 `web/`。当前推荐路径逻辑：

```python
Path(__file__).parent.parent / "web"
```

### 行情价格币种不一致

不要在后端做汇率换算。应检查请求层是否锁死 CNY：

- Buff：Cookie `locale=zh-Hans; currency=CNY`
- UUYP：`Accept-Language: zh-CN,zh;q=0.9`
- Steam：`currency=23` 或 `steamCurrencyId=23`

### UUYP 连通性正常但下单提示登录异常

GET 通过只说明网络可达。POST 下单还需要完整业务鉴权，包括 App Token、设备头、Cookie、`uk` 和直连/代理策略。检查 `config/platform_session_state.json` 和日志中的 auth/risk cooldown。

### JIT 批量复验很慢

检查是否触发 Steam / Buff / UUYP 熔断。熔断开启后应看到批次级跳过日志，而不是每个饰品都等待超时。

### 日志乱码

项目文件和日志均按 UTF-8 处理。PowerShell 里可先执行：

```powershell
chcp 65001
```

然后重新启动进程。

## 开发规范

- 优先复用现有 Provider、Fetcher、数据库 upsert 封装。
- 平台 ID 解析应优先使用数据库和本地 mapper，避免在交易执行链路临时在线搜索。
- 新数据源写入价格必须使用时间戳覆盖防御。
- 新平台下单必须先实现 Provider preflight，再接入 `app/services/trading/platform_adapters.py` 和能力注册表。
- 自动化交易入口应优先创建 `PlatformAction`，不要绕过状态机直接长链路执行。
- 涉及真实下单、上架、接受报价的验证先走 `safe_mode=true` 和 `/api/trade/platform_actions/smoke`。
- 修改后至少运行：

```powershell
python -m compileall DataEngine app utils
```

## 发布文档

- 本期完成度记录：[analysis/multi_platform_automation_completion_20260510.md](analysis/multi_platform_automation_completion_20260510.md)
- 剩余工程跟进项：[TODOS.md](TODOS.md)
- 本地 agent/gstack 技能说明：[AGENTS.md](AGENTS.md)
- 平台 ID 映射数据来源：[DataEngine/SteamTradingSite-ID-Mapper-main/README.md](DataEngine/SteamTradingSite-ID-Mapper-main/README.md)

## 免责声明

AetherSwap 不保证任何收益，不构成投资、交易或理财建议。虚拟饰品价格可能剧烈波动，第三方平台也可能调整接口、规则和风控策略。使用本项目进行任何自动化访问、下单、求购、出售或数据抓取前，请确认你理解并接受账号限制、资产冻结、接口封禁、资金损失和法律合规风险。
