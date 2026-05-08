
let accountsCache = [];
let accountsCurrentId = null;
let selectedAccountId = null;
let accountEditId = null;
let accountsSearchTerm = '';
let accountDetailTab = "basic";
let accountStatsCache = null;
let accountGlobalTargetBalance = null;
let accountGlobalStrategy = {};
function getAccountName(acc) {
  return acc?.display_name || acc?.username || acc?.steam_id || "未命名";
}
function parseFiniteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function formatAccountAmount(value, currency = "") {
  const n = parseFiniteNumber(value);
  if (n === null) return "—";
  const code = (currency || "").toUpperCase();
  const symbols = { CNY: "¥", HKD: "HK$", USD: "$", EUR: "€", RUB: "₽", INR: "₹" };
  const body = n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return symbols[code] ? `${symbols[code]}${body}` : code ? `${body} ${code}` : body;
}
function getAccountBalanceDisplay(acc) {
  const explicit = acc?.balance_display || acc?.wallet_balance_display || acc?.steam_balance_display || acc?.balance_text;
  if (explicit) return String(explicit);
  const raw = acc?.wallet_balance ?? acc?.steam_balance ?? acc?.balance;
  const n = parseFiniteNumber(raw);
  if (n !== null) return formatAccountAmount(n, acc?.currency_code || "");
  return "未同步";
}
function getAccountTargetBalanceDisplay(acc) {
  const trade = acc?.trade_config || {};
  const own = parseFiniteNumber(trade.target_balance);
  if (own !== null) return { value: formatAccountAmount(own, acc?.currency_code || "CNY"), source: "账号目标" };
  if (accountGlobalTargetBalance !== null) {
    return { value: formatAccountAmount(accountGlobalTargetBalance, acc?.currency_code || "CNY"), source: "全局目标" };
  }
  return { value: "继承全局", source: "目标余额" };
}
function formatAccountSyncTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
function getAccountBalanceDetail(acc) {
  const balance = getAccountBalanceDisplay(acc);
  const syncedAt = formatAccountSyncTime(acc?.balance_synced_at);
  return syncedAt === "—" ? balance : `${balance}（${syncedAt}）`;
}
function formatAccountCurrency(acc) {
  const currency = (acc?.currency_code || "").toUpperCase();
  if (!currency) return "—";
  const labels = {
    CNY: "人民币 (CNY)",
    HKD: "港币 (HKD)",
    USD: "美元 (USD)",
    INR: "印度卢比 (INR)",
    RUB: "卢布 (RUB)",
    EUR: "欧元 (EUR)",
  };
  return labels[currency] || currency;
}
function formatAccountRegion(acc) {
  const region = (acc?.region_code || "").toUpperCase();
  if (!region) return "—";
  const labels = {
    CN: "中国 (CN)",
    HK: "中国香港 (HK)",
    US: "美国 (US)",
    IN: "印度 (IN)",
    RU: "俄罗斯 (RU)",
    EU: "欧元区 (EU)",
  };
  return labels[region] || region;
}
function accountStatusPill(label, tone = "info") {
  return `<span class="account-status-pill is-${escapeHtml(tone)}">${escapeHtml(label)}</span>`;
}

function accountIconSvg(name) {
  const icons = {
    eye: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"></path><circle cx="12" cy="12" r="3"></circle></svg>',
    eyeOff: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18"></path><path d="M10.6 10.6A2 2 0 0 0 12 14a2 2 0 0 0 1.4-.6"></path><path d="M9.9 5.2A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17.8 17.8 0 0 1-3.1 4.1"></path><path d="M6.6 6.7C3.6 8.7 2 12 2 12s3.5 7 10 7a10.4 10.4 0 0 0 4.2-.9"></path></svg>',
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>',
  };
  return icons[name] || "";
}

function getPayMethodLabel(value) {
  const labels = { alipay: "支付宝", wechat: "微信" };
  return labels[(value || "").toLowerCase()] || (value ? String(value) : "未设置");
}

function formatDiscountRatio(value) {
  const n = parseFiniteNumber(value);
  if (n === null) return "继承全局";
  return n.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

function tradeHasValue(trade, key) {
  return trade && trade[key] !== undefined && trade[key] !== null && trade[key] !== "";
}

function getTradeSourceLabel(enabled, trade, key) {
  if (!enabled) return "跟随全局";
  return tradeHasValue(trade, key) ? "账号覆盖" : "继承全局";
}

function getTradeModeClass(enabled, trade, key) {
  return enabled && tradeHasValue(trade, key) ? "is-custom" : "is-inherited";
}

function getGlobalTradeHint(key, acc) {
  if (key === "target_balance") {
    return accountGlobalTargetBalance !== null
      ? `全局：${formatAccountAmount(accountGlobalTargetBalance, acc?.currency_code || "CNY")}`
      : "全局未设置";
  }
  if (key === "max_discount") {
    return accountGlobalStrategy.maxDiscount !== null && accountGlobalStrategy.maxDiscount !== undefined
      ? `全局：${formatDiscountRatio(accountGlobalStrategy.maxDiscount)}`
      : "全局未设置";
  }
  if (key === "pay_method") {
    return `全局：${getPayMethodLabel(accountGlobalStrategy.payMethod || "")}`;
  }
  return "继承全局";
}

function getAccountGuardCoverage(accs) {
  const total = accs.length;
  const ready = accs.filter((a) => !!(a.steam_guard_status || {}).resolved_configured).length;
  return { ready, total };
}

function getCurrentAccountCookieState(acc, currentId) {
  if (!acc || acc.id !== currentId) {
    return { label: "非当前", tone: "muted", hint: "切换为当前账号后再验证 Cookie" };
  }
  const cookieValid = accountStatsCache?.account?.cookie_valid;
  if (cookieValid === true) return { label: "Cookie 有效", tone: "success", hint: "当前 Steam Cookie 已保存" };
  if (cookieValid === false) return { label: "Cookie 缺失", tone: "danger", hint: "请验证账号或重新登录 Steam" };
  return { label: "待同步", tone: "info", hint: "状态会随仪表盘数据刷新" };
}

async function refreshAccountStatsState() {
  try {
    accountStatsCache = await fetchJson(API + "/stats");
  } catch {
    accountStatsCache = null;
  }
  const selected = (accountsCache || []).find((x) => x.id === selectedAccountId);
  if (selected) renderAccountDetail(selected, accountsCurrentId);
}
function renderAccountDetail(acc, currentId) {
  const detail = el("account-detail");
  if (!detail) return;
  if (!acc) {
    detail.innerHTML = `<div class="account-detail-empty">
      <div class="empty-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
          <circle cx="12" cy="7" r="4"></circle>
        </svg>
      </div>
      <h3>选择一个账号</h3>
      <p>在左侧选择账号查看详情，或点击「添加」创建新账号。</p>
    </div>`;
    return;
  }
  const isCurrent = acc.id === currentId;
  const name = getAccountName(acc);
  const meta = [acc.username, acc.steam_id].filter(Boolean).join(" · ") || "—";
  const accountNote = (acc.account_note || "").trim();
  const avatar = buildAccountAvatar(name, acc.avatar_url, 64);
  const currencyLabel = formatAccountCurrency(acc);
  const regionLabel = formatAccountRegion(acc);
  const balanceDetail = getAccountBalanceDetail(acc);
  const guard = acc.steam_guard || {};
  const guardStatus = acc.steam_guard_status || {};
  const guardConfigured = !!guardStatus.resolved_configured;
  const accountGuardConfigured = !!guardStatus.account_configured;
  const identityConfigured = !!guardStatus.identity_configured;
  const trade = acc.trade_config || {};
  const tradeEnabled = trade.enabled === true;
  const tradePayMethod = trade.pay_method || "";
  const cookieState = getCurrentAccountCookieState(acc, currentId);
  const profileReady = !!(acc.steam_id && (acc.display_name || acc.avatar_url));
  const guardLabel = accountGuardConfigured ? "账号级令牌" : guardConfigured ? "使用全局令牌" : "令牌未配置";
  const guardTone = guardConfigured ? "success" : "warning";
  const tradeLabel = tradeEnabled ? "使用账号级策略" : "继承全局策略";
  const tradeTargetMode = getTradeSourceLabel(tradeEnabled, trade, "target_balance");
  const tradeDiscountMode = getTradeSourceLabel(tradeEnabled, trade, "max_discount");
  const tradePayMode = getTradeSourceLabel(tradeEnabled, trade, "pay_method");
  const tradeDirtyHint = tradeEnabled ? "覆盖已启用，留空项继续继承全局。" : "启用后可按账号覆盖目标余额、折扣和支付方式。";
  const guardSecretReady = !!guard.shared_secret;
  const guardIdentityReady = !!(guard.identity_secret && guard.device_id);
  const guardRisk = guardConfigured && !identityConfigured;
  if (!["basic", "guard", "trade"].includes(accountDetailTab)) accountDetailTab = "basic";
  detail.innerHTML = `
    <div class="account-detail-header">
      <div class="account-detail-main">
        ${avatar}
        <div class="account-detail-identity">
          <div class="account-detail-title">${escapeHtml(name)} ${isCurrent ? '<span class="badge badge-current">当前</span>' : ""}</div>
          <div class="account-detail-meta">${escapeHtml(meta)}</div>
          <div class="account-detail-chips">
            ${accountStatusPill(cookieState.label, cookieState.tone)}
            ${accountStatusPill(guardLabel, guardTone)}
            ${accountStatusPill(tradeLabel, tradeEnabled ? "success" : "muted")}
          </div>
        </div>
      </div>
      <div class="account-detail-actions">
        <button type="button" class="btn btn-secondary btn-sm" id="btn-acc-sync" data-id="${escapeHtml(acc.id)}">同步</button>
        ${!isCurrent ? `<button type="button" class="btn btn-primary btn-sm" id="btn-acc-set-current" data-id="${escapeHtml(acc.id)}">设为当前</button>` : ""}
        <button type="button" class="btn btn-edit btn-sm" id="btn-acc-edit" data-id="${escapeHtml(acc.id)}">编辑</button>
        <button type="button" class="btn btn-danger-outline btn-sm" id="btn-acc-del" data-id="${escapeHtml(acc.id)}">删除</button>
      </div>
    </div>
    <div class="account-detail-body">
      <div class="account-health-grid">
        <div class="account-health-item">
          <span class="account-health-label">资料</span>
          <strong>${profileReady ? "已同步" : "待验证"}</strong>
          <span>${acc.avatar_url ? "头像已获取" : "验证后补全头像"}</span>
        </div>
        <div class="account-health-item">
          <span class="account-health-label">登录</span>
          <strong>${escapeHtml(cookieState.label)}</strong>
          <span>${escapeHtml(cookieState.hint)}</span>
        </div>
        <div class="account-health-item">
          <span class="account-health-label">二次验证</span>
          <strong>${guardConfigured ? "可生成验证码" : "未配置"}</strong>
          <span>${identityConfigured ? "可用于交易确认" : "identity_secret 未完整"}</span>
        </div>
      </div>
      <div class="account-detail-tabs" role="tablist" aria-label="账号详情">
        <button type="button" class="account-detail-tab ${accountDetailTab === "basic" ? "active" : ""}" data-account-tab="basic">基本信息</button>
        <button type="button" class="account-detail-tab ${accountDetailTab === "guard" ? "active" : ""}" data-account-tab="guard">Steam 令牌</button>
        <button type="button" class="account-detail-tab ${accountDetailTab === "trade" ? "active" : ""}" data-account-tab="trade">任务配置</button>
      </div>
      <div class="account-detail-pane ${accountDetailTab === "basic" ? "active" : ""}" data-account-pane="basic">
        <section class="account-panel-section">
          <div class="account-section-header">
            <div>
              <h3>基础信息</h3>
              <p>用于识别账号、结算区域和自动化记录归属。</p>
            </div>
          </div>
          <div class="account-info-list">
            <div class="account-info-row"><span>Steam 用户名</span><strong class="mono">${escapeHtml(acc.username || "—")}</strong></div>
            <div class="account-info-row"><span>Steam ID</span><strong class="mono">${escapeHtml(acc.steam_id || "—")}</strong></div>
            <div class="account-info-row"><span>Steam 昵称</span><strong>${escapeHtml(acc.display_name || "—")}</strong></div>
            <div class="account-info-row"><span>账号备注</span><strong>${escapeHtml(accountNote || "—")}</strong></div>
            <div class="account-info-row"><span>Steam 余额</span><strong class="mono">${escapeHtml(balanceDetail)}</strong></div>
            <div class="account-info-row"><span>结算币种</span><strong class="mono">${escapeHtml(currencyLabel)}</strong></div>
            <div class="account-info-row"><span>地区</span><strong class="mono">${escapeHtml(regionLabel)}</strong></div>
          </div>
        </section>
        <div class="callout account-security-callout">
          <svg class="callout-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M12 9v4"></path><path d="M12 17h.01"></path><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
          </svg>
          <div class="callout-text"><strong>安全提示：</strong>Steam 密码、账号级 shared_secret、identity_secret 和 device_id 会以明文保存在本机 <span class="mono">config/accounts.json</span>；Steam/Buff Cookie 保存在 <span class="mono">config/credentials.json</span>。请勿把这些文件发给他人，建议定期更换密码。</div>
        </div>
      </div>
      <div class="account-detail-pane ${accountDetailTab === "guard" ? "active" : ""}" data-account-pane="guard">
        <section class="account-guard-section account-workbench-card">
          <div class="account-workbench-head">
            <div>
              <div class="account-workbench-title">Steam 令牌</div>
              <div class="account-workbench-desc">维护账号级 shared_secret；账号令牌会以明文保存在本机 config/accounts.json。</div>
            </div>
            <span class="account-guard-status ${guardConfigured ? "configured" : ""}" id="acct-guard-status-${escapeHtml(acc.id)}">${escapeHtml(guardLabel)}</span>
          </div>
          <div class="account-config-alert is-warning">请妥善保护 config/accounts.json；其中包含 Steam 密码和账号令牌明文。旧全局令牌兼容配置保存在 config/app_config.json。</div>
          <div class="account-guard-console">
            <button type="button" class="account-guard-code" id="acct-guard-code-${escapeHtml(acc.id)}" title="点击刷新验证码">
              <span class="guard-code-label">Steam Guard</span>
              <span class="guard-code-text">-----</span>
              <span class="guard-code-hint">${guardConfigured ? "点击刷新验证码" : "填写 shared_secret 后可生成验证码"}</span>
            </button>
          </div>
          ${guardRisk ? `<div class="account-config-alert is-warning">identity_secret 或 device_id 未完整，自动交易确认不可用。</div>` : ""}
          <div class="account-secret-grid">
            <div class="account-secret-field ${guardSecretReady ? "is-filled" : ""}">
              <div class="account-secret-label">
                <label for="acct-guard-shared">shared_secret</label>
                <span>${guardSecretReady ? "账号专属" : "继承全局"}</span>
              </div>
              <div class="account-secret-input">
                <input type="password" id="acct-guard-shared" value="${escapeHtml(guard.shared_secret || "")}" autocomplete="new-password" placeholder="留空继承全局" />
                <button type="button" class="account-icon-btn" data-secret-toggle="acct-guard-shared" title="显示或隐藏 shared_secret" aria-label="显示或隐藏 shared_secret">${accountIconSvg("eye")}</button>
              </div>
            </div>
            <div class="account-secret-field ${guard.identity_secret ? "is-filled" : ""}">
              <div class="account-secret-label">
                <label for="acct-guard-identity">identity_secret</label>
                <span>${guard.identity_secret ? "账号专属" : "继承全局"}</span>
              </div>
              <div class="account-secret-input">
                <input type="password" id="acct-guard-identity" value="${escapeHtml(guard.identity_secret || "")}" autocomplete="new-password" placeholder="留空继承全局" />
                <button type="button" class="account-icon-btn" data-secret-toggle="acct-guard-identity" title="显示或隐藏 identity_secret" aria-label="显示或隐藏 identity_secret">${accountIconSvg("eye")}</button>
              </div>
            </div>
            <div class="account-secret-field ${guard.device_id ? "is-filled" : ""}">
              <div class="account-secret-label">
                <label for="acct-guard-device">device_id</label>
                <span>${guard.device_id ? "账号专属" : "继承全局"}</span>
              </div>
              <div class="account-secret-input">
                <input type="text" id="acct-guard-device" value="${escapeHtml(guard.device_id || "")}" placeholder="android:..." />
                <button type="button" class="account-icon-btn" data-secret-copy="acct-guard-device" title="复制 device_id" aria-label="复制 device_id">${accountIconSvg("copy")}</button>
              </div>
            </div>
          </div>
          <div class="account-workbench-actions">
            <button type="button" class="btn btn-secondary btn-sm" id="btn-acct-guard-use-global" data-id="${escapeHtml(acc.id)}" ${accountGuardConfigured || guard.identity_secret || guard.device_id ? "" : "disabled"}>使用全局令牌</button>
            <button type="button" class="btn btn-primary btn-sm" id="btn-acct-guard-save" data-id="${escapeHtml(acc.id)}">保存令牌</button>
          </div>
        </section>
      </div>
      <div class="account-detail-pane ${accountDetailTab === "trade" ? "active" : ""}" data-account-pane="trade">
        <section class="account-trade-section account-workbench-card">
          <div class="account-workbench-head">
            <div>
              <div class="account-workbench-title">账号级策略</div>
              <div class="account-workbench-desc">${escapeHtml(tradeDirtyHint)}</div>
            </div>
            <div class="account-strategy-head-actions">
              <span class="account-status-pill ${tradeEnabled ? "is-success" : "is-muted"}">${escapeHtml(tradeLabel)}</span>
              <label class="account-toggle">
                <input type="checkbox" id="acct-trade-enabled" ${tradeEnabled ? "checked" : ""} />
                <span class="account-toggle-track" aria-hidden="true"></span>
                <span class="account-toggle-text">启用覆盖</span>
              </label>
            </div>
          </div>
          <div class="account-strategy-grid">
            <div class="account-strategy-field ${getTradeModeClass(tradeEnabled, trade, "target_balance")}" data-trade-field="target_balance">
              <div class="account-strategy-field-head">
                <label for="acct-trade-target-balance">目标余额</label>
                <span class="account-field-mode">${escapeHtml(tradeTargetMode)}</span>
              </div>
              <input type="number" id="acct-trade-target-balance" min="0" step="0.01" value="${escapeHtml(trade.target_balance ?? "")}" placeholder="${escapeHtml(getGlobalTradeHint("target_balance", acc))}" ${tradeEnabled ? "" : "disabled"} />
              <div class="account-field-hint">${escapeHtml(getGlobalTradeHint("target_balance", acc))}</div>
              <div class="account-field-error" id="acct-trade-target-error"></div>
            </div>
            <div class="account-strategy-field ${getTradeModeClass(tradeEnabled, trade, "max_discount")}" data-trade-field="max_discount">
              <div class="account-strategy-field-head">
                <label for="acct-trade-max-discount">最高折扣</label>
                <span class="account-field-mode">${escapeHtml(tradeDiscountMode)}</span>
              </div>
              <input type="number" id="acct-trade-max-discount" min="0.001" max="1" step="0.001" value="${escapeHtml(trade.max_discount ?? "")}" placeholder="${escapeHtml(getGlobalTradeHint("max_discount", acc))}" ${tradeEnabled ? "" : "disabled"} />
              <div class="account-field-hint">${escapeHtml(getGlobalTradeHint("max_discount", acc))}</div>
              <div class="account-field-error" id="acct-trade-discount-error"></div>
            </div>
            <div class="account-strategy-field ${getTradeModeClass(tradeEnabled, trade, "pay_method")}" data-trade-field="pay_method">
              <div class="account-strategy-field-head">
                <label for="acct-trade-pay-method">支付方式</label>
                <span class="account-field-mode">${escapeHtml(tradePayMode)}</span>
              </div>
              <select id="acct-trade-pay-method" ${tradeEnabled ? "" : "disabled"}>
                <option value="" ${!tradePayMethod ? "selected" : ""}>继承全局</option>
                <option value="alipay" ${tradePayMethod === "alipay" ? "selected" : ""}>支付宝</option>
                <option value="wechat" ${tradePayMethod === "wechat" ? "selected" : ""}>微信</option>
              </select>
              <div class="account-field-hint">${escapeHtml(getGlobalTradeHint("pay_method", acc))}</div>
            </div>
          </div>
          <div class="account-workbench-actions">
            <button type="button" class="btn btn-secondary btn-sm" id="btn-acct-trade-reset" data-id="${escapeHtml(acc.id)}">恢复全局</button>
            <button type="button" class="btn btn-primary btn-sm" id="btn-acct-trade-save" data-id="${escapeHtml(acc.id)}">保存配置</button>
          </div>
        </section>
      </div>
    </div>
  `;
  detail.querySelectorAll(".account-detail-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      accountDetailTab = btn.dataset.accountTab || "basic";
      renderAccountDetail(acc, currentId);
    });
  });
  detail.querySelector("#acct-trade-enabled")?.addEventListener("change", () => syncAccountTradeControls());
  detail.querySelectorAll("#acct-trade-target-balance, #acct-trade-max-discount, #acct-trade-pay-method").forEach((node) => {
    node.addEventListener("input", () => syncAccountTradeControls());
    node.addEventListener("change", () => syncAccountTradeControls());
  });
  detail.querySelector("#btn-acct-trade-save")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await saveAccountTradeConfig(id, e.currentTarget);
  });
  detail.querySelector("#btn-acct-trade-reset")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await resetAccountTradeConfig(id, e.currentTarget);
  });
  detail.querySelector("#btn-acct-guard-save")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await saveAccountGuard(id, e.currentTarget);
  });
  detail.querySelector("#btn-acct-guard-use-global")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await resetAccountGuardToGlobal(id, e.currentTarget);
  });
  detail.querySelector(".account-guard-code")?.addEventListener("click", async () => {
    await refreshAccountGuardCode(acc.id);
  });
  detail.querySelectorAll("[data-secret-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => toggleAccountSecret(btn));
  });
  detail.querySelectorAll("[data-secret-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => copyInputValue(btn.dataset.secretCopy));
  });
  detail.querySelector("#btn-acc-edit")?.addEventListener("click", (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) openAccountForm(id);
  });
  detail.querySelector("#btn-acc-sync")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await syncAccountBalance(id, e.currentTarget);
  });
  detail.querySelector("#btn-acc-del")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (!id) return;
    if (!confirm("确定删除此账号？")) return;
    try {
      const r = await fetchJson(API + "/accounts/" + id, { method: "DELETE" });
      if (r.ok) {
        toast("已删除");
        if (selectedAccountId === id) selectedAccountId = null;
        refreshAccounts();
      } else toast("删除失败", r.error || "");
    } catch (err) {
      toast("删除失败", err.message || "");
    }
  });
  detail.querySelector("#btn-acc-set-current")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (!id) return;
    try {
      const r = await fetchJson(API + "/accounts/" + id + "/set_current", { method: "POST" });
      if (r.ok) {
        toast("已切换当前账号");
        accountsCurrentId = id;
        refreshAccounts();
      } else toast("失败", r.error || "");
    } catch (err) {
      toast("失败", err.message || "");
    }
  });
  syncAccountTradeControls();
}

function setButtonLoading(btn, loadingText) {
  if (!btn) return () => {};
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = loadingText;
  return () => {
    btn.disabled = false;
    btn.textContent = originalText;
  };
}

function syncAccountTradeControls() {
  const enabled = !!el("acct-trade-enabled")?.checked;
  const fields = [
    { id: "acct-trade-target-balance", key: "target_balance", inherited: "跟随全局", custom: "账号覆盖" },
    { id: "acct-trade-max-discount", key: "max_discount", inherited: "跟随全局", custom: "账号覆盖" },
    { id: "acct-trade-pay-method", key: "pay_method", inherited: "继承全局", custom: "账号覆盖" },
  ];
  fields.forEach((item) => {
    const input = el(item.id);
    if (!input) return;
    input.disabled = !enabled;
    const wrap = input.closest(".account-strategy-field");
    const hasValue = String(input.value || "").trim() !== "";
    wrap?.classList.toggle("is-disabled", !enabled);
    wrap?.classList.toggle("is-custom", enabled && hasValue);
    wrap?.classList.toggle("is-inherited", !enabled || !hasValue);
    const mode = wrap?.querySelector(".account-field-mode");
    if (mode) mode.textContent = enabled && hasValue ? item.custom : item.inherited;
  });
}

function setFieldError(id, message) {
  const node = el(id);
  if (!node) return;
  node.textContent = message || "";
  node.classList.toggle("is-visible", !!message);
}

function toggleAccountSecret(btn) {
  const input = el(btn?.dataset?.secretToggle);
  if (!input) return;
  const hidden = input.type === "password";
  input.type = hidden ? "text" : "password";
  btn.innerHTML = accountIconSvg(hidden ? "eyeOff" : "eye");
  btn.setAttribute("aria-label", hidden ? "隐藏密钥" : "显示密钥");
  btn.setAttribute("title", hidden ? "隐藏密钥" : "显示密钥");
}

async function copyInputValue(inputId) {
  const value = (el(inputId)?.value || "").trim();
  if (!value) {
    toast("没有可复制的内容");
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    toast("已复制");
  } catch {
    toast("复制失败", "浏览器未允许剪贴板访问");
  }
}

async function syncAccountBalance(accountId, btn = null) {
  const origText = btn?.textContent;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "同步中…";
  }
  try {
    const r = await fetchJson(API + "/accounts/" + accountId + "/sync_balance", { method: "POST" });
    if (r.ok) {
      toast("同步完成");
      await refreshAccounts();
    } else {
      toast("同步失败", r.error || r.message || "请先验证账号 Cookie");
    }
  } catch (err) {
    toast("同步失败", err.message || "");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = origText || "同步";
    }
  }
}
async function openBrowserAndLogin() {
  try {
    const d = await fetchJson(API + "/auth/" + reloginType + "/relogin_start", { method: "POST" });
    if (d.ok) {
      if (d.novnc_url) {
        window.open(d.novnc_url, "_blank", "noopener");
        const msg = el("relogin-message");
        if (msg) {
          msg.innerHTML = `容器浏览器已打开。请在 <a href="${escapeHtml(d.novnc_url)}" target="_blank" rel="noopener">noVNC 页面</a> 完成登录，完成后点击下方按钮继续。`;
        }
      }
      toast("已打开浏览器", d.message || "");
      const btnOk = el("relogin-btn-ok");
      if (btnOk) btnOk.disabled = false;
    } else {
      toast("打开失败", d.error || "");
    }
  } catch (e) {
    toast("请求失败", e.message || "");
  }
}
async function finishRelogin(success) {
  const btnOk = el("relogin-btn-ok");
  const btnFail = el("relogin-btn-fail");
  const btnOpen = el("relogin-btn-open");
  if (btnOk) btnOk.disabled = true;
  if (btnFail) btnFail.disabled = true;
  if (btnOpen) btnOpen.disabled = true;
  const origText = btnOk?.textContent;
  if (btnOk) btnOk.textContent = "正在更新…";
  try {
    const d = await fetchJson(API + "/auth/" + reloginType + "/relogin_finish", {
      method: "POST",
      body: JSON.stringify({ success }),
    });
    hideReloginModal();
    if (success && d.ok) {
      toast("登录信息已更新");
      if (reloginType === "steam") {
        await refreshInventory(true);
        refreshAccounts();
      } else {
        await refreshStatus();
      }
    } else if (success && !d.ok) {
      toast("更新失败", d.error || "");
    }
  } catch (e) {
    hideReloginModal();
    toast("请求失败", e.message || "");
  } finally {
    if (btnOk) {
      btnOk.disabled = false;
      btnOk.textContent = origText || "完成登录";
    }
    if (btnFail) btnFail.disabled = false;
    if (btnOpen) btnOpen.disabled = false;
  }
}
function renderAccountsUI(accs, currentId) {
  const list = el("accounts-list");
  if (!list) return;
  const term = (accountsSearchTerm || "").trim().toLowerCase();
  const filtered = term
    ? accs.filter((a) => {
      const hay = [a.display_name, a.username, a.steam_id, a.account_note].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(term);
    })
    : accs;
  if (!selectedAccountId || !accs.some((x) => x.id === selectedAccountId)) {
    selectedAccountId = currentId || (filtered[0] ? filtered[0].id : null) || (accs[0] ? accs[0].id : null);
  }
  const guardCoverage = getAccountGuardCoverage(accs);
  const countText = term ? `${filtered.length} / ${accs.length}` : `${accs.length}`;
  const header = `
    <div class="accounts-list-header">
      <div class="title">账号列表</div>
      <div class="accounts-list-meta">
        <span class="count">${countText}</span>
        <span class="guard-coverage">令牌覆盖 ${guardCoverage.ready} / ${guardCoverage.total}</span>
      </div>
    </div>
  `;
  if (!filtered.length) {
    list.innerHTML = header + `<div class="accounts-list-empty">未找到匹配账号</div>`;
    renderAccountDetail(null, currentId);
    return;
  }
  const items = filtered
    .map((a) => {
      const name = getAccountName(a);
      const username = (a.username || "").trim();
      const note = (a.account_note || "").trim();
      const guardReady = !!(a.steam_guard_status || {}).resolved_configured;
      const accountGuard = !!(a.steam_guard_status || {}).account_configured;
      const tradeReady = (a.trade_config || {}).enabled === true;
      const guardLabel = accountGuard ? "账号令牌" : guardReady ? "全局令牌" : "未绑令牌";
      const targetBalance = getAccountTargetBalanceDisplay(a);
      const balanceDisplay = getAccountBalanceDisplay(a);
      const displayName = note ? `${name}（${note}）` : name;
      const isCurrent = a.id === currentId;
      const active = a.id === selectedAccountId;
      const avatar = buildAccountAvatar(name, a.avatar_url, 40);
      return `
        <div class="account-item ${active ? "active" : ""}" data-id="${escapeHtml(a.id)}" role="button" tabindex="0" title="${escapeHtml([name, username, a.steam_id].filter(Boolean).join(" · "))}">
          ${avatar}
          <div class="account-item-body">
            <div class="account-item-head">
              <div class="account-item-title">
                <span class="account-item-name">${escapeHtml(displayName)}</span>
                ${isCurrent ? '<span class="badge badge-current">当前</span>' : ""}
              </div>
              <div class="account-item-token">
                ${accountStatusPill(guardLabel, guardReady ? "success" : "warning")}
              </div>
            </div>
            <div class="account-item-line">
              <span>余额：<strong>${escapeHtml(balanceDisplay)}</strong></span>
              <span class="account-item-dot" aria-hidden="true"></span>
              <span>目标：<strong>${escapeHtml(targetBalance.value)}</strong></span>
            </div>
            ${tradeReady ? `<div class="account-item-extra">${accountStatusPill("账号级策略", "success")}</div>` : ""}
          </div>
        </div>
      `;
    })
    .join("");
  list.innerHTML = header + items;
  list.querySelectorAll(".account-item").forEach((node) => {
    const activate = () => {
      const id = node.dataset.id;
      if (!id) return;
      selectedAccountId = id;
      renderAccountsUI(accs, currentId);
    };
    node.addEventListener("click", activate);
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate();
      }
    });
  });
  renderAccountDetail(accs.find((x) => x.id === selectedAccountId) || filtered[0], currentId);
}
async function refreshAccounts() {
  const list = el("accounts-list");
  if (!list) return;
  try {
    const d = await fetchJson(API + "/accounts");
    accountsCache = d.accounts || [];
    accountsCurrentId = d.current_id || null;
    try {
      const cfg = await fetchJson(API + "/config");
      const config = cfg?.config || {};
      accountGlobalTargetBalance = parseFiniteNumber(config?.pipeline?.target_balance);
      accountGlobalStrategy = {
        targetBalance: accountGlobalTargetBalance,
        maxDiscount: parseFiniteNumber(config?.pipeline?.max_discount),
        payMethod: (config?.buff?.pay_method || "").toLowerCase(),
      };
    } catch {
      accountGlobalTargetBalance = null;
      accountGlobalStrategy = {};
    }
    // 同步账号存在标志，用于控制登录过期弹窗是否显示
    if (typeof _hasAnyAccount !== "undefined") {
      _hasAnyAccount = accountsCache.length > 0;
    }
    renderAccountsUI(accountsCache, accountsCurrentId);
    refreshAccountStatsState();
  } catch (e) {
    toast("加载失败", e.message || "");
    list.innerHTML = '<div style="padding:18px" class="text-muted">加载失败</div>';
    renderAccountDetail(null, null);
  }
}

async function saveAccountGuard(accountId, btn = null) {
  const shared = (el("acct-guard-shared")?.value || "").trim();
  const identity = (el("acct-guard-identity")?.value || "").trim();
  const device = (el("acct-guard-device")?.value || "").trim();
  const done = setButtonLoading(btn, "保存中…");
  try {
    const r = await fetchJson(API + "/accounts/" + accountId, {
      method: "PUT",
      body: JSON.stringify({
        steam_guard: {
          shared_secret: shared,
          identity_secret: identity,
          device_id: device,
        },
      }),
    });
    if (r.ok) {
      toast("令牌已保存");
      await refreshAccounts();
      await refreshAccountGuardCode(accountId);
    } else {
      toast("保存失败", r.error || "");
    }
  } catch (e) {
    toast("保存失败", e.message || "");
  } finally {
    done();
  }
}

async function resetAccountGuardToGlobal(accountId, btn = null) {
  if (!confirm("确定清空账号专属令牌并改用全局令牌？")) return;
  const done = setButtonLoading(btn, "处理中…");
  try {
    const r = await fetchJson(API + "/accounts/" + accountId, {
      method: "PUT",
      body: JSON.stringify({
        steam_guard: {
          shared_secret: "",
          identity_secret: "",
          device_id: "",
        },
      }),
    });
    if (r.ok) {
      toast("已改用全局令牌");
      await refreshAccounts();
    } else {
      toast("操作失败", r.error || "");
    }
  } catch (e) {
    toast("操作失败", e.message || "");
  } finally {
    done();
  }
}

async function saveAccountTradeConfig(accountId, btn = null) {
  const enabled = !!el("acct-trade-enabled")?.checked;
  const targetRaw = (el("acct-trade-target-balance")?.value || "").trim();
  const discountRaw = (el("acct-trade-max-discount")?.value || "").trim();
  const payMethod = (el("acct-trade-pay-method")?.value || "").trim();
  const targetBalance = targetRaw ? parseFloat(targetRaw) : NaN;
  const maxDiscount = discountRaw ? parseFloat(discountRaw) : NaN;
  setFieldError("acct-trade-target-error", "");
  setFieldError("acct-trade-discount-error", "");
  if (targetRaw && (!Number.isFinite(targetBalance) || targetBalance < 0)) {
    setFieldError("acct-trade-target-error", "目标余额必须为非负数字");
    return;
  }
  if (discountRaw && (!Number.isFinite(maxDiscount) || maxDiscount <= 0 || maxDiscount > 1)) {
    setFieldError("acct-trade-discount-error", "最高折扣需在 0 到 1 之间");
    return;
  }
  const tradeConfig = { enabled };
  if (Number.isFinite(targetBalance)) tradeConfig.target_balance = Math.round(targetBalance * 100) / 100;
  if (Number.isFinite(maxDiscount)) tradeConfig.max_discount = maxDiscount;
  if (payMethod) tradeConfig.pay_method = payMethod;
  const done = setButtonLoading(btn, "保存中…");
  try {
    const r = await fetchJson(API + "/accounts/" + accountId, {
      method: "PUT",
      body: JSON.stringify({ trade_config: tradeConfig }),
    });
    if (r.ok) {
      toast("任务配置已保存");
      await refreshAccounts();
    } else {
      toast("保存失败", r.error || "");
    }
  } catch (e) {
    toast("保存失败", e.message || "");
  } finally {
    done();
  }
}

async function resetAccountTradeConfig(accountId, btn = null) {
  const done = setButtonLoading(btn, "恢复中…");
  try {
    const r = await fetchJson(API + "/accounts/" + accountId, {
      method: "PUT",
      body: JSON.stringify({ trade_config: { enabled: false } }),
    });
    if (r.ok) {
      toast("已恢复全局策略");
      await refreshAccounts();
    } else {
      toast("恢复失败", r.error || "");
    }
  } catch (e) {
    toast("恢复失败", e.message || "");
  } finally {
    done();
  }
}

async function refreshAccountGuardCode(accountId) {
  const codeBox = document.getElementById("acct-guard-code-" + accountId);
  const codeEl = codeBox?.querySelector(".guard-code-text");
  const hintEl = codeBox?.querySelector(".guard-code-hint");
  if (codeEl) codeEl.textContent = "-----";
  if (hintEl) hintEl.textContent = "正在获取验证码…";
  try {
    const d = await fetchJson(API + "/steam_guard?account_id=" + encodeURIComponent(accountId));
    if (d.ok) {
      if (codeEl) codeEl.textContent = d.code || "-----";
      const remaining = d.server_time != null ? (d.period || 30) - (d.server_time % (d.period || 30)) : null;
      if (hintEl) hintEl.textContent = remaining != null ? `${remaining}s 后刷新` : "点击刷新验证码";
      toast("验证码已刷新");
    } else {
      if (hintEl) hintEl.textContent = d.error || "获取失败";
      toast("获取验证码失败", d.error || "");
    }
  } catch (e) {
    if (hintEl) hintEl.textContent = e.message || "获取失败";
    toast("获取验证码失败", e.message || "");
  }
}

function openAccountForm(editId = null) {
  accountEditId = editId;
  const title = el("account-form-title");
  const un = el("acc-username");
  const pw = el("acc-password");
  const note = el("acc-account-note");
  if (title) title.textContent = editId ? "编辑账号" : "添加账号";
  if (un) un.value = "";
  if (pw) pw.value = "";
  if (note) note.value = "";
  if (editId) {
    fetchJson(API + "/accounts").then((d) => {
      const a = (d.accounts || []).find((x) => x.id === editId);
      if (a) {
        if (un) un.value = a.username || "";
        if (pw) pw.placeholder = a.has_password ? "已保存，留空不修改" : "请补充密码，用于保活和交易";
        if (note) note.value = a.account_note || "";
      }
    }).catch(() => { });
  } else if (pw) pw.placeholder = "用于自动登录、保活和交易";
  const ov = el("account-form-overlay");
  if (ov) ov.classList.remove("hidden");
}
function closeAccountForm() {
  accountEditId = null;
  const ov = el("account-form-overlay");
  if (ov) ov.classList.add("hidden");
}
async function saveAccountForm() {
  const un = (el("acc-username")?.value || "").trim();
  const pw = (el("acc-password")?.value || "").trim();
  const note = (el("acc-account-note")?.value || "").trim();
  if (!un) {
    toast("请填写 Steam 用户名");
    return;
  }
  try {
    if (accountEditId) {
      const existing = (accountsCache || []).find((a) => a.id === accountEditId);
      if (!pw && existing && !existing.has_password) {
        toast("请填写 Steam 密码", "密码用于自动登录、Cookie 保活和交易恢复");
        return;
      }
      const body = { username: un, account_note: note };
      if (pw) body.password = pw;
      const r = await fetchJson(API + "/accounts/" + accountEditId, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      if (r.ok) { toast("已保存"); closeAccountForm(); refreshAccounts(); }
      else toast("保存失败", r.error || "");
    } else {
      if (!pw) {
        toast("请填写 Steam 密码", "密码用于自动登录、Cookie 保活和交易恢复");
        return;
      }
      const r = await fetchJson(API + "/accounts", {
        method: "POST",
        body: JSON.stringify({ username: un, password: pw, account_note: note, avatar_url: "" }),
      });
      if (r.ok) { toast("已添加"); closeAccountForm(); refreshAccounts(); }
      else toast("添加失败", r.error || "");
    }
  } catch (e) {
    toast("保存失败", e.message || "");
  }
}
