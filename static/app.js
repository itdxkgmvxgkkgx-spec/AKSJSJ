/* VLESS Forge – frontend logic (vanilla JS) */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  const el = {
    phasePill: $('#phase-pill'), phaseText: $('#phase-text'),
    start: $('#btn-start'), stop: $('#btn-stop'),
    bar: $('#progress-bar'), msg: $('#progress-msg'), eta: $('#progress-eta'),
    stTotal: $('#st-total'), stWorking: $('#st-working'), stFailed: $('#st-failed'),
    stBest: $('#st-best'), stSource: $('#st-source'), stTime: $('#st-time'),
    userinfo: $('#userinfo'), uiTitle: $('#ui-title'), uiBar: $('#ui-bar-fill'),
    uiUsed: $('#ui-used'), uiTotal: $('#ui-total'), uiExpire: $('#ui-expire'),
    tbody: $('#tbody'), log: $('#log'), search: $('#search'), lastRun: $('#last-run'),
    subUrl: $('#sub-url'), toast: $('#toast'),
    modal: $('#modal'), qr: $('#qr'), modalLink: $('#modal-link'), modalTitle: $('#modal-title'),
  };

  const state = { items: [], filter: 'all', q: '', sortKey: 'latency_ms', sortAsc: true, lastLogLen: 0, lastResultsMtime: null, running: false, startedAt: null };

  /* ---------- helpers ---------- */
  const fmtBytes = b => { if (b == null) return '—'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + ' ' + u[i]; };
  const fmtDur = s => { if (s == null) return '—'; s = Math.max(0, Math.round(s)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`; };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const phaseFa = { idle: 'آماده', fetch: 'دریافت اشتراک', generate: 'تولید', test: 'تست', xray: 'تست Xray', write: 'ذخیره', done: 'تمام شد', error: 'خطا' };

  function toast(msg, icon = 'fa-circle-check') {
    el.toast.innerHTML = `<i class="fa-solid ${icon}"></i>${esc(msg)}`;
    el.toast.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(() => el.toast.classList.remove('show'), 2200);
  }
  async function copy(text, label = 'کپی شد') {
    try { await navigator.clipboard.writeText(text); }
    catch { const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); }
    toast(label);
  }

  /* ---------- status polling (SSE with fallback) ---------- */
  function connect() {
    if (window.EventSource) {
      const es = new EventSource('/events');
      es.onmessage = e => applyStatus(JSON.parse(e.data));
      es.onerror = () => { es.close(); setTimeout(connect, 2000); };
    } else {
      setInterval(async () => { try { applyStatus(await (await fetch('/api/status')).json()); } catch { } }, 1000);
    }
  }

  function applyStatus(st) {
    const running = !!st.running;
    if (running && !state.running) state.startedAt = Date.now();
    state.running = running;
    el.start.disabled = running; el.stop.disabled = !running;
    el.start.innerHTML = running ? '<i class="fa-solid fa-spinner fa-spin"></i><span>در حال اجرا…</span>' : '<i class="fa-solid fa-play"></i><span>شروع تولید</span>';

    const phase = st.phase || 'idle';
    el.phasePill.className = 'pill ' + (running ? 'running' : phase === 'done' ? 'done' : phase === 'error' ? 'error' : 'idle');
    el.phaseText.textContent = phaseFa[phase] || phase;

    const total = st.total || 0, done = st.done || 0;
    const pct = total ? Math.min(100, done / total * 100) : (running ? 8 : phase === 'done' ? 100 : 0);
    el.bar.style.width = pct + '%';
    el.msg.textContent = st.message || (running ? 'در حال کار…' : 'در انتظار شروع…');
    el.eta.textContent = running && st.eta_sec != null && total ? `${done}/${total} · ~${fmtDur(st.eta_sec)} باقی` : total ? `${done}/${total}` : '';

    el.stTotal.textContent = total; el.stWorking.textContent = st.working || 0; el.stFailed.textContent = st.failed || 0;
    el.stBest.textContent = st.best_ms != null ? Math.round(st.best_ms) + ' ms' : '—';
    el.stSource.textContent = st.source_count || 0;
    if (st.started_at) {
      const end = st.finished_at ? new Date(st.finished_at) : new Date();
      el.stTime.textContent = fmtDur((end - new Date(st.started_at)) / 1000);
    }
    if (st.finished_at) el.lastRun.textContent = 'آخرین اجرا: ' + new Date(st.finished_at).toLocaleString('fa-IR');

    // userinfo
    const ui = st.userinfo || {};
    if (ui.total) {
      el.userinfo.classList.remove('hidden');
      const used = (ui.upload || 0) + (ui.download || 0);
      el.uiBar.style.width = Math.min(100, used / ui.total * 100).toFixed(1) + '%';
      el.uiUsed.textContent = fmtBytes(used); el.uiTotal.textContent = fmtBytes(ui.total);
      el.uiExpire.textContent = ui.expire ? new Date(ui.expire * 1000).toLocaleDateString('fa-IR') + ` (${Math.ceil((ui.expire * 1000 - Date.now()) / 864e5)} روز)` : '—';
      el.uiTitle.textContent = st.profile_title ? `· ${st.profile_title}` : '';
    }

    // logs
    const logs = st.logs || [];
    if (logs.length !== state.lastLogLen || (logs.length && logs[logs.length - 1].msg !== state.lastLogMsg)) {
      el.log.innerHTML = logs.map(l => `<div class="l-${l.lvl}"><span class="t">${l.t}</span>${esc(l.msg)}</div>`).join('');
      state.lastLogLen = logs.length; state.lastLogMsg = logs.length ? logs[logs.length - 1].msg : '';
      if ($('#autoscroll').checked) el.log.scrollTop = el.log.scrollHeight;
    }
    if (st.error) toast(st.error, 'fa-triangle-exclamation');

    // results refresh
    const m = st.files && st.files['result.json'];
    if (m && m !== state.lastResultsMtime) { state.lastResultsMtime = m; loadResults(); }
  }

  async function loadResults() {
    try {
      const d = await (await fetch('/api/results')).json();
      state.items = (d.items || []).map((it, i) => ({ ...it, rank: it.ok ? null : 9999 }));
      let r = 0; state.items.filter(i => i.ok).sort((a, b) => a.latency_ms - b.latency_ms).forEach(i => i.rank = ++r);
      render();
    } catch (e) { console.error(e); }
  }

  /* ---------- table ---------- */
  function latClass(ms) { return ms < 300 ? 'g' : ms < 700 ? 'y' : 'r'; }
  function render() {
    let rows = state.items.filter(i => state.filter === 'all' || (state.filter === 'ok') === !!i.ok);
    if (state.q) { const q = state.q.toLowerCase(); rows = rows.filter(i => [i.host, i.address, i.fp, i.sni, i.tag, i.name, i.source_name].join(' ').toLowerCase().includes(q)); }
    const k = state.sortKey;
    rows.sort((a, b) => {
      let x = a[k], y = b[k];
      if (k === 'latency_ms') { x = x ?? 1e9; y = y ?? 1e9; }
      if (k === 'ok') { x = +!!x; y = +!!y; }
      if (typeof x === 'string') return state.sortAsc ? x.localeCompare(y) : y.localeCompare(x);
      return state.sortAsc ? x - y : y - x;
    });
    if (!rows.length) { el.tbody.innerHTML = `<tr><td colspan="9" class="empty"><i class="fa-solid fa-satellite-dish"></i><p>${state.items.length ? 'چیزی با این فیلتر پیدا نشد.' : 'هنوز نتیجه‌ای نیست. روی «شروع تولید» بزن.'}</p></td></tr>`; return; }
    const maxLat = Math.max(...rows.filter(r => r.ok).map(r => r.latency_ms), 1);
    el.tbody.innerHTML = rows.map((it, idx) => `
      <tr style="animation-delay:${Math.min(idx, 30) * 12}ms" data-id="${it.id}">
        <td class="rank ${it.rank <= 3 ? 'top' : ''}">${it.ok ? (it.rank <= 3 ? ['🥇', '🥈', '🥉'][it.rank - 1] : it.rank) : '—'}</td>
        <td>${it.ok ? '<span class="badge ok"><i class="fa-solid fa-check"></i> سالم</span>' : '<span class="badge bad"><i class="fa-solid fa-xmark"></i> ناموفق</span>'}</td>
        <td>${it.ok ? `<span class="lat ${latClass(it.latency_ms)}">${Math.round(it.latency_ms)} ms <span class="bar"><i style="width:${Math.max(6, 100 - it.latency_ms / maxLat * 100 + 6)}%;background:currentColor"></i></span></span>` : `<span class="err">${esc(it.error || '')}</span>`}</td>
        <td><div class="host"><span class="flag">${it.flag || '🌐'}</span><div>${esc(it.source_name.replace(it.flag || '', '').trim() || it.host)}<small>${esc(it.host)}:${it.port}</small></div></div></td>
        <td><span class="addr">${esc(it.address)}</span></td>
        <td><span class="tag">${esc(it.fp)}${it.spx ? ' +spx' : ''}</span></td>
        <td><span class="addr">${esc(it.sni || '')}</span></td>
        <td><span class="tag" style="background:rgba(244,114,182,.12);color:#f9a8d4">${esc(it.security || '')}/${esc(it.network || '')}</span></td>
        <td><div class="row-actions">
          <button title="کپی" data-act="copy"><i class="fa-regular fa-copy"></i></button>
          <button title="QR" data-act="qr"><i class="fa-solid fa-qrcode"></i></button>
        </div></td>
      </tr>`).join('');
  }

  el.tbody.addEventListener('click', e => {
    const b = e.target.closest('button[data-act]'); if (!b) return;
    const id = +b.closest('tr').dataset.id; const it = state.items.find(i => i.id === id); if (!it) return;
    if (b.dataset.act === 'copy') copy(it.link, 'کانفیگ کپی شد');
    else showQR(it.link, it.name);
  });
  $$('thead th[data-sort]').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.sort;
    if (state.sortKey === k) state.sortAsc = !state.sortAsc; else { state.sortKey = k; state.sortAsc = true; }
    $$('thead th').forEach(t => t.classList.remove('sorted', 'asc'));
    th.classList.add('sorted'); if (state.sortAsc) th.classList.add('asc');
    render();
  }));
  $$('.seg button').forEach(b => b.addEventListener('click', () => { $$('.seg button').forEach(x => x.classList.remove('active')); b.classList.add('active'); state.filter = b.dataset.filter; render(); }));
  el.search.addEventListener('input', () => { state.q = el.search.value.trim(); render(); });

  /* ---------- QR ---------- */
  function showQR(text, title) {
    el.qr.innerHTML = '';
    try {
      const q = qrcode(0, 'M'); q.addData(text); q.make();
      el.qr.innerHTML = q.createImgTag(5, 8);
    } catch { el.qr.textContent = 'متن طولانی است'; }
    el.modalTitle.textContent = title || 'QR Code'; el.modalLink.textContent = text;
    $('#modal-copy').onclick = () => copy(text);
    el.modal.classList.remove('hidden');
  }
  $('#modal-close').onclick = () => el.modal.classList.add('hidden');
  el.modal.addEventListener('click', e => { if (e.target === el.modal) el.modal.classList.add('hidden'); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') el.modal.classList.add('hidden'); });

  /* ---------- actions ---------- */
  el.start.onclick = async () => {
    const body = { total: $('#opt-total').value, threads: $('#opt-threads').value, mode: $('#opt-mode').value, rounds: $('#opt-rounds').value, timeout: $('#opt-timeout').value };
    localStorage.setItem('vf-opts', JSON.stringify(body));
    const r = await fetch('/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json();
    toast(d.ok ? 'تولید شروع شد ⚡' : 'در حال اجراست', d.ok ? 'fa-rocket' : 'fa-triangle-exclamation');
  };
  el.stop.onclick = async () => { await fetch('/api/stop'); toast('متوقف شد', 'fa-stop'); };
  $('#btn-copy-all').onclick = () => { const ok = state.items.filter(i => i.ok).sort((a, b) => a.latency_ms - b.latency_ms); if (!ok.length) return toast('کانفیگ سالمی نیست', 'fa-triangle-exclamation'); copy(ok.map(i => i.link).join('\n'), `${ok.length} کانفیگ کپی شد`); };
  const subUrl = location.origin + '/sub/b64';
  el.subUrl.textContent = subUrl;
  $('#btn-copy-sub').onclick = () => copy(subUrl, 'لینک اشتراک کپی شد');
  $('#btn-sub-qr').onclick = () => showQR(subUrl, 'لینک اشتراک (base64)');

  /* ---------- theme / prefs ---------- */
  const theme = localStorage.getItem('vf-theme'); if (theme) document.documentElement.dataset.theme = theme;
  $('#btn-theme').onclick = () => { const t = document.documentElement.dataset.theme === 'light' ? '' : 'light'; document.documentElement.dataset.theme = t; localStorage.setItem('vf-theme', t); };
  try { const o = JSON.parse(localStorage.getItem('vf-opts') || '{}'); for (const [k, v] of Object.entries(o)) { const i = $('#opt-' + k); if (i) i.value = v; } } catch { }

  /* ---------- boot ---------- */
  loadResults();
  fetch('/api/status').then(r => r.json()).then(applyStatus).catch(() => { });
  // open the SSE stream only after the page has fully loaded (keeps the load event clean)
  if (document.readyState === 'complete') setTimeout(connect, 300);
  else window.addEventListener('load', () => setTimeout(connect, 300));
})();
