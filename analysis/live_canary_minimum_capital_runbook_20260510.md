# AetherSwap 最低资金 Live Canary 人工测试 SOP - 2026-05-11

本文档用于把第一次真实小额自动化交易测试流程化。目标不是验证收益，而是用最低资金验证 AetherSwap 的关键执行链路：创建 `PlatformAction`、风险门、平台预检、单笔真实执行、订单/资金状态回写、停止与复盘。

第一轮测试只允许“单平台、单饰品、单动作、单笔、手动触发”。不要启动真实后台 worker，不要一次跑完整获利循环。

## 0. 测试边界

本轮允许测试：

- `channel=live_canary` 的单条 `PlatformAction`。
- 单笔金额不超过 `trading_live_canary.max_action_cny`，建议 `0.3` 到 `1.0` 元。
- 单次真实执行只调用一次 `POST /api/trade/platform_actions/run_once`，payload 必须是 `{"safe_mode": false, "limit": 1}`。
- fake/test 利润信号只用于提高候选命中率，必须只进入 `raw_context.test_signal`，不能改变真实价格字段。

本轮禁止测试：

- 不要把 `trading_live_canary.allow_background_worker` 改成 `true`。
- 不要启动 `safe_mode=false` 的后台 worker。
- 不要使用 `limit > 1` 执行真实动作。
- 不要创建多个到期的 `live_canary` 动作同时等待执行。
- 不要把高价值饰品、非白名单饰品、非白名单平台放进 canary。

## 1. 测试方式选择

推荐方式：

- 命令行：使用 PowerShell 调用 API，负责创建动作、smoke、precheck、真实单笔 run_once。
- WebUI：只用于观察自动化运行态、到期动作、占用预算、告警和动作状态。

不推荐方式：

- 不要在 WebUI 里完成真实 live canary。当前 WebUI 的“执行一次”按钮调用的是 SAFE_MODE：`{"safe_mode": true, "limit": 10}`，适合干跑，不适合真实单笔 canary。

WebUI 地址：

```text
http://127.0.0.1:28472
```

WebUI 观察位置：

- `仪表盘` -> `自动化交易运行态`
- `订单管理`
- `执行记录`
- `运行日志`

## 2. 前置条件

测试前必须确认：

| 检查项 | 验收标准 | 不通过时处理 |
|---|---|---|
| 项目可启动 | API 可以访问 `http://127.0.0.1:28472` | 先修启动错误 |
| 配置 JSON 可解析 | `CONFIG_JSON_OK` | 修复 `config/app_config.json` JSON 格式 |
| 配置 schema 可加载 | `CONFIG_VALIDATED_OK True True True` | 修复配置字段或默认配置 |
| 目标平台登录态有效 | live smoke 返回 `ok=true` | 重新登录或更新凭据 |
| worker 未运行 | `/api/trade/live_canary/status` 中 `worker.running=false` | 调用 worker_stop |
| live canary 开关保守 | `kill_switch=true`、`allow_background_worker=false` | 先恢复安全配置 |
| 只存在一个到期 canary 动作 | `gate.next_action` 是本次测试动作 | 取消或延后其它到期动作 |

命令行进入项目：

```powershell
cd I:\cs\AetherSwap
```

启动后端：

```powershell
.\.venv\Scripts\python.exe run.py
```

如果已经有窗口在运行后端，可另开一个 PowerShell 窗口继续执行下面的 API 命令。

检查配置文件是否能解析：

```powershell
.\.venv\Scripts\python.exe -c "import json, pathlib; json.load(pathlib.Path('config/app_config.json').open('r', encoding='utf-8')); print('CONFIG_JSON_OK')"
.\.venv\Scripts\python.exe -c "from app.config_loader import load_app_config_validated; cfg=load_app_config_validated(); print('CONFIG_VALIDATED_OK', isinstance(cfg, dict), 'trading_live_canary' in cfg, 'trading_worker' in cfg)"
```

检查 API 是否存活：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/live_canary/status"
```

## 3. 准备 canary 配置

第一轮建议使用最保守配置。把 `allowed_platforms`、`allowed_action_types`、`allowed_item_ids` 或 `allowed_market_hash_names` 改成真实测试对象。

示例：允许 BUFF 上对一个低价饰品发起一条 `purchase_order`。

```powershell
$cfgResp = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/config"
$cfg = $cfgResp.config

$cfg.trading_worker = @{
  enabled = $false
  safe_mode = $true
  batch_size = 1
  poll_interval_seconds = 10
  lease_seconds = 60
}

$cfg.trading_live_canary = @{
  enabled = $true
  kill_switch = $true
  require_channel = "live_canary"
  max_action_cny = 1.0
  max_daily_cny = 3.0
  allowed_platforms = @("buff")
  allowed_action_types = @("purchase_order")
  allowed_item_ids = @(1)
  allowed_market_hash_names = @()
  require_recent_smoke_seconds = 900
  require_manual_run_once = $true
  allow_background_worker = $false
}

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/config" -ContentType "application/json" -Body (@{ config = $cfg } | ConvertTo-Json -Depth 20)
```

验收：

```powershell
$status = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/live_canary/status"
$status.config
$status.worker
$status.gate
```

通过标准：

- `config.enabled=true`
- `config.kill_switch=true`
- `config.allow_background_worker=false`
- `worker.running=false`

如果 `gate.reason=live_canary_kill_switch_enabled`，这是正常的：说明真实执行仍被 kill switch 拦住。

## 4. 选择测试饰品

选择标准：

- 实际成交/求购金额不超过 `max_action_cny`。
- 饰品必须能在目标平台真实下单或求购。
- 饰品必须和配置中的 `allowed_item_ids` 或 `allowed_market_hash_names` 对上。
- 必须拿到平台动作需要的关键参数，例如 BUFF 的 `goods_id`。
- 避免冷门到完全无人交易的饰品，否则会拖慢状态验证。

记录测试对象：

| 字段 | 示例 | 实测值 |
|---|---|---|
| platform | `buff` |  |
| action_type | `purchase_order` |  |
| item_id | `1` |  |
| market_hash_name | `AK-47 | Redline (Field-Tested)` |  |
| target_price | `0.50` |  |
| quantity | `1` |  |
| goods_id | `replace-with-real-goods-id` |  |

## 5. T0 SAFE_MODE 干跑

目的：

- 验证动作创建、风险预算、状态机、ledger 字段和 worker 处理逻辑。
- 不产生任何真实平台副作用。

创建一条 canary 动作。务必替换 `item_id`、`market_hash_name`、`target_price`、`goods_id`。

```powershell
$body = @{
  action_type = "purchase_order"
  platform = "buff"
  item_id = 1
  market_hash_name = "AK-47 | Redline (Field-Tested)"
  target_price = 0.50
  quantity = 1
  channel = "live_canary"
  expected_profit_rate = 0.20
  next_check_at = 0
  request_payload = @{
    goods_id = "replace-with-real-goods-id"
  }
  test_signal = @{
    fake_profit_rate = 9.99
    canary_profit_boost = 30
    forced_candidate_reason = "minimum-capital live canary selection"
  }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/trade/platform_actions" -ContentType "application/json" -Body $body
```

查看创建结果：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/platform_actions?channel=live_canary"
```

执行 SAFE_MODE 一次：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/trade/platform_actions/run_once" -ContentType "application/json" -Body '{"safe_mode":true,"limit":1}'
```

WebUI 观察：

- 打开 `仪表盘`。
- 点击 `刷新自动化`。
- 看 `自动化交易运行态` 中的到期动作、占用预算、告警。

结果判断：

| 结果 | 含义 | 下一步 |
|---|---|---|
| `success=true` 且 `result.claimed=1` | SAFE_MODE 已领取并处理一条动作 | 继续 T1 |
| `result.claimed=0` | 没有到期可执行动作，或动作已被前面处理 | 查看 `platform_actions?channel=live_canary` |
| 动作 `state=risk_blocked` | 风控拦截，通常是价格、品类、预算或利润参数不合规 | 调整测试对象或 canary 配置 |
| 动作 `state=failed/retry_wait` | 即使 SAFE_MODE 也遇到参数/adapter 问题 | 先看 `error_code/error_message` |

T0 验收标准：

- 动作只出现在 `channel=live_canary`。
- `locked_budget_cny <= max_action_cny`。
- `raw_context` 中有 `test_signal`。
- `target_price`、`locked_budget_cny`、`expected_profit_rate` 没有被 fake/test 信号篡改。
- 没有真实平台订单产生。

注意：如果 T0 的动作已经被 SAFE_MODE 处理成终态，真实测试前需要再创建一条新的 `live_canary` 动作，或确保当前存在一条到期、可 claim 的测试动作。

## 6. T1 Live Preflight Smoke

目的：

- 只验证目标平台客户端、凭据、登录态和能力是否可用。
- 不执行真实下单/求购/上架。

执行目标平台 smoke：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/trade/platform_actions/smoke" -ContentType "application/json" -Body '{
  "safe_mode": false,
  "live_preflight": true,
  "platforms": ["buff"],
  "capabilities": ["purchase_order"]
}'
```

查看 canary 状态：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/live_canary/status"
```

结果判断：

| 结果 | 含义 | 下一步 |
|---|---|---|
| `ok=true` | 平台能力和凭据预检通过 | 继续 T2 |
| `missing_capabilities` 非空 | 当前平台不支持该动作，或能力注册表未声明 | 换平台/动作或补 adapter |
| `reason=auth_required` 或类似登录错误 | Cookie/token/登录态不可用 | 重新登录并更新凭据 |
| `reason=preflight_exception` | 平台客户端初始化异常 | 查看 `message` 和运行日志 |
| `gate.reason=live_canary_kill_switch_enabled` | kill switch 仍打开 | 这是 T1 预期结果 |
| `gate.reason=live_canary_smoke_required` | smoke 未被记录或已过期 | 重新执行 T1 |

T1 验收标准：

- smoke 返回 `ok=true`。
- `/api/trade/live_canary/status` 的 `smoke.items` 中出现目标平台和目标能力。
- smoke 记录年龄小于 `require_recent_smoke_seconds`，默认 900 秒。

## 7. T2 真实单笔 Live Canary

目的：

- 用一笔最低资金真实验证“平台动作执行 -> 平台返回 -> ledger 回写”。
- 只允许一条动作、一次调用、一个真实副作用。

### 7.1 解除 kill switch

只在执行前短时间关闭 kill switch：

```powershell
$cfgResp = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/config"
$cfg = $cfgResp.config
$cfg.trading_live_canary.kill_switch = $false
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/config" -ContentType "application/json" -Body (@{ config = $cfg } | ConvertTo-Json -Depth 20)
```

### 7.2 预检

```powershell
$pre = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/trade/live_canary/precheck" -ContentType "application/json" -Body '{"limit":1}'
$pre
```

必须全部满足才允许继续：

- `success=true`
- `safe_to_call_run_once=true`
- `gate.allowed=true`
- `gate.next_action.channel=live_canary`
- `gate.next_action.locked_budget_cny <= config.max_action_cny`
- `gate.smoke_recent=true`
- `gate.required_capability` 是本次要测的能力，例如 `purchase_order`

拦截原因处理表：

| reason | 含义 | 处理 |
|---|---|---|
| `live_canary_disabled` | canary 未开启 | 设置 `trading_live_canary.enabled=true` |
| `live_canary_kill_switch_enabled` | kill switch 未关闭 | 只在 T2 前短暂关闭 |
| `live_canary_limit_must_be_one` | 真实执行 limit 不是 1 | 改成 `limit=1` |
| `live_canary_channel_required` | 动作不是 `live_canary` channel | 重建动作 |
| `live_canary_platform_not_allowed` | 平台不在白名单 | 调整白名单或换平台 |
| `live_canary_action_not_allowed` | 动作类型不在白名单 | 调整白名单或换动作 |
| `live_canary_item_not_allowed` | 饰品不在白名单 | 加入 `allowed_item_ids` 或 `allowed_market_hash_names` |
| `live_canary_action_cap_exceeded` | 单笔金额超过上限 | 降低 `target_price/quantity` |
| `live_canary_daily_cap_exceeded` | 当日 canary 预算超过上限 | 停止测试，次日或提高上限后再测 |
| `live_canary_smoke_required` | 缺少近期 live smoke | 回到 T1 |

### 7.3 执行真实单笔

只有预检完全通过后执行：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/trade/platform_actions/run_once" -ContentType "application/json" -Body '{"safe_mode":false,"limit":1}'
```

执行后立即恢复 kill switch：

```powershell
$cfgResp = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/config"
$cfg = $cfgResp.config
$cfg.trading_live_canary.kill_switch = $true
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/config" -ContentType "application/json" -Body (@{ config = $cfg } | ConvertTo-Json -Depth 20)
```

结果判断：

| 结果 | 含义 | 下一步 |
|---|---|---|
| `success=true` 且 `result.claimed=1` | 真实 worker 领取了 1 条动作 | 进入 T3 观察 |
| `result.failed=1` 且 `error_code=auth_required` | 未真正完成平台动作，登录态/凭据失败 | 恢复 kill switch，修凭据 |
| `result.waiting=1` | 平台已接受或进入等待状态 | 进入 T3 轮询 |
| `result.succeeded=1` | 动作已完成或平台返回终态成功 | 进入 T3 核对订单/资金 |
| `result.risk_blocked=1` | 执行前仍被风控拦截 | 停止，复查预算/品类/利润 |
| HTTP 400 且有 `reason` | canary gate 拦截 | 按 reason 表处理，不要绕过 |

T2 验收标准：

- 真实执行只调用了一次。
- 返回 `safe_mode=false`。
- 返回结果中 `claimed <= 1`。
- 执行后 `kill_switch=true` 已恢复。
- 没有启动后台 worker。

## 8. T3 观察与对账

目的：

- 确认真实平台状态、订单 ID、成交数量、资金占用和释放逻辑能回写。
- 识别真实平台和模拟环境之间的差异。

命令行查看：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/platform_actions?channel=live_canary"
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/platform_action_summary"
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/live_canary/status"
```

WebUI 查看：

- `仪表盘` -> 点击 `刷新自动化`
- 查看 `自动化交易运行态`
- 查看 `订单管理`
- 查看 `运行日志`

重点核对字段：

| 字段 | 通过标准 |
|---|---|
| `state` | 与平台真实状态一致，例如 `waiting_platform/succeeded/retry_wait` |
| `platform_order_id` | 平台接受订单后应有值 |
| `platform_listing_id` | 上架动作接受后应有值 |
| `trade_offer_id` | 涉及报价时应有值 |
| `filled_quantity` | 部分/全部成交数量正确 |
| `remaining_quantity` | 剩余数量正确 |
| `filled_amount_cny` | 已成交金额正确 |
| `locked_budget_cny` | 未成交部分仍占用，成交/取消部分释放 |
| `released_budget_cny` | 部分成交或取消后释放金额正确 |
| `error_code/error_message` | 失败时必须可解释、可行动 |

结果判断：

| 观察结果 | 含义 | 处理 |
|---|---|---|
| 平台有订单，ledger 有 `platform_order_id` | 提交流程通过 | 继续轮询状态 |
| 平台有订单，ledger 没有订单 ID | 回写缺口 | 记录 response_payload，补 adapter 解析 |
| 平台无订单，ledger 标记成功 | 严重一致性问题 | 停止测试，修 adapter |
| 平台订单部分成交，ledger 金额正确 | 部分成交闭环通过 | 继续观察剩余量 |
| 平台订单失败，ledger 进入 retry_wait/failed | 错误处理通过 | 根据 error_code 修复 |
| 占用预算未释放 | 资金占用释放逻辑有缺口 | 停止扩量，修 worker/accounting |

T3 验收标准：

- 本次动作能在 ledger 中完整追踪。
- 平台真实状态和 `PlatformAction.state` 没有相反结论。
- 如果真实平台产生订单，系统记录了平台订单 ID 或足够的 response payload。
- 资金占用、成交量、错误码没有明显异常。

## 9. T4 停止与恢复安全状态

目的：

- 确认测试结束后系统回到安全状态。

停止 worker：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/trade/platform_actions/worker_stop" -ContentType "application/json" -Body '{"timeout_seconds":5}'
```

恢复安全配置：

```powershell
$cfgResp = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/config"
$cfg = $cfgResp.config
$cfg.trading_worker.enabled = $false
$cfg.trading_worker.safe_mode = $true
$cfg.trading_worker.batch_size = 1
$cfg.trading_live_canary.kill_switch = $true
$cfg.trading_live_canary.allow_background_worker = $false
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:28472/api/config" -ContentType "application/json" -Body (@{ config = $cfg } | ConvertTo-Json -Depth 20)
```

最终检查：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:28472/api/trade/live_canary/status"
```

T4 验收标准：

- `worker.running=false`
- `config.kill_switch=true`
- `config.allow_background_worker=false`
- `trading_worker.enabled=false`
- 没有新的非预期 `live_canary` 到期动作被执行

## 10. CMD curl.exe 快速检查命令

如果只想在 Windows CMD 中做只读检查，可使用 `curl.exe`：

```cmd
cd /d I:\cs\AetherSwap
curl.exe http://127.0.0.1:28472/api/trade/live_canary/status
curl.exe http://127.0.0.1:28472/api/trade/platform_actions?channel=live_canary
curl.exe -X POST http://127.0.0.1:28472/api/trade/live_canary/precheck -H "Content-Type: application/json" -d "{\"limit\":1}"
```

真实执行的 JSON 修改和配置切换建议使用 PowerShell，因为 CMD 对复杂 JSON 的转义更容易出错。

## 11. 整体通过标准

本轮最低资金 canary 只有在以下条件全部满足时才算通过：

- T0 SAFE_MODE 干跑通过，且没有真实平台副作用。
- T1 live smoke 对目标平台/能力返回 `ok=true`。
- T2 precheck 返回 `safe_to_call_run_once=true`。
- T2 真实执行只 claim 了 1 条 `live_canary` 动作。
- T2 后 kill switch 已恢复为 `true`。
- T3 中平台真实状态和 ledger 状态一致。
- 资金占用、成交、释放字段没有明显错误。
- WebUI 自动化运行态没有新增危险告警。
- 没有启动真实后台 worker。
- 没有非白名单平台、非白名单动作、非白名单饰品被执行。

## 12. 测试记录模板

| 项目 | 记录 |
|---|---|
| 测试时间 |  |
| 平台 |  |
| 动作类型 |  |
| 饰品 |  |
| item_id |  |
| target_price |  |
| max_action_cny |  |
| T0 结果 |  |
| T1 smoke 结果 |  |
| T2 precheck 结果 |  |
| T2 run_once 结果 |  |
| 平台订单 ID |  |
| 最终 state |  |
| filled_quantity |  |
| filled_amount_cny |  |
| locked_budget_cny |  |
| released_budget_cny |  |
| error_code |  |
| 是否恢复 kill switch |  |
| 是否停止 worker |  |
| 结论 | 通过 / 不通过 |

## 13. 何时可以进入下一阶段

满足以下条件后，才考虑从“单笔 canary”进入“模块化连续测试”：

- 同一平台同一动作至少连续 3 次最低资金 canary 无账实不一致。
- 至少覆盖一次成功、一次平台等待/轮询、一次失败/重试分支。
- 所有失败都有可解释 `error_code`，且不会误判为成功。
- 手动恢复安全状态流程稳定。

下一阶段仍不建议直接开启 live background worker。应先按平台和动作拆分：

1. BUFF/UUYP/ECO/C5 的 read-only smoke。
2. 单平台 `purchase_order` 最低资金 canary。
3. 单平台 `direct_buy` 最低资金 canary。
4. Steam 侧只读/SAFE_MODE 校验。
5. 卖侧 `platform_listing/reprice/cancel/deliver` 分动作最低风险 canary。
6. 最后才考虑单账号完整闭环。
