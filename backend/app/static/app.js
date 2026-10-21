/* Dependency-free UI. API strings are rendered only as text nodes, never HTML. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const SYMBOLS = Object.freeze({ NVDABUSDT: 'NVDA', TSLABUSDT: 'TSLA', AAPLBUSDT: 'AAPL', MSFTBUSDT: 'MSFT', AMZNBUSDT: 'AMZN', GOOGBUSDT: 'GOOG', METABUSDT: 'META', COINBUSDT: 'COIN' });
  const views = {
    positions: ['持仓监控', 'PORTFOLIO MONITOR', '把价格交给监控，把注意力留给决策。'],
    picks: ['每日波段精选', 'DAILY SWING RESEARCH', '在 2–4 周的时间尺度，寻找值得进一步研究的机会。'],
    logs: ['推送记录', 'NOTIFICATION JOURNAL', '每一次提醒，都有迹可循。'],
    settings: ['配置中心', 'WORKSPACE SETTINGS', '连接数据、模型与通知，让研究按你的节奏运行。']
  };
  const state = { view: null, positions: null, picks: null, picksError: false, logs: null, runs: null, reviews: null, status: null, statusError: '', config: null, configDirty: false, launching: false, syncing: false, editing: null, positionSaving: false, detailSeq: 0, statusLoading: false, poll: null };
  const paths = {
    portfolio: ['M4 7h16v13H4z', 'M9 7V4h6v3M4 12h16M10 12v3h4v-3'],
    compass: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18', 'm16 8-2 6-6 2 2-6 6-2Z'],
    bell: ['M5 17h14l-2-3V9a5 5 0 0 0-10 0v5l-2 3ZM10 21h4M12 2v2'],
    settings: ['M4 6h16M4 12h16M4 18h16', 'M8 3v6M16 9v6M10 15v6'],
    refresh: ['M20 10a8 8 0 0 0-14-4L3 9m0-6v6h6M4 14a8 8 0 0 0 14 4l3-3m0 6v-6h-6'],
    shield: ['m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z', 'm8 12 3 3 5-6'],
    info: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18', 'M12 11v6M12 7v1'],
    close: ['m6 6 12 12M6 18 18 6'], plus: ['M12 5v14M5 12h14'],
    arrow: ['M5 12h14m-5-5 5 5-5 5'], play: ['m9 5 11 7-11 7V5Z'],
    inbox: ['m4 4-2 9v7h20v-7l-2-9H4Z', 'M2 13h6l2 3h4l2-3h6'],
    check: ['m5 12 4 4L19 6']
  };
  function el(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function icon(name) {
    const n = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', class: 'icon' })) n.setAttribute(k, v);
    for (const d of (paths[name] || paths.info)) { const p = document.createElementNS(n.namespaceURI, 'path'); p.setAttribute('d', d); n.append(p); }
    return n;
  }
  document.querySelectorAll('[data-icon]').forEach((n) => n.append(icon(n.dataset.icon)));
  function button(label, handler, className = 'button', iconName) {
    const n = el('button', className); n.type = 'button';
    if (iconName) n.append(icon(iconName));
    n.append(document.createTextNode(label));
    n.addEventListener('click', () => { Promise.resolve().then(() => handler(n)).catch((e) => toast(errorText(e), 'failed')); });
    return n;
  }
  function badge(text, type = 'neutral') { return el('span', `badge ${type}`, text); }
  function list(value) { return Array.isArray(value) ? value : []; }
  function str(value, fallback = '—') { return typeof value === 'string' && value.trim() ? value : fallback; }
  function num(value) { if (value === '' || value === null || value === undefined || typeof value === 'boolean') return null; const n = Number(value); return Number.isFinite(n) ? n : null; }
  function fmt(value, digits = 2) { const n = num(value); return n === null ? '—' : n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
  function pct(value) { const n = num(value); return n === null ? '—' : `${n > 0 ? '+' : ''}${fmt(n)}%`; }
  function signed(value) { const n = num(value); return n === null ? '—' : `${n > 0 ? '+' : ''}${fmt(n)}`; }
  function direction(value) { const n = num(value); return n === null || n === 0 ? '' : n > 0 ? 'positive' : 'negative'; }
  function date(value) {
    if (!value) return '日期未提供';
    if (typeof value !== 'string') return '日期未提供';
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
    // ISO without an offset is displayed literally: do not invent a timezone.
    if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(value)) return value.replace('T', ' ');
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? value : `${d.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}（本地）`;
  }
  function timeNode(value, cls = '') { const n = el('time', cls, date(value)); if (typeof value === 'string') n.dateTime = value; return n; }
  function errorText(e) { return e instanceof Error ? e.message : '操作失败，请稍后重试。'; }
  function apiDetail(data, fallback) {
    if (typeof data?.detail === 'string') return data.detail;
    if (Array.isArray(data?.detail)) return data.detail.map((x) => typeof x.msg === 'string' ? x.msg : '').filter(Boolean).join('；') || fallback;
    return typeof data?.message === 'string' ? data.message : fallback;
  }
  async function api(path, options = {}) {
    const { method = 'GET', body, timeout = 45000 } = options;
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(`/api${path}`, { method, credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      const contentType = response.headers.get('content-type') || '';
      let data = null;
      if (contentType.includes('application/json')) { try { data = await response.json(); } catch { throw new Error('服务器返回的数据无法解析，请刷新重试。'); } }
      if (!response.ok) {
        const defaults = { 401: '需要管理员认证，请使用浏览器的登录提示后重试。', 403: '操作未获授权，请检查当前凭据或服务权限。', 503: '服务暂不可用，请检查数据与模型配置。' };
        const e = new Error(apiDetail(data, defaults[response.status] || `请求失败（HTTP ${response.status}），请重试。`)); e.status = response.status; throw e;
      }
      if (data?.success === false) throw new Error(apiDetail(data, '操作未成功，请检查服务配置。'));
      if (data === null && response.status !== 204) throw new Error('API 未返回 JSON 数据，请检查服务是否启动。');
      return data;
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('请求超时。后台任务可能仍在运行，请刷新状态后再操作。');
      if (e instanceof TypeError) throw new Error('无法连接服务器，请检查网络后重试。');
      throw e;
    } finally { clearTimeout(timer); }
  }
  async function apiList(path) { const data = await api(path); if (!Array.isArray(data)) throw new Error('服务器列表格式异常，请刷新重试。'); return data; }
  async function busy(n, text, action) {
    if (n.disabled) return;
    const children = [...n.childNodes]; n.replaceChildren(document.createTextNode(text)); n.disabled = true; n.setAttribute('aria-busy', 'true');
    try { return await action(); } finally { n.replaceChildren(...children); n.disabled = false; n.removeAttribute('aria-busy'); }
  }
  function toast(message, type = 'success') {
    const n = el('div', `toast ${type}`); n.append(icon(type === 'failed' ? 'info' : 'check'), el('p', '', message));
    const close = button('', () => n.remove(), 'icon-button', 'close'); close.setAttribute('aria-label', '关闭通知'); n.append(close);
    $('notifications').append(n); if ($('notifications').children.length > 4) $('notifications').firstElementChild.remove();
    if (type !== 'failed') setTimeout(() => { if (!n.contains(document.activeElement) && !n.matches(':hover')) n.remove(); }, 9000);
  }
  function message(id, text, kind = 'error', retry) {
    const n = $(id); n.replaceChildren(); n.className = `message ${kind}`; n.hidden = !text;
    if (text) n.append(el('span', '', text));
    if (text && retry) n.append(button('重试', (b) => busy(b, '重试中…', retry), 'button small'));
  }
  function contentState(id, title, description = '', { loading = false, compact = false, action, actionLabel, iconName = 'inbox' } = {}) {
    const n = el('div', `state${compact ? ' compact' : ''}`);
    n.append(loading ? el('span', 'loading-mark') : (() => { const i = el('span', 'state-icon'); i.append(icon(iconName)); return i; })(), el('h3', '', title));
    if (description) n.append(el('p', '', description));
    if (action) n.append(button(actionLabel || '重试', (b) => busy(b, '请稍候…', action), 'button small'));
    $(id).replaceChildren(n);
  }
  function makeTable(headers, caption, className) {
    const wrap = el('div', 'table-scroll'); wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', `${caption}，可横向滚动`);
    const table = el('table', className); table.append(el('caption', 'sr-only', caption));
    const head = el('thead'); const row = el('tr');
    headers.forEach((h) => { const th = el('th', '', h); th.scope = 'col'; row.append(th); });
    head.append(row); const body = el('tbody'); table.append(head, body); wrap.append(table); return { wrap, body };
  }
  function td(main, secondary, cls = '') { const n = el('td'); n.append(main instanceof Node ? main : el('span', cls, main)); if (secondary !== undefined) n.append(el('span', 'secondary', secondary)); return n; }
  function sourceLink(title, url) {
    try {
      if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) throw new Error();
      const u = new URL(url); if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw new Error();
      const a = el('a', '', str(title, u.hostname)); a.href = u.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
    } catch { return el('span', 'muted', `${str(title, '来源')}（无有效 HTTP(S) 链接）`); }
  }
  function ask(title, text, label = '确认') {
    return new Promise((resolve) => {
      const d = $('confirm-dialog'); $('confirm-title').textContent = title; $('confirm-message').textContent = text; $('confirm-accept').textContent = label;
      let done = false;
      function finish(value) { if (done) return; done = true; d.close(); $('confirm-accept').removeEventListener('click', yes); $('confirm-cancel').removeEventListener('click', no); d.removeEventListener('cancel', cancel); resolve(value); }
      const yes = () => finish(true); const no = () => finish(false); const cancel = (e) => { e.preventDefault(); finish(false); };
      $('confirm-accept').addEventListener('click', yes); $('confirm-cancel').addEventListener('click', no); d.addEventListener('cancel', cancel); d.showModal(); $('confirm-cancel').focus();
    });
  }
  function route() {
    const hash = location.hash.slice(1); const view = Object.hasOwn(views, hash) ? hash : 'positions';
    if (hash === 'main') { $('main').focus(); return; }
    const changed = state.view !== view; state.view = view;
    Object.keys(views).forEach((key) => { $(`view-${key}`).hidden = key !== view; });
    document.querySelectorAll('[data-view]').forEach((n) => { if (n.dataset.view === view) n.setAttribute('aria-current', 'page'); else n.removeAttribute('aria-current'); });
    $('page-title').replaceChildren(document.createTextNode(views[view][0]), el('span', 'title-dot', '.'));
    $('breadcrumb-current').textContent = views[view][0]; $('page-eyebrow').textContent = views[view][1]; $('page-description').textContent = views[view][2];
    document.title = `${views[view][0]} · 八角波段工作台`; renderHeading();
    if (changed) { window.scrollTo(0, 0); loadView(); }
  }
  function renderHeading() {
    const box = $('heading-actions');
    const focusedIndex = box.contains(document.activeElement) ? [...box.children].indexOf(document.activeElement) : -1;
    box.replaceChildren();
    if (state.view === 'positions') {
      const sync = button(state.syncing ? '正在同步…' : '同步币安', syncPositions, 'button', 'refresh'); sync.disabled = state.syncing;
      box.append(sync, button('添加持仓', () => openPosition(), 'button primary', 'plus'));
    } else if (state.view === 'picks') {
      const run = button(state.launching || state.status?.is_running ? '研究正在运行…' : '运行今日研究', runAnalysis, 'button primary', 'play');
      const cfg = state.status?.configured; run.disabled = state.launching || Boolean(state.status?.is_running) || !cfg?.market_data || !cfg?.model || !!state.statusError;
      if (run.disabled) run.title = state.launching || state.status?.is_running ? '同一时间仅运行一个研究任务' : '请先配置市场数据与模型，并成功读取研究状态';
      box.append(run);
    } else if (state.view === 'logs') box.append(button('刷新记录', (b) => busy(b, '读取中…', loadLogs), 'button', 'refresh'));
    else if (state.view === 'settings') { const n = el('a', 'button', '保存配置 ↓'); n.href = '#save-settings'; n.addEventListener('click', (e) => { e.preventDefault(); if (!$('settings-content').hidden) { $('save-settings').scrollIntoView({ block: 'center' }); $('save-settings').focus(); } }); box.append(n); }
    if (focusedIndex >= 0 && box.children[focusedIndex] && !box.children[focusedIndex].disabled) box.children[focusedIndex].focus({ preventScroll: true });
  }
  async function loadView(force = false) {
    const view = state.view;
    if (view === 'positions') await loadPositions();
    if (view === 'picks') await loadResearch();
    if (view === 'logs') await loadLogs();
    if (view === 'settings' && (!state.config || force)) await loadConfig();
    if (view === 'settings' && state.config) { $('settings-loading').hidden = true; $('settings-content').hidden = false; }
  }

  // Portfolio: all figures are actual Binance prices, never equity research prices.
  async function loadPositions() {
    message('positions-error', '');
    if (state.positions === null) contentState('positions-content', '正在读取持仓', '获取有效持仓与最新报价…', { loading: true });
    $('positions-content').setAttribute('aria-busy', 'true');
    try {
      state.positions = (await apiList('/positions')).filter((p) => p && p.is_active !== false && p.is_active !== 0);
      renderPositions();
    } catch (e) {
      message('positions-error', `${errorText(e)}${state.positions !== null ? ' 下方保留上次读取结果，可能已过期。' : ''}`, 'error', loadPositions);
      if (state.positions === null) contentState('positions-content', '持仓暂不可用', '尚未获取到账户数据。请检查服务，或稍后重试。');
    } finally { $('positions-content').removeAttribute('aria-busy'); }
  }
  function renderPositions() {
    const positions = state.positions || []; $('metric-count').textContent = positions.length; $('position-count').textContent = `${positions.length} 笔持仓`;
    const priced = positions.filter((p) => num(p.current_price) !== null && num(p.quantity) !== null);
    const costed = priced.filter((p) => num(p.buy_price) !== null);
    const complete = positions.length > 0 && priced.length === positions.length;
    const costComplete = complete && costed.length === positions.length;
    const value = complete ? priced.reduce((s, p) => s + num(p.current_price) * num(p.quantity), 0) : null;
    const pnl = costComplete ? costed.reduce((s, p) => s + (num(p.current_price) - num(p.buy_price)) * num(p.quantity), 0) : null;
    $('metric-value').textContent = fmt(value); $('metric-value-note').textContent = !positions.length ? '暂无有效持仓' : !complete ? `${positions.length - priced.length} 笔报价缺失，不显示合计` : '按最近读取的币安报价计算';
    $('metric-pnl').textContent = signed(pnl); $('metric-pnl').className = `numeric ${direction(pnl)}`; $('metric-pnl-note').textContent = costComplete ? '基于录入成本 · 不含费用' : '等待完整成本与报价';
    if (!positions.length) { contentState('positions-content', '还没有监控中的持仓', '从币安同步已有持仓，或手动录入成本与数量。这里不会展示示例账户。', { action: () => openPosition(), actionLabel: '添加第一笔持仓', iconName: 'portfolio' }); return; }
    const { wrap, body } = makeTable(['币安代币对 / 数量', '成本 / 最新价', '未实现盈亏', '操作'], '有效持仓列表，价格单位 USDT', 'position-table');
    positions.forEach((p) => {
      const current = num(p.current_price), buy = num(p.buy_price), qty = num(p.quantity);
      const pnlValue = current !== null && buy !== null && qty !== null ? (current - buy) * qty : null;
      const pnlPct = current !== null && buy !== null && buy > 0 ? ((current - buy) / buy) * 100 : null;
      const row = el('tr'); const symbol = el('strong', 'symbol-text', str(p.symbol));
      row.append(td(symbol, `数量 ${fmt(qty, 6)}`));
      const priceCell = td(el('span', 'numeric', fmt(buy)), current === null ? '现价暂缺' : `现价 ${fmt(current)}`); row.append(priceCell);
      const gain = el('strong', `numeric ${direction(pnlValue)}`, signed(pnlValue)); row.append(td(gain, pnlPct === null ? '缺少报价' : pct(pnlPct)));
      const actions = el('div', 'row-actions');
      actions.append(button('设置', () => openPosition(p), 'text-button'), button('AI 分析', () => analyzePosition(p), 'text-button'));
      row.append(td(actions)); body.append(row);
      const rulesRow = el('tr', 'position-detail-row'); const rulesCell = el('td'); rulesCell.colSpan = 4;
      const rules = el('div', 'position-rule'); const interval = num(p.check_interval_hours);
      rules.append(el('span', '', `止盈 ${num(p.take_profit_price) === null ? '未设' : fmt(p.take_profit_price)} / 止损 ${num(p.stop_loss_price) === null ? '未设' : fmt(p.stop_loss_price)}`), el('span', '', interval === 0 ? '定期汇报已关闭 · 风险监控仍开启' : interval === null ? '汇报周期未提供' : `每 ${interval} 小时汇报`));
      const ladders = ladderArray(p.ladder_alerts); if (ladders.length) rules.append(el('span', '', `阶梯 ${ladders.map((v) => `${v > 0 ? '+' : ''}${v}%`).join(' / ')}`));
      if (p.include_ai_analysis) rules.append(el('span', '', '含 AI 分析'));
      rules.append(button('测试提醒', (b) => busy(b, '发送中…', async () => { try { const r = await api(`/positions/${encodeURIComponent(p.id)}/test-alert`, { method: 'POST', timeout: 120000 }); toast(r.message || '测试请求成功，请核对推送记录。'); } catch (e) { toast(errorText(e), 'failed'); } }), 'text-button'), button('停止监控', (b) => stopPosition(p, b), 'text-button'));
      rulesCell.append(rules); rulesRow.append(rulesCell); body.append(rulesRow);
    });
    $('positions-content').replaceChildren(wrap);
  }
  function ladderArray(value) {
    let v = value;
    if (typeof v === 'string') { try { v = JSON.parse(v); } catch { return []; } }
    return list(v).map(num).filter((n) => n !== null);
  }
  async function syncPositions(b) {
    if (state.syncing) return;
    if (!await ask('从币安同步持仓？', '将读取账户余额并对齐监控列表。币安已不存在的持仓可能停止监控；不会执行买入或卖出。', '确认同步')) return;
    state.syncing = true; renderHeading();
    try { const r = await api('/positions/sync-binance', { method: 'POST', timeout: 120000 }); const count = num(r.count); const deactivated = Array.isArray(r.deactivated) ? r.deactivated.length : num(r.deactivated); toast(`${r.message || '账户同步完成'}${count === null ? '' : ` · 同步 ${count} 笔`}${deactivated === null ? '' : ` · 停止 ${deactivated} 笔`}`); await loadPositions(); }
    catch (e) { message('positions-error', `同步失败：${errorText(e)} 原有界面数据未被替换。`, 'error'); }
    finally { state.syncing = false; renderHeading(); }
  }
  async function stopPosition(p, b) {
    if (!await ask('停止持仓监控？', `停止 ${str(p.symbol)} 的提醒与监控。这不会在币安卖出，也不会删除历史记录；之后账户同步可能重新启用该持仓。`, '停止监控')) return;
    await busy(b, '处理中…', async () => { await api(`/positions/${encodeURIComponent(p.id)}`, { method: 'DELETE' }); toast('已停止监控，未执行任何交易。'); await loadPositions(); });
  }
  async function openPosition(position = null, candidateSymbol = '') {
    if (state.positionSaving) return;
    state.editing = position;
    const form = $('position-form'); form.reset(); message('position-save-error', '');
    $('position-dialog-title').textContent = position ? '编辑持仓监控' : '添加持仓';
    const select = $('position-symbol'); select.replaceChildren();
    Object.entries(SYMBOLS).forEach(([symbol, underlying]) => { const o = el('option', '', `${symbol} · 对应 ${underlying}`); o.value = symbol; select.append(o); });
    const symbol = position?.symbol || candidateSymbol;
    if (symbol && !Object.hasOwn(SYMBOLS, symbol)) { toast('该标的不在原有 8 个代币对范围内，不能添加或编辑。', 'failed'); return; }
    select.value = symbol || Object.keys(SYMBOLS)[0]; select.disabled = !!position;
    $('position-cost').value = position?.buy_price ?? ''; $('position-quantity').value = position?.quantity ?? '';
    $('position-target').value = position?.take_profit_price ?? ''; $('position-stop').value = position?.stop_loss_price ?? '';
    $('position-interval').value = position?.check_interval_hours ?? state.config?.default_check_interval_hours ?? 4;
    $('position-ladder').value = position ? ladderArray(position.ladder_alerts).join(', ') : '';
    $('position-ai').checked = position ? Boolean(position.include_ai_analysis) : state.config?.default_include_ai_analysis ?? true;
    $('position-symbol-note').textContent = candidateSymbol ? '已仅带入代币对。请自行填写币安成本、数量及提醒价；没有复制任何美股研究价格。' : '仅限原有 8 个币安代币对；研究周期为 2–4 周。';
    for (const input of form.querySelectorAll('input')) input.setCustomValidity('');
    $('position-dialog').showModal(); if (candidateSymbol || position) $('position-cost').focus(); else select.focus();
  }
  async function savePosition(e) {
    e.preventDefault(); if (state.positionSaving) return;
    const form = $('position-form');
    for (const id of ['position-cost', 'position-quantity', 'position-target', 'position-stop']) { const n = $(id); n.setCustomValidity(n.value && !(num(n.value) > 0) ? '请输入大于 0 的有限数值。' : ''); }
    const raw = $('position-ladder').value.trim(); const parts = raw ? raw.split(/[,，]/).map((x) => x.trim()) : [];
    const ladders = parts.map(num); $('position-ladder').setCustomValidity(parts.some((x) => !x) || ladders.some((n) => n === null || n <= 0 || n > 1000) ? '请输入逗号分隔的大于 0 且不超过 1000 的涨幅百分比，例如 5, 10。' : '');
    if (!form.reportValidity()) return;
    const editing = state.editing;
    const payload = { buy_price: num($('position-cost').value), quantity: num($('position-quantity').value), take_profit_price: num($('position-target').value), stop_loss_price: num($('position-stop').value), check_interval_hours: num($('position-interval').value), ladder_alerts: [...new Set(ladders)], include_ai_analysis: $('position-ai').checked };
    if (!editing) payload.symbol = $('position-symbol').value;
    const selectedSymbol = editing?.symbol || payload.symbol;
    if (!Object.hasOwn(SYMBOLS, selectedSymbol)) { message('position-save-error', '请选择原有 8 个代币对之一。'); return; }
    state.positionSaving = true; message('position-save-error', '');
    const controls = [...form.querySelectorAll('input, select, button')]; const oldDisabled = controls.map((n) => n.disabled); controls.forEach((n) => { n.disabled = true; });
    $('save-position').textContent = '正在保存…';
    try { await api(editing ? `/positions/${encodeURIComponent(editing.id)}` : '/positions', { method: editing ? 'PUT' : 'POST', body: payload }); $('position-dialog').close(); toast('持仓监控已保存，未执行任何交易。'); state.editing = null; location.hash = 'positions'; await loadPositions(); }
    catch (err) { message('position-save-error', errorText(err)); }
    finally { state.positionSaving = false; controls.forEach((n, i) => { n.disabled = oldDisabled[i]; }); $('save-position').textContent = '保存持仓'; }
  }

  // Research: explicit price basis gates all research prices, including legacy data.
  function openDetail(title) {
    state.detailSeq += 1; $('detail-title').textContent = title; $('detail-content').replaceChildren(); $('detail-actions').replaceChildren(button('关闭', () => $('detail-dialog').close(), 'button'));
    if (!$('detail-dialog').open) $('detail-dialog').showModal();
    return state.detailSeq;
  }
  async function analyzePosition(p) {
    const seq = openDetail(`${str(p.symbol)} · 波段分析`);
    contentState('detail-content', '正在研究底层美股', '使用真实市场数据与模型生成 2–4 周分析，可能需要一些时间。', { loading: true });
    try {
      const data = await api(`/positions/${encodeURIComponent(p.id)}/analyze`, { method: 'POST', timeout: 180000 });
      if (seq !== state.detailSeq || !$('detail-dialog').open) return;
      renderResearchDetail(data);
    } catch (e) { if (seq === state.detailSeq && $('detail-dialog').open) contentState('detail-content', '分析未能完成', errorText(e), { action: () => analyzePosition(p), actionLabel: '重新分析', iconName: 'info' }); }
  }
  function researchPrices(p) {
    if (p.price_basis !== 'US_EQUITY_USD') return el('p', 'message warning', '旧版或未声明价格口径的数据：研究价格已隐藏，请重新运行研究。');
    const box = el('div', 'research-prices'); const range = list(p.entry_range);
    const entry = range.length === 2 && range.every((x) => num(x) !== null) ? `${fmt(range[0])}–${fmt(range[1])}` : '未提供';
    for (const [label, value] of [['入场研究区间 · USD', entry], ['研究目标 · USD', fmt(p.target_price)], ['研究止损 · USD', fmt(p.stop_loss_price)]]) { const n = el('div', 'research-price'); n.append(el('span', '', label), el('strong', '', value)); box.append(n); }
    return box;
  }
  function renderResearchDetail(p, candidate = false) {
    const box = $('detail-content'); box.replaceChildren();
    box.append(el('div', 'message info', '价格口径：底层美股 USD，不是币安可执行价格。研究目标与止损不会复制到持仓配置。'), el('p', 'detail-meta', `${str(p.symbol)} → ${str(p.underlying_symbol, SYMBOLS[p.symbol] || '美股代码未提供')} · ${str(p.horizon, '周期未提供')} · 美股数据截至 ${date(p.as_of)}`), el('h3', '', '研究逻辑'), el('p', 'prose', str(p.reason, '未提供研究逻辑')), researchPrices(p));
    const signals = list(p.signals).filter((s) => typeof s === 'string');
    if (signals.length) { box.append(el('h3', '', `规则筛选依据${num(p.score) === null ? '' : ` · ${fmt(p.score, 0)} / 100`}`)); const ul = el('ul', 'detail-list'); signals.forEach((s) => ul.append(el('li', '', s))); box.append(ul, el('p', 'muted', '规则分用于排序，不代表上涨概率，也不是完整 VCP / CANSLIM 认证。')); }
    if (typeof p.level_method === 'string' && p.level_method.trim()) box.append(el('h3', '', '研究价位计算'), el('p', 'prose', p.level_method));
    box.append(el('h3', '', '失效条件'), el('p', 'prose', str(p.invalidation, '未单独提供；请结合风险项核验。')));
    box.append(el('h3', '', '主要风险'));
    const risks = list(p.risks); if (!risks.length) box.append(el('p', 'muted', '未提供风险明细；不代表没有风险。'));
    else { const ul = el('ul', 'detail-list'); risks.forEach((r) => ul.append(el('li', '', typeof r === 'string' ? r : '风险项格式异常'))); box.append(ul); }
    box.append(el('h3', '', '催化与事件'));
    const catalysts = list(p.catalysts); if (!catalysts.length) box.append(el('p', 'muted', '未提供事件明细。'));
    else { const ul = el('ul', 'detail-list'); catalysts.forEach((c) => { const li = el('li'); li.append(sourceLink(c.title, c.source_url), el('span', 'source-date', date(c.date))); ul.append(li); }); box.append(ul); }
    box.append(el('h3', '', '可核验来源'));
    if (!list(p.sources).length) box.append(el('p', 'muted', '此记录未提供来源，请谨慎使用。'));
    else { const ul = el('ul', 'source-list'); list(p.sources).forEach((s) => { const li = el('li'); li.append(sourceLink(s.title, s.url), el('span', 'source-date', `来源日期：${date(s.as_of)}`)); ul.append(li); }); box.append(ul); }
    if (candidate && Object.hasOwn(SYMBOLS, p.symbol)) $('detail-actions').append(button('录入币安持仓', () => { $('detail-dialog').close(); openPosition(null, p.symbol); }, 'button primary', 'plus'));
  }
  async function loadResearch() { await Promise.all([loadStatus(), loadPicks(), loadRuns(), loadReviews()]); }
  function renderStatus() {
    renderHeading(); const box = $('analysis-config'); box.replaceChildren();
    if (!state.status) { box.append(badge('研究状态未知', 'warning')); $('analysis-state').textContent = state.statusError || '等待服务返回研究状态'; return; }
    const cfg = state.status.configured || {};
    [['market_data', '市场数据'], ['model', '分析模型'], ['notifications', '通知渠道']].forEach(([key, label]) => box.append(badge(`${label} · ${cfg[key] ? '已配置' : '未配置'}`, cfg[key] ? 'success' : 'warning')));
    const setup = el('a', 'text-link', '管理配置 ↗'); setup.href = '#settings'; box.append(setup);
    const running = state.launching || state.status.is_running;
    $('run-push').disabled = running || !cfg.notifications;
    if (!cfg.notifications) $('run-push').checked = false;
    const run = state.status.latest_run;
    $('analysis-state').textContent = running ? '正在研究中 · 页面会自动更新，请勿重复启动' : !cfg.market_data || !cfg.model ? '配置未就绪 · 请先连接市场数据和模型' : run ? `最近运行：${runLabel(run.status)} · ${date(run.finished_at || run.started_at)}` : '尚未运行研究 · 可手动运行或配置每日调度';
    renderScope();
    if (state.picks !== null && !state.picksError && !state.picks.length) renderPicks();
    $('hide-all-picks').disabled = !state.picks?.length || state.picksError || running;
    document.querySelectorAll('[data-run-push]').forEach((n) => { if (!n.hasAttribute('aria-busy')) n.disabled = !cfg.notifications || running; });
  }
  async function loadStatus() {
    if (state.statusLoading) return;
    state.statusLoading = true;
    try {
      const previousRunning = state.status?.is_running; const data = await api('/analysis/status', { timeout: 20000 });
      if (!data || typeof data.configured !== 'object') throw new Error('研究状态格式异常，请刷新重试。');
      state.status = data; state.statusError = ''; $('connection-status').textContent = 'API 已连接'; $('connection-status').className = 'connection success';
      message('picks-error', ''); renderStatus();
      if (previousRunning && !data.is_running && !state.launching && state.view === 'picks') { await Promise.all([loadPicks(), loadRuns(), loadReviews()]); announceRun(data.latest_run); }
    } catch (e) {
      state.statusError = errorText(e); $('connection-status').textContent = '研究状态不可用'; $('connection-status').className = 'connection failed';
      message('picks-error', `无法读取研究状态：${errorText(e)}`, 'error', loadStatus); renderHeading();
      $('analysis-state').textContent = '状态读取失败 · 请刷新后再启动研究';
    } finally { state.statusLoading = false; }
  }
  function runLabel(status) {
    return ({ success: '研究完成', completed: '研究完成', failed: '研究失败', error: '研究失败', running: '研究中', pending: '等待运行', no_candidates: '暂无合格机会', no_opportunity: '暂无合格机会', no_opportunities: '暂无合格机会', empty: '暂无合格机会', skipped: '已跳过', missing_config: '配置不完整', partial: '部分完成', partial_success: '部分完成' })[status] || str(status, '状态未提供');
  }
  function pushLabel(status) {
    return ({ success: '推送成功', sent: '推送成功', failed: '推送失败', partial: '部分渠道失败', pending: '等待推送', skipped: '未推送', disabled: '未启用推送', not_requested: '未请求推送', sending: '推送中', not_configured: '渠道未配置' })[status] || str(status, '投递状态未提供');
  }
  function runStyle(status) { return ['success', 'completed'].includes(status) ? 'success' : ['failed', 'error'].includes(status) ? 'failed' : ['running', 'pending'].includes(status) ? 'info' : 'warning'; }
  function pushStyle(status) { return ['success', 'sent'].includes(status) ? 'success' : status === 'failed' ? 'failed' : status === 'partial' ? 'warning' : 'neutral'; }
  function renderScope() {
    const box = $('research-scope'); box.replaceChildren(); const chips = el('div', 'scope-chips');
    const universe = list(state.status?.universe).filter((u) => u && Object.hasOwn(SYMBOLS, u.symbol));
    if (!universe.length) chips.append(el('p', 'muted', '服务尚未返回研究范围。'));
    else universe.forEach((u) => chips.append(badge(`${u.symbol} → ${str(u.underlying_symbol, SYMBOLS[u.symbol])}`)));
    box.append(chips);
    if (!list(state.status?.skills).length) box.append(el('p', 'muted', '服务尚未提供技能来源。'));
    list(state.status?.skills).forEach((s) => { const row = el('div', 'skill-row'); row.append(sourceLink(s.name, s.source_url), el('span', 'source-date', `版本：${str(s.revision)}`), el('p', '', str(s.description, '没有描述'))); box.append(row); });
  }
  async function loadPicks() {
    if (state.picks === null) contentState('picks-content', '正在读取候选', '只展示已存储的真实研究记录。', { loading: true });
    try { state.picks = (await apiList('/analysis/picks')).filter((p) => p && Object.hasOwn(SYMBOLS, p.symbol)); state.picksError = false; renderPicks(); }
    catch (e) { state.picksError = true; contentState('picks-content', '候选读取失败', errorText(e), { action: loadPicks }); $('hide-all-picks').disabled = true; }
  }
  function renderPicks() {
    const picks = state.picks || []; $('pick-count').textContent = `/${picks.length}`; $('hide-all-picks').disabled = !picks.length || state.launching || !!state.status?.is_running;
    if (!picks.length) {
      const latest = state.status?.latest_run; const cfg = state.status?.configured;
      const missing = cfg && (!cfg.market_data || !cfg.model); const failed = ['failed', 'error', 'missing_config'].includes(latest?.status);
      const noOpportunity = latest && num(latest.count) === 0 && !['failed', 'error', 'running', 'pending', 'missing_config'].includes(latest.status);
      contentState('picks-content', missing ? '先把研究基础准备好' : failed ? '最近一次研究未完成' : noOpportunity ? '本轮没有符合条件的机会' : '暂无可见研究候选', missing ? '连接 FMP 数据与分析模型后，即可开始 2–4 周波段研究。' : failed ? (latest.message || '请查看运行记录中的错误，并修复配置后重试。') : noOpportunity ? '筛选条件没有达到，不为凑数而推荐。可在运行记录中核对结果。' : '尚未生成研究，或当前候选已被隐藏。历史记录仍然保留。', { iconName: 'compass' }); return;
    }
    const box = $('picks-content'); box.replaceChildren();
    picks.forEach((p, i) => {
      const card = el('article', 'pick-card'); card.style.setProperty('--stagger', `${Math.min(i, 5) * 24}ms`);
      const identity = el('div', 'pick-identity'); identity.append(el('span', 'eyebrow', 'UNDERLYING'), el('h3', '', str(p.underlying_symbol, SYMBOLS[p.symbol])), el('p', 'token-symbol', p.symbol));
      const score = num(p.score); identity.append(badge(score === null ? '分数未提供' : `筛选分 ${fmt(score, 0)} / 100`, 'info'), el('p', 'as-of', `美股数据截至\n${date(p.as_of)}`));
      const main = el('div', 'pick-main'); main.append(el('p', 'pick-reason', str(p.reason, '记录未提供研究逻辑。')), researchPrices(p));
      const risk = list(p.risks).filter((r) => typeof r === 'string'); main.append(el('p', 'risk-line', risk.length ? `风险提示：${risk[0]}` : '风险提示：未提供明细，不代表没有风险。'));
      const footer = el('div', 'pick-footer'), actions = el('div', 'row-actions');
      actions.append(button('逻辑、事件与来源 ↗', () => { openDetail(`${str(p.underlying_symbol, SYMBOLS[p.symbol])} · 研究详情`); renderResearchDetail(p, true); }, 'text-button'));
      if (p.id !== undefined && p.id !== null) actions.append(button('隐藏', (b) => hidePicks(p.id, b), 'text-button'));
      footer.append(actions, button('录入币安持仓', () => openPosition(null, p.symbol), 'button small', 'plus'));
      main.append(footer); card.append(identity, main); box.append(card);
    });
  }
  function announceRun(run) {
    if (!run) return;
    const failed = ['failed', 'error', 'missing_config'].includes(run.status);
    const count = num(run.count);
    const running = ['running', 'pending'].includes(run.status);
    message('run-message', `${runLabel(run.status)}${count === null ? '' : ` · ${count} 个候选`}${run.message ? `。${run.message}` : ''}。${pushLabel(run.push_status)}`, failed ? 'error' : running ? 'info' : count === 0 ? 'warning' : 'success');
  }
  async function runAnalysis() {
    if (state.launching || state.status?.is_running || !state.status?.configured?.market_data || !state.status?.configured?.model || state.statusError) return;
    const previousRunId = state.status?.latest_run?.id;
    let requestFailed = false;
    let requestedRunId = null;
    state.launching = true; renderStatus(); $('hide-all-picks').disabled = true;
    message('run-message', '研究已请求，正在读取行情与来源、执行筛选和模型分析。请耐心等待，不要重复启动。', 'info');
    schedulePoll(2000);
    try {
      const push = $('run-push').checked;
      const r = await api(`/analysis/run${push ? '?push_notifications=true' : ''}`, { method: 'POST', timeout: 240000 });
      requestedRunId = r.run_id;
      announceRun({ ...r, push_status: state.status?.latest_run?.id === r.run_id ? state.status.latest_run.push_status : undefined });
    } catch (e) { requestFailed = true; message('run-message', `研究请求未完成：${errorText(e)} 请核对运行记录，勿在后台仍运行时再次启动。`, 'error'); }
    finally {
      state.launching = false; await loadStatus(); await Promise.all([loadPicks(), loadRuns(), loadReviews()]);
      const latest = state.status?.latest_run;
      if (!requestFailed && !state.statusError && !state.status?.is_running && latest && (latest.id === requestedRunId || latest.id !== previousRunId)) announceRun(latest);
      $('run-push').checked = false; renderStatus(); schedulePoll(state.status?.is_running ? 4000 : 15000);
    }
  }
  async function hidePicks(id, b) {
    const all = id === null;
    if (!await ask(all ? '隐藏全部研究候选？' : '隐藏此候选？', '只隐藏当前候选，不删除研究运行历史与到期回顾。', '确认隐藏')) return;
    await busy(b, '处理中…', async () => { await api(`/analysis/picks${all ? '' : `/${encodeURIComponent(id)}`}`, { method: 'DELETE' }); toast('候选已隐藏，历史记录仍保留。'); await loadPicks(); });
  }
  async function loadRuns() {
    if (state.runs === null) contentState('runs-content', '读取运行历史', '', { loading: true, compact: true });
    try {
      state.runs = await apiList('/analysis/runs?limit=20');
      if (!state.runs.length) { contentState('runs-content', '尚无运行记录', '手动或定时研究后，结果与错误将在这里留档。', { compact: true }); return; }
      const box = $('runs-content'); box.replaceChildren();
      state.runs.forEach((r) => {
        const row = el('article', 'run-item'), top = el('div', 'run-top');
        top.append(timeNode(r.started_at, 'run-date'), badge(runLabel(r.status), runStyle(r.status)), badge(pushLabel(r.push_status), pushStyle(r.push_status)));
        const pushable = ['not_requested', 'failed', 'pending', 'partial'].includes(r.push_status) && !['running', 'pending'].includes(r.status) && r.id !== null && r.id !== undefined;
        if (pushable) { const n = button(['failed', 'partial'].includes(r.push_status) ? '重试推送' : '推送结果', (b) => pushRun(r, b), 'text-button'); n.dataset.runPush = 'true'; n.disabled = !state.status?.configured?.notifications || state.launching || !!state.status?.is_running; if (n.disabled) n.title = '请先配置通知渠道，并等待当前研究完成'; top.append(n); }
        row.append(top, el('p', '', `运行 #${r.id ?? '—'} · 候选 ${num(r.count) === null ? '未提供' : r.count} · ${r.message || '未提供运行说明'}`));
        if (r.finished_at) row.append(el('p', '', `结束于 ${date(r.finished_at)}`));
        const errors = list(r.errors);
        if (errors.length) { const details = el('details'); details.append(el('summary', '', `查看 ${errors.length} 条运行错误`)); const ul = el('ul', 'run-errors'); errors.forEach((err) => ul.append(el('li', '', typeof err === 'string' ? err : str(err?.message, str(err?.error, '错误明细格式异常'))))); details.append(ul); row.append(details); }
        box.append(row);
      });
    } catch (e) { contentState('runs-content', '运行历史读取失败', errorText(e), { compact: true, action: loadRuns }); }
  }
  async function pushRun(run, b) {
    if (['success', 'sent'].includes(run.push_status)) return;
    if (!await ask('推送此轮研究结果？', `向已配置渠道发送运行 #${run.id} 的结果，只重试尚未确认成功的渠道与分段。`, '确认推送')) return;
    await busy(b, '推送中…', async () => {
      try { const r = await api(`/analysis/runs/${encodeURIComponent(run.id)}/push`, { method: 'POST', timeout: 120000 }); toast(r.message || pushLabel(r.status)); }
      catch (e) { toast(`推送未成功：${errorText(e)}`, 'failed'); }
      finally { await Promise.all([loadRuns(), loadStatus()]); }
    });
  }
  async function loadReviews() {
    if (state.reviews === null) contentState('reviews-content', '读取到期回顾', '', { loading: true, compact: true });
    try {
      state.reviews = await apiList('/analysis/reviews');
      if (!state.reviews.length) { contentState('reviews-content', '回顾尚未到期', '有研究记录且经过 10 或 20 个交易日后，才展示真实收益与基准比较。', { compact: true }); return; }
      const { wrap, body } = makeTable(['底层美股 / 截至', '交易日', '收益', '基准收益', '超额收益'], '研究到期回顾，底层美股 USD 收益', 'reviews-table');
      state.reviews.forEach((r) => { const row = el('tr'); row.append(td(str(r.underlying_symbol, SYMBOLS[r.symbol] || str(r.symbol)), date(r.as_of)), td(num(r.horizon_sessions) === null ? '—' : String(r.horizon_sessions)), td(pct(r.return_pct), undefined, `numeric ${direction(r.return_pct)}`), td(pct(r.benchmark_return_pct), undefined, 'numeric'), td(pct(r.excess_return_pct), undefined, `numeric ${direction(r.excess_return_pct)}`)); body.append(row); });
      $('reviews-content').replaceChildren(wrap);
    } catch (e) { contentState('reviews-content', '回顾读取失败', errorText(e), { compact: true, action: loadReviews }); }
  }
  async function updateReviews(b) {
    await busy(b, '更新中…', async () => { const r = await api('/analysis/review', { method: 'POST', timeout: 120000 }); toast(r.message || `已更新 ${num(r.count) ?? 0} 条到期回顾。`); await loadReviews(); });
  }
  function schedulePoll(delay = 15000) {
    clearTimeout(state.poll); state.poll = setTimeout(async () => { await loadStatus(); schedulePoll(state.launching || state.status?.is_running ? 4000 : 15000); }, delay);
  }

  // Notification journal: failed and unknown statuses are never called delivered.
  async function loadLogs() {
    message('logs-error', '');
    if (state.logs === null) contentState('logs-content', '正在读取推送记录', '', { loading: true });
    try { state.logs = await apiList('/alerts/logs'); renderLogs(); }
    catch (e) { message('logs-error', errorText(e), 'error', loadLogs); contentState('logs-content', '记录暂不可用', '请重试，失败请求不代表没有历史记录。'); }
  }
  function renderLogs() {
    const filter = $('log-filter').value; const logs = list(state.logs).filter((l) => filter === 'all' || l.status === filter);
    if (!logs.length) { contentState('logs-content', filter === 'all' ? '还没有推送记录' : '没有匹配此状态的记录', filter === 'all' ? '监控触发提醒或发送测试后，实际投递结果将显示在这里。' : '切换为「全部状态」查看其他记录。', { iconName: 'bell' }); return; }
    const { wrap, body } = makeTable(['时间', '代币对 / 类型', '通知内容', '投递状态'], '通知投递日志', 'logs-table');
    const types = { take_profit: '止盈提醒', stop_loss: '止损提醒', ladder: '阶梯提醒', periodic: '定期汇报', test: '测试提醒', daily_pick: '每日精选', ai_picks: '每日精选', analysis: '研究结果' };
    logs.forEach((l) => { const row = el('tr'); row.append(td(timeNode(l.created_at, 'numeric')), td(el('strong', 'numeric', str(l.symbol)), types[l.alert_type] || str(l.alert_type, '类型未提供')), td(el('div', 'log-message', str(l.message, '无通知内容'))), td(badge(l.status === 'success' ? '成功' : l.status === 'failed' ? '失败' : '未知', l.status === 'success' ? 'success' : l.status === 'failed' ? 'failed' : 'neutral'))); body.append(row); });
    $('logs-content').replaceChildren(wrap);
  }

  // Settings: explicit allowlists; account credentials NEVER enter generic config payloads.
  const textFields = ['binance_mcp_url', 'openai_base_url', 'openai_model', 'feishu_app_id', 'ai_picks_push_time', 'ai_picks_push_timezone'];
  const secretFields = ['fmp_api_key', 'openai_api_key', 'feishu_app_secret'];
  const webhookFields = ['feishu_webhook_url', 'wecom_webhook_url'];
  const numberFields = ['analysis_top_n', 'analysis_min_score', 'earnings_blackout_days', 'default_check_interval_hours'];
  const flagFields = ['ai_picks_push_enabled', 'default_include_ai_analysis'];
  const defaults = { ai_picks_push_time: '21:30', ai_picks_push_timezone: 'Asia/Shanghai', analysis_top_n: 3, analysis_min_score: 60, earnings_blackout_days: 3, default_check_interval_hours: 4, ai_picks_push_enabled: false, default_include_ai_analysis: true };
  const masked = (v) => typeof v === 'string' && /\*{3,}|•{3,}/.test(v);
  function sanitizedConfig(data) {
    const clean = {};
    [...textFields, ...numberFields, ...flagFields, ...webhookFields, 'has_fmp_key', 'has_openai_key', 'has_feishu_app_secret', 'has_binance_key', 'has_binance_secret', 'feishu_bot_connected'].forEach((k) => { if (data[k] !== undefined) clean[k] = data[k]; });
    return { ...defaults, ...clean };
  }
  function applyConfig(cfg) {
    // Preserve valid server-side IANA zones even when they are not a preset option.
    const zone = cfg.ai_picks_push_timezone, zones = $('ai_picks_push_timezone');
    if (typeof zone === 'string' && zone && ![...zones.options].some((o) => o.value === zone)) { const option = el('option', '', zone); option.value = zone; zones.append(option); }
    [...textFields, ...numberFields, ...webhookFields].forEach((k) => { $(k).value = masked(cfg[k]) ? '' : cfg[k] ?? ''; });
    flagFields.forEach((k) => { $(k).checked = Boolean(cfg[k]); }); secretFields.forEach((k) => { $(k).value = ''; });
    ['fmp-status', 'model-status', 'binance-status'].forEach((id, i) => { const configured = i === 0 ? cfg.has_fmp_key : i === 1 ? cfg.has_openai_key : cfg.has_binance_key && cfg.has_binance_secret; const n = $(id); n.textContent = configured ? '凭据已保存 · 请测试连接' : '尚未配置'; n.className = `badge ${configured ? 'success' : 'warning'}`; });
    $('feishu-bot-status').textContent = `应用 Secret ${cfg.has_feishu_app_secret ? '已保存' : '未配置'} · 长连接${cfg.feishu_bot_connected ? '已连接' : '未连接'}。`;
    state.configDirty = false; $('save-status').textContent = '配置已读取，密钥留空表示保留';
  }
  async function loadConfig() {
    message('settings-error', ''); $('settings-loading').hidden = !!state.config;
    try {
      const data = await api('/config'); if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('配置格式异常，请刷新重试。');
      state.config = sanitizedConfig(data); applyConfig(state.config); $('settings-content').hidden = false;
    } catch (e) { message('settings-error', errorText(e), 'error', loadConfig); }
    finally { $('settings-loading').hidden = true; }
  }
  function genericPayload() {
    const payload = {};
    textFields.forEach((k) => { payload[k] = $(k).value.trim(); });
    numberFields.forEach((k) => { payload[k] = num($(k).value); });
    flagFields.forEach((k) => { payload[k] = $(k).checked; });
    [...secretFields, ...webhookFields].forEach((k) => { const v = $(k).value.trim(); if (v && !masked(v)) payload[k] = v; });
    return payload;
  }
  function lockForm(form, locked) {
    const controls = [...form.querySelectorAll('input, select, button')];
    if (locked) controls.forEach((n) => { n.dataset.wasDisabled = String(n.disabled); n.disabled = true; });
    else controls.forEach((n) => { n.disabled = n.dataset.wasDisabled === 'true'; delete n.dataset.wasDisabled; });
  }
  async function saveConfig(e) {
    e.preventDefault(); if (!$('settings-form').reportValidity() || $('save-settings').disabled) return;
    const payload = genericPayload(); const form = $('settings-form'); lockForm(form, true); $('save-settings').textContent = '正在保存…'; $('save-status').textContent = '正在写入服务器…'; message('settings-error', '');
    try {
      const r = await api('/config', { method: 'POST', body: payload });
      secretFields.forEach((k) => { $(k).value = ''; }); state.configDirty = false;
      state.config = sanitizedConfig({ ...state.config, ...payload, ...(payload.fmp_api_key ? { has_fmp_key: true } : {}), ...(payload.openai_api_key ? { has_openai_key: true } : {}), ...(payload.feishu_app_secret ? { has_feishu_app_secret: true } : {}) });
      $('save-status').textContent = '已保存 · 配置实时生效'; toast(r.message || '配置已保存。'); await loadStatus();
      // Re-read server flags without overwriting the user's fields with masks.
      try { state.config = sanitizedConfig(await api('/config')); applyConfig(state.config); $('save-status').textContent = '已保存 · 配置实时生效'; }
      catch (err) { $('save-status').textContent = `保存成功，但状态刷新失败：${errorText(err)}`; }
    } catch (err) { message('settings-error', `保存失败：${errorText(err)} 当前输入已保留，请修正后重试。`); $('save-status').textContent = '未保存 · 请检查错误信息'; }
    finally { lockForm(form, false); $('save-settings').replaceChildren(document.createTextNode('保存配置 ↗')); }
  }
  async function testConnection(service, b) {
    const out = $(`test-${service}`); out.className = 'test-result'; out.textContent = '正在测试…';
    await busy(b, '测试中…', async () => {
      const payload = { service };
      const add = (key, value) => { if (value && !masked(value)) payload[key] = value; };
      if (service === 'fmp') add('api_key', $('fmp_api_key').value.trim());
      if (service === 'llm') { add('api_key', $('openai_api_key').value.trim()); add('base_url', $('openai_base_url').value.trim()); add('model', $('openai_model').value.trim()); }
      if (service === 'feishu' || service === 'wecom') add('webhook_url', $(`${service}_webhook_url`).value.trim());
      if (service === 'feishu_bot') { add('app_id', $('feishu_app_id').value.trim()); add('app_secret', $('feishu_app_secret').value.trim()); }
      // Binance tests use only the saved pair, never the in-progress replacement draft.
      try { const r = await api('/config/test', { method: 'POST', body: payload, timeout: 120000 }); out.className = 'test-result positive'; out.textContent = `测试成功 · ${r.message || '服务已响应'}`; }
      catch (err) { out.className = 'test-result negative'; out.textContent = `测试失败 · ${errorText(err)}`; }
    });
  }
  async function saveBinance(e) {
    e.preventDefault(); if (!$('binance-form').reportValidity() || $('save-binance').disabled) return;
    message('binance-save-error', '');
    const payload = { api_key: $('new-binance-key').value.trim(), api_secret: $('new-binance-secret').value.trim(), current_api_secret: $('current-binance-secret').value.trim() };
    const form = $('binance-form'); lockForm(form, true); $('save-binance').textContent = '正在验证账户…';
    try { const r = await api('/config/binance-credentials', { method: 'POST', body: payload, timeout: 120000 }); form.reset(); toast(r.message || '账户验证成功，币安凭据已更新。'); $('binance-status').textContent = '凭据已保存 · 验证成功'; $('binance-status').className = 'badge success'; if (state.config) { state.config.has_binance_key = true; state.config.has_binance_secret = true; } }
    catch (err) { message('binance-save-error', `验证失败：${errorText(err)} 原凭据保持不变。`); }
    finally { lockForm(form, false); $('save-binance').textContent = '验证并更新凭据'; }
  }

  $('position-form').addEventListener('submit', savePosition);
  $('position-form').querySelectorAll('input').forEach((n) => n.addEventListener('input', () => n.setCustomValidity('')));
  $('position-dialog').addEventListener('cancel', (e) => { if (state.positionSaving) e.preventDefault(); });
  document.querySelectorAll('[data-close-dialog]').forEach((n) => n.addEventListener('click', () => { const d = n.closest('dialog'); if (d.id === 'position-dialog' && state.positionSaving) return; d.close(); }));
  $('detail-dialog').addEventListener('close', () => { state.detailSeq += 1; });
  $('hide-all-picks').addEventListener('click', () => hidePicks(null, $('hide-all-picks')).catch((e) => toast(errorText(e), 'failed')));
  $('update-reviews').addEventListener('click', () => updateReviews($('update-reviews')).catch((e) => toast(errorText(e), 'failed')));
  $('log-filter').addEventListener('change', renderLogs);
  $('settings-form').addEventListener('submit', saveConfig);
  $('settings-form').addEventListener('input', () => { state.configDirty = true; $('save-status').textContent = '有未保存的修改'; });
  $('binance-form').addEventListener('submit', saveBinance);
  document.querySelectorAll('[data-test]').forEach((n) => n.addEventListener('click', () => testConnection(n.dataset.test, n).catch((e) => toast(errorText(e), 'failed'))));
  $('refresh-view').addEventListener('click', async () => {
    if (state.view === 'settings' && state.configDirty && !await ask('丢弃未保存的修改？', '刷新配置会清空当前未保存的输入。服务器中已经保存的配置不变。', '丢弃并刷新')) return;
    await busy($('refresh-view'), '…', () => loadView(true));
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('beforeunload', (e) => { if (state.configDirty) { e.preventDefault(); e.returnValue = ''; } });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { loadStatus(); if (state.view === 'positions' && !$('position-dialog').open) loadPositions(); } });
  route(); if (state.view !== 'picks') loadStatus(); schedulePoll();
  // Read defaults without rendering or persisting any credentials into inputs.
  if (state.view !== 'settings') api('/config').then((cfg) => { if (!state.config && !state.configDirty) { state.config = sanitizedConfig(cfg); applyConfig(state.config); } }).catch(() => { /* Settings shows its own retryable error on first visit. */ });
  setInterval(() => { if (!document.hidden && state.view === 'positions' && !$('position-dialog').open && !state.syncing && !$('positions-content').contains(document.activeElement)) loadPositions(); }, 60000);
})();
