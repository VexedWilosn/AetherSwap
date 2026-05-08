
let accountsCache = [];
let accountsCurrentId = null;
let selectedAccountId = null;
let accountEditId = null;
let accountsSearchTerm = '';
let accountDetailTab = "basic";
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
  const name = acc.display_name || acc.username || acc.steam_id || "未命名";
  const meta = [acc.username, acc.steam_id].filter(Boolean).join(" · ") || "—";
  const accountNote = (acc.account_note || "").trim();
  const avatar = buildAccountAvatar(name, acc.avatar_url, 56);
  const currency = (acc.currency_code || "").toUpperCase();
  let currencyLabel = currency || "—";
  if (currency === "CNY") currencyLabel = "人民币 (CNY)";
  else if (currency === "HKD") currencyLabel = "港币 (HKD)";
  else if (currency === "USD") currencyLabel = "美元 (USD)";
  else if (currency === "INR") currencyLabel = "印度卢比 (INR)";
  else if (currency === "RUB") currencyLabel = "卢布 (RUB)";
  else if (currency === "EUR") currencyLabel = "欧元 (EUR)";
  const region = (acc.region_code || "").toUpperCase();
  let regionLabel = region || "—";
  if (region === "CN") regionLabel = "中国 (CN)";
  else if (region === "HK") regionLabel = "中国香港 (HK)";
  else if (region === "US") regionLabel = "美国 (US)";
  else if (region === "IN") regionLabel = "印度 (IN)";
  else if (region === "RU") regionLabel = "俄罗斯 (RU)";
  else if (region === "EU") regionLabel = "欧元区 (EU)";
  const guard = acc.steam_guard || {};
  const guardStatus = acc.steam_guard_status || {};
  const guardConfigured = !!guardStatus.resolved_configured;
  const accountGuardConfigured = !!guardStatus.account_configured;
  const trade = acc.trade_config || {};
  const tradeEnabled = trade.enabled === true;
  const tradePayMethod = trade.pay_method || "";
  if (!["basic", "guard", "trade"].includes(accountDetailTab)) accountDetailTab = "basic";
  detail.innerHTML = `
    <div class="account-detail-header">
      <div class="account-detail-main">
        ${avatar}
        <div style="min-width:0">
          <div class="account-detail-title">${escapeHtml(name)} ${isCurrent ? '<span class="badge badge-current">当前</span>' : ""}</div>
          <div class="account-detail-meta">${escapeHtml(meta)}</div>
        </div>
      </div>
      <div class="account-detail-actions">
        <button type="button" class="btn btn-secondary btn-sm" id="btn-acc-verify" data-id="${escapeHtml(acc.id)}">验证</button>
        ${!isCurrent ? `<button type="button" class="btn btn-primary btn-sm" id="btn-acc-set-current" data-id="${escapeHtml(acc.id)}">设为当前</button>` : ""}
        <button type="button" class="btn btn-edit btn-sm" id="btn-acc-edit" data-id="${escapeHtml(acc.id)}">编辑</button>
        <button type="button" class="btn btn-danger-outline btn-sm" id="btn-acc-del" data-id="${escapeHtml(acc.id)}">删除</button>
      </div>
    </div>
    <div class="account-detail-body">
      <div class="account-detail-tabs" role="tablist" aria-label="账号详情">
        <button type="button" class="account-detail-tab ${accountDetailTab === "basic" ? "active" : ""}" data-account-tab="basic">基本信息</button>
        <button type="button" class="account-detail-tab ${accountDetailTab === "guard" ? "active" : ""}" data-account-tab="guard">Steam 令牌</button>
        <button type="button" class="account-detail-tab ${accountDetailTab === "trade" ? "active" : ""}" data-account-tab="trade">任务配置</button>
      </div>
      <div class="account-detail-pane ${accountDetailTab === "basic" ? "active" : ""}" data-account-pane="basic">
        <div class="kv-grid">
          <div class="kv"><div class="k">Steam 用户名</div><div class="v mono">${escapeHtml(acc.username || "—")}</div></div>
          <div class="kv"><div class="k">Steam ID</div><div class="v mono">${escapeHtml(acc.steam_id || "—")}</div></div>
          <div class="kv"><div class="k">Steam 昵称</div><div class="v">${escapeHtml(acc.display_name || "—")}</div></div>
          <div class="kv"><div class="k">账号备注</div><div class="v">${escapeHtml(accountNote || "-")}</div></div>
          <div class="kv"><div class="k">头像</div><div class="v">${acc.avatar_url ? "已获取" : "未获取"}</div></div>
          <div class="kv"><div class="k">结算币种</div><div class="v mono">${escapeHtml(currencyLabel)}</div></div>
          <div class="kv"><div class="k">地区</div><div class="v mono">${escapeHtml(regionLabel)}</div></div>
        </div>
        <div class="callout">
          <svg class="callout-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M12 9v4"></path><path d="M12 17h.01"></path><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
          </svg>
          <div class="callout-text"><strong>安全提示：</strong>若你选择保存密码，仅用于自动填充登录。建议系统环境保持可信，定期更换密码并开启 Steam 令牌等二次验证。</div>
        </div>
      </div>
      <div class="account-detail-pane ${accountDetailTab === "guard" ? "active" : ""}" data-account-pane="guard">
        <div class="account-guard-section">
          <div class="account-guard-header">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
            <span>Steam 令牌</span>
            <span class="account-guard-status ${guardConfigured ? "configured" : ""}" id="acct-guard-status-${escapeHtml(acc.id)}">${accountGuardConfigured ? "账号级" : guardConfigured ? "使用全局" : "未配置"}</span>
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
        <div class="account-trade-section">
          <div class="account-trade-header">
            <div>
              <div class="account-trade-title">交易任务配置</div>
              <div class="account-trade-desc">为多账号任务预留的账号级配置；当前主流程仍以全局设置为准。</div>
            </div>
            <label class="account-trade-switch">
              <input type="checkbox" id="acct-trade-enabled" ${tradeEnabled ? "checked" : ""} />
              <span>启用自动交易</span>
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
  const header = `
    <div class="accounts-list-header">
      <div class="title">账号列表</div>
      <div class="count">${filtered.length}${term ? ` / ${accs.length}` : ""}</div>
    </div>
  `;
  if (!filtered.length) {
    list.innerHTML = header + `<div style="padding:18px" class="text-muted">未找到匹配账号</div>`;
    renderAccountDetail(null, currentId);
    return;
  }
  const items = filtered
    .map((a) => {
      const name = a.display_name || a.username || a.steam_id || "未命名";
      const currency = (a.currency_code || "").toUpperCase();
      const region = (a.region_code || "").toUpperCase();
      const extras = [];
      if (currency) extras.push(currency);
      if (region) extras.push(region);
      const subMain = [a.username, a.steam_id].filter(Boolean).join(" · ") || "—";
      const sub = extras.length ? `${subMain} · ${extras.join(" / ")}` : subMain;
      const isCurrent = a.id === currentId;
      const active = a.id === selectedAccountId;
      const avatar = buildAccountAvatar(name, a.avatar_url, 40);
      return `
        <div class="account-item ${active ? "active" : ""}" data-id="${escapeHtml(a.id)}" role="button" tabindex="0">
          ${avatar}
          <div class="account-item-body">
            <div class="account-item-title">${escapeHtml(name)} ${isCurrent ? '<span class="badge badge-current">当前</span>' : ""}</div>
            <div class="account-item-sub">${escapeHtml(sub)}</div>
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
    // 同步账号存在标志，用于控制登录过期弹窗是否显示
    if (typeof _hasAnyAccount !== "undefined") {
      _hasAnyAccount = accountsCache.length > 0;
    }
    renderAccountsUI(accountsCache, accountsCurrentId);
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
  const sid = el("acc-steam-id");
  const dn = el("acc-display-name");
  const note = el("acc-account-note");
  if (title) title.textContent = editId ? "编辑账号" : "添加账号";
  if (un) un.value = "";
  if (pw) pw.value = "";
  if (sid) sid.value = "";
  if (dn) dn.value = "";
  if (note) note.value = "";
  if (editId) {
    const accs = [];
    fetchJson(API + "/accounts").then((d) => {
      const a = (d.accounts || []).find((x) => x.id === editId);
      if (a) {
        if (un) un.value = a.username || "";
        if (pw) pw.placeholder = "已保存，留空不修改";
        if (sid) sid.value = a.steam_id || "";
        if (dn) dn.value = a.display_name || "";
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
  const sid = (el("acc-steam-id")?.value || "").trim();
  const dn = (el("acc-display-name")?.value || "").trim();
  const note = (el("acc-account-note")?.value || "").trim();
  try {
    if (accountEditId) {
      const body = { username: un, steam_id: sid, display_name: dn, account_note: note };
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
        body: JSON.stringify({ username: un, password: pw, steam_id: sid, display_name: dn, account_note: note, avatar_url: "" }),
      });
      if (r.ok) { toast("已添加"); closeAccountForm(); refreshAccounts(); }
      else toast("添加失败", r.error || "");
    }
  } catch (e) {
    toast("保存失败", e.message || "");
  }
}
