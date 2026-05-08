/* ═══════════════════════════════════════
   AetherSwap — Gap Closure JS
   Dashboard cards, log dedup, icon cache,
   payment timer
   ═══════════════════════════════════════ */

// --- Icon Cache (07) ---
const STEAM_ICON_BASE = 'https://community.akamai.steamstatic.com/economy/image/';
const ICON_CACHE_KEY = 'aetherswap_icon_cache';

function getIconUrl(iconPath, size) {
  if (!iconPath) return '';
  return STEAM_ICON_BASE + iconPath + '/' + (size || '96fx96f');
}
function getIconCache() {
  try { return JSON.parse(localStorage.getItem(ICON_CACHE_KEY) || '{}'); } catch { return {}; }
}
function setIconCache(map) {
  try {
    var existing = getIconCache();
    var merged = Object.assign({}, existing, map);
    localStorage.setItem(ICON_CACHE_KEY, JSON.stringify(merged));
  } catch {}
}
function getIconForName(name) {
  var cache = getIconCache();
  return cache[name] || cache[(name || '').split(' | ')[0]] || '';
}

// --- Dashboard Recent Trades (03 §1.3) ---
function updateDashRecentTrades(purchases) {
  var body = document.getElementById('dash-recent-trades-body');
  if (!body) return;
  var recent = (purchases || []).slice(0, 5);
  if (!recent.length) {
    body.innerHTML = '<div class="empty-state" style="padding:16px 0"><div class="empty-state-icon">📋</div><div class="empty-state-desc">暂无交易记录</div></div>';
    return;
  }
  var html = recent.map(function(t) {
    var name = (t.name || '—').toString();
    var price = t.price != null ? '¥' + Number(t.price).toFixed(2) : '—';
    var safeN = name.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return '<div class="dash-card-row"><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + safeN + '</span><span class="dash-card-value" style="font-size:12px">' + price + '</span></div>';
  }).join('');
  body.innerHTML = html;
}

// --- Dashboard Inventory Overview (03 §1.3) ---
function updateDashInventory(data) {
  var countEl = document.getElementById('dash-inv-count');
  var valueEl = document.getElementById('dash-inv-value');
  var taxEl = document.getElementById('dash-inv-after-tax');
  if (countEl && data.count != null) countEl.textContent = data.count;
  if (valueEl && data.value != null) valueEl.textContent = '¥' + Number(data.value).toFixed(2);
  if (taxEl && data.after_tax != null) taxEl.textContent = '¥' + Number(data.after_tax).toFixed(2);
}

// --- Dashboard Account Status (03 §1.3) ---
function updateDashAccountStatus(data) {
  var nameEl = document.getElementById('dash-acct-name');
  var cookieEl = document.getElementById('dash-acct-cookie');
  var buffEl = document.getElementById('dash-acct-buff');
  if (nameEl) nameEl.textContent = data.display_name || data.username || '—';
  if (cookieEl) {
    var cv = data.cookie_valid;
    cookieEl.textContent = cv === true ? '有效' : cv === false ? '过期' : '未知';
    cookieEl.className = 'badge ' + (cv === true ? 'badge-success' : cv === false ? 'badge-danger' : 'badge-info');
  }
  if (buffEl) {
    var bv = data.buff_valid;
    buffEl.textContent = bv === true ? '已登录' : bv === false ? '未登录' : '未知';
    buffEl.className = 'badge ' + (bv === true ? 'badge-success' : bv === false ? 'badge-danger' : 'badge-info');
  }
}

// --- Payment Timer (08 §4.5) ---
var _payTimerStart = null;
var _payTimerInterval = null;
function startPaymentTimer() {
  stopPaymentTimer();
  _payTimerStart = Date.now();
  var timerEl = document.getElementById('pay-timer');
  if (!timerEl) {
    // Create timer element next to the h3
    var h3 = document.querySelector('#pending-payment h3');
    if (h3) {
      timerEl = document.createElement('span');
      timerEl.id = 'pay-timer';
      timerEl.className = 'payment-timer';
      h3.parentNode.insertBefore(timerEl, h3.nextSibling);
    }
  }
  _payTimerInterval = setInterval(function() {
    var elapsed = Math.floor((Date.now() - _payTimerStart) / 1000);
    var min = String(Math.floor(elapsed / 60)).padStart(2, '0');
    var sec = String(elapsed % 60).padStart(2, '0');
    var el = document.getElementById('pay-timer');
    if (el) el.textContent = min + ':' + sec;
  }, 1000);
}
function stopPaymentTimer() {
  clearInterval(_payTimerInterval);
  _payTimerInterval = null;
  var el = document.getElementById('pay-timer');
  if (el) el.textContent = '';
}

// --- Log Dedup (04 §2.3) ---
var _lastLogMsg = '';
var _lastLogCount = 0;
var _lastLogSpan = null;

function resetLogDedupState() {
  _lastLogMsg = '';
  _lastLogCount = 0;
  _lastLogSpan = null;
}

function appendLogWithDedup(frag, line) {
  var msg = line.msg || '';
  var level = line.level || 'info';
  var time = line.t;

  if (msg === _lastLogMsg && _lastLogSpan) {
    _lastLogCount++;
    // Update the count badge on the existing span
    var badge = _lastLogSpan.querySelector('.log-repeat-badge');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'log-repeat-badge';
      badge.style.cssText = 'margin-left:8px;font-size:11px;padding:1px 6px;border-radius:999px;background:var(--bg-input);color:var(--text-muted);font-weight:600;';
      _lastLogSpan.appendChild(badge);
    }
    badge.textContent = '×' + _lastLogCount;
    return; // Don't add new line
  }

  // New unique message
  _lastLogMsg = msg;
  _lastLogCount = 1;

  var timeStr = '';
  if (time != null) {
    var d = new Date(time * 1000);
    timeStr = d.toTimeString().slice(0, 8);
  }

  var span = document.createElement('span');
  span.className = _levelClass(level);
  span.textContent = timeStr + ' [' + level + '] ' + msg;
  _lastLogSpan = span;

  if (frag.childNodes.length > 0) {
    frag.appendChild(document.createTextNode('\n'));
  }
  frag.appendChild(span);
}
