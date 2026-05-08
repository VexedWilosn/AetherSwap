
let accountsCache = [];
let accountsCurrentId = null;
let selectedAccountId = null;
let accountEditId = null;
let accountsSearchTerm = '';
let accountDetailTab = "basic";
let accountStatsCache = null;
let accountGlobalTargetBalance = null;
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
        <button type="button" class="btn btn-secondary btn-sm" id="btn-acc-verify" data-id="${escapeHtml(acc.id)}">验证</button>
        <button type="button" class="btn btn-secondary btn-sm" id="btn-acc-sync-balance" data-id="${escapeHtml(acc.id)}">同步余额</button>
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
            <div class="account-info-row"><span>结算币种</span><strong class="mono">${escapeHtml(currencyLabel)}</strong></div>
            <div class="account-info-row"><span>地区</span><strong class="mono">${escapeHtml(regionLabel)}</strong></div>
          </div>
        </section>
        <div class="callout account-security-callout">
          <svg class="callout-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M12 9v4"></path><path d="M12 17h.01"></path><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
          </svg>
          <div class="callout-text"><strong>安全提示：</strong>保存的密码仅用于自动填充登录。建议定期更换密码，并为账号配置 Steam 令牌。</div>
        </div>
      </div>
      <div class="account-detail-pane ${accountDetailTab === "guard" ? "active" : ""}" data-account-pane="guard">
        <div class="account-guard-section account-panel-section">
          <div class="account-section-header">
            <div>
              <h3>Steam 令牌</h3>
              <p>维护账号级 shared_secret，必要时用于自动登录和交易确认。</p>
            </div>
            <span class="account-guard-status ${guardConfigured ? "configured" : ""}" id="acct-guard-status-${escapeHtml(acc.id)}">${escapeHtml(guardLabel)}</span>
          </div>
          <div class="account-guard-code" id="acct-guard-code-${escapeHtml(acc.id)}" title="点击复制令牌">
            <span class="guard-code-text">-----</span>
            <span class="guard-code-hint">${guardConfigured ? "点击刷新验证码" : "填写 shared_secret 后可生成验证码"}</span>
          </div>
          <div class="account-guard-fields">
            <div class="field">
              <label>shared_secret</label>
              <input type="password" id="acct-guard-shared" value="${escapeHtml(guard.shared_secret || "")}" autocomplete="new-password" />
            </div>
            <div class="field">
              <label>identity_secret</label>
              <input type="password" id="acct-guard-identity" value="${escapeHtml(guard.identity_secret || "")}" autocomplete="new-password" />
            </div>
            <div class="field">
              <label>device_id</label>
              <input type="text" id="acct-guard-device" value="${escapeHtml(guard.device_id || "")}" placeholder="android:..." />
            </div>
          </div>
          <div class="account-guard-actions">
            <button type="button" class="btn btn-secondary btn-sm" id="btn-acct-guard-refresh" data-id="${escapeHtml(acc.id)}">刷新验证码</button>
            <button type="button" class="btn btn-primary btn-sm" id="btn-acct-guard-save" data-id="${escapeHtml(acc.id)}">保存令牌</button>
          </div>
        </div>
      </div>
      <div class="account-detail-pane ${accountDetailTab === "trade" ? "active" : ""}" data-account-pane="trade">
        <div class="account-trade-section account-panel-section">
          <div class="account-trade-header">
            <div>
              <div class="account-trade-title">账号级策略</div>
              <div class="account-trade-desc">预留给多账号策略覆盖；当前启动任务仍以系统设置中的全局策略为准。</div>
            </div>
            <label class="account-trade-switch">
              <input type="checkbox" id="acct-trade-enabled" ${tradeEnabled ? "checked" : ""} />
              <span>启用账号级策略</span>
            </label>
          </div>
          <div class="account-trade-grid">
            <div class="field">
              <label>目标余额</label>
              <input type="number" id="acct-trade-target-balance" min="0" step="0.01" value="${escapeHtml(trade.target_balance ?? "")}" placeholder="继承全局" />
            </div>
            <div class="field">
              <label>最高折扣</label>
              <input type="number" id="acct-trade-max-discount" min="0.001" max="1" step="0.001" value="${escapeHtml(trade.max_discount ?? "")}" placeholder="继承全局" />
            </div>
            <div class="field">
              <label>支付方式</label>
              <select id="acct-trade-pay-method">
                <option value="" ${!tradePayMethod ? "selected" : ""}>继承全局</option>
                <option value="alipay" ${tradePayMethod === "alipay" ? "selected" : ""}>支付宝</option>
                <option value="wechat" ${tradePayMethod === "wechat" ? "selected" : ""}>微信</option>
              </select>
            </div>
          </div>
          <div class="account-trade-actions">
            <button type="button" class="btn btn-primary btn-sm" id="btn-acct-trade-save" data-id="${escapeHtml(acc.id)}">保存配置</button>
          </div>
        </div>
      </div>
    </div>
  `;
  detail.querySelectorAll(".account-detail-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      accountDetailTab = btn.dataset.accountTab || "basic";
      renderAccountDetail(acc, currentId);
    });
  });
  detail.querySelector("#btn-acct-trade-save")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await saveAccountTradeConfig(id);
  });
  detail.querySelector("#btn-acct-guard-save")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await saveAccountGuard(id);
  });
  detail.querySelector("#btn-acct-guard-refresh")?.addEventListener("click", async (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) await refreshAccountGuardCode(id);
  });
  detail.querySelector(".account-guard-code")?.addEventListener("click", async () => {
    await refreshAccountGuardCode(acc.id);
  });
  detail.querySelector("#btn-acc-edit")?.addEventListener("click", (e) => {
    const id = e.currentTarget?.dataset?.id;
    if (id) openAccountForm(id);
  });
  detail.querySelector("#btn-acc-sync-balance")?.addEventListener("click", async (e) => {
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
  detail.querySelector("#btn-acc-verify")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const id = btn?.dataset?.id;
    if (!id) return;
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "验证中…";
    try {
      const r = await fetchJson(API + "/accounts/" + id + "/verify", { method: "POST" });
      if (r.ok) {
        toast("验证通过", r.message || "可自动登录");
        refreshAccounts();
      } else if (r.status === "need_2fa") {
        toast(r.message || "需要二次验证");
        showReloginModal("steam");
        const btnOpen = el("relogin-btn-open");
        if (btnOpen) btnOpen.click();
      } else {
        toast("验证未通过", typeof humanizeError === "function" ? humanizeError(r.message) : (r.message || "请检查账号密码"));
      }
    } catch (err) {
      toast("验证失败", typeof humanizeError === "function" ? humanizeError(err.message) : (err.message || ""));
    } finally {
      btn.disabled = false;
      btn.textContent = origText || "验证";
    }
  });
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
      toast("余额已同步");
      await refreshAccounts();
    } else {
      toast("同步失败", r.error || r.message || "请先验证账号 Cookie");
    }
  } catch (err) {
    toast("同步失败", err.message || "");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = origText || "同步余额";
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
      accountGlobalTargetBalance = parseFiniteNumber(cfg?.config?.pipeline?.target_balance);
    } catch {
      accountGlobalTargetBalance = null;
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

async function saveAccountGuard(accountId) {
  const shared = (el("acct-guard-shared")?.value || "").trim();
  const identity = (el("acct-guard-identity")?.value || "").trim();
  const device = (el("acct-guard-device")?.value || "").trim();
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
  }
}

async function saveAccountTradeConfig(accountId) {
  const enabled = !!el("acct-trade-enabled")?.checked;
  const targetRaw = (el("acct-trade-target-balance")?.value || "").trim();
  const discountRaw = (el("acct-trade-max-discount")?.value || "").trim();
  const payMethod = (el("acct-trade-pay-method")?.value || "").trim();
  const targetBalance = targetRaw ? parseFloat(targetRaw) : NaN;
  const maxDiscount = discountRaw ? parseFloat(discountRaw) : NaN;
  if (targetRaw && (!Number.isFinite(targetBalance) || targetBalance < 0)) {
    toast("保存失败", "目标余额必须为非负数字");
    return;
  }
  if (discountRaw && (!Number.isFinite(maxDiscount) || maxDiscount <= 0 || maxDiscount > 1)) {
    toast("保存失败", "最高折扣需在 0 到 1 之间");
    return;
  }
  const tradeConfig = { enabled };
  if (Number.isFinite(targetBalance)) tradeConfig.target_balance = Math.round(targetBalance * 100) / 100;
  if (Number.isFinite(maxDiscount)) tradeConfig.max_discount = maxDiscount;
  if (payMethod) tradeConfig.pay_method = payMethod;
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
  }
}

async function refreshAccountGuardCode(accountId) {
  const codeBox = document.getElementById("acct-guard-code-" + accountId);
  const codeEl = codeBox?.querySelector(".guard-code-text");
  const hintEl = codeBox?.querySelector(".guard-code-hint");
  if (codeEl) codeEl.textContent = "-----";
  try {
    const d = await fetchJson(API + "/steam_guard?account_id=" + encodeURIComponent(accountId));
    if (d.ok) {
      if (codeEl) codeEl.textContent = d.code || "-----";
      const remaining = d.server_time != null ? (d.period || 30) - (d.server_time % (d.period || 30)) : null;
      if (hintEl) hintEl.textContent = remaining != null ? `${remaining}s 后刷新` : "点击复制验证码";
      try {
        await navigator.clipboard.writeText(d.code || "");
        toast("验证码已复制", d.code || "");
      } catch {
        toast("验证码", d.code || "");
      }
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
        if (pw) pw.placeholder = "已保存，留空不修改";
        if (note) note.value = a.account_note || "";
      }
    }).catch(() => { });
  } else if (pw) pw.placeholder = "保存后仅用于自动填充";
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
  try {
    if (accountEditId) {
      const body = { username: un, account_note: note };
      if (pw) body.password = pw;
      const r = await fetchJson(API + "/accounts/" + accountEditId, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      if (r.ok) { toast("已保存"); closeAccountForm(); refreshAccounts(); }
      else toast("保存失败", r.error || "");
    } else {
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
