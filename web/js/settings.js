
let inventoryRefreshSeconds = 60;
let inventoryTimer = null;
let currentPriceRefreshMinutes = 10;
let currentPriceTimer = null;
let settingsLoadingConfig = false;
let settingsDirtyTabs = new Set();
let settingsDirtyTrackingBound = false;
const SETTINGS_TAB_LABELS = {
  trade: "交易策略",
  sell: "出售策略",
  notify: "推送与确认",
  stability: "价格稳定性",
  proxy: "网络代理",
  system: "系统",
};

function readNumberInput(id, parser = parseFloat) {
  const node = el(id);
  if (!node) return undefined;
  const raw = String(node.value ?? "").trim();
  if (raw === "") return undefined;
  const value = parser(raw);
  return Number.isFinite(value) ? value : undefined;
}
function isInternalWebviewShell() {
  try {
    return new URLSearchParams(window.location.search).get("shell") === "webview";
  } catch {
    return false;
  }
}
function applyInternalUiScale(value) {
  if (isInternalWebviewShell()) {
    document.documentElement.style.zoom = value || "0.7";
  } else {
    document.documentElement.style.zoom = "";
  }
}

function getActiveSettingsTabName() {
  return document.querySelector(".settings-tab.active")?.dataset?.settab || "trade";
}

function getSettingsTabNameForElement(node) {
  return node?.closest?.(".settings-tab-pane")?.dataset?.settabPane || getActiveSettingsTabName();
}

function updateSettingsDirtyUI() {
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    const name = tab.dataset?.settab;
    const dirty = settingsDirtyTabs.has(name);
    tab.classList.toggle("is-dirty", dirty);
    tab.title = dirty ? `${SETTINGS_TAB_LABELS[name] || name} 有未保存修改` : "";
  });
}

function markSettingsTabDirty(name) {
  if (settingsLoadingConfig) return;
  const tabName = name || getActiveSettingsTabName();
  if (!tabName) return;
  settingsDirtyTabs.add(tabName);
  updateSettingsDirtyUI();
}

function clearSettingsTabDirty(name) {
  if (name) settingsDirtyTabs.delete(name);
  else settingsDirtyTabs.clear();
  updateSettingsDirtyUI();
}

function isSettingsTabDirty(name) {
  return settingsDirtyTabs.has(name);
}

function shouldAllowSettingsTabSwitch(nextName) {
  const currentName = getActiveSettingsTabName();
  if (!nextName || nextName === currentName || !settingsDirtyTabs.has(currentName)) return true;
  const label = SETTINGS_TAB_LABELS[currentName] || currentName;
  return confirm(`当前「${label}」有未保存修改，确定切换到其他设置页？`);
}

function bindSettingsDirtyTracking() {
  if (settingsDirtyTrackingBound) return;
  const panel = el("panel-settings");
  if (!panel) return;
  settingsDirtyTrackingBound = true;
  const markFromEvent = (event) => {
    const target = event.target;
    if (!target?.matches?.("input, textarea, select")) return;
    if (target.type === "file") return;
    markSettingsTabDirty(getSettingsTabNameForElement(target));
  };
  panel.addEventListener("input", markFromEvent);
  panel.addEventListener("change", markFromEvent);
}

async function loadConfig() {
  settingsLoadingConfig = true;
  try {
  const d = await fetchJson(API + "/config");
  const c = d.config || {};
  const i = c.iflow || {};
  const b = c.buff || {};
  const p = c.pipeline || {};
  const s = c.stability || {};
  const inv = c.inventory || {};
  const sys = c.system || {};
  const gGames = el("cfg-games");
  if (gGames) gGames.value = i.type || i.games || "";
  const gPlatforms = el("cfg-platforms");
  if (gPlatforms) gPlatforms.value = i.platforms || "";
  const gSort = el("cfg-sort_by");
  if (gSort) gSort.value = i.sort_by || "";
  const gMinPrice = el("cfg-min_price");
  if (gMinPrice) gMinPrice.value = i.min_price ?? "";
  const gMaxPrice = el("cfg-max_price");
  if (gMaxPrice) gMaxPrice.value = i.max_price ?? "";
  const gMinVolume = el("cfg-min_volume");
  if (gMinVolume) gMinVolume.value = i.min_volume ?? "";
  const gPay = el("cfg-pay_method");
  if (gPay) gPay.value = (b.pay_method || "wechat").toLowerCase();
  const gTarget = el("cfg-target_balance");
  if (gTarget) gTarget.value = p.target_balance ?? "";
  const gMaxDisc = el("cfg-max_discount");
  if (gMaxDisc) gMaxDisc.value = p.max_discount ?? 0.8;
  const gHugeProfitOffset = el("cfg-huge_profit_offset");
  if (gHugeProfitOffset) gHugeProfitOffset.value = p.huge_profit_offset ?? "";
  const gIflowTopN = el("cfg-iflow_top_n");
  if (gIflowTopN) gIflowTopN.value = p.iflow_top_n ?? "";
  const gExclude = el("cfg-exclude_keywords");
  if (gExclude) gExclude.value = (p.exclude_keywords && p.exclude_keywords.length > 0 ? p.exclude_keywords : ["印花"]).join("\n");
  const gCv = el("cfg-cv_threshold");
  if (gCv) gCv.value = s.cv_threshold ?? "";
  const gR2 = el("cfg-r2_threshold");
  if (gR2) gR2.value = s.r2_threshold ?? "";
  const gPpCeil = el("cfg-price_percentile_ceil");
  if (gPpCeil) gPpCeil.value = s.price_percentile_ceil ?? "";
  const gR2Rising = el("cfg-r2_rising_threshold");
  if (gR2Rising) gR2Rising.value = s.r2_rising_threshold ?? "";
  const gSlopeCeil = el("cfg-slope_pct_ceil");
  if (gSlopeCeil) gSlopeCeil.value = s.slope_pct_ceil ?? "";
  const gMaCeil = el("cfg-ma_deviation_ceil");
  if (gMaCeil) gMaCeil.value = s.ma_deviation_ceil ?? "";
  const gLpMa30Ceil = el("cfg-last_price_ma30_ceil");
  if (gLpMa30Ceil) gLpMa30Ceil.value = s.last_price_ma30_ceil ?? "";
  const gSlopeFloor = el("cfg-slope_stable_floor");
  if (gSlopeFloor) gSlopeFloor.value = s.slope_stable_floor ?? "";
  const gPpRising = el("cfg-price_percentile_ceil_rising");
  if (gPpRising) gPpRising.value = s.price_percentile_ceil_rising ?? "";
  const gUseVwap = el("cfg-use_vwap");
  if (gUseVwap) gUseVwap.checked = s.use_vwap !== false;
  const gRetrySec = el("cfg-retry_interval_seconds");
  if (gRetrySec) gRetrySec.value = p.retry_interval_seconds ?? "";
  const verboseCb = el("cfg-verbose-debug");
  if (verboseCb) verboseCb.checked = !!p.verbose_debug;
  const steamListingsDebugCb = el("cfg-steam-listings-debug");
  if (steamListingsDebugCb) steamListingsDebugCb.checked = !!p.steam_listings_debug;
  const sellStrategy = el("cfg-sell_strategy");
  if (sellStrategy) sellStrategy.value = String(p.sell_strategy ?? 1);
  const sellOffset = el("cfg-sell_price_offset");
  if (sellOffset) sellOffset.value = p.sell_price_offset ?? "";
  const wallVol = el("cfg-sell_price_wall_volume");
  if (wallVol) wallVol.value = p.sell_price_wall_volume ?? "";
  const maxIgnore = el("cfg-sell_price_max_ignore_volume");
  if (maxIgnore) maxIgnore.value = p.sell_price_max_ignore_volume ?? "";
  const sellTrendDays = el("cfg-sell_trend_days");
  if (sellTrendDays) sellTrendDays.value = p.sell_trend_days ?? "";
  const maxListingsPerItem = el("cfg-max_listings_per_item");
  if (maxListingsPerItem) maxListingsPerItem.value = p.max_listings_per_item ?? "";
  const listingDelayEl = el("cfg-listing_delay_seconds");
  if (listingDelayEl) listingDelayEl.value = p.listing_delay_seconds ?? "";
  const resellRatioEl = el("cfg-resell_ratio");
  if (resellRatioEl) resellRatioEl.value = p.resell_ratio ?? "";
  const safeHardCap = el("cfg-safe_purchase_hard_qty_cap");
  if (safeHardCap) safeHardCap.value = p.safe_purchase_hard_qty_cap ?? "";
  const safeLiqRatio = el("cfg-safe_purchase_liquidity_ratio");
  if (safeLiqRatio) safeLiqRatio.value = p.safe_purchase_liquidity_ratio ?? "";
  const safeLowPriceThresh = el("cfg-safe_purchase_low_price_threshold");
  if (safeLowPriceThresh) safeLowPriceThresh.value = p.safe_purchase_low_price_threshold ?? "";
  const safeLowPricePenalty = el("cfg-safe_purchase_low_price_penalty");
  if (safeLowPricePenalty) safeLowPricePenalty.value = p.safe_purchase_low_price_penalty ?? "";
  const safeLowPriceCap = el("cfg-safe_purchase_low_price_hard_cap");
  if (safeLowPriceCap) safeLowPriceCap.value = p.safe_purchase_low_price_hard_cap ?? "";
  const sellPressureN = el("cfg-sell_pressure_orders_n");
  if (sellPressureN) sellPressureN.value = p.sell_pressure_orders_n ?? "";
  const sellPressureThresh = el("cfg-sell_pressure_threshold");
  if (sellPressureThresh) sellPressureThresh.value = p.sell_pressure_threshold ?? "";
  const currentPriceRefreshEl = el("cfg-current-price-refresh-minutes");
  if (currentPriceRefreshEl) currentPriceRefreshEl.value = p.current_price_refresh_minutes ?? "";
  const currentPriceValue = parseInt(p.current_price_refresh_minutes, 10);
  currentPriceRefreshMinutes = Number.isFinite(currentPriceValue) ? currentPriceValue : 10;
  const marketCircuitEnabled = el("cfg-market-price-circuit-enabled");
  if (marketCircuitEnabled) marketCircuitEnabled.checked = p.market_price_circuit_enabled !== false;
  const gStartTimeLimitEnabled = el("cfg-start-time-limit-enabled");
  if (gStartTimeLimitEnabled) gStartTimeLimitEnabled.checked = !!p.start_time_limit_enabled;
  const gStartTimeHour = el("cfg-start-time-hour");
  if (gStartTimeHour) gStartTimeHour.value = p.start_time_hour ?? "";
  const gEndTimeHour = el("cfg-end-time-hour");
  if (gEndTimeHour) gEndTimeHour.value = p.end_time_hour ?? "";
  const invInput = el("cfg-inv-refresh");
  if (invInput) invInput.value = inv.refresh_seconds ?? "";
  const inventoryRefreshValue = parseInt(inv.refresh_seconds, 10);
  inventoryRefreshSeconds = Number.isFinite(inventoryRefreshValue) ? inventoryRefreshValue : 60;
  const n = c.notify || {};
  const gPush = el("cfg-pushplus_token");
  if (gPush) gPush.value = n.pushplus_token ?? "";
  const gHoldingsReport = el("cfg-holdings_report_interval_hours");
  if (gHoldingsReport) gHoldingsReport.value = n.holdings_report_interval_hours ?? "";
  const gHoldingsThreshold = el("cfg-holdings_report_change_threshold_pct");
  if (gHoldingsThreshold) gHoldingsThreshold.value = n.holdings_report_change_threshold_pct ?? "";
  const gHoldingsDropEnabled = el("cfg-holdings-drop-enabled");
  if (gHoldingsDropEnabled) gHoldingsDropEnabled.checked = n.holdings_report_drop_enabled !== false;
  const gEmailUser = el("cfg-email_user");
  if (gEmailUser) gEmailUser.value = n.email_user ?? "";
  const gEmailPass = el("cfg-email_pass");
  if (gEmailPass) gEmailPass.value = n.email_pass ?? "";
  const gImap = el("cfg-imap_server");
  if (gImap) gImap.value = n.imap_server ?? "";
  const gTargetSender = el("cfg-target_sender");
  if (gTargetSender) gTargetSender.value = n.target_sender ?? "";
  const gAllowedSender = el("cfg-allowed_sender");
  if (gAllowedSender) gAllowedSender.value = n.allowed_sender ?? "";
  const gSubSuccess = el("cfg-subject_success");
  if (gSubSuccess) gSubSuccess.value = n.subject_success ?? "";
  const gSubFail = el("cfg-subject_fail");
  if (gSubFail) gSubFail.value = n.subject_fail ?? "";
  const gEmailTimeout = el("cfg-email_timeout_seconds");
  if (gEmailTimeout) gEmailTimeout.value = n.email_timeout_seconds ?? "";
  const gFx = el("cfg-exchange-refresh-hours");
  if (gFx) gFx.value = sys.exchange_rate_refresh_hours ?? "";
  const gKeepalive = el("cfg-session-keepalive-hours");
  if (gKeepalive) gKeepalive.value = sys.session_keepalive_hours ?? "";
  const gUiScale = el("cfg-ui_scale");
  if (gUiScale) {
    gUiScale.value = sys.ui_scale || "0.7";
    applyInternalUiScale(sys.ui_scale || "0.7");
    gUiScale.addEventListener("change", (e) => {
      applyInternalUiScale(e.target.value);
    });
  }
  const sd = c.steam_deals || {};
  const gSdEnabled = el("cfg-steam-deals-enabled");
  if (gSdEnabled) gSdEnabled.checked = !!sd.enabled;
  const gSdRefresh = el("cfg-steam-deals-auto-refresh-days");
  if (gSdRefresh) gSdRefresh.value = sd.auto_refresh_days ?? "";
  const gSdGameThreads = el("cfg-steam-deals-game-threads");
  if (gSdGameThreads) gSdGameThreads.value = sd.max_game_threads ?? "";
  const gSdRegionThreads = el("cfg-steam-deals-region-threads");
  if (gSdRegionThreads) gSdRegionThreads.value = sd.max_region_threads ?? "";
  // 加载完成后刷新 UX 状态组件
  updateUXStatus(c);
  clearSettingsTabDirty();
  } finally {
    settingsLoadingConfig = false;
  }
}

function formToConfig() {
  return {
    iflow: {
      type: el("cfg-games") ? el("cfg-games").value.trim() : undefined,
      platforms: el("cfg-platforms") ? el("cfg-platforms").value.trim() : undefined,
      sort_by: el("cfg-sort_by") ? el("cfg-sort_by").value.trim() : undefined,
      min_price: el("cfg-min_price") ? parseFloat(el("cfg-min_price").value) || undefined : undefined,
      max_price: el("cfg-max_price") ? parseFloat(el("cfg-max_price").value) || undefined : undefined,
      min_volume: el("cfg-min_volume") ? parseInt(el("cfg-min_volume").value, 10) || undefined : undefined,
    },
    buff: {
      pay_method: el("cfg-pay_method") ? el("cfg-pay_method").value : undefined,
      game: el("cfg-buff-game") ? el("cfg-buff-game").value.trim() : undefined,
      price_tolerance: el("cfg-price_tolerance") ? parseFloat(el("cfg-price_tolerance").value) || undefined : undefined,
    },
    pipeline: {
      target_balance: el("cfg-target_balance") ? parseFloat(el("cfg-target_balance").value) || undefined : undefined,
      max_discount: el("cfg-max_discount") ? parseFloat(el("cfg-max_discount").value) || undefined : undefined,
      huge_profit_offset: el("cfg-huge_profit_offset") ? parseFloat(el("cfg-huge_profit_offset").value) : undefined,
      iflow_top_n: el("cfg-iflow_top_n") ? parseInt(el("cfg-iflow_top_n").value, 10) || undefined : undefined,
      sell_price_ratio: el("cfg-sell_ratio") ? parseFloat(el("cfg-sell_ratio").value) || undefined : undefined,
      retry_interval_seconds: el("cfg-retry_interval_seconds") ? parseInt(el("cfg-retry_interval_seconds").value, 10) || undefined : undefined,
      exclude_keywords: Array.from(
        new Set(
          (el("cfg-exclude_keywords").value || "")
            .split(/\n/)
            .map((s) => s.trim())
            .filter(Boolean)
        )
      ),
      verbose_debug: el("cfg-verbose-debug") ? el("cfg-verbose-debug").checked : false,
      steam_listings_debug: el("cfg-steam-listings-debug") ? el("cfg-steam-listings-debug").checked : false,
      sell_strategy: el("cfg-sell_strategy") ? parseInt(el("cfg-sell_strategy").value, 10) || 1 : 1,
      sell_price_offset: el("cfg-sell_price_offset") ? parseFloat(el("cfg-sell_price_offset").value) || 0 : 0,
      sell_price_wall_volume: el("cfg-sell_price_wall_volume") ? parseInt(el("cfg-sell_price_wall_volume").value, 10) : undefined,
      sell_price_max_ignore_volume: el("cfg-sell_price_max_ignore_volume") ? parseInt(el("cfg-sell_price_max_ignore_volume").value, 10) : undefined,
      sell_trend_days: el("cfg-sell_trend_days") ? parseInt(el("cfg-sell_trend_days").value, 10) || undefined : undefined,
      max_listings_per_item: el("cfg-max_listings_per_item") ? parseInt(el("cfg-max_listings_per_item").value, 10) || undefined : undefined,
      listing_delay_seconds: el("cfg-listing_delay_seconds") ? parseInt(el("cfg-listing_delay_seconds").value, 10) || undefined : undefined,
      resell_ratio: el("cfg-resell_ratio") ? parseFloat(el("cfg-resell_ratio").value) || undefined : undefined,
      safe_purchase_hard_qty_cap: el("cfg-safe_purchase_hard_qty_cap") ? parseInt(el("cfg-safe_purchase_hard_qty_cap").value, 10) : undefined,
      safe_purchase_liquidity_ratio: el("cfg-safe_purchase_liquidity_ratio") ? parseFloat(el("cfg-safe_purchase_liquidity_ratio").value) : undefined,
      safe_purchase_low_price_threshold: el("cfg-safe_purchase_low_price_threshold") ? parseFloat(el("cfg-safe_purchase_low_price_threshold").value) : undefined,
      safe_purchase_low_price_penalty: el("cfg-safe_purchase_low_price_penalty") ? parseFloat(el("cfg-safe_purchase_low_price_penalty").value) : undefined,
      safe_purchase_low_price_hard_cap: el("cfg-safe_purchase_low_price_hard_cap") ? parseInt(el("cfg-safe_purchase_low_price_hard_cap").value, 10) : undefined,
      sell_pressure_orders_n: el("cfg-sell_pressure_orders_n") ? parseInt(el("cfg-sell_pressure_orders_n").value, 10) : undefined,
      sell_pressure_threshold: el("cfg-sell_pressure_threshold") ? parseFloat(el("cfg-sell_pressure_threshold").value) : undefined,
      current_price_refresh_minutes: readNumberInput("cfg-current-price-refresh-minutes", (v) => parseInt(v, 10)),
      market_price_circuit_enabled: el("cfg-market-price-circuit-enabled") ? el("cfg-market-price-circuit-enabled").checked : undefined,
      start_time_limit_enabled: !!el("cfg-start-time-limit-enabled")?.checked,
      start_time_hour: el("cfg-start-time-hour") ? (parseInt(el("cfg-start-time-hour").value, 10) >= 0 && parseInt(el("cfg-start-time-hour").value, 10) <= 23 ? parseInt(el("cfg-start-time-hour").value, 10) : undefined) : undefined,
      end_time_hour: el("cfg-end-time-hour") ? (parseInt(el("cfg-end-time-hour").value, 10) >= 0 && parseInt(el("cfg-end-time-hour").value, 10) <= 23 ? parseInt(el("cfg-end-time-hour").value, 10) : undefined) : undefined,
    },
    stability: {
      days: el("cfg-stability-days") ? parseInt(el("cfg-stability-days").value, 10) || undefined : undefined,
      cv_threshold: el("cfg-cv_threshold") ? parseFloat(el("cfg-cv_threshold").value) || undefined : undefined,
      r2_threshold: el("cfg-r2_threshold") ? parseFloat(el("cfg-r2_threshold").value) || undefined : undefined,
      price_percentile_ceil: el("cfg-price_percentile_ceil") ? parseFloat(el("cfg-price_percentile_ceil").value) : undefined,
      r2_rising_threshold: el("cfg-r2_rising_threshold") ? parseFloat(el("cfg-r2_rising_threshold").value) : undefined,
      slope_pct_ceil: el("cfg-slope_pct_ceil") ? parseFloat(el("cfg-slope_pct_ceil").value) : undefined,
      ma_deviation_ceil: el("cfg-ma_deviation_ceil") ? parseFloat(el("cfg-ma_deviation_ceil").value) : undefined,
      last_price_ma30_ceil: el("cfg-last_price_ma30_ceil") ? parseFloat(el("cfg-last_price_ma30_ceil").value) : undefined,
      slope_stable_floor: el("cfg-slope_stable_floor") ? parseFloat(el("cfg-slope_stable_floor").value) : undefined,
      price_percentile_ceil_rising: el("cfg-price_percentile_ceil_rising") ? parseFloat(el("cfg-price_percentile_ceil_rising").value) : undefined,
      use_vwap: el("cfg-use_vwap") ? el("cfg-use_vwap").checked : undefined,
    },
    inventory: {
      refresh_seconds: readNumberInput("cfg-inv-refresh", (v) => parseInt(v, 10)),
    },
    notify: {
      pushplus_token: el("cfg-pushplus_token") ? el("cfg-pushplus_token").value.trim() : undefined,
      holdings_report_interval_hours: el("cfg-holdings_report_interval_hours") ? parseInt(el("cfg-holdings_report_interval_hours").value, 10) : undefined,
      holdings_report_change_threshold_pct: el("cfg-holdings_report_change_threshold_pct") ? parseFloat(el("cfg-holdings_report_change_threshold_pct").value) : undefined,
      holdings_report_drop_enabled: el("cfg-holdings-drop-enabled") ? !!el("cfg-holdings-drop-enabled").checked : undefined,
      email_user: el("cfg-email_user") ? el("cfg-email_user").value.trim() : undefined,
      email_pass: el("cfg-email_pass") ? el("cfg-email_pass").value.trim() : undefined,
      imap_server: el("cfg-imap_server") ? el("cfg-imap_server").value.trim() : undefined,
      target_sender: el("cfg-target_sender") ? el("cfg-target_sender").value.trim() : undefined,
      allowed_sender: el("cfg-allowed_sender") ? el("cfg-allowed_sender").value.trim() : undefined,
      subject_success: el("cfg-subject_success") ? el("cfg-subject_success").value.trim() : undefined,
      subject_fail: el("cfg-subject_fail") ? el("cfg-subject_fail").value.trim() : undefined,
      email_timeout_seconds: el("cfg-email_timeout_seconds") ? parseInt(el("cfg-email_timeout_seconds").value, 10) || undefined : undefined,
    },
    system: {
      exchange_rate_refresh_hours: readNumberInput("cfg-exchange-refresh-hours"),
      session_keepalive_hours: readNumberInput("cfg-session-keepalive-hours"),
      ui_scale: el("cfg-ui_scale") ? el("cfg-ui_scale").value : undefined,
    },
    steam_deals: {
      enabled: !!el("cfg-steam-deals-enabled")?.checked,
      auto_refresh_days: el("cfg-steam-deals-auto-refresh-days") ? parseInt(el("cfg-steam-deals-auto-refresh-days").value, 10) : undefined,
      max_game_threads: el("cfg-steam-deals-game-threads") ? parseInt(el("cfg-steam-deals-game-threads").value, 10) || undefined : undefined,
      max_region_threads: el("cfg-steam-deals-region-threads") ? parseInt(el("cfg-steam-deals-region-threads").value, 10) || undefined : undefined,
    },
  };
}

function removeUndefinedValues(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return value;
  const out = {};
  Object.entries(value).forEach(([key, child]) => {
    if (child === undefined) return;
    const compacted = removeUndefinedValues(child);
    if (compacted === undefined) return;
    out[key] = compacted;
  });
  return out;
}

function pickKeys(source, keys) {
  const out = {};
  keys.forEach((key) => {
    if (source && source[key] !== undefined) out[key] = source[key];
  });
  return out;
}

function configForSettingsTab(tabName) {
  const all = formToConfig();
  const tab = tabName || getActiveSettingsTabName();
  if (tab === "trade") {
    return removeUndefinedValues({
      iflow: all.iflow,
      buff: pickKeys(all.buff, ["pay_method", "game", "price_tolerance"]),
      pipeline: pickKeys(all.pipeline, [
        "target_balance",
        "max_discount",
        "huge_profit_offset",
        "iflow_top_n",
        "exclude_keywords",
        "start_time_limit_enabled",
        "start_time_hour",
        "end_time_hour",
      ]),
    });
  }
  if (tab === "sell") {
    return removeUndefinedValues({
      pipeline: pickKeys(all.pipeline, [
        "sell_price_ratio",
        "sell_strategy",
        "sell_price_offset",
        "sell_price_wall_volume",
        "sell_price_max_ignore_volume",
        "sell_trend_days",
        "max_listings_per_item",
        "listing_delay_seconds",
        "resell_ratio",
        "safe_purchase_hard_qty_cap",
        "safe_purchase_liquidity_ratio",
        "safe_purchase_low_price_threshold",
        "safe_purchase_low_price_penalty",
        "safe_purchase_low_price_hard_cap",
        "sell_pressure_orders_n",
        "sell_pressure_threshold",
      ]),
    });
  }
  if (tab === "notify") return removeUndefinedValues({ notify: all.notify });
  if (tab === "stability") return removeUndefinedValues({ stability: all.stability });
  if (tab === "system") {
    return removeUndefinedValues({
      pipeline: pickKeys(all.pipeline, [
        "retry_interval_seconds",
        "current_price_refresh_minutes",
        "market_price_circuit_enabled",
      ]),
      inventory: all.inventory,
      system: all.system,
      steam_deals: all.steam_deals,
    });
  }
  return removeUndefinedValues(all);
}

async function saveConfigFromForm() {
  const d = await fetchJson(API + "/config");
  const merged = deepMerge(d.config || {}, removeUndefinedValues(formToConfig()));
  await fetchJson(API + "/config", { method: "POST", body: JSON.stringify({ config: merged }) });
  await loadConfig();
  setupInventoryAutoRefresh();
}

async function saveSettingsTab(tabName) {
  const tab = tabName || getActiveSettingsTabName();
  if (tab === "proxy") {
    await saveProxyConfig();
    clearSettingsTabDirty("proxy");
    return;
  }
  const d = await fetchJson(API + "/config");
  const merged = deepMerge(d.config || {}, configForSettingsTab(tab));
  await fetchJson(API + "/config", { method: "POST", body: JSON.stringify({ config: merged }) });
  if (tab === "system") {
    const invRefresh = readNumberInput("cfg-inv-refresh", (v) => parseInt(v, 10));
    const priceRefresh = readNumberInput("cfg-current-price-refresh-minutes", (v) => parseInt(v, 10));
    inventoryRefreshSeconds = Number.isFinite(invRefresh) ? invRefresh : 60;
    currentPriceRefreshMinutes = Number.isFinite(priceRefresh) ? priceRefresh : 10;
    applyInternalUiScale(el("cfg-ui_scale")?.value || "0.7");
    setupInventoryAutoRefresh();
  }
  if (tab === "notify") updateUXStatus(merged);
  clearSettingsTabDirty(tab);
  toast("已保存", SETTINGS_TAB_LABELS[tab] || "设置");
}
async function startPipeline() {
  try {
    await saveConfigFromForm();
    const d = await fetchJson(API + "/config");
    await fetchJson(API + "/pipeline/start", { method: "POST", body: JSON.stringify({ config: d.config || {} }) });
    toast("启动请求已发送");
    refreshStatus();
  } catch (e) {
    toast("启动失败", e.message || "请检查配置与后端日志");
  }
}
async function stopPipeline() {
  try {
    await fetchJson(API + "/pipeline/stop", { method: "POST" });
    toast("停止请求已发送");
    refreshStatus();
  } catch (e) {
    toast("停止失败", e.message || "请稍后再试");
  }
}
async function confirmPayment(ok) {
  try {
    const box = el("pending-payment");
    const payment_id = box?.dataset.paymentId || undefined;
    const r = await fetchJson(API + "/confirm_payment", { method: "POST", body: JSON.stringify({ ok, payment_id }) });
    if (r && r.stale) {
      toast("确认已过期", "该支付确认不属于当前订单");
      refreshStatus();
      return;
    }
    el("pending-payment")?.classList.add("hidden");
    toast(ok ? "已确认付款" : "已标记为失败");
    refreshStatus();
  } catch (e) {
    toast("操作失败", e.message || "请稍后再试");
  }
}
async function exportConfig() {
  try {
    // 优先用后端直接下载（适合内置浏览器，后端设置 Content-Disposition: attachment）
    const a = document.createElement("a");
    a.href = API + "/export_full/download";
    a.target = "_blank";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("已导出完整数据", "配置、账号、交易、凭证、操作记录");
  } catch (e) {
    toast("导出失败", e.message || "请稍后再试");
  }
}
function isFullBackup(json) {
  return json && (typeof json.version === "number" || json.app_config != null || json.credentials != null || json.transactions != null || json.accounts != null);
}
async function importConfigFromFile(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const json = JSON.parse(text);
    if (isFullBackup(json)) {
      const r = await fetchJson(API + "/import_full", { method: "POST", body: JSON.stringify(json) });
      if (!r.ok) throw new Error(r.error || "导入失败");
      await loadConfig();
      await refreshTransactions();
      await refreshAccounts();
      logLines = [];
      const out = el("log-output");
      if (out) out.dataset.lastIndex = "0";
      await refreshLog();
      toast("已恢复完整数据", "配置、账号、交易、凭证、操作记录");
    } else {
      await fetchJson(API + "/config", { method: "POST", body: JSON.stringify({ config: json }) });
      await loadConfig();
      toast("已导入配置", "仅应用配置已写入");
    }
  } catch (e) {
    toast("导入失败", e.message || "请确认 JSON 格式正确");
  } finally {
    const input = el("cfg-import-file");
    if (input) input.value = "";
  }
}
function setupInventoryAutoRefresh() {
  if (inventoryTimer) {
    clearInterval(inventoryTimer);
    inventoryTimer = null;
  }
  if (currentPriceTimer) {
    clearInterval(currentPriceTimer);
    currentPriceTimer = null;
  }
  if (inventoryRefreshSeconds && inventoryRefreshSeconds > 0) {
    inventoryTimer = setInterval(() => {
      refreshInventory(true);
    }, inventoryRefreshSeconds * 1000);
  }
  if (currentPriceRefreshMinutes && currentPriceRefreshMinutes > 0) {
    refreshMarketPrices();
    currentPriceTimer = setInterval(() => {
      refreshMarketPrices();
    }, currentPriceRefreshMinutes * 60 * 1000);
  }
}

// ---- 配置完整性检查 & 新手引导向导 ----
const WIZARD_SKIP_KEY = "aetherswap_onboard_skip";

function getSteamGuardSetupStatus(accounts, cfg) {
  const list = Array.isArray(accounts) ? accounts : [];
  const sg = cfg?.steam_guard || {};
  const sc = cfg?.steam_confirm || {};
  const globalShared = !!sg.shared_secret;
  const globalIdentity = !!sc.identity_secret;
  const accountShared = list.some((a) => {
    const status = a.steam_guard_status || {};
    const guard = a.steam_guard || {};
    return !!status.resolved_configured || !!guard.shared_secret;
  });
  const accountIdentity = list.some((a) => {
    const status = a.steam_guard_status || {};
    const guard = a.steam_guard || {};
    return !!status.identity_configured || !!guard.identity_secret;
  });
  const hasShared = accountShared || globalShared;
  const hasIdentity = accountIdentity || globalIdentity;
  return {
    hasShared,
    hasIdentity,
    complete: hasShared && hasIdentity,
  };
}

function _wizardIsFirstTime(cfg, accounts, buffNoCookie) {
  const n = cfg.notify || {};
  const guardStatus = getSteamGuardSetupStatus(accounts, cfg);
  const noConfig = !guardStatus.hasShared && !guardStatus.hasIdentity && !n.pushplus_token;
  const noAccount = !accounts || accounts.length === 0;
  // 全未配置 或者 buff cookie 不存在也弹向导
  return (noConfig && noAccount) || buffNoCookie;
}

async function checkAndShowOnboardingWizard() {
  if (localStorage.getItem(WIZARD_SKIP_KEY) === "1") return false;
  let cfg = {}, accounts = [], buffNoCookie = false;
  try {
    const [cfgData, accData, statusData] = await Promise.all([
      fetchJson(API + "/config"),
      fetchJson(API + "/accounts"),
      fetchJson(API + "/status"),
    ]);
    cfg = cfgData.config || {};
    accounts = accData.accounts || [];
    buffNoCookie = !!statusData.buff_no_cookie;

    if (typeof _hasAnyAccount !== 'undefined') {
      _hasAnyAccount = accounts.length > 0;
    }
  } catch (e) { /* 网络错误时默认弹出 */ }
  if (!_wizardIsFirstTime(cfg, accounts, buffNoCookie)) return false;
  // 只有「其他都已配置、仅 Buff Cookie 缺失」时才直接跳到第 3 步
  const guardStatus = getSteamGuardSetupStatus(accounts, cfg);
  const n = cfg.notify || {};
  const configDone = guardStatus.complete;
  const accountDone = accounts.length > 0;
  const onlyBuffMissing = buffNoCookie && configDone && accountDone && !!n.pushplus_token;
  _showWizard(onlyBuffMissing);
  return true;
}

function _showWizard(startAtBuffStep = false) {
  // startAtBuffStep=true 仅当「其他已配置、仅 Buff Cookie 缺失」时才成立

  const overlay = el("onboard-wizard-overlay");
  if (!overlay) return;
  overlay.classList.remove("hidden");

  let currentStep = 0;
  const TOTAL_STEPS = 4; // steps 1-4 (0 is welcome)

  const dots = overlay.querySelectorAll(".wizard-dot");
  const lines = overlay.querySelectorAll(".wizard-line");
  const steps = overlay.querySelectorAll(".wizard-step");
  const btnNext = el("wizard-btn-next");
  const btnSkip = el("wizard-btn-skip");
  const btnPrev = el("wizard-btn-prev");
  const noRemindCb = el("wizard-no-remind");

  // Buff relogin state
  let _buffReloginStarted = false;

  function updateProgress(step) {
    dots.forEach((d, i) => {
      d.classList.remove("active", "done");
      if (i < step) d.classList.add("done");
      else if (i === step) d.classList.add("active");
    });
    lines.forEach((l, i) => {
      l.classList.toggle("done", i < step);
    });
  }

  function updateButtons(step) {
    if (btnPrev) btnPrev.style.display = step === 0 ? "none" : "";
    if (step === 0) {
      btnNext.textContent = "开始配置 →";
      btnSkip.textContent = "跳过全部";
    } else if (step === TOTAL_STEPS) {
      btnNext.textContent = "完成引导 ✓";
      btnSkip.textContent = "跳过";
    } else {
      btnNext.textContent = "下一步 →";
      btnSkip.textContent = "跳过此步";
    }
  }

  function goToStep(step) {
    currentStep = step;
    steps.forEach((s, i) => s.classList.toggle("active", i === step));
    updateProgress(step);
    updateButtons(step);

    // 进入 Buff 步骤时重置状态
    if (step === 4) {
      _buffReloginStarted = false;
      const doneBtn = el("wiz-buff-done");
      if (doneBtn) doneBtn.disabled = true;
      const statusEl = el("wiz-buff-status");
      if (statusEl) statusEl.textContent = "";
    }
  }

  const wizRestoreBtn = el("wiz-restore-btn");
  const wizRestoreFile = el("wiz-restore-file");
  const wizRestoreStatus = el("wiz-restore-status");
  if (wizRestoreBtn && wizRestoreFile) {
    wizRestoreBtn.onclick = () => wizRestoreFile.click();
    wizRestoreFile.onchange = async () => {
      const file = wizRestoreFile.files && wizRestoreFile.files[0];
      if (!file) return;
      wizRestoreBtn.disabled = true;
      if (wizRestoreStatus) { wizRestoreStatus.style.display = "block"; wizRestoreStatus.textContent = "⏳ 正在导入，请稍候…"; wizRestoreStatus.style.color = "var(--text-muted,#aaa)"; }
      try {
        const text = await file.text();
        const json = JSON.parse(text);
        if (!isFullBackup(json)) throw new Error("所选文件不是完整备份，请确认文件正确");
        const r = await fetchJson(API + "/import_full", { method: "POST", body: JSON.stringify(json) });
        if (!r.ok) throw new Error(r.error || "导入失败");
        if (wizRestoreStatus) { wizRestoreStatus.textContent = "✅ 数据已恢复！正在刷新…"; wizRestoreStatus.style.color = "#4ade80"; }
        await loadConfig();
        try { await refreshTransactions(); } catch { }
        try { await refreshAccounts(); } catch { }
        try { logLines = []; const out = el("log-output"); if (out) out.dataset.lastIndex = "0"; await refreshLog(); } catch { }
        toast("已从备份恢复全部数据", "配置、账号、交易记录均已导入");
        setTimeout(() => closeWizard(null), 900);
      } catch (e) {
        if (wizRestoreStatus) { wizRestoreStatus.textContent = "❌ " + (e.message || "导入失败，请确认 JSON 格式正确"); wizRestoreStatus.style.color = "#f87171"; }
        wizRestoreBtn.disabled = false;
      } finally {
        wizRestoreFile.value = "";
      }
    };
  }

  // Buff 登录按钮

  const buffOpenBtn = el("wiz-buff-open");
  const buffDoneBtn = el("wiz-buff-done");
  if (buffOpenBtn) {
    buffOpenBtn.onclick = async () => {
      buffOpenBtn.disabled = true;
      const statusEl = el("wiz-buff-status");
      if (statusEl) statusEl.textContent = "正在打开浏览器，请稍候…";
      try {
        const r = await fetchJson(API + "/auth/buff/relogin_start", { method: "POST" });
        if (r.ok) {
          _buffReloginStarted = true;
          if (r.novnc_url) {
            window.open(r.novnc_url, "_blank", "noopener");
          }
          if (statusEl) {
            if (r.novnc_url) {
              statusEl.innerHTML = `✅ 容器浏览器已打开。请在 <a href="${escapeHtml(r.novnc_url)}" target="_blank" rel="noopener">noVNC 页面</a> 完成 Buff 登录后点击「已完成登录」。`;
            } else {
              statusEl.textContent = r.message || "✅ 浏览器已打开，请在其中完成 Buff 登录后点击「已完成登录」。";
            }
          }
          if (buffDoneBtn) buffDoneBtn.disabled = false;
        } else {
          if (statusEl) statusEl.textContent = "❌ 打开失败：" + (r.error || "请检查运行环境");
          buffOpenBtn.disabled = false;
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = "❌ 请求失败：" + (e.message || "");
        buffOpenBtn.disabled = false;
      }
    };
  }
  if (buffDoneBtn) {
    buffDoneBtn.onclick = async () => {
      buffDoneBtn.disabled = true;
      const statusEl = el("wiz-buff-status");
      if (statusEl) statusEl.textContent = "正在保存 Cookie，请稍候…";
      try {
        const r = await fetchJson(API + "/auth/buff/relogin_finish", {
          method: "POST",
          body: JSON.stringify({ success: true }),
        });
        if (r.ok) {
          if (statusEl) statusEl.textContent = "✅ Buff Cookie 已保存！";
          setTimeout(() => {
            if (currentStep === TOTAL_STEPS) closeWizard("accounts");
            else goToStep(currentStep + 1);
          }, 800);
        } else {
          if (statusEl) statusEl.textContent = "❌ 保存失败：" + (r.error || "");
          buffDoneBtn.disabled = false;
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = "❌ 请求失败：" + (e.message || "");
        buffDoneBtn.disabled = false;
      }
    };
  }

  async function saveCurrentStep() {
    try {
      const d = await fetchJson(API + "/config");
      const cfg = d.config || {};
      if (currentStep === 1) {
        const username = (el("wiz-account-username")?.value || "").trim();
        const password = (el("wiz-account-password")?.value || "").trim();
        const accountNote = (el("wiz-account-note")?.value || "").trim();
        try {
          const accData = await fetchJson(API + "/accounts");
          const accountList = accData.accounts || [];
          const existing = (accData.accounts || []).find((a) =>
            username && a.username === username
          );
          const current = accountList.find((a) => a.id === accData.current_id) || accountList[0];
          if (!username && !accountNote) {
            if (current?.has_password) return true;
            toast("请填写 Steam 用户名和密码", "保存密码后才能自动保活和执行交易");
            return false;
          }
          if (existing) {
            if (!password && !existing.has_password) {
              toast("请填写 Steam 密码", "该账号还没有保存密码，无法自动保活和执行交易");
              return false;
            }
            const body = {};
            if (accountNote) body.account_note = accountNote;
            if (password) body.password = password;
            if (Object.keys(body).length) {
              await fetchJson(API + "/accounts/" + encodeURIComponent(existing.id), {
                method: "PUT",
                body: JSON.stringify(body),
              });
            }
            await fetchJson(API + "/accounts/" + encodeURIComponent(existing.id) + "/set_current", { method: "POST" });
          } else if (!username && accountNote) {
            const target = current;
            if (!target) {
              toast("请填写 Steam 用户名和密码", "保存密码后才能自动保活和执行交易");
              return false;
            }
            if (!password && !target.has_password) {
              toast("请填写 Steam 密码", "该账号还没有保存密码，无法自动保活和执行交易");
              return false;
            }
            const body = { account_note: accountNote };
            if (password) body.password = password;
            await fetchJson(API + "/accounts/" + encodeURIComponent(target.id), {
              method: "PUT",
              body: JSON.stringify(body),
            });
          } else if (username) {
            if (!password) {
              toast("请填写 Steam 密码", "保存密码后才能自动保活和执行交易");
              return false;
            }
            const r = await fetchJson(API + "/accounts", {
              method: "POST",
              body: JSON.stringify({
                username,
                password,
                account_note: accountNote,
                avatar_url: "",
              }),
            });
            if (r.ok && r.account?.id) {
              await fetchJson(API + "/accounts/" + encodeURIComponent(r.account.id) + "/set_current", { method: "POST" });
            }
          }
          try { await refreshAccounts(); } catch { }
        } catch (e) {
          toast("账号保存失败", e.message || "请稍后在账号管理中添加");
          return false;
        }
      } else if (currentStep === 2) {
        const ss = (el("wiz-shared-secret")?.value || "").trim();
        const is = (el("wiz-identity-secret")?.value || "").trim();
        if (!ss && !is) return true;
        let accountSaved = false;
        try {
          const accData = await fetchJson(API + "/accounts");
          const list = accData.accounts || [];
          const target = list.find((a) => a.id === accData.current_id) || list[0];
          if (target) {
            const guard = {
              ...(target.steam_guard || {}),
              ...(ss ? { shared_secret: ss } : {}),
              ...(is ? { identity_secret: is } : {}),
            };
            const r = await fetchJson(API + "/accounts/" + encodeURIComponent(target.id), {
              method: "PUT",
              body: JSON.stringify({ steam_guard: guard }),
            });
            accountSaved = !!r.ok;
          }
        } catch {
          accountSaved = false;
        }
        if (!accountSaved) {
          const sg = { ...(cfg.steam_guard || {}), ...(ss ? { shared_secret: ss } : {}) };
          const sc = { ...(cfg.steam_confirm || {}), ...(is ? { identity_secret: is } : {}) };
          await fetchJson(API + "/config", {
            method: "POST",
            body: JSON.stringify({ config: { ...cfg, steam_guard: sg, steam_confirm: sc } }),
          });
        } else {
          try { await refreshAccounts(); } catch { }
        }
      } else if (currentStep === 3) {
        const tok = (el("wiz-pushplus-token")?.value || "").trim();
        if (!tok) return true;
        const notify = { ...(cfg.notify || {}), pushplus_token: tok };
        await fetchJson(API + "/config", {
          method: "POST",
          body: JSON.stringify({ config: { ...cfg, notify } }),
        });
        const gPush = el("cfg-pushplus_token");
        if (gPush) gPush.value = tok;
      }
      // step 4 (Buff) is handled by its own buttons
      try { updateUXStatus(((await fetchJson(API + "/config")).config || {})); } catch { }
      return true;
    } catch (e) {
      toast("保存失败", e.message || "请稍后手动在设置页填写");
      return false;
    }
  }

  function closeWizard(goToTab) {
    // 如果用户在 Buff 步骤打开了浏览器但没点「已完成」，发送 cancel
    if (_buffReloginStarted) {
      fetchJson(API + "/auth/buff/relogin_finish", {
        method: "POST",
        body: JSON.stringify({ success: false }),
      }).catch(() => { });
      _buffReloginStarted = false;
    }
    if (noRemindCb && noRemindCb.checked) {
      localStorage.setItem(WIZARD_SKIP_KEY, "1");
    }
    overlay.classList.add("hidden");
    if (goToTab) {
      const tabEl = document.querySelector(`[data-tab="${goToTab}"]`);
      if (tabEl) tabEl.click();
    }
  }

  btnNext.onclick = async () => {
    if (currentStep < TOTAL_STEPS) {
      const ok = await saveCurrentStep();
      if (!ok) return;
      goToStep(currentStep + 1);
    } else {
      closeWizard("accounts");
    }
  };

  if (btnPrev) {
    btnPrev.onclick = () => {
      if (currentStep <= 0) return;
      if (currentStep === 4 && _buffReloginStarted) {
        fetchJson(API + "/auth/buff/relogin_finish", {
          method: "POST",
          body: JSON.stringify({ success: false }),
        }).catch(() => { });
        _buffReloginStarted = false;
      }
      goToStep(currentStep - 1);
    };
  }

  btnSkip.onclick = async () => {
    if (currentStep === 0 || currentStep === TOTAL_STEPS) {
      closeWizard(null);
    } else if (currentStep === 1) {
      try {
        const accData = await fetchJson(API + "/accounts");
        const list = accData.accounts || [];
        if (!list.some((a) => a.has_password)) {
          toast("请先保存 Steam 密码", "没有已保存密码的账号时，无法跳过账号配置");
          return;
        }
      } catch {
        toast("账号检查失败", "请填写 Steam 用户名和密码后继续");
        return;
      }
      goToStep(currentStep + 1);
    } else if (currentStep === 4 && _buffReloginStarted) {
      // 已打开浏览器但选择跳过：取消 relogin
      fetchJson(API + "/auth/buff/relogin_finish", {
        method: "POST",
        body: JSON.stringify({ success: false }),
      }).catch(() => { });
      _buffReloginStarted = false;
      goToStep(currentStep + 1);
    } else if (currentStep < TOTAL_STEPS) {
      goToStep(currentStep + 1);
    } else {
      closeWizard(null);
    }
  };

  // 如果只是 buff cookie 缺失，从步骤 4 开始
  goToStep(startAtBuffStep ? 4 : 0);
}


// ---- UX 状态统一更新入口 ----
async function updateUXStatus(cfg) {
  let accounts = [];
  try {
    const d = await fetchJson(API + "/accounts");
    accounts = d.accounts || [];
  } catch (e) { }
  updateNavBadges(cfg, accounts);
  renderGettingStartedCard(cfg, accounts);
}

function updateNavBadges(cfg, accounts) {
  const n = cfg.notify || {};
  const guardStatus = getSteamGuardSetupStatus(accounts, cfg);
  const settingsOk = !!n.pushplus_token;
  const accountOk = accounts.length > 0;

  const badgeSettings = el("nav-badge-settings");
  if (badgeSettings) badgeSettings.classList.toggle("hidden", settingsOk);

  const badgeAccounts = el("nav-badge-accounts");
  if (badgeAccounts) {
    badgeAccounts.classList.toggle("hidden", accountOk && guardStatus.complete);
    badgeAccounts.title = !accountOk ? "尚未添加账号" : (!guardStatus.complete ? "Steam 账号令牌未配置" : "");
  }
}

function renderGettingStartedCard(cfg, accounts) {
  const card = el("getting-started-card");
  if (!card) return;

  // 已被用户手动关闭时不再显示
  if (localStorage.getItem("aetherswap_gs_card_closed") === "1") {
    card.style.display = "none";
    return;
  }

  const n = cfg.notify || {};
  const guardStatus = getSteamGuardSetupStatus(accounts, cfg);

  const steps = [
    {
      done: accounts.length > 0,
      label: "添加 Steam 账号并登录（<span class='gs-link' onclick='document.querySelector(\"[data-tab=accounts]\").click()'>账号管理</span>）",
    },
    {
      done: guardStatus.complete,
      label: "填写账号 Steam 令牌（<span class='gs-link' onclick='document.querySelector(\"[data-tab=accounts]\").click()'>账号管理 → Steam 令牌</span>）",
    },
    {
      done: !!n.pushplus_token,
      label: "填写 PushPlus 推送 Token（<span class='gs-link' onclick='document.querySelector(\"[data-tab=settings]\").click()'>系统设置 → 推送与邮箱</span>）",
    },
    {
      done: accounts.length > 0 && guardStatus.complete && !!n.pushplus_token,
      label: "返回仪表盘点击「启动任务」🚀",
    },
  ];

  const allDone = steps.every((s) => s.done);
  if (allDone) {
    card.style.display = "none";
    return;
  }

  card.style.display = "";
  const stepsEl = el("gs-steps");
  if (!stepsEl) return;
  stepsEl.innerHTML = steps
    .map(
      (s) =>
        `<div class="gs-step ${s.done ? "done" : ""}">
          <span class="gs-icon">${s.done ? "✅" : "⬜"}</span>
          <span>${s.label}</span>
        </div>`
    )
    .join("");

  // 绑定关闭按钮（只绑一次）
  const closeBtn = el("btn-gs-close");
  if (closeBtn && !closeBtn._bound) {
    closeBtn._bound = true;
    closeBtn.addEventListener("click", () => {
      localStorage.setItem("aetherswap_gs_card_closed", "1");
      card.style.display = "none";
    });
  }
}

function bindUXEvents() {
  // 账号面板操作提示关闭按钮
  const aguClose = el("btn-agu-close");
  if (aguClose) {
    aguClose.addEventListener("click", () => {
      const callout = el("accounts-guide-callout");
      if (callout) callout.classList.add("hidden");
    });
  }
}
