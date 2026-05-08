
function formatTimeHHMM(d = new Date()) {
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}
function tabSwitch(name) {
  console.log("tabSwitch called with name:", name);
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));

  const panel = el("panel-" + name);
  const btn = document.querySelector(`.nav-menu [data-tab="${name}"]`);
  console.log("tabSwitch found panel:", panel, "btn:", btn);
  if (panel) {
    panel.classList.add("active");
    panel.style.animation = 'none';
    panel.offsetHeight;
    panel.style.animation = '';
  }
  if (btn) btn.classList.add("active");
  if (name === "debug") refreshLog();
  if (name === "inventory") refreshInventory(false);
  if (name === "transactions") { refreshTransactions(); refreshAnalytics(); }
  // analytics refresh is now handled by txSubTabSwitch
  if (name === "accounts") refreshAccounts();
  if (name === "proxy") {
    loadProxyConfig();
  }
}

function settingsTabSwitch(name) {
  document.querySelectorAll('.settings-tab-pane').forEach(p => {
    p.classList.remove('active');
    p.style.display = 'none';
  });
  document.querySelectorAll('.settings-tab').forEach(b => b.classList.remove('active'));
  const pane = document.querySelector('.settings-tab-pane[data-settab-pane="' + name + '"]');
  const btn = document.querySelector('.settings-tab[data-settab="' + name + '"]');
  if (pane) {
    pane.style.display = 'block';
    pane.classList.add('active');
  }
  if (btn) btn.classList.add('active');
}

function txSubTabSwitch(name) {
  document.querySelectorAll('.tx-pane').forEach(p => {
    p.classList.remove('active');
    p.style.display = 'none';
  });
  document.querySelectorAll('.tx-sub-tab').forEach(b => b.classList.remove('active'));
  const pane = document.getElementById('tx-pane-' + name);
  const btn = document.querySelector('.tx-sub-tab[data-txsub="' + name + '"]');
  if (pane) {
    pane.style.display = 'block';
    pane.classList.add('active');
  }
  if (btn) btn.classList.add('active');
  // Trigger data refresh for the selected sub-tab
  if (name === 'purchases' || name === 'purchase-history') refreshTransactions();
  if (name === 'analytics') refreshAnalytics();
}

// --- Phase 2.1: Update dashboard extra stat cards ---
function updateDashboardExtras(data) {
  const heldEl = document.getElementById("stat-held-count");
  const pendingEl = document.getElementById("stat-pending-orders");
  if (heldEl && data.held_count != null) heldEl.textContent = data.held_count;
  if (pendingEl && data.pending_orders != null) pendingEl.textContent = data.pending_orders;
}

// --- Phase 2.5: Update payment item info ---
function updatePaymentItemInfo(p) {
  const nameEl = document.getElementById("pay-item-name");
  const priceEl = document.getElementById("pay-item-price");
  const iconEl = document.getElementById("pay-item-icon");
  if (nameEl) nameEl.textContent = p.name || "";
  // Price display: unit_price × num = total_price
  if (priceEl) {
    if (p.unit_price && p.num && p.num > 1) {
      priceEl.textContent = "¥" + Number(p.unit_price).toFixed(2) + " × " + p.num + " = ¥" + Number(p.total_price).toFixed(2);
    } else if (p.total_price) {
      priceEl.textContent = "¥" + Number(p.total_price).toFixed(2);
    } else if (p.unit_price) {
      priceEl.textContent = "¥" + Number(p.unit_price).toFixed(2);
    } else {
      priceEl.textContent = "";
    }
  }
  // Icon: try backend icon_url first, then cache lookup by name
  if (iconEl) {
    var iconSrc = "";
    if (p.icon_url && typeof getIconUrl === 'function') {
      iconSrc = getIconUrl(p.icon_url);
    } else if (typeof getIconForName === 'function') {
      var cached = getIconForName(p.steam_market_name || p.name);
      if (cached && typeof getIconUrl === 'function') iconSrc = getIconUrl(cached);
    }
    if (iconSrc) {
      iconEl.src = iconSrc;
      iconEl.style.display = "block";
    } else {
      iconEl.style.display = "none";
    }
  }
}

// --- Phase 2.6: Item icon helper (Steam CDN) ---
function getItemIconUrl(market_hash_name) {
  if (!market_hash_name) return "";
  // Use Steam community market image CDN
  const encoded = encodeURIComponent(market_hash_name);
  return "https://community.akamai.steamstatic.com/economy/image/class/730/" + encoded + "/128fx128f";
}

// --- Phase 3.3: Render empty state ---
function renderEmptyState(container, icon, title, desc) {
  if (!container) return;
  container.innerHTML = '<div class="empty-state">' +
    '<div class="empty-state-icon">' + (icon || "📭") + '</div>' +
    '<div class="empty-state-title">' + (title || "暂无数据") + '</div>' +
    '<div class="empty-state-desc">' + (desc || "") + '</div>' +
    '</div>';
}

let lastStatus = "idle";
let _lastLyricText = "";
let _lastReceiveText = "";
// 是否已配置账号的缓存标志（避免 popup 在未配置时误弹出）
let _hasAnyAccount = false;
function humanizeError(msg) {
  const text = String(msg || "");
  if (!text) return "";
  if (text.includes("steamLoginSecure")) return "Steam Cookie 缺少 steamLoginSecure，请手动重新登录 Steam。";
  if (text.includes("SSLEOFError") || text.includes("HTTPSConnectionPool")) return "Steam 网络连接失败，请检查加速器、代理或稍后重试。";
  if (text.includes("Max retries exceeded")) return "网络请求重试耗尽，请检查代理/加速器。";
  return text;
}

function pushLyricLine(text, subText) {
  if (text === _lastLyricText) return;
  _lastLyricText = text;
  const track = el("step-scroll-track");
  if (!track) return;
  const LINE_H = 24;
  const MAX_LINES = 10;
  const oldSub = track.querySelector(".lyric-sub");
  if (oldSub) oldSub.remove();
  const oldLines = track.querySelectorAll(".lyric-line");
  oldLines.forEach((l, i) => {
    const dist = oldLines.length - i;
    let op;
    if (dist === 1) op = 0.35;
    else if (dist === 2) op = 0.15;
    else op = 0.05;
    l.style.transition = "opacity 0.6s ease";
    l.style.opacity = String(op);
  });
  const line = document.createElement("div");
  line.className = "lyric-line";
  line.textContent = text;
  line.style.opacity = "0";
  line.style.transition = "none";
  track.appendChild(line);
  if (subText) {
    const sub = document.createElement("div");
    sub.className = "lyric-line lyric-sub";
    sub.textContent = subText;
    sub.style.opacity = "0";
    sub.style.transition = "none";
    track.appendChild(sub);
  }
  track.style.transition = "none";
  track.style.transform = `translateY(${LINE_H}px)`;
  void track.offsetHeight;
  track.style.transition = "";
  track.style.transform = "translateY(0)";
  requestAnimationFrame(() => {
    line.style.transition = "opacity 0.45s cubic-bezier(0.22, 1, 0.36, 1) 0.12s";
    line.style.opacity = "1";
    const newSub = track.querySelector(".lyric-sub");
    if (newSub) {
      newSub.style.transition = "opacity 0.45s cubic-bezier(0.22, 1, 0.36, 1) 0.2s";
      newSub.style.opacity = "0.3";
    }
  });
  while (track.querySelectorAll(".lyric-line:not(.lyric-sub)").length > MAX_LINES) {
    track.removeChild(track.firstChild);
  }
  const mainCount = track.querySelectorAll(".lyric-line:not(.lyric-sub)").length;
  if (mainCount > 1) {
    const vp = el("step-scroll-viewport");
    if (vp) vp.style.height = subText ? "72px" : "48px";
  }
}
async function refreshStatus() {
  try {
    const d = await fetchJson(API + "/status");
    if (d.buff_auth_expired && _hasAnyAccount) {
      showReloginModal("buff");
    }
    const raw = d.status || "idle";
    const statusText = raw === "running" ? "运行中" : raw === "error" ? "错误" : raw === "stopped" ? "已停止" : "空闲中";
    const top = el("status-text");
    if (top) top.textContent = statusText;
    const inline = el("status-text-inline");
    if (inline) inline.textContent = statusText;
    const stepDesc = d.step || "";
    const item = d.progress_item || "";
    const newText = stepDesc && item ? `${stepDesc}：${item}` : (stepDesc || item || "—");
    const nextItem = d.next_progress_item || "";
    const subText = stepDesc && nextItem ? `${stepDesc}：${nextItem}` : nextItem;
    pushLyricLine(newText, subText);
    if (d.receive && d.receive.message) {
      const receiveText = `收货：${d.receive.message}`;
      if (receiveText !== _lastReceiveText) {
        _lastReceiveText = receiveText;
        pushLyricLine(receiveText);
      }
    }
    if (d.price_meta && typeof handleMarketPriceMeta === "function") {
      handleMarketPriceMeta(d.price_meta);
    }
    const pill = el("status-pill");
    if (pill) {
      pill.classList.remove("status-idle", "status-running", "status-stopped", "status-error");
      pill.classList.add(raw === "running" ? "status-running" : raw === "error" ? "status-error" : raw === "stopped" ? "status-stopped" : "status-idle");
    }
    const controlBar = document.querySelector(".control-bar");
    if (controlBar) {
      if (raw === "running") controlBar.classList.add("running");
      else controlBar.classList.remove("running");
    }
    const lu = el("last-updated");
    if (lu) lu.textContent = formatTimeHHMM();
    if (raw !== lastStatus) {
      if (raw === "running") toast("已开始运行");
      if (raw === "stopped") toast("已停止");
      if (raw === "error") toast("发生错误", d.step || "请看调试日志");
      lastStatus = raw;
    }
  } catch {
  }
  try {
    const d = await fetchJson(API + "/pending_payment");
    const p = d.pending;
    const box = el("pending-payment");
    if (!box) return;
    if (p && p.pay_url) {
      box.classList.remove("hidden");
      if (typeof startPaymentTimer === 'function' && !_payTimerInterval) startPaymentTimer();
      const link = el("pay-link");
      if (link) {
        link.href = p.pay_url;
        link.textContent = p.pay_type === "wechat" ? "微信支付链接（可复制到浏览器）" : "打开支付链接";
      }
      const t = el("pay-type");
      if (t) t.textContent = p.name ? "订单: " + p.name : "";
      const ps = el("pay-status");
      if (ps) ps.textContent = p.order_id ? "订单号: " + p.order_id : "";
      box.dataset.payUrl = p.pay_url;
      box.dataset.paymentId = p.payment_id || "";
      updatePaymentItemInfo(p);
      // Payment ratio & volume badges (08)
      var ratioEl = el("pay-ratio");
      if (ratioEl) {
        var r = p.value_ratio || p.ratio;
        if (r) { ratioEl.textContent = "折扣 " + Number(r).toFixed(4); ratioEl.style.display = "inline-flex"; }
        else ratioEl.style.display = "none";
      }
      var volEl = el("pay-volume");
      if (volEl) {
        if (p.daily_volume) { volEl.textContent = "日成交 " + p.daily_volume; volEl.style.display = "inline-flex"; }
        else volEl.style.display = "none";
      }
      const qrWrap = el("pay-qrcode-wrap");
      const qrBox = el("pay-qrcode");
      if (p.pay_type === "wechat" && qrWrap && qrBox && typeof QRCode !== "undefined") {
        qrWrap.classList.remove("hidden");
        qrBox.innerHTML = "";
        try {
          new QRCode(qrBox, { text: p.pay_url, width: 200, height: 200 });
        } catch (e) {
          qrWrap.classList.add("hidden");
        }
      } else {
        if (qrWrap) qrWrap.classList.add("hidden");
        if (qrBox) qrBox.innerHTML = "";
      }
    } else {
      box.classList.add("hidden");
      box.dataset.payUrl = "";
      box.dataset.paymentId = "";
      if (typeof stopPaymentTimer === 'function') stopPaymentTimer();
      const qrWrap = el("pay-qrcode-wrap");
      const qrBox = el("pay-qrcode");
      if (qrWrap) qrWrap.classList.add("hidden");
      if (qrBox) qrBox.innerHTML = "";
    }
  } catch {
  }
  try {
    const s = await fetchJson(API + "/stats");
    const set = (id, text) => {
      const e = el(id);
      if (e) e.textContent = text;
    };
    animateValue(el("stat-total-purchased"), s.total_purchased ?? 0);
    animateValue(el("stat-total-sold"), s.total_sold ?? 0);
    const diffEl = el("stat-diff");
    if (s.total_profit != null) {
      animateValue(diffEl, s.total_profit);
      diffEl.classList.remove("text-ok", "text-bad");
      if (s.total_profit > 0) diffEl.classList.add("text-ok");
      else if (s.total_profit < 0) diffEl.classList.add("text-bad");
    } else set("stat-diff", "—");
    const ratioEl = el("stat-ratio");
    if (s.discount_ratio != null) {
      animateValue(ratioEl, s.discount_ratio, 400);
      const targetRatio = 0.85;
      ratioEl.classList.remove("text-ok", "text-bad");
      if (s.discount_ratio <= targetRatio) ratioEl.classList.add("text-ok");
      else ratioEl.classList.add("text-bad");
    } else set("stat-ratio", "—");
      updateDashboardExtras(s);
      // --- Dashboard info cards (03 §1.3) ---
      if (typeof updateDashRecentTrades === 'function' && s.recent_purchases) {
        updateDashRecentTrades(s.recent_purchases);
      }
      if (typeof updateDashInventory === 'function') {
        updateDashInventory({ count: s.inventory_count, value: s.inventory_value, after_tax: s.inventory_after_tax });
      }
      if (typeof updateDashAccountStatus === 'function' && s.account) {
        updateDashAccountStatus(s.account);
      }
  } catch {
  }
}
let reloginType = "steam";
let inventoryRefreshInFlight = false;
function showReloginModal(type, opts = {}) {
  reloginType = type || "steam";
  const overlay = el("relogin-overlay");
  if (overlay) overlay.classList.remove("hidden");
  const title = el("relogin-title");
  const msg = el("relogin-message");
  if (reloginType === "buff") {
    if (title) title.textContent = "Buff 登录已过期";
    if (msg) msg.textContent = "登录已过期，请在弹出的浏览器中重新登录 Buff，完成后点击下方按钮继续。";
  } else {
    if (title) title.textContent = "Steam 登录已过期";
    if (opts.reason === "need_2fa") {
      if (msg) msg.textContent = "需要二次验证（验证码），请点击下方按钮打开浏览器并完成 Steam 登录。";
    } else if (opts.error) {
      if (msg) msg.textContent = opts.error;
    } else {
      if (msg) msg.textContent = "登录已过期，请在弹出的浏览器中重新登录 Steam，完成后点击下方按钮继续。";
    }
  }
  const btnOk = el("relogin-btn-ok");
  if (btnOk) btnOk.disabled = false;
}
function hideReloginModal() {
  const overlay = el("relogin-overlay");
  if (overlay) overlay.classList.add("hidden");
}
async function refreshInventory(forceRefresh = true) {
  if (inventoryRefreshInFlight) return;
  inventoryRefreshInFlight = true;
  try {
    const d = await fetchJson(API + "/inventory" + (forceRefresh ? "?refresh=1" : ""));
    if (d.auth_expired && _hasAnyAccount) {
      showReloginModal("steam", { reason: d.auth_expired_reason, error: d.error });
      return;
    }
    const items = d.items || [];
    const note = el("inv-cache-note");
    if (note) {
      if (d.cached) {
        note.textContent = d.message || "当前显示缓存库存";
        note.classList.add("text-bad");
      } else {
        const ts = d.inventory_meta && d.inventory_meta.updated_at ? new Date(d.inventory_meta.updated_at * 1000) : null;
        note.textContent = ts && !isNaN(ts.getTime()) ? "实时刷新于 " + ts.toLocaleTimeString() : "";
        note.classList.remove("text-bad");
      }
    }
    const tbody = document.querySelector("#inv-table tbody");
    if (!tbody) return;
    let totalValue = 0;
    const rowHtmls = [];
    for (const it of items) {
      const sellHtml =
        `<span class="${it.can_sell ? "text-ok" : "text-bad"}">${it.can_sell ? "是" : "否"}</span>` +
        (it.marketable ? "" : ' <span class="text-bad">(不可上架)</span>');
      const tradeHtml =
        `<span class="${it.can_trade ? "text-ok" : "text-bad"}">${it.can_trade ? "是" : "否"}</span>` +
        (it.tradable ? "" : ' <span class="text-bad">(不可交易)</span>');
      const rawTime = it.cooldown_at_iso || it.cooldown_text || "";
      let displayTime = rawTime;
      if (it.cooldown_at_iso) {
        const d = new Date(it.cooldown_at_iso);
        if (!isNaN(d.getTime())) {
          displayTime = d.toLocaleString();
        }
      }
      const timeHtml = displayTime ? `<span class="text-bad">${escapeHtml(displayTime)}</span>` : "—";
      const lowest = Number(it.lowest_price) || 0;
      totalValue += lowest;
      const lowestStr = lowest > 0 ? lowest.toFixed(2) : "—";
      const mhn = (it.market_hash_name || it.name || "").trim();
      const steamUrl = mhn
        ? "https://steamcommunity.com/market/listings/730/" + encodeURIComponent(mhn)
        : "";
      const buffUrl = mhn
        ? "https://buff.163.com/market/csgo?tab=selling&search=" + encodeURIComponent(mhn)
        : "";
      const linksHtml = mhn
        ? `<a href="${steamUrl}" target="_blank" rel="noopener" class="link-steam">Steam</a> <a href="${buffUrl}" target="_blank" rel="noopener" class="link-buff">Buff</a>`
        : "—";
      const iconHtml = it.icon_url && typeof getIconUrl === 'function'
        ? `<img class="item-icon" src="${getIconUrl(it.icon_url)}" alt="" loading="lazy" onerror="this.style.display='none'" />`
        : '';
      rowHtmls.push(`
        <tr><td class="item-name-cell">${iconHtml}<span>${escapeHtml(it.name || "")}</span></td>
        <td class="inv-links">${linksHtml}</td>
        <td>${sellHtml}</td>
        <td>${tradeHtml}</td>
        <td>${timeHtml}</td>
        <td class="mono">${escapeHtml(lowestStr)}</td>
        <td class="muted small">${escapeHtml(it.cooldown_text || "")}</td></tr>
      `);
    }
    tbody.innerHTML = rowHtmls.join("");
    const c = el("inv-count");
    if (c) c.textContent = items.length;
    const v = el("inv-total-value");
    if (v) v.textContent = totalValue.toFixed(2);
    const taxEl = el("inv-tax-value");
    if (taxEl) taxEl.textContent = (totalValue / 1.15).toFixed(2);
    // Update icon cache (07)
    if (typeof setIconCache === 'function') {
      var iconMap = {};
      items.forEach(function(it) {
        var name = (it.market_hash_name || it.name || '').trim();
        if (name && it.icon_url) iconMap[name] = it.icon_url;
      });
      if (Object.keys(iconMap).length > 0) setIconCache(iconMap);
    }
  } catch (e) {
    toast("刷新库存失败", humanizeError(e.message) || "请检查 Steam Cookie");
  } finally {
    inventoryRefreshInFlight = false;
  }
}
async function refreshMarketPrices() {
  try {
    const d = await fetchJson(API + "/market-prices");
    if (typeof handleMarketPriceMeta === "function") handleMarketPriceMeta(d.price_meta);
    const prices = d.prices || {};
    const sources = d.sources || {};
    if (Object.keys(prices).length === 0) return;
    const invItems = getInventoryCache();
    if (invItems && invItems.length > 0) {
      let totalValue = 0;
      const tbody = document.querySelector("#inv-table tbody");
      if (tbody) {
        const rows = tbody.querySelectorAll("tr");
        rows.forEach((row) => {
          const nameTd = row.querySelector("td:first-child");
          const priceTd = row.querySelectorAll("td")[5];
          if (!nameTd || !priceTd) return;
          const name = nameTd.textContent.trim();
          const price = prices[name];
          if (price != null) {
            priceTd.textContent = price.toFixed(2);
          }
        });
        rows.forEach((row) => {
          const priceTd = row.querySelectorAll("td")[5];
          const v = parseFloat(priceTd?.textContent);
          if (!isNaN(v)) totalValue += v;
        });
        const v = el("inv-total-value");
        if (v) v.textContent = totalValue.toFixed(2);
        const taxEl = el("inv-tax-value");
        if (taxEl) taxEl.textContent = (totalValue / 1.15).toFixed(2);
      }
    }
    if (!lastEnrichData) {
      await refreshTransactions();
    }
    if (lastEnrichData && lastEnrichData.length > 0) {
      for (const t of lastEnrichData) {
        if (t.type !== "purchase" || t.sale_price != null) continue;
        const p = prices[t.name];
        if (p != null) {
          t.current_market_price = p;
          if (sources[t.name]) {
            t.current_market_price_source = sources[t.name];
            t.current_market_price_source_label = sources[t.name] === "steam_lowest" ? "最低价/中位价摘要" : "智能价";
          }
        }
      }
      lastEnrichTime = Date.now();
      refreshTransactions();
    }
  } catch (e) {
  }
}
function getInventoryCache() {
  const tbody = document.querySelector("#inv-table tbody");
  if (!tbody) return [];
  return Array.from(tbody.querySelectorAll("tr")).map((row) => {
    const tds = row.querySelectorAll("td");
    return { name: tds[0]?.textContent.trim() || "" };
  });
}
function aggregateByItemName(purchases, resellRatio = 0.85) {
  const ratio = Math.max(0.01, Math.min(1, Number(resellRatio) || 0.85));
  const byName = new Map();
  for (const t of purchases) {
    const name = (t.name || "—").toString();
    if (!byName.has(name)) {
      byName.set(name, {
        name,
        count: 0,
        totalPrice: 0,
        totalMp: 0,
        mpCount: 0,
        heldCount: 0,
        totalCurrentPrice: 0,
        currentPriceCount: 0,
        soldCount: 0,
        totalSalePrice: 0,
        totalDiscountRatio: 0,
        totalCashProfit: 0,
        totalDeviation: 0,
        totalSoldMp: 0,
        deviationCount: 0,
      });
    }
    const r = byName.get(name);
    r.count += 1;
    r.totalPrice += Number(t.price) || 0;
    if (t.market_price != null) {
      r.totalMp += Number(t.market_price);
      r.mpCount += 1;
    }
    const sold = t.sale_price != null && Number(t.sale_price) > 0;
    if (!sold) {
      r.heldCount += 1;
      if (t.current_market_price != null) {
        r.totalCurrentPrice += Number(t.current_market_price);
        r.currentPriceCount += 1;
      }
    }
    if (sold) {
      const saleP = Number(t.sale_price);
      const cost = Number(t.price) || 0;
      const mp = t.market_price != null ? Number(t.market_price) : 0;
      const afterTax = saleP / 1.15;
      r.soldCount += 1;
      r.totalSalePrice += saleP;
      if (afterTax > 0 && cost > 0) r.totalDiscountRatio += cost / afterTax;
      r.totalCashProfit += afterTax * ratio - cost;
      if (mp > 0) {
        r.totalDeviation += saleP - mp;
        r.totalSoldMp += mp;
        r.deviationCount += 1;
      }
    }
  }
  return Array.from(byName.values())
    .map((r) => ({
      name: r.name,
      count: r.count,
      heldCount: r.heldCount,
      soldCount: r.soldCount,
      avgPrice: r.count > 0 ? r.totalPrice / r.count : 0,
      avgMp: r.mpCount > 0 ? r.totalMp / r.mpCount : null,
      avgCurrentPrice: r.currentPriceCount > 0 ? r.totalCurrentPrice / r.currentPriceCount : null,
      totalSaleAmount: r.soldCount > 0 ? r.totalSalePrice : null,
      avgSalePrice: r.soldCount > 0 ? r.totalSalePrice / r.soldCount : null,
      avgDiscountRatio: r.soldCount > 0 && r.totalDiscountRatio > 0 ? r.totalDiscountRatio / r.soldCount : null,
      totalCashProfit: r.soldCount > 0 ? r.totalCashProfit : null,
      avgDeviation: r.deviationCount > 0 ? r.totalDeviation / r.deviationCount : null,
      avgDeviationPct: r.deviationCount > 0 && r.totalSoldMp > 0 ? (r.totalDeviation / r.totalSoldMp) * 100 : null,
    }))
    .sort((a, b) => b.count - a.count);
}
function refreshAnalytics(purchases, resellRatio) {
  const tbody = document.querySelector("#analytics-table tbody");
  if (!tbody) return;
  const render = (list, ratio) => {
    const rows = aggregateByItemName(list, ratio);
    const kpiWrap = el("analytics-kpis");
    const insightsWrap = el("analytics-insights");
    const fmtMoney = (value) => value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
    const fmtPlainMoney = (value) => value == null ? "—" : value.toFixed(2);
    const valueClass = (value) => value > 0 ? "text-ok" : value < 0 ? "text-bad" : "";
    const soldItems = list.filter((t) => t.sale_price != null && Number(t.sale_price) > 0);
    const heldItems = list.filter((t) => !(t.sale_price != null && Number(t.sale_price) > 0));
    const totalInvest = list.reduce((s, t) => s + (Number(t.price) || 0), 0);
    let realizedProfit = 0;
    let unrealizedProfit = 0;
    let heldPricedCount = 0;
    let winCount = 0;
    let discountSum = 0;
    let discountCount = 0;
    let deviationSum = 0;
    let deviationCount = 0;
    soldItems.forEach((t) => {
      const cost = Number(t.price) || 0;
      const sale = Number(t.sale_price) || 0;
      const afterTax = sale / 1.15;
      const cashProfit = afterTax * ratio - cost;
      realizedProfit += cashProfit;
      if (cashProfit > 0) winCount += 1;
      if (afterTax > 0 && cost > 0) {
        discountSum += cost / afterTax;
        discountCount += 1;
      }
      if (t.market_price != null) {
        deviationSum += sale - Number(t.market_price);
        deviationCount += 1;
      }
    });
    heldItems.forEach((t) => {
      const cost = Number(t.price) || 0;
      const cur = t.current_market_price != null ? Number(t.current_market_price) : null;
      if (cur != null && cur > 0) {
        unrealizedProfit += (cur / 1.15) * ratio - cost;
        heldPricedCount += 1;
      }
    });
    const winRate = soldItems.length ? `${((winCount / soldItems.length) * 100).toFixed(1)}%` : "—";
    const avgDiscount = discountCount ? (discountSum / discountCount).toFixed(4) : "—";
    const avgDeviation = deviationCount ? deviationSum / deviationCount : null;
    if (kpiWrap) {
      kpiWrap.innerHTML = [
        `<div class="analytics-kpi"><span class="analytics-kpi-label">总投入</span><span class="analytics-kpi-value mono">${fmtPlainMoney(totalInvest)}</span><span class="analytics-kpi-hint">${list.length} 条记录</span></div>`,
        `<div class="analytics-kpi"><span class="analytics-kpi-label">已实现变现收益</span><span class="analytics-kpi-value mono ${valueClass(soldItems.length ? realizedProfit : 0)}">${fmtMoney(soldItems.length ? realizedProfit : null)}</span><span class="analytics-kpi-hint">${soldItems.length} 条已出售</span></div>`,
        `<div class="analytics-kpi"><span class="analytics-kpi-label">未实现变现收益</span><span class="analytics-kpi-value mono ${valueClass(heldPricedCount ? unrealizedProfit : 0)}">${fmtMoney(heldPricedCount ? unrealizedProfit : null)}</span><span class="analytics-kpi-hint">${heldPricedCount} / ${heldItems.length} 条有现价</span></div>`,
        `<div class="analytics-kpi"><span class="analytics-kpi-label">胜率</span><span class="analytics-kpi-value mono">${winRate}</span><span class="analytics-kpi-hint">${winCount} / ${soldItems.length || 0} 盈利</span></div>`,
        `<div class="analytics-kpi"><span class="analytics-kpi-label">平均折扣比率</span><span class="analytics-kpi-value mono ${avgDiscount === "—" ? "" : (parseFloat(avgDiscount) > ratio ? "text-bad" : "text-ok")}">${avgDiscount}</span><span class="analytics-kpi-hint">越低越划算</span></div>`,
        `<div class="analytics-kpi"><span class="analytics-kpi-label">平均价格偏离</span><span class="analytics-kpi-value mono ${valueClass(avgDeviation || 0)}">${avgDeviation == null ? "—" : fmtMoney(avgDeviation)}</span><span class="analytics-kpi-hint">出售价相对购入市场价</span></div>`,
      ].join("");
    }
    const renderRankList = (items, emptyText, formatter) => {
      if (!items.length) return `<div class="analytics-rank-empty">${escapeHtml(emptyText)}</div>`;
      return `<ol class="analytics-rank-list">${items.map((item) => `<li><span class="analytics-rank-name">${typeof buildItemNameHtml === "function" ? buildItemNameHtml(item.name) : escapeHtml(item.name)}</span>${formatter(item)}</li>`).join("")}</ol>`;
    };
    if (insightsWrap) {
      const winners = rows.filter((r) => r.totalCashProfit != null).slice().sort((a, b) => b.totalCashProfit - a.totalCashProfit).slice(0, 3);
      const risks = rows.filter((r) => r.totalCashProfit != null).slice().sort((a, b) => a.totalCashProfit - b.totalCashProfit).slice(0, 3);
      const discounts = rows.filter((r) => r.avgDiscountRatio != null).slice().sort((a, b) => a.avgDiscountRatio - b.avgDiscountRatio).slice(0, 3);
      insightsWrap.innerHTML = [
        `<section class="analytics-insight-card"><div class="analytics-insight-title">收益排行</div>${renderRankList(winners, "暂无已出售记录", (r) => `<span class="analytics-rank-value mono ${valueClass(r.totalCashProfit)}">${fmtMoney(r.totalCashProfit)}</span>`)}</section>`,
        `<section class="analytics-insight-card"><div class="analytics-insight-title">风险排行</div>${renderRankList(risks, "暂无亏损样本", (r) => `<span class="analytics-rank-value mono ${valueClass(r.totalCashProfit)}">${fmtMoney(r.totalCashProfit)}</span>`)}</section>`,
        `<section class="analytics-insight-card"><div class="analytics-insight-title">折扣表现</div>${renderRankList(discounts, "暂无折扣样本", (r) => `<span class="analytics-rank-value mono ${r.avgDiscountRatio > ratio ? "text-bad" : "text-ok"}">${r.avgDiscountRatio.toFixed(4)}</span>`)}</section>`,
      ].join("");
    }
    const rowHtmls = rows.map((r) => {
      const nameHtml = typeof buildItemNameHtml === "function" ? buildItemNameHtml(r.name) : escapeHtml(r.name);
      const avgPriceStr = r.avgPrice > 0 ? r.avgPrice.toFixed(2) : "—";
      const avgMpStr = r.avgMp != null ? r.avgMp.toFixed(2) : "—";
      const avgCurrentStr = r.avgCurrentPrice != null ? r.avgCurrentPrice.toFixed(2) : "—";
      const totalSaleStr = r.totalSaleAmount != null ? r.totalSaleAmount.toFixed(2) : "—";
      const avgDiscountStr = r.avgDiscountRatio != null ? r.avgDiscountRatio.toFixed(4) : "—";
      const discountRatioClass = avgDiscountStr !== "—" ? (parseFloat(avgDiscountStr) > ratio ? "text-bad" : "text-ok") : "";
      const cashProfitStr = r.totalCashProfit != null ? (r.totalCashProfit >= 0 ? "+" : "") + r.totalCashProfit.toFixed(2) : "—";
      const cashClass = r.totalCashProfit != null ? (r.totalCashProfit > 0 ? "text-ok" : r.totalCashProfit < 0 ? "text-bad" : "") : "";
      const avgDevPctStr = r.avgDeviationPct != null ? (r.avgDeviationPct >= 0 ? "+" : "") + r.avgDeviationPct.toFixed(2) + "%" : "";
      const avgDevStr = r.avgDeviation != null ? (r.avgDeviation >= 0 ? "+" : "") + r.avgDeviation.toFixed(2) + (avgDevPctStr ? " (" + avgDevPctStr + ")" : "") : "—";
      const devClass = r.avgDeviation != null ? (r.avgDeviation > 0 ? "text-ok" : r.avgDeviation < 0 ? "text-bad" : "") : "";
      return `<tr><td>${nameHtml}</td><td class="mono">${r.heldCount}</td><td class="mono">${r.soldCount}</td><td class="mono">${avgPriceStr}</td><td class="mono">${avgMpStr}</td><td class="mono">${avgCurrentStr}</td><td class="mono">${totalSaleStr}</td><td class="mono ${discountRatioClass}">${avgDiscountStr}</td><td class="mono ${cashClass}">${cashProfitStr}</td><td class="mono ${devClass}">${avgDevStr}</td></tr>`;
    });
    tbody.innerHTML = rowHtmls.length
      ? rowHtmls.join("")
      : (typeof txTableStateRow === "function"
        ? txTableStateRow(10, "暂无收益分析", "有买入记录后会按物品汇总持仓、成交和收益。", "empty")
        : "<tr><td colspan='10' class='text-muted'>暂无数据</td></tr>");
  };
  if (purchases != null && resellRatio != null) {
    render(purchases, resellRatio);
  } else {
    if (typeof setTxTableLoading === "function") {
      setTxTableLoading(tbody, 10, "正在加载收益分析", "正在汇总交易记录。");
    }
    fetchJson(API + "/transactions?enrich_current_price=0")
      .then((d) => {
        const list = (d.transactions || []).filter((t) => t.type === "purchase");
        const ratio = Math.max(0.01, Math.min(1, Number(d.resell_ratio) || 0.85));
        render(list, ratio);
      })
      .catch((e) => {
        toast("加载数据分析失败", e.message || "");
        if (typeof setTxTableError === "function") {
          setTxTableError(tbody, 10, "收益分析加载失败", e.message || "请稍后重试。");
        } else {
          tbody.innerHTML = "<tr><td colspan='10' class='text-muted'>加载失败</td></tr>";
        }
      });
  }
}
async function copyPayLink() {
  const box = el("pending-payment");
  const url = box?.dataset.payUrl;
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    toast("已复制链接");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = url;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("已复制链接");
  }
}
function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => tabSwitch(btn.dataset.tab));
  });
  el("btn-quit")?.addEventListener("click", () => {
    if (confirm("确定要退出程序吗？退出后后台所有的交易、保活和查询任务都将停止。")) {
      fetchJson(API + "/system/shutdown", { method: "POST" })
        .then(() => {
          toast("程序正在退出", "您可以安全地直接关闭此窗口", 100000);
          setTimeout(() => window.close(), 1000);
        })
        .catch(() => {
          toast("程序已退出", "您可以安全地直接关闭此窗口", 100000);
        });
    }
  });
  el("btn-start")?.addEventListener("click", startPipeline);
  el("btn-stop")?.addEventListener("click", stopPipeline);
  el("btn-paid")?.addEventListener("click", () => confirmPayment(true));
  el("btn-fail")?.addEventListener("click", () => confirmPayment(false));
  el("btn-copy-pay")?.addEventListener("click", copyPayLink);
  el("btn-save-config")?.addEventListener("click", () =>
    saveConfigFromForm()
      .then(() => toast("设置已保存"))
      .catch((e) => toast("保存失败", e.message || "请稍后再试"))
  );
  el("btn-refresh-inventory")?.addEventListener("click", () => refreshInventory(true));
  el("btn-receive-now")?.addEventListener("click", async () => {
    const btn = el("btn-receive-now");
    const old = btn?.textContent;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "收货中…";
    }
    try {
      const r = await fetchJson(API + "/receive_now", { method: "POST" });
      if (r.ok) {
        toast("收货完成", r.received ? `处理 ${r.received} 个报价` : "没有新的待处理报价");
        refreshTransactions();
        refreshInventory(true);
        refreshStatus();
      } else {
        toast("收货失败", humanizeError(r.error) || "请看运行日志");
      }
    } catch (e) {
      toast("收货失败", humanizeError(e.message) || "请求异常");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = old || "立即收货";
      }
    }
  });
  el("btn-retry-smart-price")?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (typeof retrySmartMarketPrices === "function") retrySmartMarketPrices();
  });
  el("btn-clear-market-circuit")?.addEventListener("click", (e) => {
    e.stopPropagation();
    el("market-price-notice-more")?.classList.remove("open");
    el("btn-market-price-notice-more")?.setAttribute("aria-expanded", "false");
    if (typeof clearMarketPriceCircuit === "function") clearMarketPriceCircuit();
  });
  el("btn-market-price-notice-more")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const wrap = el("market-price-notice-more");
    const open = !wrap?.classList.contains("open");
    document.querySelectorAll(".tx-toolbar-more.open").forEach((node) => {
      node.classList.remove("open");
      node.querySelector("[aria-expanded]")?.setAttribute("aria-expanded", "false");
    });
    if (wrap) wrap.classList.toggle("open", open);
    e.currentTarget.setAttribute("aria-expanded", open ? "true" : "false");
  });
  el("btn-tx-more-actions")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const wrap = el("tx-more-actions");
    const open = !wrap?.classList.contains("open");
    document.querySelectorAll(".tx-toolbar-more.open").forEach((node) => node.classList.remove("open"));
    document.querySelectorAll(".market-price-notice-more.open").forEach((node) => {
      node.classList.remove("open");
      node.querySelector("[aria-expanded]")?.setAttribute("aria-expanded", "false");
    });
    if (wrap) wrap.classList.toggle("open", open);
    e.currentTarget.setAttribute("aria-expanded", open ? "true" : "false");
  });
  el("btn-holdings-column-sort")?.addEventListener("click", (e) => {
    e.stopPropagation();
    el("tx-more-actions")?.classList.remove("open");
    el("btn-tx-more-actions")?.setAttribute("aria-expanded", "false");
    if (typeof toggleHoldingsColumnSortMode === "function") toggleHoldingsColumnSortMode();
  });
  el("btn-holdings-column-sort-done")?.addEventListener("click", () => {
    if (typeof setHoldingsColumnSortMode === "function") setHoldingsColumnSortMode(false);
  });
  el("btn-holdings-column-sort-reset")?.addEventListener("click", () => {
    if (typeof resetHoldingsColumnOrderPreference === "function") resetHoldingsColumnOrderPreference();
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest(".tx-toolbar-more")) return;
    document.querySelectorAll(".tx-toolbar-more.open").forEach((node) => {
      node.classList.remove("open");
      node.querySelector("[aria-expanded]")?.setAttribute("aria-expanded", "false");
    });
    if (e.target.closest(".market-price-notice-more")) return;
    document.querySelectorAll(".market-price-notice-more.open").forEach((node) => {
      node.classList.remove("open");
      node.querySelector("[aria-expanded]")?.setAttribute("aria-expanded", "false");
    });
  });
  el("btn-open-add-purchase")?.addEventListener("click", () => {
    el("tx-more-actions")?.classList.remove("open");
    const trigger = el("btn-tx-more-actions");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    el("edit-tx-overlay")?.classList.add("hidden");
    el("sell-tx-overlay")?.classList.add("hidden");
    el("add-purchase-overlay")?.classList.remove("hidden");
    el("add-purchase-name")?.focus();
  });
  el("add-purchase-cancel")?.addEventListener("click", () => {
    el("add-purchase-overlay")?.classList.add("hidden");
  });
  el("btn-refresh-market-price")?.addEventListener("click", () => {
    if (typeof retrySmartMarketPrices === "function") retrySmartMarketPrices();
  });
  el("market-price-notice")?.addEventListener("click", (e) => {
    if (e.target.closest(".market-price-notice-actions")) return;
    if (typeof retrySmartMarketPrices === "function") retrySmartMarketPrices();
  });
  el("market-price-notice")?.addEventListener("keydown", (e) => {
    if (e.target.closest(".market-price-notice-actions")) return;
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    if (typeof retrySmartMarketPrices === "function") retrySmartMarketPrices();
  });
  el("btn-add-account")?.addEventListener("click", () => openAccountForm());
  el("holdings-filter-search")?.addEventListener("input", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("holdings-filter-status")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("holdings-filter-price")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("holdings-sort-by")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("holdings-sort-dir")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("btn-reset-holdings-filter")?.addEventListener("click", () => {
    const search = el("holdings-filter-search");
    const status = el("holdings-filter-status");
    const price = el("holdings-filter-price");
    const sortBy = el("holdings-sort-by");
    const sortDir = el("holdings-sort-dir");
    if (search) search.value = "";
    if (status) status.value = "all";
    if (price) price.value = "all";
    if (sortBy) sortBy.value = "time";
    if (sortDir) sortDir.value = "desc";
    if (typeof resetHoldingsColumnOrderPreference === "function") resetHoldingsColumnOrderPreference();
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("accounts-search")?.addEventListener("input", (e) => {
    accountsSearchTerm = e.target?.value || "";
    renderAccountsUI(accountsCache || [], accountsCurrentId);
  });
  el("account-form-cancel")?.addEventListener("click", closeAccountForm);
  el("account-form-save")?.addEventListener("click", () => saveAccountForm());
  el("btn-add-purchase")?.addEventListener("click", async () => {
    const nameEl = el("add-purchase-name");
    const steamLinkEl = el("add-purchase-steam-link");
    const priceEl = el("add-purchase-price");
    const qtyEl = el("add-purchase-quantity");
    const goodsIdEl = el("add-purchase-goods-id");
    const name = (nameEl?.value || "").trim();
    const steamLink = (steamLinkEl?.value || "").trim();
    const price = priceEl ? parseFloat(priceEl.value) : NaN;
    let qty = qtyEl ? parseInt(qtyEl.value, 10) : 1;
    if (!name && !steamLink) {
      toast("请填写物品名称或 Steam 市场链接");
      return;
    }
    if (!Number.isFinite(price) || price <= 0) {
      toast("请填写有效价格");
      return;
    }
    if (!Number.isFinite(qty) || qty < 1) qty = 1;
    const goodsId = goodsIdEl?.value ? parseInt(goodsIdEl.value, 10) : null;
    if (goodsId !== null && isNaN(goodsId)) {
      toast("goods_id 须为数字");
      return;
    }
    try {
      const payload = { name, price, quantity: qty };
      if (steamLink) payload.steam_link = steamLink;
      if (goodsId != null && goodsId > 0) payload.goods_id = goodsId;
      const res = await fetchJson(API + "/purchase", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (res.ok !== true) {
        toast(res.error || "添加失败");
        return;
      }
      if (nameEl) nameEl.value = "";
      if (steamLinkEl) steamLinkEl.value = "";
      if (priceEl) priceEl.value = "";
      if (qtyEl) qtyEl.value = "1";
      if (goodsIdEl) goodsIdEl.value = "";
      el("add-purchase-overlay")?.classList.add("hidden");
      await refreshTransactions();
      refreshStatus();
      toast(res.added > 1 ? `已添加 ${res.added} 条操作记录` : "已添加操作记录");
    } catch (e) {
      toast("添加失败", e.message || "");
    }
  });
  el("btn-clear-log")?.addEventListener("click", clearLog);
  el("btn-toggle-pause")?.addEventListener("click", togglePause);
  el("btn-toggle-scroll")?.addEventListener("click", toggleAutoScroll);
  el("btn-download-log")?.addEventListener("click", downloadLog);
  el("btn-export-log")?.addEventListener("click", exportLog);
  el("log-search")?.addEventListener("input", () => renderLogFull());
  el("log-level")?.addEventListener("change", () => renderLogFull());
  el("cfg-verbose-debug")?.addEventListener("change", async () => {
    try {
      const d = await fetchJson(API + "/config");
      const cfg = d.config || {};
      const pipeline = { ...(cfg.pipeline || {}), verbose_debug: el("cfg-verbose-debug").checked };
      await fetchJson(API + "/config", { method: "POST", body: JSON.stringify({ config: { ...cfg, pipeline } }) });
      toast("已保存", "详细调试 " + (el("cfg-verbose-debug").checked ? "已开启，下次运行生效" : "已关闭"));
    } catch (e) {
      toast("保存失败", e.message || "");
    }
  });
  el("cfg-steam-listings-debug")?.addEventListener("change", async () => {
    try {
      const d = await fetchJson(API + "/config");
      const cfg = d.config || {};
      const pipeline = { ...(cfg.pipeline || {}), steam_listings_debug: el("cfg-steam-listings-debug").checked };
      await fetchJson(API + "/config", { method: "POST", body: JSON.stringify({ config: { ...cfg, pipeline } }) });
      toast("已保存", "Steam 在售/历史调试 " + (el("cfg-steam-listings-debug").checked ? "已开启" : "已关闭"));
    } catch (e) {
      toast("保存失败", e.message || "");
    }
  });
  el("btn-export-config")?.addEventListener("click", exportConfig);
  el("btn-data-init")?.addEventListener("click", async () => {
    if (!confirm("警告：此操作将清空所有数据（包括交易记录、自动挂刀配置、绑定的 Steam 账号与凭据）！\n\n您确定要进行“恢复出厂设置”吗？")) {
      return;
    }
    if (!confirm("再次确认：数据一旦清空将无法恢复。是否继续？")) {
      return;
    }
    try {
      await fetchJson(API + "/data/init", { method: "POST" });
      await refreshStatus();
      toast("数据已清空，即将刷新页面");
      setTimeout(() => location.reload(), 1500);
    } catch (e) {
      toast("初始化失败", e.message || "");
    }
  });
  el("btn-sync-sold")?.addEventListener("click", async () => {
    const btn = el("btn-sync-sold");
    if (btn?.disabled) return;
    btn.disabled = true;
    el("tx-more-actions")?.classList.remove("open");
    toast("正在同步持仓状态", "请稍候…");
    try {
      const r = await fetchJson(API + "/sync_sold_from_history", { method: "POST" });
      if (r.ok) {
        await refreshTransactions();
        await refreshStatus();
        const u = r.updated ?? 0;
        const f = r.filled ?? 0;
        if (u || f) toast("同步持仓状态完成", `售出更新 ${u} 条，填充 assetid ${f} 条`);
        else toast("同步持仓状态完成", "无变更");
      } else {
        toast("同步持仓状态失败", r.error || "接口请求未返回 error 字段");
      }
    } catch (e) {
      toast("同步持仓状态失败", e.message || "请求异常");
    } finally {
      btn.disabled = false;
    }
  });
  el("btn-repair-error")?.addEventListener("click", async () => {
    const btn = el("btn-repair-error");
    if (btn?.disabled) return;
    btn.disabled = true;
    toast("正在紧急修复", "请稍候…");
    try {
      const r = await fetchJson(API + "/repair_error_records", { method: "POST" });
      if (r.ok) {
        await refreshTransactions();
        await refreshStatus();
        const filled = r.filled ?? 0;
        const missing = r.missing ?? 0;
        const total = r.total ?? 0;
        if (missing === 0) toast("紧急修复成功", `已填入 ${filled}/${total} 条，状态已更新`);
        else toast("紧急修复完成", `填入 ${filled}/${total} 条，未填入 ${missing} 条`);
      } else {
        toast("紧急修复失败", r.error || "接口未返回 error 字段");
      }
    } catch (e) {
      toast("紧急修复失败", e.message || "请求异常");
    } finally {
      btn.disabled = false;
    }
  });
  el("cfg-import-file")?.addEventListener("change", (e) => importConfigFromFile(e.target.files?.[0]));
  el("relogin-btn-open")?.addEventListener("click", openBrowserAndLogin);
  el("relogin-btn-ok")?.addEventListener("click", () => finishRelogin(true));
  el("relogin-btn-fail")?.addEventListener("click", () => finishRelogin(false));
  el("edit-tx-cancel")?.addEventListener("click", () => {
    el("edit-tx-overlay")?.classList.add("hidden");
    delete el("edit-tx-overlay")?.dataset.editType;
    delete el("edit-tx-overlay")?.dataset.editIdx;
  });
  el("edit-tx-save")?.addEventListener("click", async () => {
    const ov = el("edit-tx-overlay");
    const type = ov?.dataset?.editType;
    const idx = parseInt(ov?.dataset?.editIdx ?? "", 10);
    if (!type || !Number.isFinite(idx)) return;
    const nameVal = (el("edit-tx-name")?.value ?? "").trim();
    const priceVal = el("edit-tx-price")?.value;
    const price = priceVal ? parseFloat(priceVal) : NaN;
    const goodsIdVal = el("edit-tx-goods-id")?.value;
    const goodsId = goodsIdVal ? parseInt(goodsIdVal, 10) : null;
    const marketPriceVal = el("edit-tx-market-price")?.value;
    const marketPrice = marketPriceVal ? parseFloat(marketPriceVal) : null;
    if (!Number.isFinite(price) || price <= 0) {
      toast("请输入有效金额");
      return;
    }
    const payload = { type, idx, name: nameVal || null, price: Math.round(price * 100) / 100 };
    if (goodsId !== null && !isNaN(goodsId)) payload.goods_id = goodsId;
    if (type === "purchase") {
      payload.market_price = (marketPriceVal === "" || !Number.isFinite(marketPrice) || marketPrice < 0) ? 0 : Math.round(marketPrice * 100) / 100;
      const assetidVal = (el("edit-tx-assetid")?.value ?? "").trim();
      payload.assetid = assetidVal || null;
      const statusVal = el("edit-tx-listing")?.value;
      payload.pending_receipt = statusVal === "2";
      payload.listing = statusVal === "1";
    }
    try {
      const r = await fetchJson(API + "/transaction", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        ov.classList.add("hidden");
        delete ov.dataset.editType;
        delete ov.dataset.editIdx;
        refreshTransactions();
        toast("已保存");
      } else {
        toast("保存失败", r.error || "");
      }
    } catch (e) {
      toast("保存失败", e.message || "");
    }
  });
  el("sell-tx-cancel")?.addEventListener("click", () => {
    el("sell-tx-overlay")?.classList.add("hidden");
    delete el("sell-tx-overlay")?.dataset.sellIdx;
    delete el("sell-tx-overlay")?.dataset.sellIdxList;
  });
  el("sell-tx-confirm")?.addEventListener("click", async () => {
    const ov = el("sell-tx-overlay");
    const priceVal = el("sell-tx-price")?.value;
    const salePrice = priceVal ? parseFloat(priceVal) : NaN;
    if (!Number.isFinite(salePrice) || salePrice <= 0) {
      toast("请输入有效售出价格");
      return;
    }
    const listJson = ov?.dataset?.sellIdxList;
    const idxList = listJson ? (() => { try { return JSON.parse(listJson); } catch { return []; } })() : null;
    const singleIdx = listJson ? null : parseInt(ov?.dataset?.sellIdx ?? "", 10);
    const indices = Array.isArray(idxList) && idxList.length ? idxList : (Number.isFinite(singleIdx) ? [singleIdx] : []);
    if (!indices.length) return;
    try {
      const priceRounded = Math.round(salePrice * 100) / 100;
      for (const idx of indices) {
        const r = await fetchJson(API + "/transaction", {
          method: "PUT",
          body: JSON.stringify({ type: "purchase", idx, sale_price: priceRounded }),
        });
        if (!r.ok) {
          toast("操作失败", r.error || "");
          return;
        }
      }
      ov.classList.add("hidden");
      delete ov.dataset.sellIdx;
      delete ov.dataset.sellIdxList;
      if (indices.length > 1) holdingsMultiSelectMode = false;
      refreshTransactions();
      toast(indices.length > 1 ? `已批量记录售出 ${indices.length} 条` : "已记录售出");
    } catch (e) {
      toast("操作失败", e.message || "");
    }
  });
  el("btn-holdings-multiselect")?.addEventListener("click", () => {
    holdingsMultiSelectMode = !holdingsMultiSelectMode;
    if (typeof syncHoldingsMultiSelectUI === "function") syncHoldingsMultiSelectUI();
  });
  el("btn-holdings-toggle-cols")?.addEventListener("click", () => {
    setHoldingsShowMoreColumns(!holdingsShowMoreColumns);
  });
  el("btn-history-multiselect")?.addEventListener("click", () => {
    historyMultiSelectMode = !historyMultiSelectMode;
    if (typeof syncHistoryMultiSelectUI === "function") syncHistoryMultiSelectUI();
  });
  el("btn-history-toggle-cols")?.addEventListener("click", () => {
    setHistoryShowMoreColumns(!historyShowMoreColumns);
  });
  el("history-filter-search")?.addEventListener("input", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("history-filter-status")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("history-filter-period")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("history-sort-by")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("history-sort-dir")?.addEventListener("change", () => {
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("btn-reset-history-filter")?.addEventListener("click", () => {
    const search = el("history-filter-search");
    if (search) search.value = "";
    const status = el("history-filter-status");
    if (status) status.value = "all";
    const period = el("history-filter-period");
    if (period) period.value = "all";
    const sortBy = el("history-sort-by");
    if (sortBy) sortBy.value = "time";
    const sortDir = el("history-sort-dir");
    if (sortDir) sortDir.value = "desc";
    if (typeof rerenderTransactionsFromCache === "function") rerenderTransactionsFromCache();
  });
  el("btn-history-batch-del")?.addEventListener("click", async () => {
    const checked = document.querySelectorAll("#transactions-table-purchase-history .history-checkbox:checked");
    const indices = Array.from(checked).map((cb) => parseInt(cb.dataset.idx, 10)).filter((n) => Number.isFinite(n));
    if (!indices.length) {
      toast("请先勾选要删除的项");
      return;
    }
    if (!confirm(`确定删除所选 ${indices.length} 条记录？删除后将从持有饰品与操作记录中同时移除。`)) return;
    try {
      const sorted = indices.slice().sort((a, b) => b - a);
      for (const idx of sorted) {
        const r = await fetchJson(API + "/transaction?" + new URLSearchParams({ type: "purchase", idx }), { method: "DELETE" });
        if (!r.ok) { toast("删除失败", r.error || ""); return; }
      }
      historyMultiSelectMode = false;
      refreshTransactions();
      toast(`已删除 ${indices.length} 条`);
    } catch (e) {
      toast("删除失败", e.message || "");
    }
  });
  el("btn-holdings-batch-del")?.addEventListener("click", async () => {
    const checked = document.querySelectorAll("#transactions-table-purchases .holding-checkbox:checked");
    const indices = Array.from(checked).map((cb) => parseInt(cb.dataset.idx, 10)).filter((n) => Number.isFinite(n));
    if (!indices.length) {
      toast("请先勾选要删除的项");
      return;
    }
    if (!confirm(`确定删除所选 ${indices.length} 条记录？`)) return;
    try {
      const sorted = indices.slice().sort((a, b) => b - a);
      for (const idx of sorted) {
        const r = await fetchJson(API + "/transaction?" + new URLSearchParams({ type: "purchase", idx }), { method: "DELETE" });
        if (!r.ok) { toast("删除失败", r.error || ""); return; }
      }
      holdingsMultiSelectMode = false;
      refreshTransactions();
      toast(`已删除 ${indices.length} 条`);
    } catch (e) {
      toast("删除失败", e.message || "");
    }
  });
  el("btn-holdings-batch-sell")?.addEventListener("click", () => {
    const checked = document.querySelectorAll("#transactions-table-purchases .holding-checkbox:checked");
    const indices = Array.from(checked).map((cb) => parseInt(cb.dataset.idx, 10)).filter((n) => Number.isFinite(n));
    if (!indices.length) {
      toast("请先勾选要售出的项");
      return;
    }
    const nameEl = el("sell-tx-item-name");
    if (nameEl) nameEl.textContent = `批量售出（共 ${indices.length} 条）`;
    el("sell-tx-overlay").dataset.sellIdxList = JSON.stringify(indices);
    delete el("sell-tx-overlay").dataset.sellIdx;
    el("sell-tx-price").value = "";
    el("sell-tx-overlay").classList.remove("hidden");
    el("sell-tx-price")?.focus();
  });
  el("btn-theme")?.addEventListener("click", () => Theme.cycle());
}
function setupScrollToTop() {
  const btn = el("scroll-top-btn");
  if (!btn) return;
  const panels = document.querySelectorAll(".panel");
  const check = () => {
    const active = document.querySelector(".panel.active");
    if (active && active.scrollTop > 200) btn.classList.add("visible");
    else btn.classList.remove("visible");
  };
  panels.forEach(p => p.addEventListener("scroll", check, { passive: true }));
  btn.addEventListener("click", () => {
    const active = document.querySelector(".panel.active");
    if (active) active.scrollTo({ top: 0, behavior: "smooth" });
  });
}
function setupKeyboardShortcuts() {
  const tabMap = { "1": "auto", "2": "transactions", "3": "inventory", "4": "accounts", "5": "proxy", "6": "steam-deals", "7": "gift", "8": "settings", "9": "debug" };
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT" || e.target.isContentEditable) return;
    if (e.altKey && tabMap[e.key]) {
      e.preventDefault();
      tabSwitch(tabMap[e.key]);
      const text = document.querySelector(`[data-tab="${tabMap[e.key]}"] span`)?.innerText;
      if (text) el("page-title-display").innerText = text;
    }
  });
}
async function init() {
  Theme.apply(Theme.get());
  if (window.matchMedia) {
    const mm = window.matchMedia("(prefers-color-scheme: dark)");
    mm.addEventListener?.("change", () => {
      if (Theme.get() === "system") Theme.apply("system");
    });
  }
  if (typeof _updateScrollBtn === "function") _updateScrollBtn();
  bindEvents();
  setupScrollToTop();
  setupKeyboardShortcuts();
  setupButtonInteractions();
  bindUXEvents();

  let wizardShown = false;
  try {
    // 提前执行新手引导检查，避免被后续可能超时的库存请求阻塞
    wizardShown = await checkAndShowOnboardingWizard();
  } catch (e) {
    console.warn("Failed to check onboarding wizard:", e);
  }

  try {
    await loadConfig();
    await loadProxyConfig();
  } catch (e) {
    toast("加载配置失败", e.message || "请检查后端是否可用");
  }

  if (wizardShown) {
    // 如果弹出了引导，则不对无配置的 Steam 发起可能超时的库存请求，仅设置自动刷新
    setupInventoryAutoRefresh();
  } else {
    // 异步加载库存，避免因 Steam 网络问题阻塞页面其余部分的初始化和展示
    refreshInventory(true).then(() => {
      setupInventoryAutoRefresh();
    });
  }

  await refreshStatus();
  setInterval(refreshStatus, 2000);
  setInterval(() => {
    if (document.querySelector("#panel-debug.active")) refreshLog();
  }, 1500);
}
function setupButtonInteractions() {
  document.addEventListener('mousemove', (e) => {
    const btn = e.target.closest('.btn');
    if (btn) {
      const rect = btn.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width * 100).toFixed(0);
      const y = ((e.clientY - rect.top) / rect.height * 100).toFixed(0);
      btn.style.setProperty('--ripple-x', x + '%');
      btn.style.setProperty('--ripple-y', y + '%');
    }
  });
}
init();
