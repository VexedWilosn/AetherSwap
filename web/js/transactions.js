
let holdingsMultiSelectMode = false;
let historyMultiSelectMode = false;
const TX_HOLDINGS_COLUMNS_KEY = "aetherswap_holdings_show_extra_cols";
const TX_HISTORY_COLUMNS_KEY = "aetherswap_history_show_extra_cols";
const TX_PRICE_REFRESH_RECORD_KEY = "aetherswap_price_refresh_record";
const TX_HOLDINGS_SORT_KEY = "aetherswap_holdings_sort";
const TX_HOLDINGS_COLUMN_ORDER_KEY = "aetherswap_holdings_column_order";
const TX_HOLDINGS_EXTRA_COLUMNS_KEY = "aetherswap_holdings_extra_columns";
const TX_TRADE_COOLDOWN_DAYS = 7;
const TX_HOLDINGS_FIXED_COLUMNS = ["select"];
const TX_HOLDINGS_COLUMN_ORDER = [
  "time",
  "name",
  "account",
  "unlock",
  "automation",
  "assetid",
  "buy_price",
  "buy_market",
  "current_market",
  "after_tax",
  "discount",
  "cash_profit",
  "self_use",
  "market_change",
  "actions",
];
const TX_HOLDINGS_DEFAULT_EXTRA_COLUMNS = ["assetid", "buy_market", "after_tax", "self_use", "market_change"];
const TX_HOLDINGS_COLUMN_LABELS = {
  time: "时间",
  name: "物品/说明",
  account: "账号",
  unlock: "解禁时间",
  automation: "自动化",
  assetid: "assetid",
  buy_price: "购入价",
  buy_market: "购入市场价",
  current_market: "现市场价",
  after_tax: "税后价格",
  discount: "实际折扣比率",
  cash_profit: "变现收益",
  self_use: "自用收益",
  market_change: "市场变动",
  actions: "操作",
};
let txAccountsCapabilityCache = null;
let txAccountsCapabilityAt = 0;
let holdingsColumnSortMode = false;
let holdingsColumnDragPlaceholder = null;
let txHoldingsSort = readHoldingsSortPreference();
let txHoldingsColumnOrder = readHoldingsColumnOrderPreference();
let txHoldingsExtraColumns = readHoldingsExtraColumnsPreference();
let txHoldingsFilters = { search: "", status: "all", price: "all" };
function readTxColumnPreference(key, defaultValue = false) {
  try {
    const raw = localStorage.getItem(key);
    if (raw == null) return !!defaultValue;
    return raw === "1";
  } catch {
    return !!defaultValue;
  }
}
function writeTxColumnPreference(key, value) {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
  }
}
function readHoldingsSortPreference() {
  try {
    const raw = localStorage.getItem(TX_HOLDINGS_SORT_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    const by = TX_HOLDINGS_COLUMN_ORDER.includes(parsed.by) ? parsed.by : "time";
    const dir = parsed.dir === "asc" ? "asc" : "desc";
    return { by, dir };
  } catch {
    return { by: "time", dir: "desc" };
  }
}
function writeHoldingsSortPreference(value) {
  try {
    localStorage.setItem(TX_HOLDINGS_SORT_KEY, JSON.stringify(value));
  } catch {
  }
}
function readHoldingsColumnOrderPreference() {
  try {
    const raw = localStorage.getItem(TX_HOLDINGS_COLUMN_ORDER_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return TX_HOLDINGS_COLUMN_ORDER.slice();
    const known = parsed.filter((col) => TX_HOLDINGS_COLUMN_ORDER.includes(col));
    return [...known, ...TX_HOLDINGS_COLUMN_ORDER.filter((col) => !known.includes(col))];
  } catch {
    return TX_HOLDINGS_COLUMN_ORDER.slice();
  }
}
function writeHoldingsColumnOrderPreference(order) {
  txHoldingsColumnOrder = order.filter((col) => TX_HOLDINGS_COLUMN_ORDER.includes(col));
  try {
    localStorage.setItem(TX_HOLDINGS_COLUMN_ORDER_KEY, JSON.stringify(txHoldingsColumnOrder));
  } catch {
  }
}
function readHoldingsExtraColumnsPreference() {
  try {
    const raw = localStorage.getItem(TX_HOLDINGS_EXTRA_COLUMNS_KEY);
    const parsed = raw ? JSON.parse(raw) : TX_HOLDINGS_DEFAULT_EXTRA_COLUMNS;
    if (!Array.isArray(parsed)) return TX_HOLDINGS_DEFAULT_EXTRA_COLUMNS.slice();
    return parsed.filter((col) => TX_HOLDINGS_COLUMN_ORDER.includes(col) && !TX_HOLDINGS_FIXED_COLUMNS.includes(col));
  } catch {
    return TX_HOLDINGS_DEFAULT_EXTRA_COLUMNS.slice();
  }
}
function writeHoldingsExtraColumnsPreference(cols) {
  txHoldingsExtraColumns = cols.filter((col) => TX_HOLDINGS_COLUMN_ORDER.includes(col) && !TX_HOLDINGS_FIXED_COLUMNS.includes(col));
  try {
    localStorage.setItem(TX_HOLDINGS_EXTRA_COLUMNS_KEY, JSON.stringify(txHoldingsExtraColumns));
  } catch {
  }
}
function resetHoldingsColumnOrderPreference() {
  txHoldingsColumnOrder = TX_HOLDINGS_COLUMN_ORDER.slice();
  txHoldingsExtraColumns = TX_HOLDINGS_DEFAULT_EXTRA_COLUMNS.slice();
  try {
    localStorage.removeItem(TX_HOLDINGS_COLUMN_ORDER_KEY);
    localStorage.removeItem(TX_HOLDINGS_EXTRA_COLUMNS_KEY);
  } catch {
  }
  applyHoldingsColumnOrder();
  renderHoldingsColumnSortBar();
}
function readMarketPriceRefreshRecord() {
  try {
    const raw = localStorage.getItem(TX_PRICE_REFRESH_RECORD_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}
function writeMarketPriceRefreshRecord(record) {
  try {
    localStorage.setItem(TX_PRICE_REFRESH_RECORD_KEY, JSON.stringify(record));
  } catch {
  }
}
let holdingsShowMoreColumns = readTxColumnPreference(TX_HOLDINGS_COLUMNS_KEY);
let historyShowMoreColumns = readTxColumnPreference(TX_HISTORY_COLUMNS_KEY, true);
let lastEnrichTime = 0;
let lastEnrichData = null;
let lastTransactionsResellRatio = 0.85;
let lastMarketPriceHintKey = "";
let lastMarketPriceMeta = null;
let smartPriceRetrying = false;
let marketCircuitClearing = false;
let marketCircuitDeadlineMs = 0;
let marketCircuitTimer = null;
let lastMarketPriceRefreshRecord = readMarketPriceRefreshRecord();
function txTableStateRow(colspan, title, detail = "", state = "empty") {
  const spinner = state === "loading" ? '<span class="tx-table-state-spinner" aria-hidden="true"></span>' : "";
  const detailHtml = detail ? `<span class="tx-table-state-detail">${escapeHtml(detail)}</span>` : "";
  return `<tr class="tx-table-state-row is-${escapeHtml(state)}"><td colspan="${colspan}"><div class="tx-table-state">${spinner}<span class="tx-table-state-title">${escapeHtml(title)}</span>${detailHtml}</div></td></tr>`;
}
function tbodyHasOnlyState(tbody) {
  return !!tbody && tbody.children.length === 1 && tbody.firstElementChild?.classList.contains("tx-table-state-row");
}
function setTxTableLoading(tbody, colspan, title, detail = "") {
  if (!tbody) return;
  if (tbody.children.length === 0 || tbodyHasOnlyState(tbody)) {
    tbody.innerHTML = txTableStateRow(colspan, title, detail, "loading");
  }
}
function setTxTableError(tbody, colspan, title, detail = "") {
  if (!tbody) return;
  if (tbody.children.length === 0 || tbodyHasOnlyState(tbody)) {
    tbody.innerHTML = txTableStateRow(colspan, title, detail, "error");
  }
}
function formatDateTime(tsSeconds) {
  if (!tsSeconds) return "—";
  const d = new Date(tsSeconds * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function buildItemNameHtml(nameText) {
  const safeName = (nameText || "—").toString();
  const iconPath = typeof getIconForName === "function" ? getIconForName(safeName) : "";
  const nameIconHtml = iconPath && typeof getIconUrl === "function"
    ? `<img class="item-icon" src="${getIconUrl(iconPath)}" alt="" loading="lazy" onerror="this.style.display='none'" />`
    : "";
  return `<span class="item-name-cell">${nameIconHtml}<span>${escapeHtml(safeName)}</span></span>`;
}
function normalizeAccountLabel(acc) {
  if (!acc) return "当前账号";
  return acc.display_name || acc.username || acc.steam_id || "当前账号";
}
async function refreshTxAccountCapability() {
  const now = Date.now();
  if (txAccountsCapabilityCache && now - txAccountsCapabilityAt < 30000) return txAccountsCapabilityCache;
  try {
    const d = await fetchJson(API + "/accounts");
    const accounts = d.accounts || [];
    const current = accounts.find((a) => a.id === d.current_id) || accounts[0] || null;
    txAccountsCapabilityCache = { accounts, current, currentId: d.current_id || null };
    txAccountsCapabilityAt = now;
    if (typeof accountsCache !== "undefined") accountsCache = accounts;
    if (typeof accountsCurrentId !== "undefined") accountsCurrentId = d.current_id || null;
  } catch {
    const fallbackAccounts = typeof accountsCache !== "undefined" ? (accountsCache || []) : [];
    const fallbackId = typeof accountsCurrentId !== "undefined" ? accountsCurrentId : null;
    txAccountsCapabilityCache = {
      accounts: fallbackAccounts,
      current: fallbackAccounts.find((a) => a.id === fallbackId) || fallbackAccounts[0] || null,
      currentId: fallbackId,
    };
  }
  return txAccountsCapabilityCache;
}
function getTxAccountCapability() {
  if (txAccountsCapabilityCache) return txAccountsCapabilityCache;
  const fallbackAccounts = typeof accountsCache !== "undefined" ? (accountsCache || []) : [];
  const fallbackId = typeof accountsCurrentId !== "undefined" ? accountsCurrentId : null;
  return {
    accounts: fallbackAccounts,
    current: fallbackAccounts.find((a) => a.id === fallbackId) || fallbackAccounts[0] || null,
    currentId: fallbackId,
  };
}
function getUnlockState(t) {
  if (!t.at) return { unlockTs: null, locked: false, label: "—", detail: "未知购入时间" };
  const unlockTs = Number(t.at) + TX_TRADE_COOLDOWN_DAYS * 24 * 60 * 60;
  const remainingMs = unlockTs * 1000 - Date.now();
  if (remainingMs <= 0) return { unlockTs, locked: false, label: "已解禁", detail: formatDateTime(unlockTs) };
  const remainingHours = Math.ceil(remainingMs / 3600000);
  const remainingDays = Math.ceil(remainingHours / 24);
  const label = remainingDays > 1 ? `${remainingDays}天后` : `${remainingHours}小时后`;
  return { unlockTs, locked: true, label, detail: formatDateTime(unlockTs) };
}
function getHoldingFilterStatus(t) {
  if (t.pending_receipt) return "pending";
  if (String(t.listing_status || "").toLowerCase() === "error") return "error";
  if (t.listing) return "listing";
  return getUnlockState(t).locked ? "locked" : "ready";
}
function readHoldingsFiltersFromUI() {
  txHoldingsFilters = {
    search: (el("holdings-filter-search")?.value || "").trim().toLowerCase(),
    status: el("holdings-filter-status")?.value || "all",
    price: el("holdings-filter-price")?.value || "all",
  };
  const sortBy = el("holdings-sort-by")?.value || txHoldingsSort.by;
  const sortDir = el("holdings-sort-dir")?.value || txHoldingsSort.dir;
  txHoldingsSort = {
    by: TX_HOLDINGS_COLUMN_ORDER.includes(sortBy) ? sortBy : "time",
    dir: sortDir === "asc" ? "asc" : "desc",
  };
  writeHoldingsSortPreference(txHoldingsSort);
  return txHoldingsFilters;
}
function filterHoldingsList(holdings) {
  const filters = txHoldingsFilters || {};
  return holdings.filter((t) => {
    const search = filters.search || "";
    if (search) {
      const hay = [t.name, t.assetid, t.goods_id].filter((v) => v != null && v !== "").join(" ").toLowerCase();
      if (!hay.includes(search)) return false;
    }
    if (filters.status && filters.status !== "all" && getHoldingFilterStatus(t) !== filters.status) return false;
    if (filters.price && filters.price !== "all") {
      const source = t.current_market_price_source || "";
      if (filters.price === "smart" && source !== "smart") return false;
      if (filters.price === "fallback" && source !== "steam_lowest") return false;
      if (filters.price === "missing" && t.current_market_price != null) return false;
    }
    return true;
  });
}
function getHoldingSortValue(t, key, resellRatio = 0.85) {
  const cost = Number(t.price) || 0;
  const cur = t.current_market_price != null ? Number(t.current_market_price) : null;
  const buyMarket = t.market_price != null ? Number(t.market_price) : null;
  const afterTax = cur != null && cur > 0 ? cur / 1.15 : null;
  const ratio = Math.max(0.01, Math.min(1, Number(resellRatio) || 0.85));
  if (key === "time") return Number(t.at) || 0;
  if (key === "name") return (t.name || "").toString().toLowerCase();
  if (key === "account") return normalizeAccountLabel(getTxAccountCapability().current).toLowerCase();
  if (key === "unlock") return getUnlockState(t).unlockTs || 0;
  if (key === "automation") return getAutomationState(t, getTxAccountCapability()).key || "";
  if (key === "assetid") return t.assetid || "";
  if (key === "buy_price") return cost;
  if (key === "buy_market") return buyMarket;
  if (key === "current_market") return cur;
  if (key === "after_tax") return afterTax;
  if (key === "discount") return afterTax != null && afterTax > 0 && cost > 0 ? cost / afterTax : null;
  if (key === "cash_profit") return afterTax != null && cost > 0 ? afterTax * ratio - cost : null;
  if (key === "self_use") return afterTax != null && cost > 0 ? afterTax - cost : null;
  if (key === "market_change") return cur != null && buyMarket != null ? cur - buyMarket : null;
  return Number(t.at) || 0;
}
function sortHoldingsList(list, resellRatio = 0.85) {
  const sort = txHoldingsSort || { by: "time", dir: "desc" };
  const dir = sort.dir === "asc" ? 1 : -1;
  return list.slice().sort((a, b) => {
    const av = getHoldingSortValue(a, sort.by, resellRatio);
    const bv = getHoldingSortValue(b, sort.by, resellRatio);
    const aMissing = av == null || av === "";
    const bMissing = bv == null || bv === "";
    if (aMissing && bMissing) return (Number(b.at) || 0) - (Number(a.at) || 0);
    if (aMissing) return 1;
    if (bMissing) return -1;
    if (typeof av === "string" || typeof bv === "string") {
      const cmp = String(av).localeCompare(String(bv), "zh-Hans-CN", { numeric: true });
      return cmp === 0 ? (Number(b.at) || 0) - (Number(a.at) || 0) : cmp * dir;
    }
    const cmp = Number(av) - Number(bv);
    return cmp === 0 ? (Number(b.at) || 0) - (Number(a.at) || 0) : cmp * dir;
  });
}
function syncHoldingsSortControls() {
  const sortBy = el("holdings-sort-by");
  const sortDir = el("holdings-sort-dir");
  if (sortBy) sortBy.value = txHoldingsSort.by || "time";
  if (sortDir) sortDir.value = txHoldingsSort.dir || "desc";
}
function refreshHoldingsFilterUI(total, filtered) {
  const countEl = el("holdings-filter-count");
  if (countEl) countEl.textContent = total === filtered ? `${total} 项` : `${filtered} / ${total} 项`;
}
function rerenderTransactionsFromCache() {
  if (!Array.isArray(lastEnrichData)) return;
  readHoldingsFiltersFromUI();
  applyTransactionsToUI(
    lastEnrichData,
    el("purchases-summary"),
    document.querySelector("#transactions-table-purchases tbody"),
    document.querySelector("#transactions-table-purchase-history tbody"),
    lastTransactionsResellRatio
  );
}
function getAutomationState(t, accountCapability) {
  const account = accountCapability?.current || null;
  const guardStatus = account?.steam_guard_status || {};
  const hasConfirmToken = !!guardStatus.identity_configured;
  const unlock = getUnlockState(t);
  if (t.pending_receipt) return { key: "pending", label: "待收货", hint: "收货后进入交易冷却", className: "is-pending", actionable: false, unlock };
  if (t.listing_status === "error") return { key: "error", label: "上架异常", hint: "需要检查 Steam 在售状态", className: "is-error", actionable: false, unlock };
  if (t.listing) return { key: "listing", label: "出售中", hint: "已在 Steam 市场挂售", className: "is-listing", actionable: true, unlock };
  if (unlock.locked) return { key: "cooldown", label: "冷却中", hint: `${unlock.detail} 可交易`, className: "is-cooldown", actionable: false, unlock };
  if (hasConfirmToken) return { key: "auto", label: "可自动上架", hint: "当前账号令牌可自动确认", className: "is-auto", actionable: true, unlock };
  return { key: "manual", label: "需人工确认", hint: "配置身份令牌后可自动确认上架", className: "is-manual", actionable: true, unlock };
}
function renderAutomationBadge(state) {
  return `<span class="tx-state-pill ${state.className}" title="${escapeHtml(state.hint)}">${escapeHtml(state.label)}</span>`;
}
function renderTradeAction(t, state, type, idx, multiSelectMode) {
  if (multiSelectMode) {
    return `<button type="button" class="btn btn-sm btn-edit tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button>`;
  }
  if (state.key === "cooldown" || state.key === "pending") {
    return `<button type="button" class="btn btn-sm btn-secondary tx-btn-disabled" disabled title="${escapeHtml(state.hint)}">${escapeHtml(state.key === "cooldown" ? state.unlock.label + "可售" : "待收货")}</button><div class="tx-actions-dropdown"><button type="button" class="tx-actions-trigger" title="更多">⋮</button><div class="tx-actions-menu"><button type="button" class="tx-action-item tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button><button type="button" class="tx-action-item tx-action-danger tx-btn-del" data-type="${escapeHtml(type)}" data-idx="${idx}">删除</button></div></div>`;
  }
  if (state.key === "listing") {
    return `<div class="tx-actions-dropdown"><button type="button" class="tx-actions-trigger" title="操作">⋮</button><div class="tx-actions-menu"><button type="button" class="tx-action-item ph-btn-delist" data-type="purchase" data-idx="${idx}">下架</button><button type="button" class="tx-action-item tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button><button type="button" class="tx-action-item tx-action-danger tx-btn-del" data-type="${escapeHtml(type)}" data-idx="${idx}">删除</button></div></div>`;
  }
  const primaryClass = state.key === "auto" ? "tx-btn-auto-list" : "tx-btn-sell";
  const primaryLabel = state.key === "auto" ? "自动上架" : "记录售出";
  return `<button type="button" class="btn btn-sm btn-primary ${primaryClass}" data-type="${escapeHtml(type)}" data-idx="${idx}">${primaryLabel}</button><div class="tx-actions-dropdown"><button type="button" class="tx-actions-trigger" title="更多">⋮</button><div class="tx-actions-menu"><button type="button" class="tx-action-item tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button><button type="button" class="tx-action-item tx-action-danger tx-btn-del" data-type="${escapeHtml(type)}" data-idx="${idx}">删除</button></div></div>`;
}
function getSelectedCount(selector) {
  return document.querySelectorAll(selector + ":checked").length;
}
function bindSelectionCount(selector, countId) {
  const update = () => {
    const countEl = el(countId);
    if (countEl) countEl.textContent = String(getSelectedCount(selector));
  };
  document.querySelectorAll(selector).forEach((cb) => cb.addEventListener("change", update));
  update();
}
function formatPriceRefreshTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  const now = new Date();
  const hhmmss = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
  if (d.toDateString() === now.toDateString()) return hhmmss;
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hhmmss}`;
}
function summarizeMarketPriceSources(holdings) {
  const list = Array.isArray(holdings) ? holdings : [];
  return {
    smart: list.filter((t) => t.current_market_price != null && t.current_market_price_source === "smart").length,
    fallback: list.filter((t) => t.current_market_price != null && t.current_market_price_source === "steam_lowest").length,
    missing: list.filter((t) => t.current_market_price == null).length,
    total: list.length,
  };
}
function getMarketPriceIssueCounts(holdings) {
  const counts = summarizeMarketPriceSources(holdings);
  return {
    ...counts,
    hasIssue: counts.missing > 0 || counts.fallback > 0,
  };
}
function getLiveMarketCircuit(circuit = {}) {
  const live = { ...(circuit || {}) };
  if (marketCircuitDeadlineMs > 0) {
    const remaining = Math.max(0, Math.ceil((marketCircuitDeadlineMs - Date.now()) / 1000));
    live.remaining_seconds = remaining;
    live.open = remaining > 0;
  }
  return live;
}
function getCurrentMarketCircuit() {
  return getLiveMarketCircuit(lastMarketPriceMeta?.circuit || {});
}
function formatMarketCircuitRemaining(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  const mins = Math.floor(total / 60);
  const secs = Math.floor(total % 60);
  if (mins <= 0) return `${secs}秒`;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}
function stopMarketCircuitTimer() {
  if (marketCircuitTimer) {
    clearInterval(marketCircuitTimer);
    marketCircuitTimer = null;
  }
}
function syncMarketCircuitTimer(meta) {
  const circuit = meta?.circuit || {};
  const remaining = Number(circuit.remaining_seconds || 0);
  if (!circuit.open || remaining <= 0) {
    marketCircuitDeadlineMs = 0;
    stopMarketCircuitTimer();
    return;
  }
  marketCircuitDeadlineMs = Date.now() + remaining * 1000;
  if (marketCircuitTimer) return;
  marketCircuitTimer = setInterval(() => {
    if (!lastMarketPriceMeta || marketCircuitDeadlineMs <= 0) {
      stopMarketCircuitTimer();
      return;
    }
    const holdings = Array.isArray(lastEnrichData)
      ? lastEnrichData.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0))
      : [];
    updateMarketPriceNotice(lastMarketPriceMeta, holdings);
    if (Date.now() >= marketCircuitDeadlineMs) stopMarketCircuitTimer();
  }, 1000);
}
function buildMarketPriceIssueDetail(counts, meta = null) {
  const parts = [];
  const missing = Number(counts?.missing || 0);
  const fallback = Number(counts?.fallback || 0);
  const smart = Number(counts?.smart || 0);
  const total = Number(counts?.total || 0);
  const allMissing = missing > 0 && smart === 0 && fallback === 0 && total > 0;
  if (missing > 0 && smart === 0 && fallback === 0 && total > 0) {
    parts.push("智能价和最低/中位价摘要都没有取到");
  } else if (missing > 0) {
    parts.push(`${missing} 项暂无现市场价`);
  }
  if (fallback > 0) parts.push(`${fallback} 项使用最低/中位价摘要`);
  const circuit = getLiveMarketCircuit(meta?.circuit || {});
  if (circuit.open) {
    parts.push(`Steam 智能价熔断剩余 ${formatMarketCircuitRemaining(circuit.remaining_seconds)}`);
    parts.push("重试已暂停，当前仅能使用最低价/中位价摘要");
    parts.push("确认代理/加速器恢复后可在更多里解除熔断");
  }
  if (meta?.proxy_enabled === false && Number(meta?.configured_proxy_count || 0) > 0) {
    parts.push("已配置代理但代理池未启用");
  }
  const warning = meta?.warning ? String(meta.warning) : "";
  const isCircuitWarning = warning.includes("熔断");
  if (warning && !isCircuitWarning && !(allMissing && warning.includes("现市场价未获取到"))) {
    parts.push(meta.warning);
  }
  return parts.join("；") || "现市场价刷新未完成";
}
function syncMarketPriceRefreshControls() {
  const btn = el("btn-refresh-market-price");
  const circuit = getCurrentMarketCircuit();
  if (btn) {
    btn.disabled = smartPriceRetrying || !!circuit.open;
    if (smartPriceRetrying) btn.textContent = "刷新中...";
    else if (circuit.open) btn.textContent = `熔断中 ${formatMarketCircuitRemaining(circuit.remaining_seconds)}`;
    else btn.textContent = "刷新价格";
    btn.title = circuit.open
      ? "Steam 智能价熔断保护中，暂不重试；确认代理/加速器恢复后可在提示栏更多操作中解除熔断。"
      : "";
  }
  const recordEl = el("market-price-refresh-record");
  if (!recordEl) return;
  const record = lastMarketPriceRefreshRecord;
  if (!record) {
    recordEl.textContent = "未刷新";
    recordEl.title = "尚未刷新实时现市场价";
    return;
  }
  const time = formatPriceRefreshTime(record.at);
  // Simplified display text — details go into tooltip
  let label;
  if (record.error) {
    label = `${time} 刷新失败`;
  } else if (record.missing > 0) {
    label = record.missing >= record.total
      ? `${time} · 价格获取失败`
      : `${time} · ${record.missing} 项暂无价格`;
  } else if (record.fallback > 0) {
    label = `${time} · 已刷新（${record.fallback} 项为参考价）`;
  } else {
    label = `${time} 已刷新`;
  }
  recordEl.textContent = label;
  recordEl.title = `${record.mode || "刷新"}：${record.error || record.warning || "刷新完成"}\n总数 ${record.total}，精准价 ${record.smart}，参考价 ${record.fallback}，缺失 ${record.missing}`;
}
function recordMarketPriceRefresh(holdings, meta, mode, error) {
  const counts = summarizeMarketPriceSources(holdings);
  lastMarketPriceRefreshRecord = {
    at: Date.now(),
    mode,
    error: error || "",
    warning: error || buildMarketPriceIssueDetail(counts, meta),
    proxy_enabled: meta?.proxy_enabled,
    configured_proxy_count: meta?.configured_proxy_count,
    circuit: meta?.circuit || null,
    ...counts,
  };
  writeMarketPriceRefreshRecord(lastMarketPriceRefreshRecord);
  syncMarketPriceRefreshControls();
}
function handleMarketPriceMeta(meta) {
  lastMarketPriceMeta = meta || null;
  syncMarketCircuitTimer(lastMarketPriceMeta);
  const holdings = Array.isArray(lastEnrichData)
    ? lastEnrichData.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0))
    : [];
  updateMarketPriceNotice(lastMarketPriceMeta, holdings);
  if (!meta || !meta.warning) return;
  const circuit = meta.circuit || {};
  const key = `${meta.warning}|${circuit.open ? circuit.remaining_seconds : 0}`;
  if (key === lastMarketPriceHintKey) return;
  lastMarketPriceHintKey = key;
}
function marketPriceSourceLabel(t) {
  const source = t.current_market_price_source;
  if (source === "steam_lowest") return "最低价/中位价摘要";
  if (source === "smart") return "智能价";
  return t.current_market_price_source_label || "";
}
function updateMarketPriceNotice(meta, holdings) {
  const notice = el("market-price-notice");
  if (!notice) return;
  const titleEl = el("market-price-notice-title");
  const detailEl = el("market-price-notice-detail");
  const retryBtn = el("btn-retry-smart-price");
  const clearBtn = el("btn-clear-market-circuit");
  const moreWrap = el("market-price-notice-more");
  const moreTrigger = el("btn-market-price-notice-more");
  const list = Array.isArray(holdings) ? holdings : [];
  const fallbackCount = list.filter((t) => t.current_market_price != null && t.current_market_price_source === "steam_lowest").length;
  const missingCount = list.filter((t) => t.current_market_price == null).length;
  const circuit = getLiveMarketCircuit(meta?.circuit || {});
  const warning = meta?.warning ? String(meta.warning) : "";
  const liveWarning = warning && !(warning.includes("熔断") && !circuit.open);
  const shouldShow = list.length > 0 && (fallbackCount > 0 || missingCount > 0 || !!liveWarning || !!meta?.fallback_used || !!circuit.open);
  if (!shouldShow) {
    notice.classList.add("hidden");
    notice.classList.remove("is-loading");
    if (retryBtn) {
      retryBtn.disabled = false;
      retryBtn.textContent = "重试获取现市场价";
    }
    if (clearBtn) {
      clearBtn.disabled = false;
    }
    if (moreWrap) {
      moreWrap.classList.add("hidden");
      moreWrap.classList.remove("open");
    }
    if (moreTrigger) {
      moreTrigger.setAttribute("aria-expanded", "false");
    }
    return;
  }
  const counts = summarizeMarketPriceSources(list);
  const detail = buildMarketPriceIssueDetail(counts, meta);
  if (titleEl) {
    if (circuit.open) titleEl.textContent = "Steam 智能价熔断中";
    else if (missingCount > 0 && counts.smart === 0 && fallbackCount === 0) titleEl.textContent = "现市场价获取失败";
    else if (missingCount > 0) titleEl.textContent = "现市场价未完整获取";
    else if (fallbackCount > 0 || meta?.fallback_used) titleEl.textContent = "当前现市场价使用最低价/中位价摘要";
    else titleEl.textContent = "Steam 智能价提示";
  }
  if (detailEl) detailEl.textContent = detail;
  notice.classList.remove("hidden");
  notice.classList.toggle("is-loading", smartPriceRetrying);
  if (retryBtn) {
    retryBtn.disabled = smartPriceRetrying || !!circuit.open;
    if (smartPriceRetrying) retryBtn.textContent = "获取中...";
    else if (circuit.open) retryBtn.textContent = `熔断中 ${formatMarketCircuitRemaining(circuit.remaining_seconds)}`;
    else retryBtn.textContent = "重试获取现市场价";
    retryBtn.title = circuit.open
      ? "熔断倒计时结束前不会自动重试；确认网络恢复后可在更多里解除熔断。"
      : "";
  }
  if (clearBtn) {
    clearBtn.disabled = smartPriceRetrying || marketCircuitClearing;
    clearBtn.textContent = marketCircuitClearing ? "解除中..." : "解除熔断";
  }
  if (moreWrap) {
    moreWrap.classList.toggle("hidden", !circuit.open);
    if (!circuit.open) moreWrap.classList.remove("open");
  }
  if (moreTrigger) {
    moreTrigger.disabled = smartPriceRetrying || marketCircuitClearing;
    moreTrigger.setAttribute("aria-expanded", moreWrap?.classList.contains("open") ? "true" : "false");
  }
  syncMarketPriceRefreshControls();
}
function getCurrentHoldingsColumnOrderFromHeader() {
  const table = el("transactions-table-purchases");
  const cols = Array.from(table?.querySelectorAll("thead th[data-col]") || [])
    .map((th) => th.dataset.col)
    .filter((col) => col && !TX_HOLDINGS_FIXED_COLUMNS.includes(col));
  return [...cols.filter((col) => TX_HOLDINGS_COLUMN_ORDER.includes(col)), ...TX_HOLDINGS_COLUMN_ORDER.filter((col) => !cols.includes(col))];
}
function applyHoldingsColumnOrder() {
  const table = el("transactions-table-purchases");
  if (!table) return;
  table.classList.toggle("is-column-sort-mode", holdingsColumnSortMode);
  const fullOrder = ["select", ...txHoldingsColumnOrder];
  const reorderCells = (row) => {
    const cellsByCol = new Map(Array.from(row.children).map((cell) => [cell.dataset.col, cell]));
    fullOrder.forEach((col) => {
      const cell = cellsByCol.get(col);
      if (cell) row.appendChild(cell);
    });
  };
  const headerRow = table.querySelector("thead tr");
  if (headerRow) reorderCells(headerRow);
  table.querySelectorAll("tbody tr").forEach((row) => {
    if (!row.querySelector("[data-col]")) return;
    reorderCells(row);
  });
  table.querySelectorAll("thead th[data-col], tbody td[data-col]").forEach((cell) => {
    const col = cell.dataset.col;
    if (!col || TX_HOLDINGS_FIXED_COLUMNS.includes(col)) return;
    cell.classList.toggle("tx-extra-col", txHoldingsExtraColumns.includes(col));
  });
  bindHoldingsColumnDrag();
}
function setHoldingColumnZone(col, zone) {
  if (!col || TX_HOLDINGS_FIXED_COLUMNS.includes(col)) return;
  const next = txHoldingsExtraColumns.filter((item) => item !== col);
  if (zone === "extra") next.push(col);
  writeHoldingsExtraColumnsPreference(next);
}
function moveHoldingColumnBefore(from, to, insertAfter = false, targetZone = null) {
  if (!from || !to || from === to || TX_HOLDINGS_FIXED_COLUMNS.includes(from) || TX_HOLDINGS_FIXED_COLUMNS.includes(to)) return;
  if (targetZone) setHoldingColumnZone(from, targetZone);
  const order = txHoldingsColumnOrder.filter((col) => col !== from);
  const toIdx = order.indexOf(to);
  if (toIdx < 0) return;
  order.splice(toIdx + (insertAfter ? 1 : 0), 0, from);
  writeHoldingsColumnOrderPreference(order);
  applyHoldingsColumnOrder();
  renderHoldingsColumnSortBar();
}
function moveHoldingColumnToZoneEnd(from, targetZone) {
  if (!from || TX_HOLDINGS_FIXED_COLUMNS.includes(from)) return;
  setHoldingColumnZone(from, targetZone);
  const sameZoneCols = txHoldingsColumnOrder.filter((col) => {
    if (col === from) return false;
    const isExtra = txHoldingsExtraColumns.includes(col);
    return targetZone === "extra" ? isExtra : !isExtra;
  });
  const lastInZone = sameZoneCols[sameZoneCols.length - 1];
  const order = txHoldingsColumnOrder.filter((col) => col !== from);
  const insertAt = lastInZone ? order.indexOf(lastInZone) + 1 : (targetZone === "extra" ? order.length : 0);
  order.splice(Math.max(0, insertAt), 0, from);
  writeHoldingsColumnOrderPreference(order);
  applyHoldingsColumnOrder();
  renderHoldingsColumnSortBar();
}
function clearHoldingsColumnPlaceholder() {
  if (holdingsColumnDragPlaceholder) {
    holdingsColumnDragPlaceholder.remove();
    holdingsColumnDragPlaceholder = null;
  }
}
function showHoldingsColumnPlaceholder(target, insertAfter = false) {
  if (!target || !target.parentElement) return;
  if (!holdingsColumnDragPlaceholder) {
    holdingsColumnDragPlaceholder = document.createElement("span");
    holdingsColumnDragPlaceholder.className = "tx-column-placeholder";
    holdingsColumnDragPlaceholder.setAttribute("aria-hidden", "true");
  }
  const width = Math.max(56, Math.round(target.getBoundingClientRect().width));
  holdingsColumnDragPlaceholder.style.width = `${width}px`;
  target.parentElement.insertBefore(holdingsColumnDragPlaceholder, insertAfter ? target.nextSibling : target);
}
function showHoldingsColumnPlaceholderAtEnd(wrap) {
  if (!wrap) return;
  if (!holdingsColumnDragPlaceholder) {
    holdingsColumnDragPlaceholder = document.createElement("span");
    holdingsColumnDragPlaceholder.className = "tx-column-placeholder";
    holdingsColumnDragPlaceholder.setAttribute("aria-hidden", "true");
  }
  holdingsColumnDragPlaceholder.style.width = "72px";
  wrap.appendChild(holdingsColumnDragPlaceholder);
}
function bindHoldingsColumnDrag() {
  const table = el("transactions-table-purchases");
  if (!table) return;
  table.querySelectorAll("thead th[data-col]").forEach((th) => {
    const col = th.dataset.col;
    const draggable = holdingsColumnSortMode && col && !TX_HOLDINGS_FIXED_COLUMNS.includes(col);
    th.draggable = draggable;
    th.classList.toggle("tx-draggable-col", draggable);
    if (!draggable || th.dataset.dragBound === "1") return;
    th.dataset.dragBound = "1";
    th.addEventListener("dragstart", (e) => {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", col);
      th.classList.add("is-dragging");
    });
    th.addEventListener("dragend", () => {
      th.classList.remove("is-dragging");
      table.querySelectorAll("thead th.is-drop-before, thead th.is-drop-after").forEach((node) => node.classList.remove("is-drop-before", "is-drop-after"));
    });
    th.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const after = e.offsetX > th.offsetWidth / 2;
      table.querySelectorAll("thead th.is-drop-before, thead th.is-drop-after").forEach((node) => {
        if (node !== th) node.classList.remove("is-drop-before", "is-drop-after");
      });
      th.classList.toggle("is-drop-before", !after);
      th.classList.toggle("is-drop-after", after);
    });
    th.addEventListener("dragleave", () => {
      th.classList.remove("is-drop-before", "is-drop-after");
    });
    th.addEventListener("drop", (e) => {
      e.preventDefault();
      th.classList.remove("is-drop-before", "is-drop-after");
      const from = e.dataTransfer.getData("text/plain");
      const to = th.dataset.col;
      moveHoldingColumnBefore(from, to, e.offsetX > th.offsetWidth / 2);
    });
  });
}
function setHoldingsColumnSortMode(enabled) {
  holdingsColumnSortMode = !!enabled;
  const bar = el("holdings-column-sort-bar");
  const menuBtn = el("btn-holdings-column-sort");
  if (bar) bar.classList.toggle("hidden", !holdingsColumnSortMode);
  if (menuBtn) menuBtn.textContent = holdingsColumnSortMode ? "退出列排序" : "调整列顺序";
  applyHoldingsColumnOrder();
  renderHoldingsColumnSortBar();
}
function toggleHoldingsColumnSortMode() {
  setHoldingsColumnSortMode(!holdingsColumnSortMode);
}
function renderHoldingsColumnSortBar() {
  const bar = el("holdings-column-sort-bar");
  if (!bar || !holdingsColumnSortMode) return;
  const visibleWrap = el("holdings-column-sort-visible");
  const extraWrap = el("holdings-column-sort-extra");
  const extraLabel = el("holdings-column-sort-extra-label");
  const renderChip = (col) => `<button class="tx-column-chip" type="button" draggable="true" data-col="${escapeHtml(col)}">${escapeHtml(TX_HOLDINGS_COLUMN_LABELS[col] || col)}</button>`;
  const visibleCols = txHoldingsColumnOrder.filter((col) => !txHoldingsExtraColumns.includes(col));
  const extraCols = txHoldingsColumnOrder.filter((col) => txHoldingsExtraColumns.includes(col));
  if (visibleWrap) visibleWrap.innerHTML = visibleCols.map(renderChip).join("");
  if (extraWrap) extraWrap.innerHTML = extraCols.map(renderChip).join("");
  if (extraLabel) extraLabel.textContent = holdingsShowMoreColumns ? "更多数据" : "收起数据";
  const clearZoneHighlight = () => {
    bar.querySelectorAll(".tx-column-sort-lane.is-drop-zone").forEach((node) => node.classList.remove("is-drop-zone"));
  };
  [visibleWrap, extraWrap].forEach((wrap) => {
    if (!wrap || wrap.dataset.dropBound === "1") return;
    wrap.dataset.dropBound = "1";
    wrap.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const chip = e.target.closest(".tx-column-chip");
      if (chip?.classList.contains("is-dragging")) {
        showHoldingsColumnPlaceholderAtEnd(wrap);
        return;
      }
      if (!chip && wrap.lastElementChild) showHoldingsColumnPlaceholder(wrap.lastElementChild, true);
      else if (!chip) showHoldingsColumnPlaceholderAtEnd(wrap);
    });
    wrap.addEventListener("drop", (e) => {
      e.preventDefault();
      clearHoldingsColumnPlaceholder();
      const from = e.dataTransfer.getData("text/plain");
      const targetZone = wrap.id === "holdings-column-sort-extra" ? "extra" : "visible";
      const chip = e.target.closest(".tx-column-chip");
      if (chip?.classList.contains("is-dragging")) {
        moveHoldingColumnToZoneEnd(from, targetZone);
        return;
      }
      if (chip) moveHoldingColumnBefore(from, chip.dataset.col, e.offsetX > chip.offsetWidth / 2, targetZone);
      else moveHoldingColumnToZoneEnd(from, targetZone);
    });
  });
  [visibleWrap?.closest(".tx-column-sort-lane"), extraWrap?.closest(".tx-column-sort-lane")].forEach((lane) => {
    if (!lane || lane.dataset.dropBound === "1") return;
    lane.dataset.dropBound = "1";
    lane.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      clearZoneHighlight();
      lane.classList.add("is-drop-zone");
      const list = lane.querySelector(".tx-column-sort-list");
      const chip = e.target.closest(".tx-column-chip");
      if (!chip && list) showHoldingsColumnPlaceholderAtEnd(list);
    });
    lane.addEventListener("dragleave", (e) => {
      if (e.relatedTarget && lane.contains(e.relatedTarget)) return;
      lane.classList.remove("is-drop-zone");
    });
    lane.addEventListener("drop", (e) => {
      e.preventDefault();
      clearZoneHighlight();
      clearHoldingsColumnPlaceholder();
      const list = lane.querySelector(".tx-column-sort-list");
      const targetZone = list?.id === "holdings-column-sort-extra" ? "extra" : "visible";
      const chip = e.target.closest(".tx-column-chip");
      if (chip && !chip.classList.contains("is-dragging")) moveHoldingColumnBefore(e.dataTransfer.getData("text/plain"), chip.dataset.col, e.offsetX > chip.offsetWidth / 2, targetZone);
      else moveHoldingColumnToZoneEnd(e.dataTransfer.getData("text/plain"), targetZone);
    });
  });
  bar.querySelectorAll(".tx-column-chip").forEach((chip) => {
    chip.addEventListener("dragstart", (e) => {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", chip.dataset.col || "");
      chip.classList.add("is-dragging");
      showHoldingsColumnPlaceholder(chip, true);
    });
    chip.addEventListener("dragend", () => {
      chip.classList.remove("is-dragging");
      clearZoneHighlight();
      clearHoldingsColumnPlaceholder();
    });
    chip.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "move";
      showHoldingsColumnPlaceholder(chip, e.offsetX > chip.offsetWidth / 2);
    });
    chip.addEventListener("dragleave", () => {
    });
    chip.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      clearHoldingsColumnPlaceholder();
      const wrap = chip.closest(".tx-column-sort-list");
      const targetZone = wrap?.id === "holdings-column-sort-extra" ? "extra" : "visible";
      moveHoldingColumnBefore(e.dataTransfer.getData("text/plain"), chip.dataset.col, e.offsetX > chip.offsetWidth / 2, targetZone);
    });
  });
}
function renderTxTable(tbody, list, isPurchase = false, resellRatio = 0.85, multiSelectMode = false) {
  const ratio = Math.max(0.01, Math.min(1, Number(resellRatio) || 0.85));
  const accountCapability = getTxAccountCapability();
  const accountName = normalizeAccountLabel(accountCapability.current);
  const rowHtmls = [];
  for (const t of list) {
    const timeStr = formatDateTime(t.at);
    const nameText = (t.name || "—").toString();
    const nameHtml = buildItemNameHtml(nameText);
    const idx = t.idx;
    const type = t.type;
    const checkCell = isPurchase
      ? `<td data-col="select" class="holding-select-cell ${multiSelectMode ? "" : "hidden"}"><input type="checkbox" class="holding-checkbox" data-idx="${idx}" /></td>`
      : "";
    const priceCell = `<td data-col="buy_price" class="mono">${escapeHtml(Number(t.price).toFixed(2))}</td>`;
    if (isPurchase) {
      const state = getAutomationState(t, accountCapability);
      const actHtml = renderTradeAction(t, state, type, idx, multiSelectMode);
      const accountCell = `<td data-col="account"><span class="tx-account-cell" title="${escapeHtml(accountName)}">${escapeHtml(accountName)}</span></td>`;
      const unlockCell = `<td data-col="unlock"><span class="tx-unlock-cell ${state.unlock.locked ? "is-locked" : "is-ready"}"><span>${escapeHtml(state.unlock.detail)}</span><small>${escapeHtml(state.unlock.label)}</small></span></td>`;
      const automationCell = `<td data-col="automation">${renderAutomationBadge(state)}</td>`;
      const mp = t.market_price != null ? Number(t.market_price).toFixed(2) : "—";
      const cur = t.current_market_price != null ? Number(t.current_market_price) : null;
      const cmp = cur != null ? cur.toFixed(2) : "";
      const cmpSource = marketPriceSourceLabel(t);
      const cmpTitle = cmpSource ? ` title="价格来源：${escapeHtml(cmpSource)}"` : "";
      const cmpCell = cmp
        ? `<td data-col="current_market" class="mono"${cmpTitle}>${escapeHtml(cmp)}${cmpSource === "最低价/中位价摘要" ? '<span class="tx-price-source">摘</span>' : ""}</td>`
        : '<td data-col="current_market" class="mono text-muted" title="现市场价暂未获取到">—</td>';
      const marketAtBuy = t.market_price != null ? Number(t.market_price) : null;
      let plCell = `<td data-col="market_change" class="tx-extra-col"></td>`;
      if (cur != null && marketAtBuy != null && marketAtBuy > 0) {
        const diff = cur - marketAtBuy;
        const pct = ((diff / marketAtBuy) * 100).toFixed(2) + "%";
        const cls = diff > 0 ? "text-ok" : diff < 0 ? "text-bad" : "";
        plCell = `<td data-col="market_change" class="mono tx-extra-col ${cls}">${diff >= 0 ? "+" : ""}${diff.toFixed(2)} (${diff >= 0 ? "+" : ""}${pct})</td>`;
      }
      const cost = Number(t.price) || 0;
      const afterTaxVal = cur != null && cur > 0 ? cur / 1.15 : null;
      const afterTax = afterTaxVal != null ? afterTaxVal.toFixed(2) : "";
      const discountRatio = afterTaxVal != null && afterTaxVal > 0 && cost > 0 ? (cost / afterTaxVal).toFixed(4) : "";
      const discountRatioClass = discountRatio ? (parseFloat(discountRatio) > ratio ? "text-bad" : "text-ok") : "";
      const cashProfit = afterTaxVal != null && cost > 0 ? (afterTaxVal * ratio - cost).toFixed(2) : "";
      const profitClass = cashProfit ? (parseFloat(cashProfit) > 0 ? "text-ok" : parseFloat(cashProfit) < 0 ? "text-bad" : "") : "";
      const selfUseProfit = afterTaxVal != null && cost > 0 ? (afterTaxVal - cost).toFixed(2) : "";
      const selfUseClass = selfUseProfit ? (parseFloat(selfUseProfit) > 0 ? "text-ok" : parseFloat(selfUseProfit) < 0 ? "text-bad" : "") : "";
      const afterTaxCell = afterTax ? `<td data-col="after_tax" class="mono tx-extra-col">${escapeHtml(afterTax)}</td>` : `<td data-col="after_tax" class="tx-extra-col"></td>`;
      const discountRatioCell = discountRatio ? `<td data-col="discount" class="mono ${discountRatioClass}">${escapeHtml(discountRatio)}</td>` : '<td data-col="discount"></td>';
      const profitCell = cashProfit ? `<td data-col="cash_profit" class="mono ${profitClass}">${escapeHtml(parseFloat(cashProfit) >= 0 ? "+" + cashProfit : cashProfit)}</td>` : '<td data-col="cash_profit"></td>';
      const selfUseCell = selfUseProfit ? `<td data-col="self_use" class="mono tx-extra-col ${selfUseClass}">${escapeHtml(parseFloat(selfUseProfit) >= 0 ? "+" + selfUseProfit : selfUseProfit)}</td>` : `<td data-col="self_use" class="tx-extra-col"></td>`;
      const assetidCell = `<td data-col="assetid" class="mono tx-extra-col">${escapeHtml(t.assetid ?? "—")}</td>`;
      const buyMarketCell = `<td data-col="buy_market" class="mono tx-extra-col">${escapeHtml(mp)}</td>`;
      rowHtmls.push(`<tr>${checkCell}<td data-col="time" class="mono">${escapeHtml(timeStr)}</td><td data-col="name">${nameHtml}</td>${accountCell}${unlockCell}${automationCell}${assetidCell}${priceCell}${buyMarketCell}${cmpCell}${afterTaxCell}${discountRatioCell}${profitCell}${selfUseCell}${plCell}<td data-col="actions" class="tx-actions">${actHtml}</td></tr>`);
    } else {
      const actHtml = `<div class="tx-actions-dropdown"><button type="button" class="tx-actions-trigger" title="操作">⋮</button><div class="tx-actions-menu"><button type="button" class="tx-action-item tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button><button type="button" class="tx-action-item tx-action-danger tx-btn-del" data-type="${escapeHtml(type)}" data-idx="${idx}">删除</button></div></div>`;
      const assetidCell = `<td class="mono">${escapeHtml(t.assetid ?? "—")}</td>`;
      rowHtmls.push(`<tr>${checkCell}<td class="mono">${escapeHtml(timeStr)}</td><td>${nameHtml}</td>${assetidCell}${priceCell}<td class="tx-actions">${actHtml}</td></tr>`);
    }
  }
  if (isPurchase && !rowHtmls.length) {
    tbody.innerHTML = txTableStateRow(16, "暂无当前持仓", "买入记录会在这里显示，已出售的记录请看交易流水。", "empty");
  } else {
    tbody.innerHTML = rowHtmls.join("");
  }
  if (isPurchase) applyHoldingsColumnOrder();
  bindSelectionCount("#transactions-table-purchases .holding-checkbox", "holdings-selected-count");
  tbody.querySelectorAll(".ph-btn-delist").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm("确定下架该饰品？下架后 assetid 会变更。")) return;
      const idx = parseInt(btn.dataset.idx, 10);
      btn.disabled = true;
      toast("下架中", "请稍候…");
      try {
        const r = await fetchJson(API + "/purchase/" + idx + "/delist", { method: "POST" });
        if (r.ok) {
          const detail = r.assetid != null && r.assetid !== "" ? "新 assetid: " + r.assetid : "新 assetid 为空，请使用「同步售出/持有」补全";
          toast("下架成功", detail);
          refreshTransactions();
          refreshStatus();
        } else {
          toast("下架失败", r.error || "接口未返回 error 字段");
        }
      } catch (e) {
        toast("下架失败", e.message || "请求异常");
      } finally {
        btn.disabled = false;
      }
    });
  });
  tbody.querySelectorAll(".tx-btn-del").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm("确定删除这条记录？")) return;
      const type = btn.dataset.type;
      const idx = parseInt(btn.dataset.idx, 10);
      try {
        const r = await fetchJson(API + "/transaction?" + new URLSearchParams({ type, idx }), { method: "DELETE" });
        if (r.ok) {
          toast("已删除");
          refreshTransactions();
        } else {
          toast("删除失败", r.error || "");
        }
      } catch (e) {
        toast("删除失败", e.message || "");
      }
    });
  });
  tbody.querySelectorAll(".tx-btn-edit").forEach(btn => {
    btn.addEventListener("click", () => {
      const type = btn.dataset.type;
      const idx = parseInt(btn.dataset.idx, 10);
      const t = list.find(x => x.type === type && x.idx === idx);
      if (!t) return;
      el("edit-tx-name").value = t.name || "";
      el("edit-tx-price").value = t.price ?? "";
      el("edit-tx-goods-id").value = t.goods_id ?? "";
      const mpWrap = el("edit-tx-market-price-wrap");
      if (mpWrap) mpWrap.style.display = type === "purchase" ? "" : "none";
      const mpEl = el("edit-tx-market-price");
      if (mpEl) mpEl.value = type === "purchase" ? (t.market_price ?? "") : "";
      const assetidWrap = el("edit-tx-assetid-wrap");
      if (assetidWrap) assetidWrap.style.display = type === "purchase" ? "" : "none";
      const assetidEl = el("edit-tx-assetid");
      if (assetidEl) assetidEl.value = type === "purchase" ? (t.assetid ?? "") : "";
      const listingWrap = el("edit-tx-listing-wrap");
      if (listingWrap) listingWrap.style.display = type === "purchase" ? "" : "none";
      const listingEl = el("edit-tx-listing");
      if (listingEl) {
        if (type !== "purchase") listingEl.value = "0";
        else if (t.pending_receipt) listingEl.value = "2";
        else if (t.listing) listingEl.value = "1";
        else listingEl.value = "0";
      }
      el("edit-tx-overlay").dataset.editType = type;
      el("edit-tx-overlay").dataset.editIdx = String(idx);
      el("edit-tx-overlay").classList.remove("hidden");
    });
  });
  tbody.querySelectorAll(".tx-btn-sell").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.idx, 10);
      const t = list.find(x => x.type === "purchase" && x.idx === idx);
      if (!t) return;
      const nameEl = el("sell-tx-item-name");
      if (nameEl) nameEl.textContent = t.name || "—";
      el("sell-tx-overlay").dataset.sellIdx = String(idx);
      const priceEl = el("sell-tx-price");
      if (priceEl) priceEl.value = "";
      el("sell-tx-overlay").classList.remove("hidden");
      if (priceEl) priceEl.focus();
    });
  });
  tbody.querySelectorAll(".tx-btn-auto-list").forEach(btn => {
    btn.addEventListener("click", () => {
      toast("已具备自动上架条件", "启动任务后，出售阶段会按策略和 Steam 解禁状态处理该饰品。");
    });
  });
}
function renderPurchaseHistoryTable(tbody, list, resellRatio = 0.85, multiSelectMode = false) {
  const ratio = Math.max(0.01, Math.min(1, Number(resellRatio) || 0.85));
  const accountCapability = getTxAccountCapability();
  const accountName = normalizeAccountLabel(accountCapability.current);
  const sorted = list.slice().sort((a, b) => (b.at || 0) - (a.at || 0));
  const rowHtmls = [];
  for (const t of sorted) {
    const timeStr = formatDateTime(t.at);
    const soldTimeStr = formatDateTime(t.sold_at);
    const nameText = (t.name || "—").toString();
    const nameHtml = buildItemNameHtml(nameText);
    const idx = t.idx;
    const checkCell = `<td class="holding-select-cell ${multiSelectMode ? "" : "hidden"}"><input type="checkbox" class="history-checkbox" data-idx="${idx}" /></td>`;
    const cost = Number(t.price) || 0;
    const mp = t.market_price != null ? Number(t.market_price).toFixed(2) : "—";
    const sold = t.sale_price != null && Number(t.sale_price) > 0;
    const state = getAutomationState(t, accountCapability);
    const listingError = t.listing_status === "error";
    const statusStr = t.pending_receipt ? "待收货" : sold ? "已出售" : listingError ? "ERROR" : t.listing ? "出售中" : "持有中";
    const statusCellClass = t.pending_receipt ? "status-pending" : sold ? "status-sold" : listingError ? "status-error" : t.listing ? "status-listing" : "status-holding";
    const salePriceStr = sold ? Number(t.sale_price).toFixed(2) : "—";
    let discountRatioStr = "—", cashProfitStr = "—", selfUseStr = "—", discountRatioClass = "";
    if (sold) {
      const afterTax = Number(t.sale_price) / 1.15;
      discountRatioStr = afterTax > 0 && cost > 0 ? (cost / afterTax).toFixed(4) : "—";
      discountRatioClass = discountRatioStr !== "—" ? (parseFloat(discountRatioStr) > ratio ? "text-bad" : "text-ok") : "";
      const cashProfit = afterTax > 0 && cost >= 0 ? afterTax * ratio - cost : 0;
      const selfUse = afterTax - cost;
      cashProfitStr = (cashProfit >= 0 ? "+" : "") + cashProfit.toFixed(2);
      selfUseStr = (selfUse >= 0 ? "+" : "") + selfUse.toFixed(2);
    }
    const cashClass = sold && parseFloat(cashProfitStr) !== 0 ? (parseFloat(cashProfitStr) > 0 ? "text-ok" : "text-bad") : "";
    const selfUseClass = sold && parseFloat(selfUseStr) !== 0 ? (parseFloat(selfUseStr) > 0 ? "text-ok" : "text-bad") : "";
    let deviationCell = `<td class="tx-extra-col"></td>`;
    if (sold) {
      const marketAtBuy = t.market_price != null ? Number(t.market_price) : 0;
      const saleP = Number(t.sale_price) || 0;
      if (marketAtBuy > 0) {
        const diff = saleP - marketAtBuy;
        const pct = ((diff / marketAtBuy) * 100).toFixed(2) + "%";
        const devClass = diff > 0 ? "text-ok" : diff < 0 ? "text-bad" : "";
        deviationCell = `<td class="mono tx-extra-col ${devClass}">${diff >= 0 ? "+" : ""}${diff.toFixed(2)} (${diff >= 0 ? "+" : ""}${pct})</td>`;
      } else {
        deviationCell = `<td class="mono tx-extra-col">—</td>`;
      }
    } else {
      deviationCell = `<td class="mono tx-extra-col">—</td>`;
    }
    const hasListing = !multiSelectMode && t.listing;
    const delistItem = hasListing ? `<button type="button" class="tx-action-item ph-btn-delist" data-type="purchase" data-idx="${idx}">下架</button>` : "";
    const actHtml = !multiSelectMode ? `<div class="tx-actions-dropdown"><button type="button" class="tx-actions-trigger" title="操作">⋮</button><div class="tx-actions-menu">${delistItem}<button type="button" class="tx-action-item tx-action-danger ph-btn-del" data-type="purchase" data-idx="${idx}">删除</button></div></div>` : "";
    const assetidStr = t.assetid ?? "—";
    rowHtmls.push(`<tr>${checkCell}<td class="mono">${escapeHtml(timeStr)}</td><td>${nameHtml}</td><td><span class="tx-account-cell" title="${escapeHtml(accountName)}">${escapeHtml(accountName)}</span></td><td>${renderAutomationBadge(sold ? { className: "is-sold", label: "已完成", hint: "该记录已出售" } : state)}</td><td class="mono tx-extra-col">${escapeHtml(assetidStr)}</td><td class="mono">${escapeHtml(Number(t.price).toFixed(2))}</td><td class="mono tx-extra-col">${escapeHtml(mp)}</td><td class="status-cell ${statusCellClass}">${escapeHtml(statusStr)}</td><td class="mono">${escapeHtml(salePriceStr)}</td><td class="mono tx-extra-col">${escapeHtml(soldTimeStr)}</td><td class="mono ${discountRatioClass}">${escapeHtml(discountRatioStr)}</td><td class="mono ${cashClass}">${escapeHtml(cashProfitStr)}</td><td class="mono tx-extra-col ${selfUseClass}">${escapeHtml(selfUseStr)}</td>${deviationCell}<td class="tx-actions">${actHtml}</td></tr>`);
  }
  tbody.innerHTML = rowHtmls.length
    ? rowHtmls.join("")
    : txTableStateRow(16, "暂无交易流水", "买入、出售和下架记录会在这里显示。", "empty");
  bindSelectionCount("#transactions-table-purchase-history .history-checkbox", "history-selected-count");
  tbody.querySelectorAll(".ph-btn-delist").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm("确定下架该饰品？下架后 assetid 会变更。")) return;
      const idx = parseInt(btn.dataset.idx, 10);
      btn.disabled = true;
      toast("下架中", "请稍候…");
      try {
        const r = await fetchJson(API + "/purchase/" + idx + "/delist", { method: "POST" });
        if (r.ok) {
          const detail = r.assetid != null && r.assetid !== "" ? "新 assetid: " + r.assetid : "新 assetid 为空，请使用「同步售出/持有」补全";
          toast("下架成功", detail);
          refreshTransactions();
          refreshStatus();
        } else {
          toast("下架失败", r.error || "接口未返回 error 字段");
        }
      } catch (e) {
        toast("下架失败", e.message || "请求异常");
      } finally {
        btn.disabled = false;
      }
    });
  });
  tbody.querySelectorAll(".ph-btn-del").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm("确定删除这条记录？删除后将从持有饰品与操作记录中同时移除。")) return;
      const idx = parseInt(btn.dataset.idx, 10);
      try {
        const r = await fetchJson(API + "/transaction?" + new URLSearchParams({ type: "purchase", idx }), { method: "DELETE" });
        if (r.ok) {
          toast("已删除");
          refreshTransactions();
        } else {
          toast("删除失败", r.error || "");
        }
      } catch (e) {
        toast("删除失败", e.message || "");
      }
    });
  });
}
function applyTransactionsToUI(all, summaryEl, tbodyP, tbodyHistory, resellRatio = 0.85) {
  const purchases = all.filter((t) => t.type === "purchase");
  const holdings = purchases.filter((t) => !(t.sale_price != null && Number(t.sale_price) > 0));
  const ratio = Math.max(0.01, Math.min(1, Number(resellRatio) || 0.85));
  const filteredHoldings = sortHoldingsList(filterHoldingsList(holdings), ratio);
  lastTransactionsResellRatio = ratio;
  const holdingsCountEl = el("tx-tab-count-purchases");
  const historyCountEl = el("tx-tab-count-history");
  if (holdingsCountEl) holdingsCountEl.textContent = String(holdings.length);
  if (historyCountEl) historyCountEl.textContent = String(purchases.length);
  refreshTxAccountCapability().then(() => {
    if (lastEnrichData === all) {
      if (tbodyP) {
        if (holdings.length && !filteredHoldings.length) {
          tbodyP.innerHTML = txTableStateRow(16, "没有匹配的持仓", "调整筛选条件后再试。", "empty");
        } else {
          renderTxTable(tbodyP, filteredHoldings, true, ratio, holdingsMultiSelectMode);
        }
      }
      if (tbodyHistory) renderPurchaseHistoryTable(tbodyHistory, purchases, ratio, historyMultiSelectMode);
    }
  });
  syncTxColumnToggleUI();
  if (tbodyP) {
    if (holdings.length && !filteredHoldings.length) {
      tbodyP.innerHTML = txTableStateRow(16, "没有匹配的持仓", "调整筛选条件后再试。", "empty");
    } else {
      renderTxTable(tbodyP, filteredHoldings, true, ratio, holdingsMultiSelectMode);
    }
  }
  if (tbodyHistory) renderPurchaseHistoryTable(tbodyHistory, purchases, ratio, historyMultiSelectMode);
  updateMarketPriceNotice(lastMarketPriceMeta, holdings);
  refreshHoldingsFilterUI(holdings.length, filteredHoldings.length);
  syncHoldingsSortControls();
  syncMarketPriceRefreshControls();
  syncHistoryMultiSelectUI();
  const historySummaryEl = el("purchase-history-summary");
  if (historySummaryEl && purchases.length) {
    const totalPrice = purchases.reduce((s, t) => s + (Number(t.price) || 0), 0);
    const totalMp = purchases.reduce((s, t) => s + (t.market_price != null ? Number(t.market_price) : 0), 0);
    const totalSalePrice = purchases.reduce((s, t) => s + (t.sale_price != null && Number(t.sale_price) > 0 ? Number(t.sale_price) : 0), 0);
    const totalAfterTax = totalSalePrice > 0 ? totalSalePrice / 1.15 : null;
    const soldItems = purchases.filter((t) => t.sale_price != null && Number(t.sale_price) > 0);
    let ratioSum = 0, ratioCount = 0, totalCashProfit = 0, totalSelfUseProfit = 0;
    soldItems.forEach((t) => {
      const afterTax = Number(t.sale_price) / 1.15;
      const cost = Number(t.price) || 0;
      if (afterTax > 0 && cost > 0) { ratioSum += cost / afterTax; ratioCount += 1; }
      totalCashProfit += afterTax * ratio - cost;
      totalSelfUseProfit += afterTax - cost;
    });
    const discountRatio = ratioCount > 0 ? (ratioSum / ratioCount).toFixed(4) : "—";
    const discountRatioClass = discountRatio !== "—" ? (parseFloat(discountRatio) > ratio ? "text-bad" : "text-ok") : "";
    const cashProfitVal = soldItems.length > 0 ? totalCashProfit : null;
    const selfUseProfitVal = soldItems.length > 0 ? totalSelfUseProfit : null;
    const profitClass = cashProfitVal != null && cashProfitVal > 0 ? "text-ok" : cashProfitVal != null && cashProfitVal < 0 ? "text-bad" : "";
    const selfUseClass = selfUseProfitVal != null && selfUseProfitVal > 0 ? "text-ok" : selfUseProfitVal != null && selfUseProfitVal < 0 ? "text-bad" : "";
    const soldMp = purchases.reduce((s, t) => s + (t.sale_price != null && Number(t.sale_price) > 0 && t.market_price != null ? Number(t.market_price) : 0), 0);
    const totalDeviation = totalSalePrice > 0 && soldMp > 0 ? totalSalePrice - soldMp : null;
    const totalDeviationPct = totalDeviation != null && soldMp > 0 ? ((totalDeviation / soldMp) * 100).toFixed(2) + "%" : "—";
    const deviationClass = totalDeviation != null && totalDeviation > 0 ? "text-ok" : totalDeviation != null && totalDeviation < 0 ? "text-bad" : "";
    const deviationStr = totalDeviation != null ? `${totalDeviation >= 0 ? "+" : ""}${totalDeviation.toFixed(2)} (${totalDeviation >= 0 ? "+" : ""}${totalDeviationPct})` : "—";
    const salePriceStr = totalSalePrice > 0 ? totalSalePrice.toFixed(2) : "—";
    const afterTaxStr = totalAfterTax != null ? totalAfterTax.toFixed(2) : "—";
    const cashProfitStr = cashProfitVal != null ? (cashProfitVal >= 0 ? "+" : "") + cashProfitVal.toFixed(2) : "—";
    const selfUseProfitStr = selfUseProfitVal != null ? (selfUseProfitVal >= 0 ? "+" : "") + selfUseProfitVal.toFixed(2) : "—";
    historySummaryEl.innerHTML = [
      `<span class="summary-stat"><span class="summary-label">总购入价</span><span class="summary-value mono">${totalPrice.toFixed(2)}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总购入市场价</span><span class="summary-value mono">${totalMp.toFixed(2)}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总出售价</span><span class="summary-value mono">${salePriceStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总税后价格</span><span class="summary-value mono">${afterTaxStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">实际折扣比率</span><span class="summary-value mono ${discountRatioClass}">${discountRatio}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总变现收益</span><span class="summary-value mono ${profitClass}">${cashProfitStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总自用收益</span><span class="summary-value mono ${selfUseClass}">${selfUseProfitStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总价格偏离度</span><span class="summary-value mono ${deviationClass}">${deviationStr}</span></span>`,
    ].join("");
    historySummaryEl.style.display = "";
  } else if (historySummaryEl) {
    historySummaryEl.textContent = "";
    historySummaryEl.style.display = "none";
  }
  if (summaryEl && holdings.length) {
    const totalPrice = holdings.reduce((s, t) => s + (Number(t.price) || 0), 0);
    const totalMp = holdings.reduce((s, t) => s + (t.market_price != null ? Number(t.market_price) : 0), 0);
    const hasAnyCmp = holdings.some((t) => t.current_market_price != null);
    const totalCmp = hasAnyCmp ? holdings.reduce((s, t) => s + (t.current_market_price != null ? Number(t.current_market_price) : 0), 0) : null;
    const totalPl = totalCmp != null ? totalCmp - totalMp : null;
    const totalPlPct = totalPl != null && totalMp > 0 ? ((totalPl / totalMp) * 100).toFixed(2) + "%" : "—";
    const plClass = totalPl != null && totalPl > 0 ? "text-ok" : totalPl != null && totalPl < 0 ? "text-bad" : "";
    const cmpStr = totalCmp != null ? totalCmp.toFixed(2) : "—";
    const hasFallbackCmp = holdings.some((t) => t.current_market_price != null && t.current_market_price_source === "steam_lowest");
    const cmpSummaryLabel = hasFallbackCmp ? '总现市场价<span class="tx-price-source" title="部分条目使用 Steam 最低价/中位价摘要">摘</span>' : '总现市场价';
    const plStr = totalPl != null ? `${totalPl >= 0 ? "+" : ""}${totalPl.toFixed(2)} (${totalPl >= 0 ? "+" : ""}${totalPlPct})` : "—";
    const totalAfterTax = totalCmp != null && totalCmp > 0 ? totalCmp / 1.15 : null;
    const afterTaxStr = totalAfterTax != null ? totalAfterTax.toFixed(2) : "—";
    let ratioSum = 0, ratioCount = 0, totalCashProfit = 0, totalSelfUseProfit = 0;
    holdings.forEach((t) => {
      const cmp = t.current_market_price != null ? Number(t.current_market_price) : null;
      if (cmp == null || cmp <= 0) return;
      const afterTax = cmp / 1.15;
      const cost = Number(t.price) || 0;
      if (afterTax > 0 && cost > 0) { ratioSum += cost / afterTax; ratioCount += 1; }
      totalCashProfit += afterTax * ratio - cost;
      totalSelfUseProfit += afterTax - cost;
    });
    const discountRatio = ratioCount > 0 ? (ratioSum / ratioCount).toFixed(4) : "—";
    const discountRatioClass = discountRatio !== "—" ? (parseFloat(discountRatio) > ratio ? "text-bad" : "text-ok") : "";
    const cashProfitVal = ratioCount > 0 ? totalCashProfit : null;
    const selfUseProfitVal = ratioCount > 0 ? totalSelfUseProfit : null;
    const profitClass = cashProfitVal != null && cashProfitVal > 0 ? "text-ok" : cashProfitVal != null && cashProfitVal < 0 ? "text-bad" : "";
    const selfUseProfitClass = selfUseProfitVal != null && selfUseProfitVal > 0 ? "text-ok" : selfUseProfitVal != null && selfUseProfitVal < 0 ? "text-bad" : "";
    const cashProfitStr = cashProfitVal != null ? (cashProfitVal >= 0 ? "+" : "") + cashProfitVal.toFixed(2) : "—";
    const selfUseProfitStr = selfUseProfitVal != null ? (selfUseProfitVal >= 0 ? "+" : "") + selfUseProfitVal.toFixed(2) : "—";
    summaryEl.innerHTML = [
      `<span class="summary-stat"><span class="summary-label">数量</span><span class="summary-value mono">${holdings.length}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总购入价</span><span class="summary-value mono">${totalPrice.toFixed(2)}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总购入市场价</span><span class="summary-value mono">${totalMp.toFixed(2)}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">${cmpSummaryLabel}</span><span class="summary-value mono">${cmpStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总税后价格</span><span class="summary-value mono">${afterTaxStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">实际折扣比率</span><span class="summary-value mono ${discountRatioClass}">${discountRatio}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总变现收益</span><span class="summary-value mono ${profitClass}">${cashProfitStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总自用收益</span><span class="summary-value mono ${selfUseProfitClass}">${selfUseProfitStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总市场变动</span><span class="summary-value mono ${plClass}">${plStr}</span></span>`,
    ].join("");
  } else if (summaryEl) {
    summaryEl.textContent = "";
    summaryEl.style.display = "none";
  }
  if (summaryEl && holdings.length) summaryEl.style.display = "";
  if (summaryEl && !holdings.length) summaryEl.style.display = "none";
  refreshAnalytics(purchases, ratio);
  syncHoldingsMultiSelectUI();
}
function syncHoldingsMultiSelectUI() {
  const selectTh = el("holding-select-th");
  const batchBar = el("holdings-batch-actions");
  const multiselectBtn = el("btn-holdings-multiselect");
  document.querySelectorAll("#transactions-table-purchases .holding-select-cell").forEach((cell) => {
    cell.classList.toggle("hidden", !holdingsMultiSelectMode);
  });
  if (holdingsMultiSelectMode) {
    if (selectTh) selectTh.classList.remove("hidden");
    if (batchBar) { batchBar.classList.remove("hidden"); batchBar.style.display = "flex"; }
    if (multiselectBtn) multiselectBtn.textContent = "取消多选";
    if (multiselectBtn) multiselectBtn.setAttribute("aria-pressed", "true");
    bindSelectionCount("#transactions-table-purchases .holding-checkbox", "holdings-selected-count");
  } else {
    if (selectTh) selectTh.classList.add("hidden");
    if (batchBar) { batchBar.classList.add("hidden"); batchBar.style.display = "none"; }
    if (multiselectBtn) multiselectBtn.textContent = "多选";
    if (multiselectBtn) multiselectBtn.setAttribute("aria-pressed", "false");
    document.querySelectorAll("#transactions-table-purchases .holding-checkbox:checked").forEach((cb) => { cb.checked = false; });
    const countEl = el("holdings-selected-count");
    if (countEl) countEl.textContent = "0";
  }
}
function syncHistoryMultiSelectUI() {
  const selectTh = el("history-select-th");
  const batchBar = el("history-batch-actions");
  const multiselectBtn = el("btn-history-multiselect");
  document.querySelectorAll("#transactions-table-purchase-history .holding-select-cell").forEach((cell) => {
    cell.classList.toggle("hidden", !historyMultiSelectMode);
  });
  if (historyMultiSelectMode) {
    if (selectTh) selectTh.classList.remove("hidden");
    if (batchBar) { batchBar.classList.remove("hidden"); batchBar.style.display = "flex"; }
    if (multiselectBtn) multiselectBtn.textContent = "取消多选";
    if (multiselectBtn) multiselectBtn.setAttribute("aria-pressed", "true");
    bindSelectionCount("#transactions-table-purchase-history .history-checkbox", "history-selected-count");
  } else {
    if (selectTh) selectTh.classList.add("hidden");
    if (batchBar) { batchBar.classList.add("hidden"); batchBar.style.display = "none"; }
    if (multiselectBtn) multiselectBtn.textContent = "多选";
    if (multiselectBtn) multiselectBtn.setAttribute("aria-pressed", "false");
    document.querySelectorAll("#transactions-table-purchase-history .history-checkbox:checked").forEach((cb) => { cb.checked = false; });
    const countEl = el("history-selected-count");
    if (countEl) countEl.textContent = "0";
  }
}
function syncTxColumnToggleUI() {
  const holdingsTable = el("transactions-table-purchases");
  const historyTable = el("transactions-table-purchase-history");
  const holdingsBtn = el("btn-holdings-toggle-cols");
  const historyBtn = el("btn-history-toggle-cols");
  if (holdingsTable) holdingsTable.classList.toggle("show-extra-cols", holdingsShowMoreColumns);
  if (historyTable) historyTable.classList.toggle("show-extra-cols", historyShowMoreColumns);
  if (holdingsBtn) {
    holdingsBtn.textContent = holdingsShowMoreColumns ? "收起数据" : "显示更多数据";
    holdingsBtn.setAttribute("aria-pressed", holdingsShowMoreColumns ? "true" : "false");
  }
  if (historyBtn) {
    historyBtn.textContent = historyShowMoreColumns ? "收起数据" : "显示更多数据";
    historyBtn.setAttribute("aria-pressed", historyShowMoreColumns ? "true" : "false");
  }
}
function setHoldingsShowMoreColumns(value) {
  holdingsShowMoreColumns = !!value;
  writeTxColumnPreference(TX_HOLDINGS_COLUMNS_KEY, holdingsShowMoreColumns);
  syncTxColumnToggleUI();
  renderHoldingsColumnSortBar();
}
function setHistoryShowMoreColumns(value) {
  historyShowMoreColumns = !!value;
  writeTxColumnPreference(TX_HISTORY_COLUMNS_KEY, historyShowMoreColumns);
  syncTxColumnToggleUI();
}
function getCurrentPriceRefreshMinutes() {
  return Math.max(1, parseInt(el("cfg-current-price-refresh-minutes")?.value, 10) || 10);
}
async function refreshTransactions(options = {}) {
  const forceSmartPrice = !!options.forceSmartPrice;
  const tbodyP = document.querySelector("#transactions-table-purchases tbody");
  const tbodyHistory = document.querySelector("#transactions-table-purchase-history tbody");
  const summaryEl = el("purchases-summary");
  if (!tbodyP && !tbodyHistory) return;
  setTxTableLoading(tbodyP, 16, "正在加载当前持仓", "正在读取交易记录和市场价格。");
  setTxTableLoading(tbodyHistory, 16, "正在加载交易流水", "正在读取买入、出售和下架记录。");
  try {
    const d = await fetchJson(API + "/transactions?enrich_current_price=0");
    let all = d.transactions || [];
    let resellRatio = d.resell_ratio;
    const byKey = (t) => `${t.type}:${t.idx}`;
    const enrichedMap = new Map((lastEnrichData || []).map((t) => [byKey(t), t]));
    for (const t of all) {
      const e = enrichedMap.get(byKey(t));
      if (e && e.current_market_price != null) t.current_market_price = e.current_market_price;
      if (e && e.current_market_price_source) {
        t.current_market_price_source = e.current_market_price_source;
        t.current_market_price_source_label = e.current_market_price_source_label;
      }
    }
    const holdings = all.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0));
    const missingCurrentPrice = holdings.some((t) => t.current_market_price == null);
    const refreshAgeMs = getCurrentPriceRefreshMinutes() * 60 * 1000;
    const priceCacheStale = !lastEnrichTime || (Date.now() - lastEnrichTime) > refreshAgeMs;
    let priceRefreshAttempted = false;
    let priceRefreshMeta = null;
    let priceRefreshError = "";
    if (holdings.length && (forceSmartPrice || missingCurrentPrice || priceCacheStale)) {
      priceRefreshAttempted = true;
      try {
        const params = new URLSearchParams({ enrich_current_price: "1" });
        if (forceSmartPrice) params.set("force_smart_price", "1");
        const enriched = await fetchJson(API + "/transactions?" + params.toString());
        if (Array.isArray(enriched.transactions)) {
          all = enriched.transactions;
          resellRatio = enriched.resell_ratio ?? resellRatio;
          lastEnrichTime = Date.now();
        }
        priceRefreshMeta = enriched.price_meta || null;
      } catch (e) {
        priceRefreshError = e.message || "请求失败";
      }
    }
    lastEnrichData = all;
    if (priceRefreshMeta) handleMarketPriceMeta(priceRefreshMeta);
    if (priceRefreshAttempted) {
      const refreshedHoldings = all.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0));
      recordMarketPriceRefresh(refreshedHoldings, priceRefreshMeta || lastMarketPriceMeta, forceSmartPrice ? "手动刷新" : "自动刷新", priceRefreshError);
    }
    applyTransactionsToUI(all, summaryEl, tbodyP, tbodyHistory, resellRatio);
  } catch (e) {
    toast("加载操作记录失败", e.message || "");
    setTxTableError(tbodyP, 16, "当前持仓加载失败", e.message || "请稍后重试。");
    setTxTableError(tbodyHistory, 16, "交易流水加载失败", e.message || "请稍后重试。");
  }
}
async function retrySmartMarketPrices() {
  if (smartPriceRetrying) return;
  const circuit = getCurrentMarketCircuit();
  if (circuit.open) {
    const msg = `Steam 智能价熔断剩余 ${formatMarketCircuitRemaining(circuit.remaining_seconds)}，确认代理/加速器恢复后可在提示栏“更多”里解除熔断。`;
    toast("智能价熔断中", msg);
    const holdings = Array.isArray(lastEnrichData)
      ? lastEnrichData.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0))
      : [];
    updateMarketPriceNotice(lastMarketPriceMeta, holdings);
    return;
  }
  smartPriceRetrying = true;
  syncMarketPriceRefreshControls();
  updateMarketPriceNotice(lastMarketPriceMeta, Array.isArray(lastEnrichData) ? lastEnrichData : []);
  try {
    await refreshTransactions({ forceSmartPrice: true });
    const holdings = Array.isArray(lastEnrichData)
      ? lastEnrichData.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0))
      : [];
    const counts = getMarketPriceIssueCounts(holdings);
    if (counts.missing > 0) toast("现市场价获取失败", buildMarketPriceIssueDetail(counts, lastMarketPriceMeta));
    else if (counts.fallback > 0) toast("智能价仍未完全恢复", `${counts.fallback} 条使用最低价/中位价摘要`);
    else toast("智能价已更新");
  } finally {
    smartPriceRetrying = false;
    syncMarketPriceRefreshControls();
    const holdings = Array.isArray(lastEnrichData)
      ? lastEnrichData.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0))
      : [];
    updateMarketPriceNotice(lastMarketPriceMeta, holdings);
  }
}
async function clearMarketPriceCircuit() {
  if (marketCircuitClearing) return;
  marketCircuitClearing = true;
  syncMarketPriceRefreshControls();
  updateMarketPriceNotice(lastMarketPriceMeta, Array.isArray(lastEnrichData) ? lastEnrichData : []);
  try {
    const d = await fetchJson(API + "/market-price/circuit/clear", { method: "POST" });
    lastMarketPriceMeta = d.price_meta || { ...(lastMarketPriceMeta || {}), circuit: d.circuit || { open: false, remaining_seconds: 0 } };
    toast("已解除市场价熔断", "后台自动刷新将恢复；如果 Steam 仍失败，熔断会再次保护。");
    await refreshTransactions({ forceSmartPrice: true });
  } catch (e) {
    toast("解除熔断失败", e.message || "请稍后重试");
  } finally {
    marketCircuitClearing = false;
    syncMarketPriceRefreshControls();
    const holdings = Array.isArray(lastEnrichData)
      ? lastEnrichData.filter((t) => t.type === "purchase" && !(t.sale_price != null && Number(t.sale_price) > 0))
      : [];
    updateMarketPriceNotice(lastMarketPriceMeta, holdings);
  }
}


// --- Phase 3.2: Dropdown menu toggle ---
document.addEventListener("click", function(e) {
  document.querySelectorAll(".tx-actions-dropdown.open").forEach(function(d) {
    if (!d.contains(e.target)) d.classList.remove("open");
  });
  var trigger = e.target.closest(".tx-actions-trigger");
  if (trigger) {
    e.stopPropagation();
    var dropdown = trigger.closest(".tx-actions-dropdown");
    var wasOpen = dropdown.classList.contains("open");
    document.querySelectorAll(".tx-actions-dropdown.open").forEach(function(d) { d.classList.remove("open"); });
    if (!wasOpen) dropdown.classList.add("open");
  }
  var actionItem = e.target.closest(".tx-action-item");
  if (actionItem) {
    var dropdown2 = actionItem.closest(".tx-actions-dropdown");
    if (dropdown2) setTimeout(function() { dropdown2.classList.remove("open"); }, 100);
  }
});
