
let inventoryRefreshSeconds = 60;
let inventoryTimer = null;
let credentialsVisible = false;
let credentialLoginPlatform = null;
const DEFAULT_ACTION_RISK_SEGMENTS = [
  { min_price: 0, max_price: 10, max_capital_per_item: 80, max_inventory_per_item: 8 },
  { min_price: 10, max_price: 100, max_capital_per_item: 300, max_inventory_per_item: 3 },
  { min_price: 100, max_price: "", max_capital_per_item: 800, max_inventory_per_item: 1 },
];

function renderActionRiskSegments(segments, count) {
  const box = el("action-risk-segments");
  if (!box) return;
  const total = Math.max(1, Math.min(parseInt(count, 10) || 3, 5));
  const normalized = Array.from({ length: total }, (_, idx) => {
    return { ...(DEFAULT_ACTION_RISK_SEGMENTS[idx] || DEFAULT_ACTION_RISK_SEGMENTS[DEFAULT_ACTION_RISK_SEGMENTS.length - 1]), ...((segments || [])[idx] || {}) };
  });
  box.innerHTML = normalized.map((seg, idx) => `
    <div class="field-row field-row--4 action-risk-segment" data-index="${idx}">
      <div class="field">
        <label>第 ${idx + 1} 段最低价</label>
        <input type="number" class="cfg-action-seg-min" min="0" step="0.01" value="${seg.min_price ?? 0}" />
      </div>
      <div class="field">
        <label>第 ${idx + 1} 段最高价</label>
        <input type="number" class="cfg-action-seg-max" min="0" step="0.01" value="${seg.max_price ?? ""}" placeholder="不限" />
      </div>
      <div class="field">
        <label>单品资金上限</label>
        <input type="number" class="cfg-action-seg-capital" min="0" step="1" value="${seg.max_capital_per_item ?? 0}" />
      </div>
      <div class="field">
        <label>单品库存上限</label>
        <input type="number" class="cfg-action-seg-inventory" min="0" step="1" value="${seg.max_inventory_per_item ?? 0}" />
      </div>
    </div>
  `).join("");
}

function readActionRiskSegments() {
  return Array.from(document.querySelectorAll(".action-risk-segment")).map((row) => {
    const maxRaw = row.querySelector(".cfg-action-seg-max")?.value;
    return {
      min_price: parseFloat(row.querySelector(".cfg-action-seg-min")?.value || "0") || 0,
      max_price: maxRaw === "" ? null : parseFloat(maxRaw || "0"),
      max_capital_per_item: parseFloat(row.querySelector(".cfg-action-seg-capital")?.value || "0") || 0,
      max_inventory_per_item: parseInt(row.querySelector(".cfg-action-seg-inventory")?.value || "0", 10) || 0,
    };
  });
}

function setCredentialsVisible(visible) {
  credentialsVisible = !!visible;
  ["cfg-buff-cookie", "cfg-uuyp-cookie", "cfg-eco-cookie", "cfg-steamdt-openapi-key"].forEach((id) => {
    const elNode = el(id);
    if (elNode) {
      if (elNode.tagName === "TEXTAREA") {
        elNode.style.webkitTextSecurity = credentialsVisible ? "none" : "disc";
        elNode.style.textSecurity = credentialsVisible ? "none" : "disc";
      } else {
        elNode.type = credentialsVisible ? "text" : "password";
      }
    }
  });
  const btn = el("btn-toggle-credentials");
  if (btn) btn.textContent = credentialsVisible ? "隐藏 Cookie" : "显示 Cookie";
}
async function loadConfig() {
  const d = await fetchJson(API + "/config");
  const c = d.config || {};
  const b = c.buff || {};
  const p = c.pipeline || {};
  const ps = c.priority_scheduler || {};
  const cl = c.crawl_layers || {};
  const sd = c.steamdt || {};
  const sdOpenapiPrice = sd.openapi_price || {};
  const ap = c.action_policy || {};
  const s = c.stability || {};
  const inv = c.inventory || {};
  const sys = c.system || {};
  const gPay = el("cfg-pay_method");
  if (gPay) gPay.value = (b.pay_method || "wechat").toLowerCase();
  const gTarget = el("cfg-target_balance");
  if (gTarget) gTarget.value = p.target_balance ?? "";
  const gMaxDisc = el("cfg-max_discount");
  if (gMaxDisc) gMaxDisc.value = p.max_discount ?? 0.8;
  const gHugeProfitOffset = el("cfg-huge_profit_offset");
  if (gHugeProfitOffset) gHugeProfitOffset.value = p.huge_profit_offset ?? "";
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
  const steamBalanceCostRatioEl = el("cfg-steam_balance_cost_ratio");
  if (steamBalanceCostRatioEl) steamBalanceCostRatioEl.value = p.steam_balance_cost_ratio ?? p.resell_ratio ?? "";
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
  const maxStalenessEl = el("cfg-max-staleness-minutes");
  if (maxStalenessEl) maxStalenessEl.value = p.max_staleness_minutes ?? "";
  const poJitBypassEl = el("cfg-purchase-order-jit-bypass-minutes");
  if (poJitBypassEl) poJitBypassEl.value = p.purchase_order_jit_bypass_minutes ?? "";
  const priorityEnabled = el("cfg-priority-enabled");
  if (priorityEnabled) priorityEnabled.checked = ps.enabled !== false;
  const priorityInterval = el("cfg-priority-global-interval");
  if (priorityInterval) priorityInterval.value = ps.global_interval_seconds ?? "";
  const priorityMinVolume = el("cfg-priority-min-volume");
  if (priorityMinVolume) priorityMinVolume.value = ps.min_volume_24h ?? "";
  const priorityMinProfit = el("cfg-priority-min-profit");
  if (priorityMinProfit) priorityMinProfit.value = ps.min_net_profit_rate ?? "";
  const p1p2 = el("cfg-priority-p1-p2-score");
  if (p1p2) p1p2.value = ps.p1_to_p2_score ?? "";
  const p2p3 = el("cfg-priority-p2-p3-score");
  if (p2p3) p2p3.value = ps.p2_to_p3_score ?? "";
  const p2p1 = el("cfg-priority-p2-p1-score");
  if (p2p1) p2p1.value = ps.p2_to_p1_score ?? "";
  const p3p2 = el("cfg-priority-p3-p2-score");
  if (p3p2) p3p2.value = ps.p3_to_p2_score ?? "";
  const p3NoProfit = el("cfg-priority-p3-no-profit-rounds");
  if (p3NoProfit) p3NoProfit.value = ps.p3_to_p2_no_profit_rounds ?? "";
  const p2NoHit = el("cfg-priority-p2-no-hit-rounds");
  if (p2NoHit) p2NoHit.value = ps.p2_to_p1_no_hit_rounds ?? "";
  const p2Hit = el("cfg-priority-p2-hit-rounds");
  if (p2Hit) p2Hit.value = ps.p2_to_p3_hit_rounds ?? "";
  const steamdtFresh = el("cfg-priority-steamdt-fresh");
  if (steamdtFresh) steamdtFresh.value = ps.steamdt_fresh_minutes ?? "";
  const jitTtl = el("cfg-priority-jit-ttl");
  if (jitTtl) jitTtl.value = ps.jit_ttl_minutes ?? "";
  const lowInterval = el("cfg-crawl-low-interval");
  if (lowInterval) lowInterval.value = cl.low_interval_seconds ?? "";
  const midInterval = el("cfg-crawl-mid-interval");
  if (midInterval) midInterval.value = cl.mid_interval_seconds ?? "";
  const lowLimit = el("cfg-crawl-low-limit");
  if (lowLimit) lowLimit.value = cl.low_limit ?? "";
  const midLimit = el("cfg-crawl-mid-limit");
  if (midLimit) midLimit.value = cl.mid_limit ?? "";
  const highLimit = el("cfg-crawl-high-limit");
  if (highLimit) highLimit.value = cl.high_limit ?? "";
  const apEnabled = el("cfg-action-policy-enabled");
  if (apEnabled) apEnabled.checked = ap.enabled !== false;
  const allowDirect = el("cfg-action-allow-direct-buy");
  if (allowDirect) allowDirect.checked = ap.allow_direct_buy !== false;
  const allowOrder = el("cfg-action-allow-buy-order");
  if (allowOrder) allowOrder.checked = ap.allow_buy_order !== false;
  const allowSell = el("cfg-action-allow-auto-sell");
  if (allowSell) allowSell.checked = !!ap.allow_auto_sell;
  const actionTtl = el("cfg-action-decision-ttl");
  if (actionTtl) actionTtl.value = ap.decision_ttl_minutes ?? "";
  const directRate = el("cfg-action-direct-buy-rate");
  if (directRate) directRate.value = ap.direct_buy_min_profit_rate ?? "";
  const orderRate = el("cfg-action-buy-order-rate");
  if (orderRate) orderRate.value = ap.buy_order_min_profit_rate ?? "";
  const sellRate = el("cfg-action-sell-rate");
  if (sellRate) sellRate.value = ap.sell_min_profit_rate ?? "";
  const actionMinVol = el("cfg-action-min-volume");
  if (actionMinVol) actionMinVol.value = ap.min_24h_volume ?? "";
  const segCount = el("cfg-action-risk-segment-count");
  const segmentCount = ap.risk_segment_count || (Array.isArray(ap.risk_segments) ? ap.risk_segments.length : 3) || 3;
  if (segCount) {
    segCount.value = String(Math.max(1, Math.min(segmentCount, 5)));
    if (!segCount._bound) {
      segCount._bound = true;
      segCount.addEventListener("change", () => renderActionRiskSegments(readActionRiskSegments(), segCount.value));
    }
  }
  renderActionRiskSegments(Array.isArray(ap.risk_segments) ? ap.risk_segments : DEFAULT_ACTION_RISK_SEGMENTS, segmentCount);
  const gStartTimeLimitEnabled = el("cfg-start-time-limit-enabled");
  if (gStartTimeLimitEnabled) gStartTimeLimitEnabled.checked = !!p.start_time_limit_enabled;
  const gStartTimeHour = el("cfg-start-time-hour");
  if (gStartTimeHour) gStartTimeHour.value = p.start_time_hour ?? "";
  const gEndTimeHour = el("cfg-end-time-hour");
  if (gEndTimeHour) gEndTimeHour.value = p.end_time_hour ?? "";
  const invInput = el("cfg-inv-refresh");
  if (invInput) invInput.value = inv.refresh_seconds ?? "";
  inventoryRefreshSeconds = parseInt(inv.refresh_seconds, 10) || inventoryRefreshSeconds || 60;
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
  const sg = c.steam_guard || {};
  const gSteamSecret = el("cfg-steam-shared-secret");
  if (gSteamSecret) gSteamSecret.value = sg.shared_secret ?? "";
  const sc = c.steam_confirm || {};
  const gAutoConfirm = el("cfg-steam-auto-confirm");
  if (gAutoConfirm) gAutoConfirm.checked = !!sc.enabled;
  const gIdentitySecret = el("cfg-steam-identity-secret");
  if (gIdentitySecret) gIdentitySecret.value = sc.identity_secret ?? "";
  const gDeviceId = el("cfg-steam-device-id");
  if (gDeviceId) gDeviceId.value = sc.device_id ?? "";
  const gFx = el("cfg-exchange-refresh-hours");
  if (gFx) gFx.value = sys.exchange_rate_refresh_hours ?? "";
  const gUiScale = el("cfg-ui_scale");
  if (gUiScale) {
    gUiScale.value = sys.ui_scale || "0.7";
    document.documentElement.style.zoom = sys.ui_scale || "0.7";
    gUiScale.addEventListener("change", (e) => {
      document.documentElement.style.zoom = e.target.value;
    });
  }
  const gThemeMode = el("cfg-theme-mode");
  if (gThemeMode) {
    const currentTheme = (typeof Theme !== "undefined" && typeof Theme.get === "function") ? Theme.get() : "dark";
    gThemeMode.value = currentTheme;
    if (!gThemeMode._bound) {
      gThemeMode._bound = true;
      gThemeMode.addEventListener("change", (e) => {
        const mode = e.target.value || "dark";
        if (typeof Theme !== "undefined" && typeof Theme.set === "function") Theme.set(mode);
        toast("主题已切换", mode === "system" ? "跟随系统" : mode === "dark" ? "深色" : "浅色");
      });
    }
  }
  const creds = d.credentials || {};
  const gBuffCookie = el("cfg-buff-cookie");
  if (gBuffCookie) {
    if (gBuffCookie.tagName === "INPUT") gBuffCookie.type = "password";
    gBuffCookie.value = (creds.buff && creds.buff.cookies) || "";
  }
  const gUuypCookie = el("cfg-uuyp-cookie");
  if (gUuypCookie) {
    if (gUuypCookie.tagName === "INPUT") gUuypCookie.type = "password";
    gUuypCookie.value = (creds.uuyp && creds.uuyp.cookies) || "";
  }
  const gEcoCookie = el("cfg-eco-cookie");
  if (gEcoCookie) {
    if (gEcoCookie.tagName === "INPUT") gEcoCookie.type = "password";
    gEcoCookie.value = (creds.eco && creds.eco.cookies) || "";
  }
  const gSteamdtOpenapiPriceEnabled = el("cfg-steamdt-openapi-price-enabled");
  if (gSteamdtOpenapiPriceEnabled) gSteamdtOpenapiPriceEnabled.checked = sdOpenapiPrice.enabled !== false;
  const gSteamdtOpenapiPriceBaseUrl = el("cfg-steamdt-openapi-price-base-url");
  if (gSteamdtOpenapiPriceBaseUrl) gSteamdtOpenapiPriceBaseUrl.value = sdOpenapiPrice.base_url ?? "https://open.steamdt.com";
  const gSteamdtOpenapiPriceTimeout = el("cfg-steamdt-openapi-price-timeout");
  if (gSteamdtOpenapiPriceTimeout) gSteamdtOpenapiPriceTimeout.value = sdOpenapiPrice.timeout_seconds ?? 20;
  const gSteamdtOpenapiPriceUseProxy = el("cfg-steamdt-openapi-price-use-proxy");
  if (gSteamdtOpenapiPriceUseProxy) gSteamdtOpenapiPriceUseProxy.checked = sdOpenapiPrice.use_proxy !== false;
  const gSteamdtOpenapiPriceBatchRpm = el("cfg-steamdt-openapi-price-batch-rpm");
  if (gSteamdtOpenapiPriceBatchRpm) gSteamdtOpenapiPriceBatchRpm.value = sdOpenapiPrice.batch_requests_per_minute ?? 1;
  const gSteamdtOpenapiPriceSingleRpm = el("cfg-steamdt-openapi-price-single-rpm");
  if (gSteamdtOpenapiPriceSingleRpm) gSteamdtOpenapiPriceSingleRpm.value = sdOpenapiPrice.single_requests_per_minute ?? 60;
  const gSteamdtOpenapiPriceSingleReserved = el("cfg-steamdt-openapi-price-single-reserved");
  if (gSteamdtOpenapiPriceSingleReserved) gSteamdtOpenapiPriceSingleReserved.value = sdOpenapiPrice.single_reserved_for_jit ?? 15;
  const gSteamdtOpenapiPriceBatchSize = el("cfg-steamdt-openapi-price-batch-size");
  if (gSteamdtOpenapiPriceBatchSize) gSteamdtOpenapiPriceBatchSize.value = sdOpenapiPrice.batch_size ?? 100;
  const gSteamdtOpenapiPriceP2Target = el("cfg-steamdt-openapi-price-p2-target");
  if (gSteamdtOpenapiPriceP2Target) gSteamdtOpenapiPriceP2Target.value = sdOpenapiPrice.p2_target_minutes ?? 60;
  const gSteamdtOpenapiPriceP3Target = el("cfg-steamdt-openapi-price-p3-target");
  if (gSteamdtOpenapiPriceP3Target) gSteamdtOpenapiPriceP3Target.value = sdOpenapiPrice.p3_target_minutes ?? 30;
  const gSteamdtOpenapiPriceTracked = el("cfg-steamdt-openapi-price-tracked");
  if (gSteamdtOpenapiPriceTracked) gSteamdtOpenapiPriceTracked.value = Array.isArray(sdOpenapiPrice.tracked_platforms) ? sdOpenapiPrice.tracked_platforms.join(",") : "steam,buff,uuyp,eco";
  const gSteamdtOpenapiPriceMode = el("cfg-steamdt-openapi-price-mode");
  if (gSteamdtOpenapiPriceMode) gSteamdtOpenapiPriceMode.value = sdOpenapiPrice.mode || "stable";
  const gSteamdtOpenapiPriceStableCycle = el("cfg-steamdt-openapi-price-stable-cycle");
  if (gSteamdtOpenapiPriceStableCycle) gSteamdtOpenapiPriceStableCycle.value = sdOpenapiPrice.stable_pool_cycle_minutes ?? 60;
  const gSteamdtOpenapiPriceDiscoveryTarget = el("cfg-steamdt-openapi-price-discovery-target");
  if (gSteamdtOpenapiPriceDiscoveryTarget) gSteamdtOpenapiPriceDiscoveryTarget.value = sdOpenapiPrice.discovery_target_minutes ?? 720;
  const gSteamdtOpenapiPriceCustomPoolShare = el("cfg-steamdt-openapi-price-custom-pool-share");
  if (gSteamdtOpenapiPriceCustomPoolShare) gSteamdtOpenapiPriceCustomPoolShare.value = sdOpenapiPrice.custom_pool_share_pct ?? 70;
  const gSteamdtOpenapiPriceAutoSwitchStable = el("cfg-steamdt-openapi-price-auto-switch-stable");
  if (gSteamdtOpenapiPriceAutoSwitchStable) gSteamdtOpenapiPriceAutoSwitchStable.checked = sdOpenapiPrice.auto_switch_to_stable_on_idle_complete !== false;

  const steamDeals = c.steam_deals || {};
  const gSdEnabled = el("cfg-steam-deals-enabled");
  if (gSdEnabled) gSdEnabled.checked = !!steamDeals.enabled;
  const gSdRefresh = el("cfg-steam-deals-auto-refresh-days");
  if (gSdRefresh) gSdRefresh.value = steamDeals.auto_refresh_days ?? "";
  const gSdGameThreads = el("cfg-steam-deals-game-threads");
  if (gSdGameThreads) gSdGameThreads.value = steamDeals.max_game_threads ?? "";
  const gSdRegionThreads = el("cfg-steam-deals-region-threads");
  if (gSdRegionThreads) gSdRegionThreads.value = steamDeals.max_region_threads ?? "";
  // 加载完成后刷新 UX 状态组件
  updateUXStatus(c);
  await loadCredentials();
}

function formToConfig() {
  return {
    buff: {
      pay_method: el("cfg-pay_method") ? el("cfg-pay_method").value : undefined,
      game: el("cfg-buff-game") ? el("cfg-buff-game").value.trim() : undefined,
      price_tolerance: el("cfg-price_tolerance") ? parseFloat(el("cfg-price_tolerance").value) || undefined : undefined,
    },
    pipeline: {
      target_balance: el("cfg-target_balance") ? parseFloat(el("cfg-target_balance").value) || undefined : undefined,
      max_discount: el("cfg-max_discount") ? parseFloat(el("cfg-max_discount").value) || undefined : undefined,
      huge_profit_offset: el("cfg-huge_profit_offset") ? parseFloat(el("cfg-huge_profit_offset").value) : undefined,
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
      steam_balance_cost_ratio: el("cfg-steam_balance_cost_ratio") ? parseFloat(el("cfg-steam_balance_cost_ratio").value) || undefined : undefined,
      safe_purchase_hard_qty_cap: el("cfg-safe_purchase_hard_qty_cap") ? parseInt(el("cfg-safe_purchase_hard_qty_cap").value, 10) : undefined,
      safe_purchase_liquidity_ratio: el("cfg-safe_purchase_liquidity_ratio") ? parseFloat(el("cfg-safe_purchase_liquidity_ratio").value) : undefined,
      safe_purchase_low_price_threshold: el("cfg-safe_purchase_low_price_threshold") ? parseFloat(el("cfg-safe_purchase_low_price_threshold").value) : undefined,
      safe_purchase_low_price_penalty: el("cfg-safe_purchase_low_price_penalty") ? parseFloat(el("cfg-safe_purchase_low_price_penalty").value) : undefined,
      safe_purchase_low_price_hard_cap: el("cfg-safe_purchase_low_price_hard_cap") ? parseInt(el("cfg-safe_purchase_low_price_hard_cap").value, 10) : undefined,
      sell_pressure_orders_n: el("cfg-sell_pressure_orders_n") ? parseInt(el("cfg-sell_pressure_orders_n").value, 10) : undefined,
      sell_pressure_threshold: el("cfg-sell_pressure_threshold") ? parseFloat(el("cfg-sell_pressure_threshold").value) : undefined,
      current_price_refresh_minutes: el("cfg-current-price-refresh-minutes") ? parseInt(el("cfg-current-price-refresh-minutes").value, 10) || undefined : undefined,
      max_staleness_minutes: el("cfg-max-staleness-minutes") ? parseInt(el("cfg-max-staleness-minutes").value, 10) || undefined : undefined,
      purchase_order_jit_bypass_minutes: el("cfg-purchase-order-jit-bypass-minutes") ? parseInt(el("cfg-purchase-order-jit-bypass-minutes").value, 10) || undefined : undefined,
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
      refresh_seconds: el("cfg-inv-refresh") ? parseInt(el("cfg-inv-refresh").value, 10) || undefined : undefined,
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
    steam_guard: {
      shared_secret: el("cfg-steam-shared-secret") ? el("cfg-steam-shared-secret").value.trim() : undefined,
    },
    steam_confirm: {
      enabled: !!el("cfg-steam-auto-confirm")?.checked,
      identity_secret: el("cfg-steam-identity-secret") ? el("cfg-steam-identity-secret").value.trim() : undefined,
      device_id: el("cfg-steam-device-id") ? el("cfg-steam-device-id").value.trim() : undefined,
    },
    system: {
      exchange_rate_refresh_hours: el("cfg-exchange-refresh-hours") ? parseFloat(el("cfg-exchange-refresh-hours").value) || undefined : undefined,
      ui_scale: el("cfg-ui_scale") ? el("cfg-ui_scale").value : undefined,
    },
    steam_deals: {
      enabled: !!el("cfg-steam-deals-enabled")?.checked,
      auto_refresh_days: el("cfg-steam-deals-auto-refresh-days") ? parseInt(el("cfg-steam-deals-auto-refresh-days").value, 10) : undefined,
      max_game_threads: el("cfg-steam-deals-game-threads") ? parseInt(el("cfg-steam-deals-game-threads").value, 10) || undefined : undefined,
      max_region_threads: el("cfg-steam-deals-region-threads") ? parseInt(el("cfg-steam-deals-region-threads").value, 10) || undefined : undefined,
    },
    priority_scheduler: {
      enabled: el("cfg-priority-enabled") ? !!el("cfg-priority-enabled").checked : undefined,
      global_interval_seconds: el("cfg-priority-global-interval") ? parseInt(el("cfg-priority-global-interval").value, 10) || undefined : undefined,
      min_volume_24h: el("cfg-priority-min-volume") ? parseInt(el("cfg-priority-min-volume").value, 10) || undefined : undefined,
      min_net_profit_rate: el("cfg-priority-min-profit") ? parseFloat(el("cfg-priority-min-profit").value) : undefined,
      p1_to_p2_score: el("cfg-priority-p1-p2-score") ? parseFloat(el("cfg-priority-p1-p2-score").value) : undefined,
      p2_to_p3_score: el("cfg-priority-p2-p3-score") ? parseFloat(el("cfg-priority-p2-p3-score").value) : undefined,
      p2_to_p1_score: el("cfg-priority-p2-p1-score") ? parseFloat(el("cfg-priority-p2-p1-score").value) : undefined,
      p3_to_p2_score: el("cfg-priority-p3-p2-score") ? parseFloat(el("cfg-priority-p3-p2-score").value) : undefined,
      p3_to_p2_no_profit_rounds: el("cfg-priority-p3-no-profit-rounds") ? parseInt(el("cfg-priority-p3-no-profit-rounds").value, 10) || undefined : undefined,
      p2_to_p1_no_hit_rounds: el("cfg-priority-p2-no-hit-rounds") ? parseInt(el("cfg-priority-p2-no-hit-rounds").value, 10) || undefined : undefined,
      p2_to_p3_hit_rounds: el("cfg-priority-p2-hit-rounds") ? parseInt(el("cfg-priority-p2-hit-rounds").value, 10) || undefined : undefined,
      steamdt_fresh_minutes: el("cfg-priority-steamdt-fresh") ? parseInt(el("cfg-priority-steamdt-fresh").value, 10) || undefined : undefined,
      jit_ttl_minutes: el("cfg-priority-jit-ttl") ? parseInt(el("cfg-priority-jit-ttl").value, 10) || undefined : undefined,
    },
    crawl_layers: {
      low_interval_seconds: el("cfg-crawl-low-interval") ? parseInt(el("cfg-crawl-low-interval").value, 10) || undefined : undefined,
      mid_interval_seconds: el("cfg-crawl-mid-interval") ? parseInt(el("cfg-crawl-mid-interval").value, 10) || undefined : undefined,
      low_limit: el("cfg-crawl-low-limit") ? parseInt(el("cfg-crawl-low-limit").value, 10) || undefined : undefined,
      mid_limit: el("cfg-crawl-mid-limit") ? parseInt(el("cfg-crawl-mid-limit").value, 10) || undefined : undefined,
      high_limit: el("cfg-crawl-high-limit") ? parseInt(el("cfg-crawl-high-limit").value, 10) || undefined : undefined,
    },
    steamdt: {
      openapi_price: {
        enabled: el("cfg-steamdt-openapi-price-enabled") ? !!el("cfg-steamdt-openapi-price-enabled").checked : undefined,
        base_url: el("cfg-steamdt-openapi-price-base-url") ? el("cfg-steamdt-openapi-price-base-url").value.trim() : undefined,
        timeout_seconds: el("cfg-steamdt-openapi-price-timeout") ? parseInt(el("cfg-steamdt-openapi-price-timeout").value, 10) || undefined : undefined,
        use_proxy: el("cfg-steamdt-openapi-price-use-proxy") ? !!el("cfg-steamdt-openapi-price-use-proxy").checked : undefined,
        batch_requests_per_minute: el("cfg-steamdt-openapi-price-batch-rpm") ? parseInt(el("cfg-steamdt-openapi-price-batch-rpm").value, 10) || undefined : undefined,
        single_requests_per_minute: el("cfg-steamdt-openapi-price-single-rpm") ? parseInt(el("cfg-steamdt-openapi-price-single-rpm").value, 10) || undefined : undefined,
        single_reserved_for_jit: el("cfg-steamdt-openapi-price-single-reserved") ? parseInt(el("cfg-steamdt-openapi-price-single-reserved").value, 10) || undefined : undefined,
        batch_size: el("cfg-steamdt-openapi-price-batch-size") ? parseInt(el("cfg-steamdt-openapi-price-batch-size").value, 10) || undefined : undefined,
        p2_target_minutes: el("cfg-steamdt-openapi-price-p2-target") ? parseInt(el("cfg-steamdt-openapi-price-p2-target").value, 10) || undefined : undefined,
        p3_target_minutes: el("cfg-steamdt-openapi-price-p3-target") ? parseInt(el("cfg-steamdt-openapi-price-p3-target").value, 10) || undefined : undefined,
        mode: el("cfg-steamdt-openapi-price-mode") ? (el("cfg-steamdt-openapi-price-mode").value || "stable") : undefined,
        stable_pool_cycle_minutes: el("cfg-steamdt-openapi-price-stable-cycle") ? parseInt(el("cfg-steamdt-openapi-price-stable-cycle").value, 10) || undefined : undefined,
        discovery_target_minutes: el("cfg-steamdt-openapi-price-discovery-target") ? parseInt(el("cfg-steamdt-openapi-price-discovery-target").value, 10) || undefined : undefined,
        custom_pool_share_pct: el("cfg-steamdt-openapi-price-custom-pool-share") ? parseInt(el("cfg-steamdt-openapi-price-custom-pool-share").value, 10) || undefined : undefined,
        auto_switch_to_stable_on_idle_complete: el("cfg-steamdt-openapi-price-auto-switch-stable") ? !!el("cfg-steamdt-openapi-price-auto-switch-stable").checked : undefined,
        tracked_platforms: el("cfg-steamdt-openapi-price-tracked")
          ? Array.from(new Set((el("cfg-steamdt-openapi-price-tracked").value || "").split(",").map((v) => v.trim().toLowerCase()).filter(Boolean)))
          : undefined,
      },
    },
    action_policy: {
      enabled: el("cfg-action-policy-enabled") ? !!el("cfg-action-policy-enabled").checked : undefined,
      allow_direct_buy: el("cfg-action-allow-direct-buy") ? !!el("cfg-action-allow-direct-buy").checked : undefined,
      allow_buy_order: el("cfg-action-allow-buy-order") ? !!el("cfg-action-allow-buy-order").checked : undefined,
      allow_auto_sell: el("cfg-action-allow-auto-sell") ? !!el("cfg-action-allow-auto-sell").checked : undefined,
      decision_ttl_minutes: el("cfg-action-decision-ttl") ? parseInt(el("cfg-action-decision-ttl").value, 10) || undefined : undefined,
      direct_buy_min_profit_rate: el("cfg-action-direct-buy-rate") ? parseFloat(el("cfg-action-direct-buy-rate").value) : undefined,
      buy_order_min_profit_rate: el("cfg-action-buy-order-rate") ? parseFloat(el("cfg-action-buy-order-rate").value) : undefined,
      sell_min_profit_rate: el("cfg-action-sell-rate") ? parseFloat(el("cfg-action-sell-rate").value) : undefined,
      min_24h_volume: el("cfg-action-min-volume") ? parseInt(el("cfg-action-min-volume").value, 10) || undefined : undefined,
      risk_segment_count: el("cfg-action-risk-segment-count") ? parseInt(el("cfg-action-risk-segment-count").value, 10) || undefined : undefined,
      risk_segments: readActionRiskSegments(),
    },
  };
}
async function saveCredentials() {
  const payload = {
    buff: { cookies: el("cfg-buff-cookie") ? el("cfg-buff-cookie").value.trim() : "" },
    uuyp: { cookies: el("cfg-uuyp-cookie") ? el("cfg-uuyp-cookie").value.trim() : "" },
    eco: { cookies: el("cfg-eco-cookie") ? el("cfg-eco-cookie").value.trim() : "" },
    steamdt_openapi: { api_key: el("cfg-steamdt-openapi-key") ? el("cfg-steamdt-openapi-key").value.trim() : "" },
  };
  const res = await fetchJson(API + "/credentials", { method: "POST", body: JSON.stringify(payload) });
  const savedAt = el("credentials-saved-at");
  if (savedAt) savedAt.textContent = `最近保存时间：${new Date().toLocaleString()}`;
  toast("保存成功", res.msg || "第三方平台凭证已更新");
}

async function loadCredentials() {
  try {
    const d = await fetchJson(API + "/credentials");
    const buff = d.buff || {};
    const uuyp = d.uuyp || {};
    const eco = d.eco || {};
    const steamdtOpenapi = d.steamdt_openapi || {};
    const gBuffCookie = el("cfg-buff-cookie");
    if (gBuffCookie) gBuffCookie.value = buff.cookies || "";
    const gUuypCookie = el("cfg-uuyp-cookie");
    if (gUuypCookie) gUuypCookie.value = uuyp.cookies || "";
    const gEcoCookie = el("cfg-eco-cookie");
    if (gEcoCookie) gEcoCookie.value = eco.cookies || "";
    const gSteamdtOpenapiKey = el("cfg-steamdt-openapi-key");
    if (gSteamdtOpenapiKey) gSteamdtOpenapiKey.value = steamdtOpenapi.api_key || "";
    const savedAt = el("credentials-saved-at");
    if (savedAt) savedAt.textContent = `最近保存时间：${new Date().toLocaleString()}`;
    await loadSteamdtCapsuleSummary();
    await loadPlatformConnectivity();
  } catch (e) {
    toast("加载失败", e.message || "后端暂不可用");
  }
}

function platformStatusLabel(status) {
  const s = String(status || "").toLowerCase();
  if (s === "ok") return "正常";
  if (s === "timeout") return "超时";
  if (s === "error") return "失败";
  if (s === "degraded") return "退化";
  if (s === "no_data") return "无数据";
  if (s === "missing_key") return "缺少Key";
  if (s === "disabled") return "未启用";
  if (s === "running") return "进行中";
  return s || "--";
}

async function loadPlatformConnectivity() {
  const tbody = el("platform-connectivity-body");
  if (!tbody) return;
  try {
    const res = await fetchJson(API + "/platform/connectivity");
    const items = Array.isArray(res.platforms) ? res.platforms : [];
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">暂无数据</td></tr>';
    } else {
      tbody.innerHTML = items.map((item) => `
        <tr>
          <td>${escapeHtml(item.platform || "--")}</td>
          <td>${escapeHtml(platformStatusLabel(item.status))}</td>
          <td>${escapeHtml(item.rows ?? 0)}</td>
          <td>${escapeHtml(item.saved ?? 0)}</td>
          <td>${escapeHtml(item.cost_seconds != null ? Number(item.cost_seconds).toFixed(2) : "--")}</td>
          <td title="${escapeHtml(item.reason || "")}">${escapeHtml(item.reason || "--")}</td>
        </tr>
      `).join("");
    }
    const tsNode = el("platform-connectivity-updated-at");
    if (tsNode) tsNode.textContent = `最近刷新：${new Date().toLocaleString()}`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-bad">${escapeHtml(e.message || "加载失败")}</td></tr>`;
  }
}

async function loadSteamdtCapsuleSummary() {
  const node = el("steamdt-capsule-summary");
  const statsNode = el("steamdt-capsule-stats");
  const listNode = el("steamdt-capsule-list");
  if (!node) return;
  try {
    const res = await fetchJson(API + "/session_capsules/steamdt");
    const summary = res.summary || {};
    const items = Array.isArray(res.items) ? res.items : [];
    const total = parseInt(summary.total || 0, 10) || 0;
    const ready = parseInt(summary.ready || 0, 10) || 0;
    const cooldown = parseInt(summary.cooldown || 0, 10) || 0;
    const leased = parseInt(summary.leased || 0, 10) || 0;
    const retired = parseInt(summary.retired || 0, 10) || 0;
    node.textContent = retired
      ? `可用 ${ready}，冷却 ${cooldown}，占用 ${leased}；已淘汰 ${retired} 个，敏感 Cookie 已清理并隐藏`
      : `可用 ${ready}，冷却 ${cooldown}，占用 ${leased}`;
    if (statsNode) {
      statsNode.innerHTML = [
        steamdtCapsuleStat("可用", ready, "ready"),
        steamdtCapsuleStat("冷却", cooldown, "cooldown"),
        steamdtCapsuleStat("占用", leased, "leased"),
        steamdtCapsuleStat("已淘汰", retired, "retired"),
        steamdtCapsuleStat("总计", total, "total"),
      ].join("");
    }
    if (listNode) {
      if (!items.length) {
        listNode.innerHTML = '<div class="steamdt-capsule-empty">暂无可用 SteamDT 会话。点击“打开浏览器采集”导入新的会话。</div>';
      } else {
        listNode.innerHTML = items.map((item) => {
          const cooldownText = formatCapsuleCooldown(item.cooldown_until);
          const statusText = item.status || "unknown";
          const failCount = Number(item.fail_count || 0);
          const authFails = Number(item.consecutive_auth_failures || 0);
          const streakCount = Number(item.failure_streak_count || 0);
          const streakReason = item.failure_streak_reason || "--";
          const statusClass = statusText === "ready" && cooldownText === "--" ? "ready" : (cooldownText !== "--" ? "cooldown" : statusText);
          const issueText = streakCount ? `${streakReason} x${streakCount}` : (item.last_failure_reason || "运行正常");
          return `
            <div class="steamdt-capsule-card">
              <div class="steamdt-capsule-card-main">
                <div class="steamdt-capsule-title">
                  <span class="steamdt-capsule-id">${escapeHtml(item.capsule_id || "--")}</span>
                  <span class="steamdt-status steamdt-status--${escapeHtml(statusClass)}">${escapeHtml(statusLabel(statusText, cooldownText))}</span>
                </div>
                <div class="steamdt-capsule-meta">
                  <span>设备 ${escapeHtml(shortId(item.device_id || "--"))}</span>
                  <span>${escapeHtml(item.proxy_binding || "direct")}</span>
                  <span>成功 ${escapeHtml(formatCapsuleTime(item.last_ok_at))}</span>
                  <span>失败 ${escapeHtml(`${failCount}${authFails ? ` / 鉴权${authFails}` : ""}`)}</span>
                </div>
                <div class="steamdt-capsule-note">${escapeHtml(issueText)}</div>
              </div>
              <div class="steamdt-capsule-actions">
                ${cooldownText !== "--" ? `<button type="button" class="btn btn-sm btn-secondary steamdt-capsule-clear-btn" data-capsule-id="${escapeHtml(item.capsule_id || "")}">清冷却</button>` : ""}
                <button type="button" class="btn btn-sm btn-danger-outline steamdt-capsule-retire-btn" data-capsule-id="${escapeHtml(item.capsule_id || "")}">淘汰</button>
              </div>
            </div>
          `;
        }).join("");
        bindSteamdtCapsuleActions(listNode);
      }
    }
  } catch (e) {
    node.textContent = "SteamDT 会话池不可用";
    if (statsNode) statsNode.innerHTML = "";
    if (listNode) {
      listNode.innerHTML = '<div class="steamdt-capsule-empty steamdt-capsule-empty--error">加载 SteamDT 会话失败</div>';
    }
  }
}

function steamdtCapsuleStat(label, value, kind) {
  return `<div class="steamdt-capsule-stat steamdt-capsule-stat--${escapeHtml(kind)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function statusLabel(status, cooldownText) {
  if (cooldownText && cooldownText !== "--") return `冷却 ${cooldownText}`;
  if (status === "ready") return "可用";
  if (status === "leased") return "占用";
  return status || "未知";
}

function shortId(value) {
  const text = String(value || "");
  if (text.length <= 14) return text || "--";
  return `${text.slice(0, 6)}...${text.slice(-4)}`;
}

function bindSteamdtCapsuleActions(scope) {
  scope.querySelectorAll(".steamdt-capsule-clear-btn").forEach((btn) => {
    if (btn._bound) return;
    btn._bound = true;
    btn.addEventListener("click", () => mutateSteamdtCapsule(btn.dataset.capsuleId, "clear_cooldown"));
  });
  scope.querySelectorAll(".steamdt-capsule-retire-btn").forEach((btn) => {
    if (btn._bound) return;
    btn._bound = true;
    btn.addEventListener("click", () => mutateSteamdtCapsule(btn.dataset.capsuleId, "retire"));
  });
}

async function mutateSteamdtCapsule(capsuleId, action) {
  if (!capsuleId) return;
  const encoded = encodeURIComponent(capsuleId);
  const url = API + `/session_capsules/steamdt/${encoded}/${action}`;
  const opts = action === "retire"
    ? { method: "POST", body: JSON.stringify({ reason: "manual_retire" }) }
    : { method: "POST" };
  try {
    const res = await fetchJson(url, opts);
    toast(action === "retire" ? "Capsule retired" : "Cooldown cleared", res.msg || "");
    await loadSteamdtCapsuleSummary();
  } catch (e) {
    toast("Action failed", e.message || "Please try again later");
  }
}

function formatCapsuleTime(value) {
  if (!value) return "--";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString();
  } catch {
    return String(value);
  }
}

function formatCapsuleCooldown(unixTs) {
  const raw = Number(unixTs || 0);
  if (!raw) return "--";
  const remain = Math.max(0, Math.round(raw - Date.now() / 1000));
  if (remain <= 0) return "--";
  if (remain < 60) return `${remain}s`;
  if (remain < 3600) return `${Math.ceil(remain / 60)}m`;
  return `${Math.ceil(remain / 3600)}h`;
}

function _formatUuypTestResult(node) {
  const msg = String(node?.msg || "");
  const hint = String(node?.hint || "");
  const text = `${msg} ${hint}`.toLowerCase();
  if (node?.ok) return "UUYP:登录成功";
  if (text.includes("bearer") || text.includes("authorization")) return "UUYP:Bearer 缺失";
  if (text.includes("deviceid") || text.includes("device id")) return "UUYP:deviceId 不匹配";
  return `UUYP:FAIL${msg ? `(${msg})` : ""}`;
}

async function testCredentials() {
  try {
    const payload = {
      buff: { cookies: el("cfg-buff-cookie") ? el("cfg-buff-cookie").value.trim() : "" },
      uuyp: { cookies: el("cfg-uuyp-cookie") ? el("cfg-uuyp-cookie").value.trim() : "" },
      eco: { cookies: el("cfg-eco-cookie") ? el("cfg-eco-cookie").value.trim() : "" },
    };
    const res = await fetchJson(API + "/credentials/test", { method: "POST", body: JSON.stringify(payload) });
    const result = res.result || {};
    const summary = [
      `BUFF:${result.buff?.ok ? "OK" : "FAIL"}`,
      _formatUuypTestResult(result.uuyp),
      `ECO:${result.eco?.ok ? "OK" : "FAIL"}`,
    ].join(" | ");
    const anyFail = !result.buff?.ok || !result.uuyp?.ok || !result.eco?.ok;
    toast(anyFail ? "测试完成（存在失败项）" : "测试成功", summary);
  } catch (e) {
    toast("测试失败", e.message || "请检查 Cookie 值");
  }
}

async function startCredentialLogin(platform) {
  try {
    credentialLoginPlatform = platform;
    const title = el("credential-login-title");
    const message = el("credential-login-message");
    const modal = el("credential-login-modal");
    if (title) title.textContent = platform === "steamdt" ? "SteamDT 会话采集" : `${platform.toUpperCase()} 浏览器登录`;
    if (message) {
      message.textContent = platform === "steamdt"
        ? "浏览器已打开，请在 SteamDT 页面完成自然访问或登录，然后点击下方按钮采集并写入会话池。"
        : "浏览器已打开，请在弹出的窗口中完成登录，登录成功后点击下方按钮提取凭证。";
    }
    if (modal) modal.classList.remove("hidden");
    await fetchJson(API + `/auth/relogin_start/${encodeURIComponent(platform)}`, { method: "POST" });
    toast("浏览器已打开", platform === "steamdt" ? "完成 SteamDT 页面访问后返回采集" : `请在 ${platform.toUpperCase()} 窗口完成登录`);
  } catch (e) {
    toast("打开失败", e.message || "浏览器环境不可用");
  }
}

async function finishCredentialLogin() {
  if (!credentialLoginPlatform) return;
  try {
    const res = await fetchJson(API + `/auth/relogin_finish/${encodeURIComponent(credentialLoginPlatform)}`, { method: "POST" });
    toast("提取成功", res.msg || "会话已保存");
    await loadCredentials();
    if (credentialLoginPlatform === "steamdt") {
      await loadSteamdtCapsuleSummary();
    }
    const modal = el("credential-login-modal");
    if (modal) modal.classList.add("hidden");
    credentialLoginPlatform = null;
  } catch (e) {
    toast("提取失败", e.message || "请先完成登录再提取");
  }
}

async function cancelCredentialLogin() {
  const modal = el("credential-login-modal");
  if (modal) modal.classList.add("hidden");
  credentialLoginPlatform = null;
}

async function saveConfigFromForm() {
  const d = await fetchJson(API + "/config");
  const merged = deepMerge(d.config || {}, formToConfig());
  await fetchJson(API + "/config", { method: "POST", body: JSON.stringify({ config: merged }) });
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
    await fetchJson(API + "/confirm_payment", { method: "POST", body: JSON.stringify({ ok }) });
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

async function syncItems() {
  const btn = el("btn-sync-items");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  toast("正在同步饰品数据...", "这可能需要十几秒，请勿刷新页面");
  try {
    const res = await fetchJson(API + "/system/sync_items", { method: "POST" });
    if (!res.success) throw new Error(res.msg || "同步失败");
    toast("同步成功", "饰品字典已更新至最新版");
  } catch (e) {
    toast("同步失败", e.message || "请稍后再试");
  } finally {
    btn.disabled = false;
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
  if (!inventoryRefreshSeconds || inventoryRefreshSeconds <= 0) return;
  refreshMarketPrices();
  inventoryTimer = setInterval(() => {
    refreshMarketPrices();
  }, inventoryRefreshSeconds * 1000);
}

// ---- 配置完整性检查 & 新手引导向导 ----
const WIZARD_SKIP_KEY = "aetherswap_onboard_skip";

function _wizardIsFirstTime(cfg, accounts, buffNoCookie) {
  const sg = cfg.steam_guard || {};
  const sc = cfg.steam_confirm || {};
  const n = cfg.notify || {};
  const noConfig = !sg.shared_secret && !sc.identity_secret && !n.pushplus_token;
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
  const sg = cfg.steam_guard || {};
  const sc = cfg.steam_confirm || {};
  const n = cfg.notify || {};
  const configDone = sg.shared_secret && sc.identity_secret;
  const accountDone = accounts.length > 0;
  const onlyBuffMissing = buffNoCookie && configDone && accountDone;
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
    if (step === 3) {
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
          if (statusEl) statusEl.textContent = "✅ 浏览器已打开，请在其中完成 Buff 登录后点击「已完成登录」。";
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
          // 自动推进到下一步
          setTimeout(() => goToStep(currentStep + 1), 800);
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
        const ss = (el("wiz-shared-secret")?.value || "").trim();
        const is = (el("wiz-identity-secret")?.value || "").trim();
        if (!ss && !is) return;
        const sg = { ...(cfg.steam_guard || {}), ...(ss ? { shared_secret: ss } : {}) };
        const sc = { ...(cfg.steam_confirm || {}), ...(is ? { identity_secret: is } : {}) };
        await fetchJson(API + "/config", {
          method: "POST",
          body: JSON.stringify({ config: { ...cfg, steam_guard: sg, steam_confirm: sc } }),
        });
        const gSteamSecret = el("cfg-steam-shared-secret");
        if (gSteamSecret && ss) gSteamSecret.value = ss;
        const gIdentSec = el("cfg-steam-identity-secret");
        if (gIdentSec && is) gIdentSec.value = is;
      } else if (currentStep === 2) {
        const tok = (el("wiz-pushplus-token")?.value || "").trim();
        if (!tok) return;
        const notify = { ...(cfg.notify || {}), pushplus_token: tok };
        await fetchJson(API + "/config", {
          method: "POST",
          body: JSON.stringify({ config: { ...cfg, notify } }),
        });
        const gPush = el("cfg-pushplus_token");
        if (gPush) gPush.value = tok;
      }
      // step 3 (Buff) is handled by its own buttons; step 4 is info-only
      try { updateUXStatus(((await fetchJson(API + "/config")).config || {})); } catch { }
    } catch (e) {
      toast("保存失败", e.message || "请稍后手动在设置页填写");
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
    if (currentStep === 3) {
      // Buff 步骤：「下一步」仅在未启动 relogin 时可直接跳过
      goToStep(4);
    } else if (currentStep < TOTAL_STEPS) {
      await saveCurrentStep();
      goToStep(currentStep + 1);
    } else {
      closeWizard("accounts");
    }
  };

  btnSkip.onclick = () => {
    if (currentStep === 0 || currentStep === TOTAL_STEPS) {
      closeWizard(null);
    } else if (currentStep === 3 && _buffReloginStarted) {
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

  // 如果只是 buff cookie 缺失，从步骤 3 开始
  goToStep(startAtBuffStep ? 3 : 0);
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
  const sg = cfg.steam_guard || {};
  const sc = cfg.steam_confirm || {};
  const n = cfg.notify || {};
  const configOk = sg.shared_secret && sc.identity_secret && n.pushplus_token;
  const accountOk = accounts.length > 0;

  const badgeSettings = el("nav-badge-settings");
  if (badgeSettings) badgeSettings.classList.toggle("hidden", !!configOk);

  const badgeAccounts = el("nav-badge-accounts");
  if (badgeAccounts) badgeAccounts.classList.toggle("hidden", accountOk);
}

function renderGettingStartedCard(cfg, accounts) {
  const card = el("getting-started-card");
  if (!card) return;

  // 已被用户手动关闭时不再显示
  if (localStorage.getItem("aetherswap_gs_card_closed") === "1") {
    card.style.display = "none";
    return;
  }

  const sg = cfg.steam_guard || {};
  const sc = cfg.steam_confirm || {};
  const n = cfg.notify || {};

  const steps = [
    {
      done: !!sg.shared_secret && !!sc.identity_secret,
      label: "填写 Steam 令牌密钥（<span class='gs-link' onclick='document.querySelector(\"[data-tab=settings]\").click()'>系统设置 → Steam 令牌</span>）",
    },
    {
      done: !!n.pushplus_token,
      label: "填写 PushPlus 推送 Token（<span class='gs-link' onclick='document.querySelector(\"[data-tab=settings]\").click()'>系统设置 → 推送与邮箱</span>）",
    },
    {
      done: accounts.length > 0,
      label: "添加 Steam 账号并登录（<span class='gs-link' onclick='document.querySelector(\"[data-tab=accounts]\").click()'>账号管理</span>）",
    },
    {
      done: accounts.length > 0 && !!sg.shared_secret && !!sc.identity_secret && !!n.pushplus_token,
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

  const saveCredBtn = el("btn-save-credentials");
  if (saveCredBtn && !saveCredBtn._bound) {
    saveCredBtn._bound = true;
    saveCredBtn.addEventListener("click", saveCredentials);
  }

  const syncItemsBtn = el("btn-sync-items");
  if (syncItemsBtn && !syncItemsBtn._bound) {
    syncItemsBtn._bound = true;
    syncItemsBtn.addEventListener("click", syncItems);
  }

  const toggleCredBtn = el("btn-toggle-credentials");
  if (toggleCredBtn && !toggleCredBtn._bound) {
    toggleCredBtn._bound = true;
    toggleCredBtn.addEventListener("click", () => setCredentialsVisible(!credentialsVisible));
  }

  const testCredBtn = el("btn-test-credentials");
  if (testCredBtn && !testCredBtn._bound) {
    testCredBtn._bound = true;
    testCredBtn.addEventListener("click", testCredentials);
  }

  const refreshConnectivityBtn = el("btn-refresh-platform-connectivity");
  if (refreshConnectivityBtn && !refreshConnectivityBtn._bound) {
    refreshConnectivityBtn._bound = true;
    refreshConnectivityBtn.addEventListener("click", loadPlatformConnectivity);
  }

  document.querySelectorAll("[data-login-platform]").forEach((btn) => {
    if (btn._bound) return;
    btn._bound = true;
    btn.addEventListener("click", () => startCredentialLogin(btn.dataset.loginPlatform));
  });

  const finishBtn = el("btn-credential-login-finish");
  if (finishBtn && !finishBtn._bound) {
    finishBtn._bound = true;
    finishBtn.addEventListener("click", finishCredentialLogin);
  }

  const cancelBtn = el("btn-credential-login-cancel");
  if (cancelBtn && !cancelBtn._bound) {
    cancelBtn._bound = true;
    cancelBtn.addEventListener("click", cancelCredentialLogin);
  }
}
