/* Read-only telemetry rendering. All controls use the existing guarded routes. */
const RuntimePanel = (() => {
  const states = {idle:"未启动",pending:"排队中",running:"执行中",waiting:"等待中",blocked:"受阻",disabled:"未启用",success:"已完成",failed:"异常",stopped:"已停止",stopping:"正在停止",retrying:"等待重试"};
  const holdingNames = {receipt:"待收货",cooldown:"冷却中",ready:"待上架",confirmation:"待确认",listed:"在售",sold:"已售",unknown:"待核实",error:"状态异常"};
  let snapshot = null, offset = 0, busy = false, lastEvents = "";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const money = value => `¥${Number(value || 0).toFixed(2)}`;
  const duration = value => { const n = Math.max(0, Math.floor(value || 0)); return n >= 3600 ? `${Math.floor(n/3600)}时 ${Math.floor(n%3600/60)}分` : n >= 60 ? `${Math.floor(n/60)}分 ${n%60}秒` : `${n}秒`; };
  const now = () => Date.now()/1000 + offset;
  const text = (id,value) => { const e = document.getElementById(id); if(e) e.textContent = value; };
  const html = (id,value) => { const e = document.getElementById(id); if(e) {
    const open = new Set([...e.querySelectorAll('details[open][data-key]')].map(d=>d.dataset.key));
    e.innerHTML = value;
    e.querySelectorAll('details[data-key]').forEach(d=>{ d.open = open.has(d.dataset.key); });
  } };
  const tag = status => `<span class="run-tag ${escape(status)}">${escape(states[status] || status)}</span>`;
  function timing(t) {
    if(t.next_run_at && t.status !== "disabled") return t.next_run_at > now() ? `${duration(t.next_run_at-now())}后再次检查` : "已到执行时间，等待调度";
    if(t.status === "running" && t.started_at) return `本轮已用 ${duration(now()-t.started_at)} · 当前步骤 ${duration(now()-t.step_started_at)}`;
    if(t.finished_at && t.started_at) return `上次用时 ${duration(t.finished_at-t.started_at)}`;
    return "等待条件满足";
  }
  function task(t) { return `<div class="run-task"><div class="run-task-head"><strong>${escape(t.name)}</strong>${tag(t.status)}</div><p>${escape(t.detail)}${t.item ? ` · ${escape(t.item)}`:""}</p><small>${escape(timing(t))}${t.total ? ` · ${t.done}/${t.total} 件已处理`:""}</small>${t.next_item ? `<p class="run-caption">下一候选：${escape(t.next_item)}</p>`:""}</div>`; }
  function render(d) {
    snapshot = d; offset = d.server_time-Date.now()/1000;
    const tasks = d.tasks || [], controls = d.controls || {}, session = d.session || {};
    const active = tasks.filter(t=>t.status === "running" || t.status === "stopping");
    const wait = tasks.filter(t=>["waiting","blocked","pending","retrying","failed"].includes(t.status))
      .sort((a,b)=>["failed","blocked","pending","retrying","waiting"].indexOf(a.status)-["failed","blocked","pending","retrying","waiting"].indexOf(b.status));
    const buy = tasks.find(t=>t.id === "buy");
    const sell = tasks.find(t=>t.id === "sell");
    text("status-text", active.length ? "任务执行中" : controls.buy_running || controls.sell_enabled ? "等待下一步" : "后台监控中");
    const pill = document.getElementById("status-pill");
    if(pill) { pill.classList.remove("status-idle","status-running","status-stopped","status-error"); pill.classList.add(active.length ? "status-running" : "status-idle"); }
    text("run-mode", ({full:"完整流程",sell:"仅出售",idle:"尚未启动"})[session.mode] || "尚未启动");
    text("run-summary", `${controls.buy_running ? "买入已启动":"买入未运行"} · ${controls.sell_enabled ? "自动上架已启用":"自动上架未启用"} · ${active.length} 项正在执行`);
    text("run-started", session.started_at ? `启动于 ${new Date(session.started_at*1000).toLocaleString()}` : "选择模式开始，后台状态独立显示");
    text("run-active-count", `${active.length} 项`);
    html("run-active", active.map(task).join("") || '<p class="run-empty">当前没有执行中的任务。查看右侧等待原因。</p>');
    html("run-waiting", wait.map(task).join("") + (d.queue || []).map(q=>task({...q,detail:"线程池任务等待执行或重试"})).join("") || '<p class="run-empty">暂无排队或等待任务</p>');
    text("run-buy-state", states[buy?.status] || "未启动");
    text("run-spent",money(session.spent)); text("run-target",`/ ${money(session.target)} 预算`);
    const progress = document.getElementById("run-buy-progress");
    if(progress) progress.value = session.target > 0 ? Math.min(100,session.spent/session.target*100):0;
    text("run-buy-detail", `本次已购买 ${session.bought || 0} 件${buy ? ` · ${buy.detail}`:" · 此模式尚未执行买入"}`);
    text("run-buy-time",buy ? `买入已用 ${duration((buy.finished_at || now())-(buy.execution_started_at || buy.started_at || now()))} · ${timing(buy)}`:"已确认并落库的成本计入本次购买，不以是否售出为准。");
    html("run-holdings", Object.entries(holdingNames).map(([key,label])=>{ const g = d.holdings?.[key] || {}; return `<div class="run-holding"><span>${label}</span><strong>${g.count || 0}<small> 件</small></strong><small>成本 ${money(g.cost)}</small></div>`; }).join(""));
    text("run-sale-detail",sell ? `${sell.detail} · 本轮处理 ${sell.done || 0}/${sell.total || 0} 件，成功提交 ${sell.listed || 0} 件。${timing(sell)}`:"按最近库存和交易记录统计；待核实表示尚无足够状态信息。");
    html("run-services", tasks.filter(t=>!["buy","sell"].includes(t.id)).map(t=>`<div class="run-service"><div class="run-task-head"><strong>${escape(t.name)}</strong>${tag(t.status)}</div><p>${escape(t.detail)}</p><small>${escape(timing(t))}</small><details data-key="${escape(t.id)}"><summary class="run-caption">上次结果 / 异常</summary><p>${escape(t.last_result || "尚无执行结果")}</p>${t.last_error ? `<p>${escape(t.last_error)}</p>`:""}</details></div>`).join("") || '<p class="run-empty">后台服务尚未注册</p>');
    const alert = document.getElementById("run-alert");
    if(alert) { alert.textContent = d.buy_blocker?.message || ""; alert.classList.toggle("hidden",!alert.textContent); }
    const eventKey = JSON.stringify(d.events);
    if(eventKey !== lastEvents) {
      lastEvents = eventKey;
      html("run-events",(d.events || []).slice(0,15).map(e=>`<details class="run-event" data-key="${escape(e.task)}-${e.at}"><summary><time>${escape(new Date(e.at*1000).toLocaleTimeString())}</time><span>${escape(e.name)} · ${escape(e.detail)}</span></summary><p>状态：${escape(states[e.status] || e.status)} · 任务：${escape(e.name)}<br>${escape(e.detail)}</p></details>`).join("") || '<p class="run-empty">启动任务后，这里会显示步骤变化和执行结果。</p>');
    }
    text("btn-sell-only",controls.sell_enabled ? "自动上架已启用" : controls.buy_running ? "恢复自动上架" : "仅启动出售");
    for(const [id,disabled] of [["btn-start",controls.buy_running],["btn-sell-only",controls.sell_enabled],["btn-stop-buy",!controls.buy_running],["btn-stop-sell",!controls.sell_enabled]]) {
      const b = document.getElementById(id); if(b) b.disabled = !!disabled;
    }
    tick();
  }
  function tick() { text("run-elapsed",snapshot?.session?.started_at ? duration((snapshot.session.finished_at || now())-snapshot.session.started_at):"—"); }
  async function refresh() {
    if(busy) return; busy = true;
    try { render(await fetchJson(API+"/runtime-panel")); text("run-connection","● 实时连接"); document.getElementById("run-connection")?.classList.remove("offline"); }
    catch { text("run-connection","连接中断 · 当前为上次快照"); document.getElementById("run-connection")?.classList.add("offline"); }
    finally { busy = false; }
  }
  async function stop(part) {
    try { await fetchJson(API+`/pipeline/${part}/stop`,{method:"POST"}); await refresh(); }
    catch(e) { toast("停止失败",e.message); }
  }
  document.addEventListener("DOMContentLoaded",()=>{
    document.getElementById("btn-stop-buy")?.addEventListener("click",()=>stop("buy"));
    document.getElementById("btn-stop-sell")?.addEventListener("click",()=>stop("sell"));
    document.getElementById("run-open-logs")?.addEventListener("click",()=>tabSwitch("debug"));
    refresh(); setInterval(refresh,2000); setInterval(tick,1000);
  });
  return {render,refresh,isBuying:()=>!!snapshot?.controls?.buy_running};
})();
