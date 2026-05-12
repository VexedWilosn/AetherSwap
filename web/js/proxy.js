let _proxyList = [];
let _proxyRuntimeMap = {};
let _platformProxyPools = { steam: [], buff: [], uuyp: [] };

async function loadProxyConfig() {
  try {
    const d = await fetchJson(API + "/proxy/config");
    const cfg = d.proxy_pool || {};
    _proxyList = normalizeProxyEntries(cfg.global_proxies || cfg.proxies || []);
    _platformProxyPools = {
      steam: normalizeProxyEntries(cfg.steam_proxies || []),
      buff: normalizeProxyEntries(cfg.buff_proxies || []),
      uuyp: normalizeProxyEntries(cfg.uuyp_proxies || []),
    };
    selectStrategy(cfg.strategy ?? 1);

    const testUrlEl = el("proxy-test-url");
    if (testUrlEl) testUrlEl.value = cfg.test_url || "https://ipv4.webshare.io/";
    const timeoutEl = el("proxy-timeout");
    if (timeoutEl) timeoutEl.value = cfg.timeout_seconds ?? 10;
    const wsKeyEl = el("proxy-webshare-apikey");
    if (wsKeyEl) wsKeyEl.value = cfg.webshare_api_key || "";
    setProxyTextarea("proxy-steam-proxies", _platformProxyPools.steam);
    setProxyTextarea("proxy-buff-proxies", _platformProxyPools.buff);
    setProxyTextarea("proxy-uuyp-proxies", _platformProxyPools.uuyp);

    await loadProxyRuntimeStatus();
    renderProxyList();
  } catch (e) {
    toast("加载代理配置失败", e.message || "请检查后端接口");
  }
}

function normalizeProxyEntries(entries) {
  return (entries || []).map((entry) => {
    if (typeof entry === "string") return parseProxyLine(entry);
    return { ...entry };
  }).filter((p) => p && p.host && p.port);
}

function parseProxyLine(line) {
  const raw = String(line || "").trim();
  if (!raw) return null;
  try {
    if (raw.includes("://")) {
      const url = new URL(raw);
      return {
        host: url.hostname,
        port: Number(url.port),
        username: decodeURIComponent(url.username || ""),
        password: decodeURIComponent(url.password || ""),
      };
    }
  } catch {}
  const parts = raw.split(":");
  const host = parts[0]?.trim() || "";
  const port = parseInt(parts[1] || "0", 10);
  if (!host || Number.isNaN(port)) return null;
  return {
    host,
    port,
    username: parts[2]?.trim() || "",
    password: parts[3]?.trim() || "",
  };
}

function proxyEntryToLine(p) {
  const base = `${p.host}:${p.port}`;
  if (p.username || p.password) return `${base}:${p.username || ""}:${p.password || ""}`;
  return base;
}

function setProxyTextarea(id, entries) {
  const node = el(id);
  if (node) node.value = (entries || []).map(proxyEntryToLine).join("\n");
}

function readProxyTextarea(id) {
  const raw = el(id)?.value || "";
  return raw.split(/[\n\r]+/).map((line) => parseProxyLine(line)).filter(Boolean);
}

function proxyResultKey(result) {
  return `${result.pool || "global"}:${result.host}:${result.port}:${result.username || ""}`;
}

async function loadProxyRuntimeStatus() {
  _proxyRuntimeMap = {};
  try {
    const d = await fetchJson(API + "/proxy/status");
    const pool = d.proxy_pool || {};
    (pool.nodes || []).forEach((node) => {
      _proxyRuntimeMap[`${node.host}:${node.port}`] = node;
    });
  } catch (e) {
    console.warn("proxy runtime status unavailable", e);
  }
}

function renderProxyList(testResults) {
  const tbody = el("proxy-list-tbody");
  if (!tbody) return;
  const resultMap = {};
  (testResults || []).filter((r) => (r.pool || "global") === "global").forEach((r) => {
    resultMap[proxyResultKey(r)] = r;
  });

  if (_proxyList.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="7" style="text-align:center;opacity:.5;padding:24px 0;">暂无代理，请添加</td>
      </tr>`;
    return;
  }

  tbody.innerHTML = _proxyList.map((p, i) => {
    const key = `global:${p.host}:${p.port}:${p.username || ""}`;
    const result = resultMap[key];
    const runtime = _proxyRuntimeMap[key] || {};
    const statusHtml = renderProxyTestStatus(result);
    const latency = result && result.status === "ok"
      ? `<span class="proxy-latency">${escapeHtml(result.latency_ms)} ms</span>`
      : `<span style="opacity:.35">--</span>`;
    return `<tr data-idx="${i}">
      <td><span class="proxy-index">#${i + 1}</span></td>
      <td><code>${escapeHtml(p.host)}:${escapeHtml(p.port)}</code></td>
      <td><span style="opacity:.6">${escapeHtml(p.username || "--")}</span></td>
      <td>${statusHtml}</td>
      <td>${latency}</td>
      <td>${renderProxyRuntime(runtime, result)}</td>
      <td><button class="btn btn-sm btn-danger-outline" onclick="removeProxy(${i})">删除</button></td>
    </tr>`;
  }).join("");
}

function renderPlatformProxyResults(testResults) {
  const container = el("proxy-platform-results");
  if (!container) return;
  const platformResults = (testResults || []).filter((r) => (r.pool || "global") !== "global");
  if (!platformResults.length) {
    container.classList.add("hidden");
    container.innerHTML = "";
    return;
  }
  container.classList.remove("hidden");
  const poolLabels = { steam: "Steam", buff: "Buff", uuyp: "UUYP" };
  container.innerHTML = ["steam", "buff", "uuyp"].map((pool) => {
    const rows = platformResults.filter((r) => r.pool === pool);
    if (!rows.length) return "";
    return `<div class="proxy-platform-result-group">
      <div class="proxy-platform-result-title">${poolLabels[pool] || pool} 测试结果</div>
      <div class="table-container">
        <table class="proxy-platform-result-table">
          <thead>
            <tr>
              <th>节点地址</th>
              <th>用户名</th>
              <th style="width:90px">连接状态</th>
              <th style="width:90px">延迟</th>
              <th>检测目标</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((r) => `
              <tr>
                <td><code>${escapeHtml(r.host)}:${escapeHtml(r.port)}</code></td>
                <td><span style="opacity:.6">${escapeHtml(r.username || "--")}</span></td>
                <td>${renderProxyTestStatus(r)}</td>
                <td>${r.status === "ok" ? `<span class="proxy-latency">${escapeHtml(r.latency_ms)} ms</span>` : `<span style="opacity:.35">--</span>`}</td>
                <td><code class="proxy-detected-ip">${escapeHtml(r.test_url || "")}</code></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>`;
  }).join("");
}

function renderProxyTestStatus(result) {
  if (!result) return '<span class="proxy-badge proxy-badge--idle">未测试</span>';
  if (result.status === "ok") return '<span class="proxy-badge proxy-badge--ok">正常</span>';
  if (result.status === "target_failed") {
    const detail = result.target_error || result.error || "";
    return `<span class="proxy-badge proxy-badge--warn" title="代理网关正常，目标站检测失败：${escapeHtml(detail)}">目标失败</span>`;
  }
  return `<span class="proxy-badge proxy-badge--fail" title="${escapeHtml(result.error || "")}">失败</span>`;
}

function renderProxyRuntime(runtime, testResult) {
  if (testResult && testResult.ip_detected) {
    return `<code class="proxy-detected-ip">${escapeHtml(testResult.ip_detected)}</code>`;
  }
  if (!runtime || !runtime.state) {
    return '<span style="opacity:.35">未加载</span>';
  }
  if (runtime.state === "active") {
    return `<span class="proxy-runtime proxy-runtime--active">活跃 · ${escapeHtml(runtime.score || 0)}</span>`;
  }
  if (runtime.state === "cooldown") {
    return `<span class="proxy-runtime proxy-runtime--cooldown" title="${escapeHtml(runtime.last_failure_reason || "")}">冷却 ${formatProxyCooldown(runtime.cooldown_remaining)}</span>`;
  }
  return `<span class="proxy-runtime proxy-runtime--down" title="${escapeHtml(runtime.last_failure_reason || "")}">不可用</span>`;
}

function formatProxyCooldown(seconds) {
  const remain = Math.max(0, Number(seconds || 0));
  if (remain < 60) return `${Math.round(remain)}s`;
  if (remain < 3600) return `${Math.ceil(remain / 60)}m`;
  return `${Math.ceil(remain / 3600)}h`;
}

function addSingleProxy() {
  const host = (el("proxy-add-host")?.value || "").trim();
  const port = parseInt(el("proxy-add-port")?.value || "0", 10);
  const username = (el("proxy-add-user")?.value || "").trim();
  const password = (el("proxy-add-pass")?.value || "").trim();
  if (!host || !port) {
    toast("请填写主机和端口", "主机和端口为必填项");
    return;
  }
  _proxyList.push({ host, port, username, password });
  renderProxyList();
  ["proxy-add-host", "proxy-add-port", "proxy-add-user", "proxy-add-pass"].forEach((id) => {
    const node = el(id);
    if (node) node.value = "";
  });
  toast("已添加代理", `${host}:${port}`);
}

function parseBulkProxyImport() {
  const raw = (el("proxy-bulk-input")?.value || "").trim();
  if (!raw) {
    toast("请输入代理列表");
    return;
  }
  const lines = raw.split(/[\n\r]+/).map((line) => line.trim()).filter(Boolean);
  let added = 0;
  const errors = [];
  lines.forEach((line, index) => {
    const parts = line.split(":");
    if (parts.length < 2) {
      errors.push(`第 ${index + 1} 行格式错误`);
      return;
    }
    const host = parts[0].trim();
    const port = parseInt(parts[1], 10);
    const username = parts[2]?.trim() || "";
    const password = parts[3]?.trim() || "";
    if (!host || Number.isNaN(port)) {
      errors.push(`第 ${index + 1} 行 IP 或端口无效`);
      return;
    }
    if (_proxyList.some((p) => p.host === host && p.port === port)) return;
    _proxyList.push({ host, port, username, password });
    added += 1;
  });
  renderProxyList();
  const bulk = el("proxy-bulk-input");
  if (bulk) bulk.value = "";
  if (errors.length) {
    toast(`已导入 ${added} 条，${errors.length} 条失败`, errors.slice(0, 3).join("; "));
  } else {
    toast(`已导入 ${added} 条代理`);
  }
}

function removeProxy(idx) {
  _proxyList.splice(idx, 1);
  renderProxyList();
}

async function testAllProxies() {
  const platformCount = ["proxy-steam-proxies", "proxy-buff-proxies", "proxy-uuyp-proxies"]
    .reduce((sum, id) => sum + readProxyTextarea(id).length, 0);
  if (_proxyList.length === 0 && platformCount === 0) {
    toast("代理列表为空", "请先添加代理 IP");
    return;
  }
  const btn = el("btn-proxy-test");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "测试中...";
  }
  await _doSaveProxyConfig();
  try {
    const d = await fetchJson(API + "/proxy/test", { method: "POST" });
    await loadProxyRuntimeStatus();
    renderProxyList(d.results || []);
    renderPlatformProxyResults(d.results || []);
    const ok = (d.results || []).filter((r) => r.status === "ok").length;
    const targetFailed = (d.results || []).filter((r) => r.status === "target_failed").length;
    const fail = (d.results || []).length - ok - targetFailed;
    toast(`测试完成：${ok} 成功 / ${targetFailed} 目标失败 / ${fail} 失败`);
  } catch (e) {
    toast("测试失败", e.message || "请检查后端日志");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "测试连通性";
    }
  }
}

async function _doSaveProxyConfig() {
  const strategy = Number(document.querySelector(".proxy-strategy-card.active")?.dataset?.strategy ?? 1);
  const enabled = strategy !== 3;
  const testUrl = el("proxy-test-url")?.value?.trim() || "https://ipv4.webshare.io/";
  const timeout = parseInt(el("proxy-timeout")?.value || "10", 10);
  const webshareApiKey = el("proxy-webshare-apikey")?.value?.trim() || "";
  _platformProxyPools = {
    steam: readProxyTextarea("proxy-steam-proxies"),
    buff: readProxyTextarea("proxy-buff-proxies"),
    uuyp: readProxyTextarea("proxy-uuyp-proxies"),
  };
  await fetchJson(API + "/proxy/config", {
    method: "POST",
    body: JSON.stringify({
      proxy_pool: {
        enabled,
        strategy,
        test_url: testUrl,
        timeout_seconds: timeout,
        webshare_api_key: webshareApiKey,
        global_proxies: _proxyList.map(proxyEntryToLine),
        steam_proxies: _platformProxyPools.steam.map(proxyEntryToLine),
        buff_proxies: _platformProxyPools.buff.map(proxyEntryToLine),
        uuyp_proxies: _platformProxyPools.uuyp.map(proxyEntryToLine),
        proxies: _proxyList,
      },
    }),
  });
  await loadProxyRuntimeStatus();
}

async function saveProxyConfig() {
  try {
    await _doSaveProxyConfig();
    renderProxyList();
    toast("代理池配置已保存");
  } catch (e) {
    toast("保存失败", e.message || "请检查后端日志");
  }
}

function selectStrategy(strategyId) {
  document.querySelectorAll(".proxy-strategy-card").forEach((card) => {
    card.classList.toggle("active", Number(card.dataset.strategy) === Number(strategyId));
  });
}

async function clearAllProxies() {
  if (!confirm(`确定要清空所有 ${_proxyList.length} 个代理吗？此操作不可撤销。`)) return;
  const btn = el("btn-proxy-clear");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "清除中...";
  }
  try {
    const d = await fetchJson(API + "/proxy/clear", { method: "POST" });
    if (d.ok) {
      _proxyList = [];
      _proxyRuntimeMap = {};
      renderProxyList();
      toast("已清空代理列表");
    } else {
      toast("清除失败", d.message || "");
    }
  } catch (e) {
    toast("清除失败", e.message || "");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "清除全部";
    }
  }
}

async function fetchWebshareProxies() {
  const apiKey = (el("proxy-webshare-apikey")?.value || "").trim();
  if (!apiKey) {
    toast("请先填写 Webshare API Key", "在检测参数区域输入后再点击获取");
    return;
  }
  const btn = el("btn-proxy-webshare");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "获取中...";
  }
  try {
    await _doSaveProxyConfig();
  } catch {}
  try {
    const d = await fetchJson(API + "/proxy/webshare", { method: "POST" });
    if (d.ok) {
      toast("获取成功", d.message || `已导入 ${d.count} 个代理`);
      await loadProxyConfig();
    } else {
      toast("获取失败", d.message || "请检查 API Key 或账户状态");
    }
  } catch (e) {
    toast("获取失败", e.message || "");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "获取订阅";
    }
  }
}
