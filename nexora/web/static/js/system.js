'use strict';
(function () {
  const root = document.getElementById('system');
  const csrf = root.dataset.csrf;
  // Escapes text for HTML, including quotes, so it's also safe inside attribute values.
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const size = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB');
  const toast = (m, e) => { const t = document.createElement('div'); t.className = 'toast' + (e ? ' error' : ''); t.textContent = m; document.getElementById('toasts').appendChild(t); setTimeout(() => t.remove(), 5000); };
  async function call(method, url, body) {
    const r = await fetch(url, { method, credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'Request failed');
    return d;
  }
  async function load() {
    const s = await call('GET', '/api/system');
    const b = s.bot, db = s.database;
    document.getElementById('sys-body').innerHTML = `
      <div class="stats">
        <div class="stat"><div class="v">${b.ready ? 'Online' : 'Offline'}</div><div class="l">${esc(b.user || 'Bot not logged in')}</div></div>
        <div class="stat"><div class="v">${b.guilds}</div><div class="l">Servers</div></div>
        <div class="stat"><div class="v">${b.latency_ms != null ? b.latency_ms + ' ms' : '—'}</div><div class="l">Gateway latency</div></div>
        <div class="stat"><div class="v">${db.query_ms} ms</div><div class="l">Database query time</div></div>
        <div class="stat"><div class="v">${size(db.size_bytes)}</div><div class="l">Database size</div></div>
        <div class="stat"><div class="v">${b.commands_run}</div><div class="l">Commands since start</div></div>
      </div>
      <div class="grid-2">
        <section class="panel"><div class="panel-head"><h3>Database backups</h3><button class="btn primary small" id="bk-new">Back up now</button></div>
          <p class="panel-desc">Stored in ${esc(db.path.replace(/[^\\/]+$/, ''))}backups. Restoring makes a safety copy first.</p>
          ${s.backups.length ? `<table class="data">${s.backups.map((k) => `<tr><td>${esc(k.name)}</td><td class="faint">${size(k.size_bytes)}</td><td class="right"><button class="btn small danger" data-restore="${esc(k.name)}">Restore</button></td></tr>`).join('')}</table>` : '<p class="muted">No backups yet.</p>'}</section>
        <section class="panel"><h3>Rows stored</h3><table class="data">${Object.entries(db.rows).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="right">${v.toLocaleString()}</td></tr>`).join('')}</table>
          <h3 class="mt">Loaded modules</h3><p class="muted">${b.cogs.map(esc).join(', ') || 'Bot not running'}</p></section>
      </div>
      <section class="panel"><h3>Feature usage across all servers</h3>${s.feature_usage.length ? `<table class="data">${s.feature_usage.map((f) => `<tr><td>${esc(f.feature)}</td><td class="right">${f.n.toLocaleString()}</td></tr>`).join('')}</table>` : '<p class="muted">No usage yet.</p>'}</section>
      <section class="panel"><div class="panel-head"><h3>Recent errors</h3>${s.errors.length ? '<button class="btn small" id="err-clear">Clear</button>' : ''}</div>
        ${s.errors.length ? `<table class="data"><tr><th>When (UTC)</th><th>Source</th><th>Server</th><th>Error</th></tr>${s.errors.map((e) => `<tr><td class="nowrap faint">${esc(e.created_at)}</td><td>${esc(e.source)}</td><td class="faint">${esc(e.guild_id || '—')}</td><td>${esc(e.message)}</td></tr>`).join('')}</table>` : '<p class="muted">No errors recorded. 🎉</p>'}</section>`;
    document.getElementById('bk-new').onclick = async () => { try { const r = await call('POST', '/api/system/backup'); toast('Backup created: ' + r.name); load(); } catch (e) { toast(e.message, true); } };
    document.querySelectorAll('[data-restore]').forEach((btn) => { btn.onclick = async () => {
      if (!confirm('Restore ' + btn.dataset.restore + '? Current data will be replaced (a safety backup is made first).')) return;
      try { const r = await call('POST', '/api/system/restore', { name: btn.dataset.restore }); toast('Restored. Safety copy: ' + r.safety_backup); load(); } catch (e) { toast(e.message, true); }
    }; });
    const clr = document.getElementById('err-clear');
    if (clr) clr.onclick = async () => { await call('POST', '/api/system/errors/clear'); load(); };
  }
  document.addEventListener('DOMContentLoaded', () => load().catch((e) => { document.getElementById('sys-body').innerHTML = `<p class="notice">${esc(e.message)}</p>`; }));
})();
