/* Интерактивное демо: настоящий интерфейс копилота проигрывает записанный сеанс без сервера.
   window.__DEMO__ = {meta, recs:[{method,path,query,body,status,headers,chunks:[[t,text]],logs:[[t,entry]],seconds}]} */
(function(){
  const D = window.__DEMO__ || {recs: []};
  const RF = window.fetch.bind(window);
  let SPEED = 1;            // 1 = ускоренно (паузы > CAP сжимаются), 0 = как было
  const CAP = 1.6;          // макс. пауза между событиями, с
  const used = {};

  const norm = p => p.replace(/^(\/api\/(?:semantics|defaults|search|map|audit|export|reset|cancel))\/[^/?]+/, '$1/:sid');
  function key(method, path, query, body){
    let k = method + ' ' + norm(path), b = null;
    try { b = body ? JSON.parse(body) : null; } catch(e) {}
    if (path === '/api/chat' && b) k += ' | ' + (b.message || '').trim();
    if (path === '/api/direct' && b) k += ' | ' + b.kind;
    if (path.startsWith('/api/search')) { const q = new URLSearchParams(query || '').get('q'); k += ' | ' + (q || ''); }
    if (path === '/api/upload') k = 'POST /api/upload_path';
    return k;
  }
  const RECS = {};
  D.recs.forEach(r => { const k = key(r.method, r.path, r.query, r.body); (RECS[k] = RECS[k] || []).push(r); });
  const QUESTIONS = D.recs.filter(r => r.path === '/api/chat').map(r => { try { return JSON.parse(r.body).message; } catch(e){ return null; } })
                          .filter((x, i, a) => x && a.indexOf(x) === i);

  function pick(k){
    const list = RECS[k]; if (!list) return null;
    const i = used[k] || 0; used[k] = i + 1;
    return list[Math.min(i, list.length - 1)];
  }
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const enc = new TextEncoder();
  function b64(s){ const bin = atob(s); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; }
  const bytes = c => c.startsWith('b64:') ? b64(c.slice(4)) : enc.encode(c);

  /* ── журнал: поддельный EventSource ── */
  const LOG_LISTENERS = new Set(); let SEQ = 0; let OPS = [];
  function logEmit(ev, data){ LOG_LISTENERS.forEach(l => l(ev, data)); }
  class FakeES {
    constructor(url){ this.url = url; this.h = {}; this.onerror = null;
      if (url.startsWith('/api/logs')) { this.fn = (ev, d) => (this.h[ev] || []).forEach(f => f({data: JSON.stringify(d)})); LOG_LISTENERS.add(this.fn); } }
    addEventListener(ev, f){ (this.h[ev] = this.h[ev] || []).push(f); }
    close(){ LOG_LISTENERS.delete(this.fn); }
  }
  window.EventSource = FakeES;
  setInterval(() => logEmit('pulse', {ops: OPS.map(o => ({name: o.name, elapsed: (Date.now() - o.t0) / 1000, idle: 0, step: o.step || ''})), seq: SEQ}), 700);

  function scaled(chunks){        // исходные времена → проигрываемые
    let prev = 0, acc = 0; const out = [];
    for (const [t] of chunks){ const dt = Math.max(0, t - prev); acc += SPEED ? Math.min(dt, CAP) : dt; out.push(acc); prev = t; }
    return out;
  }
  function playLogs(rec, total, origTotal){
    if (!rec.logs || !rec.logs.length) return;
    const ratio = origTotal > 0 ? total / origTotal : 1;
    const name = ((rec.logs.find(l => (l[1].text || '').startsWith('▶')) || [0, {text: '▶ расчёт'}])[1].text || '').slice(2);
    const op = {name, t0: Date.now(), step: ''}; OPS.push(op);
    rec.logs.forEach(([t, x]) => setTimeout(() => { SEQ += 1; op.step = (x.src === 'tool' || x.src === 'progress') ? (x.text || '').slice(0, 60) : op.step;
      logEmit('log', Object.assign({}, x, {seq: SEQ, t: Date.now() / 1000})); }, Math.min(t, origTotal) * ratio * 1000));
    setTimeout(() => { OPS = OPS.filter(o => o !== op); }, total * 1000 + 300);
  }

  function respond(rec){
    const ct = (rec.headers || {})['content-type'] || 'application/json';
    const chunks = rec.chunks || [];
    const times = scaled(chunks);
    const total = times.length ? times[times.length - 1] : 0.3;
    const origTotal = chunks.length ? chunks[chunks.length - 1][0] : 0;
    playLogs(rec, total, origTotal);
    if (ct.startsWith('text/event-stream')){
      const stream = new ReadableStream({ async start(ctrl){
        let last = 0;
        for (let i = 0; i < chunks.length; i++){ await sleep((times[i] - last) * 1000); last = times[i]; ctrl.enqueue(bytes(chunks[i][1])); }
        ctrl.close(); } });
      return Promise.resolve(new Response(stream, {status: rec.status, headers: rec.headers}));
    }
    const parts = chunks.map(c => bytes(c[1])); const n = parts.reduce((a, p) => a + p.length, 0);
    const all = new Uint8Array(n); let o = 0; parts.forEach(p => { all.set(p, o); o += p.length; });
    return sleep(Math.min(total, SPEED ? 1.2 : total) * 1000).then(() => new Response(all, {status: rec.status, headers: rec.headers}));
  }

  function sse(events){
    const txt = events.map(([ev, d]) => `event: ${ev}\ndata: ${JSON.stringify(d)}\n\n`).join('') + 'event: done\ndata: {}\n\n';
    return new Response(enc.encode(txt), {status: 200, headers: {'content-type': 'text/event-stream'}});
  }

  window.fetch = async function(input, init){
    const url = typeof input === 'string' ? input : input.url;
    if (!url.startsWith('/api/')) return RF(input, init);
    const u = new URL(url, location.href);
    const method = (init && init.method) || 'GET';
    let body = init && typeof init.body === 'string' ? init.body : null;
    if (u.pathname.startsWith('/api/cancel')) return new Response('{}', {headers: {'content-type': 'application/json'}});
    if (u.pathname === '/api/config'){ const r = pick('GET /api/config'); if (r) return respond(r); }
    const rec = pick(key(method, u.pathname, u.search.slice(1), body));
    if (rec) return respond(rec);
    if (u.pathname === '/api/chat'){
      return sse([['answer', {text: 'Это демо-запись реального сеанса: живой расчёт здесь недоступен. Ответы записаны на такие вопросы:\n\n' +
        QUESTIONS.map(q => '- ' + q).join('\n') + '\n\nВыберите вопрос в панели «Демо» справа внизу.'}]]);
    }
    if (u.pathname === '/api/semantics' || u.pathname === '/api/direct' || u.pathname === '/api/kpki' || u.pathname === '/api/expert'){
      const any = (D.recs.find(r => r.path === u.pathname));
      if (any) return respond(any);
      return sse([['error', {text: 'Этой функции нет в демо-записи.'}]]);
    }
    return new Response(JSON.stringify({detail: 'Нет в демо-записи'}), {status: 404, headers: {'content-type': 'application/json'}});
  };

  /* ── панель демо ── */
  window.addEventListener('DOMContentLoaded', () => {
    const m = D.meta || {};
    const box = document.createElement('div');
    box.id = 'demoPanel';
    box.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:999;width:340px;max-height:70vh;overflow:auto;background:#14213D;color:#F6F5F1;border-radius:12px;padding:14px 16px;font:13px/1.45 system-ui,sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.25)';
    box.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><b>Демо-запись</b><span id="dmToggle" style="cursor:pointer;opacity:.8">свернуть</span></div>
      <div id="dmBody"><div style="opacity:.8;margin:6px 0 10px">Реальный интерфейс, записанный сеанс${m.model ? ' на модели «' + m.model + '»' : ''}${m.date ? ' · ' + m.date : ''}. Excel и LLM не нужны.</div>
      <button id="dmLoad" style="width:100%;padding:8px;border-radius:8px;border:0;background:#E9A96B;color:#14213D;font-weight:600;cursor:pointer">1. Загрузить демо-модель</button>
      <div style="margin:10px 0 4px;opacity:.8">2. Кнопки на панели сверху: карта, предпроверка, факторы, эксперт, КПКИ.</div>
      <div style="margin:8px 0 4px;opacity:.8">3. Вопросы агенту:</div><div id="dmQ"></div>
      <label style="display:flex;gap:6px;align-items:center;margin-top:10px;opacity:.85"><input type="checkbox" id="dmReal"> реальное время ответов</label></div>`;
    document.body.appendChild(box);
    const qbox = box.querySelector('#dmQ');
    QUESTIONS.forEach(q => { const b = document.createElement('button'); b.textContent = q;
      b.style.cssText = 'display:block;width:100%;text-align:left;margin:4px 0;padding:6px 8px;border-radius:8px;border:1px solid #3B4F7A;background:#1F3157;color:#F6F5F1;cursor:pointer;font:12px/1.35 system-ui';
      b.onclick = () => { const t = document.getElementById('q'); if (!t || typeof send !== 'function') return; t.value = q; send(); }; qbox.appendChild(b); });
    if (!QUESTIONS.length) qbox.innerHTML = '<div style="opacity:.6">В записи нет вопросов в чат.</div>';
    box.querySelector('#dmLoad').onclick = () => { if (typeof uploadPath === 'function') uploadPath(m.model || 'demo.xlsx'); };
    box.querySelector('#dmReal').onchange = e => { SPEED = e.target.checked ? 0 : 1; };
    box.querySelector('#dmToggle').onclick = e => { const b = box.querySelector('#dmBody'); const h = b.style.display === 'none'; b.style.display = h ? '' : 'none'; e.target.textContent = h ? 'свернуть' : 'развернуть'; };
  });
})();
