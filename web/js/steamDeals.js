
(function () {
    'use strict';
    const _flag = (code, name) => `<img src="https://flagcdn.com/${code}.svg" width="16" alt="${name}" style="vertical-align: middle; margin-right: 4px; border-radius: 2px;"> ${name}`;
    const REGION_NAMES = {
        cn: _flag('cn', '国区'), ru: _flag('ru', '俄区'), kz: _flag('kz', '哈萨克'), ua: _flag('ua', '乌克兰'),
        pk: _flag('pk', '南亚'), tr: _flag('tr', '土区'), ar: _flag('ar', '阿根廷'), az: _flag('az', '阿塞拜疆'),
        vn: _flag('vn', '越南'), id: _flag('id', '印尼'), in: _flag('in', '印度'), br: _flag('br', '巴西'),
        cl: _flag('cl', '智利'), jp: _flag('jp', '日本'), hk: _flag('hk', '港区'), ph: _flag('ph', '菲律宾'),
    };
    let _offset = 0;
    const _limit = 30;
    let _loading = false;
    let _hasMore = true;
    let _search = '';
    let _sortBy = 'default_recommend';
    let _sortDir = 'desc';
    let _compareRegion = '';
    let _dealStatusFilter = '';
    let _pollTimer = null;
    let _initialized = false;
    let _fetchRunning = false;
    let _lastDbCount = 0;
    let _lastGridRefreshAt = 0;
    const $ = id => document.getElementById(id);
    function timeAgo(ts) {
        if (!ts) return '从未';
        const diff = Date.now() / 1000 - ts;
        if (diff < 60) return '刚刚';
        if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
        if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
        return `${Math.floor(diff / 86400)} 天前`;
    }
    function fmtReviews(n) {
        if (!n) return '0';
        if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
        if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
        return String(n);
    }
    function fmtCNY(val) {
        if (val == null) return '—';
        return `¥${val.toFixed(2)}`;
    }
    function debounce(fn, ms) {
        let t;
        return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    }
    async function loadGames(reset = false) {
        if (_loading) return;
        if (!reset && !_hasMore) return;
        _loading = true;
        if (reset) { _offset = 0; _hasMore = true; $('steam-deals-grid').innerHTML = ''; }
        const loadEl = $('steam-deals-loading');
        const emptyEl = $('steam-deals-empty');
        if (loadEl) loadEl.classList.remove('hidden');
        if (emptyEl) emptyEl.classList.add('hidden');
        try {
            const params = new URLSearchParams({
                offset: _offset, limit: _limit, search: _search,
                sort_by: _sortBy, sort_dir: _sortDir, compare_region: _compareRegion,
                deal_status_filter: _dealStatusFilter,
            });
            const resp = await fetch(`/api/steam-deals?${params}`);
            const data = await resp.json();
            if (data.games && data.games.length > 0) {
                renderGames(data.games);
                _offset += data.games.length;
                _hasMore = _offset < data.total;
            } else {
                _hasMore = false;
                if (_offset === 0 && emptyEl) emptyEl.classList.remove('hidden');
            }
            const tcEl = $('steam-deals-total-count');
            if (tcEl) tcEl.textContent = `共 ${data.total} 款`;
        } catch (err) {
            console.error('加载失败:', err);
        } finally {
            _loading = false;
            if (loadEl) loadEl.classList.add('hidden');
            _checkSentinel();
        }
    }
    function _checkSentinel() {
        if (!_hasMore || _loading) return;
        const sentinel = $('steam-deals-sentinel');
        if (!sentinel) return;
        const rect = sentinel.getBoundingClientRect();
        if (rect.top <= window.innerHeight + 200) {
            loadGames(false);
        }
    }
    function renderGames(games) {
        const grid = $('steam-deals-grid');
        const frag = document.createDocumentFragment();
        games.forEach(g => {
            const card = document.createElement('div');
            card.className = 'sg-card';
            const storeUrl = `https://store.steampowered.com/app/${g.app_id}/`;
            card.dataset.appid = g.app_id;
            card.dataset.storeUrl = storeUrl;
            card.dataset.gameName = g.name || '';
            card.dataset.bannerUrl = g.banner_url || '';
            const rate = g.positive_rate != null ? g.positive_rate : 0;
            const rateTone = rate >= 80 ? 'is-good' : rate >= 60 ? 'is-warn' : 'is-bad';
            const rateLabel = rate >= 95 ? '好评如潮' : rate >= 80 ? '特别好评' : rate >= 70 ? '多半好评' : rate >= 50 ? '褒贬不一' : '差评';
            const cnCny = g.cny_prices?.cn;
            let pricePills = '';
            pricePills += _pill(_flag('cn', '中国'), cnCny, 'sg-pill-cn');
            let regionPillsCount = 1;
            if (_compareRegion && _compareRegion !== 'cn') {
                const regCny = g.cny_prices?.[_compareRegion];
                let saving = null;
                let isExpensive = false;
                if (cnCny != null && regCny != null) {
                    if (regCny < cnCny) {
                        saving = `省 ¥${(cnCny - regCny).toFixed(0)}`;
                    } else if (regCny > cnCny) {
                        saving = `贵 ¥${(regCny - cnCny).toFixed(0)}`;
                        isExpensive = true;
                    }
                }
                pricePills += _pill(REGION_NAMES[_compareRegion] || _compareRegion, regCny, 'sg-pill-compare', saving, isExpensive);
                regionPillsCount++;
            }
            if (g.cheapest_regions) {
                for (const cr of g.cheapest_regions) {
                    if (regionPillsCount >= 3) break;
                    if (cr.region === _compareRegion) continue;
                    let saving = null;
                    let isExpensive = false;
                    if (cnCny != null && cr.cny != null) {
                        if (cr.cny < cnCny) {
                            saving = `省 ¥${(cnCny - cr.cny).toFixed(0)}`;
                        } else if (cr.cny > cnCny) {
                            saving = `贵 ¥${(cr.cny - cnCny).toFixed(0)}`;
                            isExpensive = true;
                        }
                    }
                    pricePills += _pill(REGION_NAMES[cr.region] || cr.region, cr.cny, 'sg-pill-cheap', saving, isExpensive);
                    regionPillsCount++;
                }
            }
            let diffBadge = '';
            if (_sortBy === 'discount_abs' && g.discount_abs_cn != null) {
                diffBadge = `<span class="sg-diff-chip is-warning">降¥${g.discount_abs_cn.toFixed(0)}</span>`;
            } else if (_sortBy === 'region_value' && g.price_diff != null && g.cny_prices?.[_compareRegion]) {
                const regCny = g.cny_prices[_compareRegion];
                const ratio = regCny > 0 ? ((g.price_diff / regCny) * 100).toFixed(0) : null;
                if (ratio !== null) {
                    diffBadge = `<span class="sg-diff-chip is-primary">省¥${g.price_diff.toFixed(0)} · 值${ratio}%</span>`;
                }
            } else if (g.price_diff != null) {
                if (g.price_diff >= 0) {
                    diffBadge = `<span class="sg-diff-chip">省¥${g.price_diff.toFixed(0)}</span>`;
                } else {
                    diffBadge = `<span class="sg-diff-chip is-danger">贵¥${Math.abs(g.price_diff).toFixed(0)}</span>`;
                }
            }
            let discBadge = g.discount_percent
                ? `<div class="sg-badge-discount">${g.discount_percent}%</div>`
                : '';
            if (g.deal_status === '新史低' || g.deal_status === '平史低') {
                const statusClass = g.deal_status === '新史低' ? 'is-low' : 'is-equal';
                discBadge += `<div class="sg-badge-status ${statusClass}">${g.deal_status}</div>`;
            }
            const uid = 'sg-expand-' + g.app_id;
            let allRows = '';
            const regionCodes = Object.keys(REGION_NAMES);
            for (let i = 0; i < regionCodes.length; i += 2) {
                const rc1 = regionCodes[i], rc2 = regionCodes[i + 1];
                const v1 = g.cny_prices?.[rc1], v2 = rc2 ? g.cny_prices?.[rc2] : null;
                allRows += `<tr>
          <td class="sg-exp-name">${REGION_NAMES[rc1]}</td>
          <td class="sg-exp-price">${fmtCNY(v1)}</td>
          <td class="sg-exp-orig">${g.prices?.[rc1] || '—'}</td>
          <td class="sg-exp-name">${rc2 ? REGION_NAMES[rc2] : ''}</td>
          <td class="sg-exp-price">${rc2 ? fmtCNY(v2) : ''}</td>
          <td class="sg-exp-orig">${rc2 ? (g.prices?.[rc2] || '—') : ''}</td>
        </tr>`;
            }
            card.innerHTML = `
        <div class="sg-card-banner">
          <img src="${g.banner_url}" alt="${g.name}" loading="lazy" onerror="this.closest('.sg-card-banner').classList.add('sg-card-banner--error')" onload="this.closest('.sg-card-banner').classList.remove('sg-card-banner--error')" />
          <button type="button" class="sg-img-retry" title="重新加载封面">
            ${REFRESH_SVG}
            重新加载图片
          </button>
          ${discBadge}
        </div>
        <div class="sg-card-content">
          <a class="sg-card-title" href="${storeUrl}" target="_blank" rel="noopener" title="${g.name}">${g.name}</a>
          <div class="sg-card-review">
            <div class="sg-review-track"><div class="sg-review-bar-fill ${rateTone}" style="width:${rate}%"></div></div>
            <span class="sg-review-rate ${rateTone}">${rate}%</span>
            <span class="sg-review-tag">${rateLabel}</span>
            <span class="sg-review-cnt">${fmtReviews(g.total_reviews)}</span>
            <a class="sg-steam-btn is-compact" href="${storeUrl}" target="_blank" rel="noopener">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M11.979 0C5.678 0 .511 4.86.022 11.037l6.432 2.658c.545-.371 1.203-.59 1.912-.59.063 0 .125.004.188.006l2.861-4.142V8.91c0-2.495 2.028-4.524 4.524-4.524 2.494 0 4.524 2.031 4.524 4.527s-2.03 4.525-4.524 4.525h-.105l-4.076 2.911c0 .052.004.105.004.159 0 1.875-1.515 3.396-3.39 3.396-1.635 0-3.016-1.173-3.331-2.727L.436 15.27C1.862 20.307 6.486 24 11.979 24c6.627 0 11.999-5.373 11.999-12S18.606 0 11.979 0zM7.54 18.21l-1.473-.61c.262.543.714.999 1.314 1.25 1.297.539 2.793-.076 3.332-1.375.263-.63.264-1.319.005-1.949s-.75-1.121-1.377-1.383c-.624-.26-1.29-.249-1.878-.03l1.523.63c.956.4 1.409 1.5 1.009 2.455-.397.957-1.497 1.41-2.455 1.012zm11.415-9.303c0-1.662-1.353-3.015-3.015-3.015-1.665 0-3.015 1.353-3.015 3.015 0 1.665 1.35 3.015 3.015 3.015 1.662 0 3.015-1.35 3.015-3.015zm-5.273-.005c0-1.252 1.013-2.266 2.265-2.266 1.249 0 2.266 1.014 2.266 2.266 0 1.251-1.017 2.265-2.266 2.265-1.252 0-2.265-1.014-2.265-2.265z"/></svg>
              Steam 商店
            </a>
          </div>
          <div class="sg-card-prices">
            ${pricePills}
            ${diffBadge}
          </div>
          <button class="sg-expand-toggle" onclick="this.closest('.sg-card').querySelector('.sg-expand-panel').classList.toggle('hidden');this.textContent=this.textContent==='查看全部区域 ▾'?'收起 ▴':'查看全部区域 ▾'">查看全部区域 ▾</button>
          <div class="sg-expand-panel hidden" id="${uid}">
            <table class="sg-expand-table"><tbody>${allRows}</tbody></table>
          </div>
        </div>
      `;
            frag.appendChild(card);
        });
        grid.appendChild(frag);
    }
    function _pill(label, cnyVal, cls, savingText, isExpensive) {
        const price = fmtCNY(cnyVal);
        const sv = savingText ? `<div class="sg-pill-saving${isExpensive ? ' is-expensive' : ''}">${savingText}</div>` : '';
        return `
      <div class="sg-pill ${cls}">
        <span class="sg-pill-label">${label}</span>
        <span class="sg-pill-price">${price}</span>
        ${sv}
      </div>`;
    }
    let _observer = null;
    function setupObserver() {
        if (_observer) _observer.disconnect();
        const sentinel = $('steam-deals-sentinel');
        if (!sentinel) return;
        _observer = new IntersectionObserver(entries => {
            if (entries[0].isIntersecting && !_loading && _hasMore) loadGames(false);
        }, { rootMargin: '200px' });
        _observer.observe(sentinel);
    }
    const REFRESH_SVG = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>`;
    const STOP_SVG = `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"></rect></svg>`;
    function setEmptyStateRunning(running) {
        const emptyEl = $('steam-deals-empty');
        const textEl = emptyEl?.querySelector('p');
        if (!textEl) return;
        textEl.textContent = running
            ? '正在获取折扣数据，已保存的游戏会陆续显示在这里'
            : '暂无数据，请点击上方「获取数据」按钮开始抓取';
    }
    function buildProgressText(s) {
        const details = [];
        if (s.total > 0) details.push(`${s.progress || 0}/${s.total}`);
        if (s.saved > 0) details.push(`保存 ${s.saved}`);
        if (s.failed > 0) details.push(`失败 ${s.failed}`);
        const suffix = details.length ? ` · ${details.join(' · ')}` : '';
        if (s.cancel_requested) return `停止中，等待已发出的请求结束${suffix}`;
        return `${s.message || '获取中...'}${suffix}`;
    }
    function setFetchButtonState(running, cancelRequested = false) {
        const btn = $('steam-deals-fetch-btn');
        if (!btn) return;
        if (running) {
            btn.disabled = !!cancelRequested;
            btn.innerHTML = cancelRequested ? '停止中...' : `${STOP_SVG} 停止获取`;
        } else {
            btn.disabled = false;
            btn.innerHTML = `${REFRESH_SVG} 获取数据`;
        }
    }
    async function fetchData() {
        if (_fetchRunning) {
            setFetchButtonState(true, true);
            try {
                const resp = await fetch('/api/steam-deals/cancel', { method: 'POST' });
                const r = await resp.json();
                if (!r.ok && typeof toast === "function") {
                    toast("停止失败", r.message || "当前没有正在运行的抓取任务");
                }
                startPolling();
            } catch (err) {
                console.error('停止获取失败:', err);
                if (typeof toast === "function") toast("停止失败", err.message || "");
                setFetchButtonState(true, false);
            }
            return;
        }
        const btn = $('steam-deals-fetch-btn');
        if (btn) { btn.disabled = true; btn.textContent = '启动中...'; }
        try {
            const resp = await fetch('/api/steam-deals/fetch', { method: 'POST' });
            const r = await resp.json();
            if (!r.ok) {
                throw new Error(r.error || "获取失败");
            }
            _fetchRunning = true;
            _lastGridRefreshAt = 0;
            setEmptyStateRunning(true);
            setFetchButtonState(true, false);
            startPolling();
        } catch (err) {
            console.error('触发获取失败:', err);
            if (typeof toast === "function") {
                toast("获取失败", err.message || "");
            }
            _fetchRunning = false;
            setEmptyStateRunning(false);
            setFetchButtonState(false);
        }
    }
    function startPolling() {
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = setInterval(pollStatus, 1500);
        pollStatus();
    }
    async function pollStatus() {
        try {
            const resp = await fetch('/api/steam-deals/status');
            const s = await resp.json();
            const prog = $('steam-deals-progress');
            const info = $('steam-deals-update-info');
            const tcEl = $('steam-deals-total-count');
            const wasRunning = _fetchRunning;
            _fetchRunning = !!s.running;
            if (s.running) {
                setEmptyStateRunning(true);
                if (prog) { prog.classList.remove('hidden'); prog.textContent = buildProgressText(s); }
                setFetchButtonState(true, !!s.cancel_requested);
                const dbCount = Number(s.total_games_in_db || 0);
                const shouldRefreshGrid = dbCount > 0 && (
                    dbCount !== _lastDbCount ||
                    Date.now() - _lastGridRefreshAt > 8000
                );
                if (shouldRefreshGrid && !_loading) {
                    _lastDbCount = dbCount;
                    _lastGridRefreshAt = Date.now();
                    loadGames(true);
                }
            } else {
                if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
                setEmptyStateRunning(false);
                if (prog) {
                    if (wasRunning && s.message) {
                        const finalMessage = s.message;
                        prog.classList.remove('hidden');
                        prog.textContent = finalMessage;
                        setTimeout(() => {
                            if (!_fetchRunning && prog.textContent === finalMessage) prog.classList.add('hidden');
                        }, 5000);
                    } else {
                        prog.classList.add('hidden');
                    }
                }
                setFetchButtonState(false);
                if (wasRunning || Number(s.total_games_in_db || 0) !== _lastDbCount) {
                    _lastDbCount = Number(s.total_games_in_db || 0);
                    _lastGridRefreshAt = Date.now();
                    loadGames(true);
                }
            }
            if (info) {
                const span = info.querySelector('span') || info;
                span.textContent = s.last_update ? `上次更新：${timeAgo(s.last_update)}` : '尚未获取数据';
            }
            if (tcEl && s.total_games_in_db > 0) tcEl.textContent = `数据库 ${s.total_games_in_db} 款`;
        } catch (err) { }
    }
    async function checkAutoRefresh() {
        try {
            const resp = await fetch('/api/steam-deals/status');
            const s = await resp.json();
            if (s.running) { startPolling(); return; }
            if (s.last_update && s.auto_refresh_days > 0) {
                const days = (Date.now() / 1000 - s.last_update) / 86400;
                if (days >= s.auto_refresh_days) fetchData();
            }
        } catch (err) { }
    }
    function init() {
        if (_initialized) return;
        _initialized = true;
        const searchInput = $('steam-deals-search-input');
        if (searchInput) searchInput.addEventListener('input', debounce(() => { _search = searchInput.value.trim(); loadGames(true); }, 400));
        const regionSel = $('steam-deals-region');
        if (regionSel) regionSel.addEventListener('change', () => {
            _compareRegion = regionSel.value;
            if (_compareRegion && _compareRegion !== 'all' && _compareRegion !== '') {
                // Single region selected: switch to region_value default
                const sortSel = $('steam-deals-sort');
                if (sortSel) sortSel.value = 'region_value|desc';
                _sortBy = 'region_value'; _sortDir = 'desc';
            } else if (!_compareRegion || _compareRegion === 'all' || _compareRegion === '') {
                // Back to all regions: switch back to default_recommend
                const sortSel = $('steam-deals-sort');
                if (sortSel) sortSel.value = 'default_recommend|desc';
                _sortBy = 'default_recommend'; _sortDir = 'desc';
            }
            loadGames(true);
        });
        const sortSel = $('steam-deals-sort');
        if (sortSel) sortSel.addEventListener('change', () => {
            const [by, dir] = sortSel.value.split('|');
            _sortBy = by; _sortDir = dir || 'asc';
            loadGames(true);
        });
        const statusSel = $('steam-deals-status-filter');
        if (statusSel) statusSel.addEventListener('change', () => {
            _dealStatusFilter = statusSel.value;
            loadGames(true);
        });
        const fetchBtn = $('steam-deals-fetch-btn');
        if (fetchBtn) fetchBtn.addEventListener('click', fetchData);
        setupObserver();

        const grid = $('steam-deals-grid');
        if (grid) {
            grid.addEventListener('click', async (e) => {
                const retryBtn = e.target.closest('.sg-img-retry');
                if (!retryBtn) return;
                e.preventDefault();
                e.stopPropagation();
                const card = retryBtn.closest('.sg-card');
                const banner = retryBtn.closest('.sg-card-banner');
                const img = banner?.querySelector('img');
                const appId = card?.dataset.appid;
                const fallbackUrl = card?.dataset.bannerUrl || img?.src?.split('?')[0];
                if (!img || !appId) return;
                retryBtn.disabled = true;
                retryBtn.textContent = '正在刷新封面...';
                banner.classList.remove('sg-card-banner--error');
                try {
                    const resp = await fetch(`/api/steam-deals/refresh-banner/${appId}`, { method: 'POST' });
                    const data = await resp.json();
                    if (data.ok && data.banner_url) {
                        card.dataset.bannerUrl = data.banner_url;
                        img.src = `${data.banner_url}${data.banner_url.includes('?') ? '&' : '?'}reload=${Date.now()}`;
                    } else if (fallbackUrl) {
                        img.src = `${fallbackUrl}${fallbackUrl.includes('?') ? '&' : '?'}reload=${Date.now()}`;
                        if (typeof toast === 'function') toast('刷新封面失败', data.error || 'Steam 未返回可用封面地址');
                    }
                } catch (err) {
                    if (fallbackUrl) img.src = `${fallbackUrl}${fallbackUrl.includes('?') ? '&' : '?'}reload=${Date.now()}`;
                    if (typeof toast === 'function') toast('刷新封面失败', err.message || '');
                } finally {
                    retryBtn.disabled = false;
                    retryBtn.innerHTML = `${REFRESH_SVG} 重新加载图片`;
                }
            });
            grid.addEventListener('contextmenu', (e) => {
                const card = e.target.closest('.sg-card');
                if (!card) return;
                e.preventDefault();

                const link = card.querySelector('a.sg-card-title');
                if (!link) return;
                const match = link.href.match(/app\/(\d+)/);
                if (!match) return;
                const appId = match[1];
                const gameName = card.dataset.gameName || link.textContent.trim() || appId;
                const storeUrl = card.dataset.storeUrl || link.href;

                const existing = document.getElementById('sg-ctx-portal');
                if (existing) existing.remove();

                // Create a fullscreen portal overlay (fixed, covers viewport, pointer-events: none)
                // then place the menu inside with absolute coords == clientX/Y
                const portal = document.createElement('div');
                portal.id = 'sg-ctx-portal';
                portal.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;overflow:visible;';
                document.body.appendChild(portal);

                const menu = document.createElement('div');
                menu.id = 'sg-context-menu';
                menu.innerHTML = `
                  <div class="sg-cm-item" data-action="gift">加入 Steam 赠礼候选</div>
                  <div class="sg-cm-item" data-action="card">生成折扣分享卡片</div>
                `;
                portal.appendChild(menu);

                // Position after render so we can read offsetWidth/Height
                requestAnimationFrame(() => {
                    let x = e.clientX;
                    let y = e.clientY;
                    if (x + menu.offsetWidth > window.innerWidth) x -= menu.offsetWidth;
                    if (y + menu.offsetHeight > window.innerHeight) y -= menu.offsetHeight;
                    menu.style.left = `${x}px`;
                    menu.style.top = `${y}px`;
                });

                menu.querySelector('[data-action="gift"]')?.addEventListener('click', () => {
                    portal.remove();
                    const payload = { appId, url: storeUrl, title: gameName, autoFetch: true };
                    if (typeof window.tabSwitch === 'function') {
                        window.tabSwitch('gift');
                    } else {
                        document.querySelector('.nav-item[data-tab="gift"]')?.click();
                    }
                    setTimeout(() => {
                        if (typeof window.prefillGiftFromSteamDeal === 'function') {
                            window.prefillGiftFromSteamDeal(payload);
                        } else {
                            window.dispatchEvent(new CustomEvent('gift:prefill-store-url', { detail: payload }));
                        }
                    }, 120);
                });

                menu.querySelector('[data-action="card"]')?.addEventListener('click', async () => {
                    portal.remove();
                    if (typeof toast === 'function') toast("正在生成卡片", "这利用网络下载高清封面进行实时渲染，请稍候...", 5000);
                    try {
                        const resp = await fetch(`/api/steam-deals/generate-card/${appId}`, { method: 'POST' });
                        const r = await resp.json();
                        if (r.ok) {
                            if (typeof toast === 'function') toast("卡片生成成功！", r.message || "折扣分享卡已保存到本地文件夹。", 8000);
                        } else {
                            if (typeof toast === 'function') toast("卡片生成失败", r.error || "未知报错", 10000);
                        }
                    } catch (err) {
                        if (typeof toast === 'function') toast("网络错误或异常", err.message || "接口调用异常", 10000);
                    }
                });

                const closePortal = (ev) => {
                    if (!menu.contains(ev.target)) {
                        portal.remove();
                        document.removeEventListener('mousedown', closePortal);
                    }
                };
                setTimeout(() => document.addEventListener('mousedown', closePortal), 0);
            });
        }

        loadGames(true);
        checkAutoRefresh();
        pollStatus();
    }
    document.addEventListener('DOMContentLoaded', () => {
        const panel = document.getElementById('panel-steam-deals');
        if (!panel) return;
        const obs = new MutationObserver(() => { if (panel.classList.contains('active')) init(); });
        obs.observe(panel, { attributes: true, attributeFilter: ['class'] });
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.addEventListener('click', () => { if (btn.dataset.tab === 'steam-deals') setTimeout(init, 50); });
        });
        if (panel.classList.contains('active')) setTimeout(init, 50);
    });
})();
