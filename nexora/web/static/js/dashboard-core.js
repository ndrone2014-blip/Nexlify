/* Nexora dashboard core: API client, helpers, schema-driven forms, dialogs, router. */
'use strict';

const NX = {
  gid: null, csrf: null, meta: null, pages: {}, current: null, charts: [], timers: [],
  maps: { channels: {}, roles: {}, voice: {}, categories: {} },
};

// ------------------------------------------------------------------ helpers
const esc = (s) => { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const icon = (n) => window.nxIcon(n);
const fmtNum = (n) => (n == null ? '—' : Number(n).toLocaleString());
function fmtTime(s) {
  if (!s) return '—';
  const d = new Date(String(s).replace(' ', 'T') + (String(s).length <= 19 ? 'Z' : ''));
  return isNaN(d) ? s : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
function fmtAgo(s) {
  const d = new Date(String(s).replace(' ', 'T') + 'Z'); const sec = (Date.now() - d) / 1000;
  if (isNaN(sec)) return s;
  if (sec < 60) return 'just now'; if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h ago'; return Math.floor(sec / 86400) + 'd ago';
}
function fmtDuration(sec) {
  if (!sec) return '—'; const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return [d && d + 'd', h && h + 'h', m && m + 'm'].filter(Boolean).join(' ') || sec + 's';
}
const chName = (id) => (id && NX.maps.channels[id] ? '#' + NX.maps.channels[id].name : (id ? 'deleted channel' : '—'));
const roleName = (id) => (id && NX.maps.roles[id] ? '@' + NX.maps.roles[id].name : (id ? 'deleted role' : '—'));
function userCell(u, fallbackId) {
  if (!u && !fallbackId) return '<span class="faint">—</span>';
  u = u || { id: fallbackId };
  const name = u.name || u.tag || ('User ' + u.id);
  return `<span class="user-cell">${u.avatar ? `<img src="${esc(u.avatar)}" alt="">` : ''}<span>${esc(name)}${u.name ? '' : ' <span class="faint">(left)</span>'}</span></span>`;
}
const canWrite = () => NX.meta && NX.meta.access === 'manage';
function pill(text, kind) { return `<span class="pill ${kind || ''}">${esc(text)}</span>`; }
const statusPill = (s) => pill(s, { open: 'info', pending: 'warn', running: 'info', scheduled: 'info', accepted: 'ok', approved: 'ok', confirmed: 'ok', ended: '', closed: '', denied: 'bad', cancelled: 'bad', dismissed: '', actioned: 'bad', implemented: 'ok', considering: 'warn', ok: 'ok', error: 'bad', skipped: '' }[s] || '');

// --------------------------------------------------------------------- API
async function api(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: { 'X-CSRF-Token': NX.csrf } };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const url = path.startsWith('/') ? path : `/api/guilds/${NX.gid}/${path}`;
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = {}; }
  if (res.status === 401) { location.href = '/auth/login?next=' + encodeURIComponent(location.pathname + location.hash); throw new Error('Not logged in'); }
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.fields = data.errors || {}; throw err;
  }
  return data;
}
const get = (p) => api('GET', p);
const post = (p, b) => api('POST', p, b === undefined ? {} : b);
const put = (p, b) => api('PUT', p, b);
const del = (p) => api('DELETE', p);

function toast(msg, isError) {
  const t = document.createElement('div');
  t.className = 'toast' + (isError ? ' error' : ''); t.textContent = msg;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), isError ? 7000 : 3500);
}
async function run(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg); return r; }
  catch (e) {
    const extra = e.fields && Object.keys(e.fields).length ? ': ' + Object.entries(e.fields).map(([k, v]) => `${k} ${v}`).join('; ') : '';
    toast(e.message + extra, true); throw e;
  }
}

// ------------------------------------------------------------------ dialog
const Dialog = {
  open(title, bodyHtml, buttons) {
    const dlg = $('#dlg');
    $('#dlg-title').textContent = title; $('#dlg-body').innerHTML = bodyHtml;
    const foot = $('#dlg-foot'); foot.innerHTML = '';
    (buttons || []).forEach((b) => {
      const el = document.createElement('button');
      el.className = 'btn ' + (b.cls || ''); el.textContent = b.label; el.type = 'button';
      el.addEventListener('click', async () => {
        if (!b.onClick) return Dialog.close();
        el.disabled = true;
        try { const keep = await b.onClick($('#dlg-body')); if (keep !== true) Dialog.close(); } catch (e) { /* toast shown */ } finally { el.disabled = false; }
      });
      foot.appendChild(el);
    });
    foot.hidden = !(buttons && buttons.length);
    if (!dlg.open) dlg.showModal();
    return $('#dlg-body');
  },
  close() { const d = $('#dlg'); if (d.open) d.close(); },
};
function confirmDialog(title, text, label, cls) {
  return new Promise((resolve) => {
    Dialog.open(title, `<p>${esc(text)}</p>`, [
      { label: 'Cancel', cls: 'ghost', onClick: () => resolve(false) },
      { label: label || 'Confirm', cls: cls || 'primary', onClick: () => resolve(true) },
    ]);
  });
}

// --------------------------------------------------------- form controls
function options(list, selected, placeholder) {
  return (placeholder !== undefined ? `<option value="">${esc(placeholder)}</option>` : '') +
    list.map((o) => `<option value="${esc(o.id)}" ${String(o.id) === String(selected) ? 'selected' : ''}>${esc(o.label)}</option>`).join('');
}
const channelList = () => NX.meta.channels.map((c) => ({ id: c.id, label: (c.category ? c.category + ' / ' : '') + '#' + c.name + (c.can_send ? '' : ' (no access)') }));
const roleList = (assignableOnly) => NX.meta.roles.filter((r) => !assignableOnly || r.assignable).map((r) => ({ id: r.id, label: '@' + r.name }));
const voiceList = () => NX.meta.voice_channels.map((c) => ({ id: c.id, label: '🔊 ' + c.name }));
const categoryList = () => NX.meta.categories.map((c) => ({ id: c.id, label: c.name }));
const channelSelect = (name, v, ph) => `<select name="${name}">${options(channelList(), v, ph === undefined ? 'None' : ph)}</select>`;
const roleSelect = (name, v, ph, assignable) => `<select name="${name}">${options(roleList(assignable), v, ph === undefined ? 'None' : ph)}</select>`;

/* Multi-select rendered as removable chips. Value is stored as JSON in data-value. */
function chipPicker(name, values, kind) {
  const list = kind === 'roles' ? roleList() : channelList();
  return `<div class="chips" data-chips="${name}" data-kind="${kind}" data-value='${esc(JSON.stringify(values || []))}'></div>`;
}
function hydrateChips(root) {
  $$('[data-chips]', root).forEach((box) => {
    const kind = box.dataset.kind;
    const list = kind === 'roles' ? roleList() : channelList();
    const draw = () => {
      const vals = JSON.parse(box.dataset.value || '[]');
      box.innerHTML = vals.map((v) => {
        const r = kind === 'roles' ? NX.maps.roles[v] : NX.maps.channels[v];
        const dot = kind === 'roles' && r && r.color ? `<i style="background:${esc(r.color)}"></i>` : '';
        return `<span class="chip">${dot}${esc(kind === 'roles' ? roleName(v) : chName(v))}<button type="button" data-remove="${esc(v)}" aria-label="Remove">×</button></span>`;
      }).join('') + `<select aria-label="Add">${options(list.filter((o) => !vals.includes(o.id)), '', '+ Add')}</select>`;
      $('select', box).addEventListener('change', (e) => {
        if (!e.target.value) return;
        box.dataset.value = JSON.stringify([...vals, e.target.value]); draw(); box.dispatchEvent(new Event('change', { bubbles: true }));
      });
      $$('[data-remove]', box).forEach((b) => b.addEventListener('click', () => {
        box.dataset.value = JSON.stringify(vals.filter((x) => x !== b.dataset.remove)); draw(); box.dispatchEvent(new Event('change', { bubbles: true }));
      }));
    };
    draw();
  });
}
function switchHtml(name, checked, label, help) {
  return `<div class="toggle"><div><label for="sw-${name}">${esc(label)}</label>${help ? `<div class="help">${esc(help)}</div>` : ''}</div>
    <label class="switch"><input type="checkbox" id="sw-${name}" name="${name}" ${checked ? 'checked' : ''}><span></span></label></div>`;
}
/* Read a simple form into an object: inputs by name, chip pickers by data-chips. */
function readForm(root) {
  const out = {};
  $$('input[name], select[name], textarea[name]', root).forEach((el) => {
    if (el.closest('[data-rows]') || el.closest('[data-list]')) return;
    out[el.name] = el.type === 'checkbox' ? el.checked : el.value;
  });
  $$('[data-chips]', root).forEach((b) => { if (!b.closest('[data-rows]')) out[b.dataset.chips] = JSON.parse(b.dataset.value || '[]'); });
  return out;
}

// ------------------------------------------------- schema-driven settings
function fieldHtml(f, value) {
  const k = f.key, id = 'f-' + k, t = f.type;
  const help = f.help ? `<div class="help">${esc(f.help)}</div>` : '';
  const lab = `<label for="${id}">${esc(f.label)}</label>`;
  let input;
  switch (t) {
    case 'bool': return `<div class="field">${switchHtml(k, value, f.label, f.help)}</div>`;
    case 'int': case 'float':
      input = `<input id="${id}" name="${k}" type="number" ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''} step="${t === 'float' ? '0.1' : '1'}" value="${esc(value)}">`; break;
    case 'text': case 'url': input = `<input id="${id}" name="${k}" type="${t === 'url' ? 'url' : 'text'}" maxlength="${f.max_length || 1000}" value="${esc(value)}">`; break;
    case 'textarea': return `<div class="field wide">${lab}<textarea id="${id}" name="${k}" maxlength="${f.max_length || 4000}">${esc(value)}</textarea>${help}</div>`;
    case 'color': input = `<div class="inline-flex"><input type="color" value="${esc(value)}" data-mirror="${k}"><input id="${id}" name="${k}" value="${esc(value)}" maxlength="7" style="width:110px"></div>`; break;
    case 'channel': input = `<select id="${id}" name="${k}">${options(channelList(), value, 'None')}</select>`; break;
    case 'voice_channel': input = `<select id="${id}" name="${k}">${options(voiceList(), value, 'None')}</select>`; break;
    case 'category': input = `<select id="${id}" name="${k}">${options(categoryList(), value, 'None')}</select>`; break;
    case 'role': input = `<select id="${id}" name="${k}">${options(roleList(), value, 'None')}</select>`; break;
    case 'channels': case 'roles': input = chipPicker(k, value, t); break;
    case 'select': input = `<select id="${id}" name="${k}">${options(f.options.map((o) => ({ id: o[0], label: o[1] })), value)}</select>`; break;
    case 'string_list': return `<div class="field wide">${lab}<textarea id="${id}" name="${k}" data-list-type="string_list" placeholder="One per line">${esc((value || []).join('\n'))}</textarea><div class="help">${esc(f.help || 'One per line.')}</div></div>`;
    case 'rows': return `<div class="field wide">${lab}${help}<div data-rows="${k}"></div></div>`;
    default: input = `<input id="${id}" name="${k}" value="${esc(value)}">`;
  }
  return `<div class="field">${lab}${input}${help}<div class="err-text" data-err="${k}"></div></div>`;
}

function rowsEditor(container, field, rows, onChange) {
  const cols = field.columns;
  const draw = () => {
    container.innerHTML = `<div class="rows-table">${rows.map((r, i) => `<div class="r" style="--cols:${cols.length}" data-i="${i}">${cols.map((c) => {
      const v = r[c.key];
      let inp;
      if (c.type === 'role') inp = `<select data-col="${c.key}">${options(roleList(), v, 'Choose role')}</select>`;
      else if (c.type === 'channel') inp = `<select data-col="${c.key}">${options(channelList(), v, 'Choose channel')}</select>`;
      else if (c.type === 'select') inp = `<select data-col="${c.key}">${options(c.options.map((o) => ({ id: o[0], label: o[1] })), v)}</select>`;
      else inp = `<input data-col="${c.key}" type="number" step="${c.type === 'float' ? '0.1' : '1'}" value="${esc(v)}">`;
      return `<div><label class="help">${esc(c.label)}</label>${inp}</div>`;
    }).join('')}<button type="button" class="btn small danger" data-del="${i}" aria-label="Remove row">${'Remove'}</button></div>`).join('')}</div>
    <button type="button" class="btn small mt" data-add>${icon('plus')} Add row</button>`;
    $$('[data-col]', container).forEach((el) => el.addEventListener('change', () => {
      const i = +el.closest('[data-i]').dataset.i; const col = cols.find((c) => c.key === el.dataset.col);
      rows[i][el.dataset.col] = (col.type === 'int' || col.type === 'float') ? Number(el.value) : el.value; onChange();
    }));
    $$('[data-del]', container).forEach((b) => b.addEventListener('click', () => { rows.splice(+b.dataset.del, 1); draw(); onChange(); }));
    $('[data-add]', container).addEventListener('click', () => {
      const r = {}; cols.forEach((c) => { r[c.key] = c.default; }); rows.push(r); draw(); onChange();
    });
  };
  draw();
}

/* Render a settings module (or some of its sections) with a sticky save bar. */
async function renderSettings(container, module, opts = {}) {
  container.innerHTML = '<p class="loading">Loading settings…</p>';
  const spec = NX.meta.modules[module];
  const { values } = await get(`settings/${module}`);
  let current = JSON.parse(JSON.stringify(values));
  const sections = spec.sections.filter((s, i) => !opts.sections || opts.sections.includes(i));
  container.innerHTML = (opts.intro !== false ? `<p class="panel-desc">${esc(spec.description)}</p>` : '') + sections.map((s, si) => `
    <section class="panel" data-section="${si}"><h3>${esc(s.title)}</h3><div class="form-grid mt">${s.fields.map((f) => fieldHtml(f, current[f.key])).join('')}</div></section>`).join('') +
    `<div class="savebar" hidden><span class="muted">You have unsaved changes</span><button class="btn ghost" data-discard>Discard</button><button class="btn primary" data-save>Save changes</button></div>
     ${canWrite() ? `<div class="right"><button class="btn ghost small" data-reset>Reset ${esc(spec.title)} to defaults</button></div>` : ''}`;
  hydrateChips(container);
  const rowsState = {};
  const fields = sections.flatMap((s) => s.fields);
  const bar = $('.savebar', container);
  const dirty = () => { if (canWrite()) bar.hidden = false; };
  fields.filter((f) => f.type === 'rows').forEach((f) => {
    rowsState[f.key] = JSON.parse(JSON.stringify(current[f.key] || []));
    rowsEditor($(`[data-rows="${f.key}"]`, container), f, rowsState[f.key], dirty);
  });
  $$('[data-mirror]', container).forEach((c) => {
    const txt = $(`input[name="${c.dataset.mirror}"]`, container);
    c.addEventListener('input', () => { txt.value = c.value.toUpperCase(); dirty(); });
    txt.addEventListener('input', () => { if (/^#[0-9a-f]{6}$/i.test(txt.value)) c.value = txt.value; });
  });
  container.addEventListener('input', dirty);
  container.addEventListener('change', dirty);
  if (!canWrite()) $$('input, select, textarea, button[data-add], button[data-del]', container).forEach((el) => { el.disabled = true; });

  const collect = () => {
    const out = {};
    fields.forEach((f) => {
      if (f.type === 'rows') { out[f.key] = rowsState[f.key]; return; }
      if (f.type === 'channels' || f.type === 'roles') { out[f.key] = JSON.parse($(`[data-chips="${f.key}"]`, container).dataset.value || '[]'); return; }
      const el = $(`[name="${f.key}"]`, container); if (!el) return;
      if (f.type === 'bool') out[f.key] = el.checked;
      else if (f.type === 'string_list') out[f.key] = el.value.split(/\n|,/).map((s) => s.trim()).filter(Boolean);
      else if (f.type === 'int' || f.type === 'float') out[f.key] = el.value === '' ? null : Number(el.value);
      else out[f.key] = el.value === '' && ['channel', 'role', 'voice_channel', 'category'].includes(f.type) ? null : el.value;
    });
    return out;
  };
  $('[data-save]', container).addEventListener('click', async () => {
    $$('[data-err]', container).forEach((e) => { e.textContent = ''; });
    try {
      const r = await api('PUT', `settings/${module}`, collect());
      current = r.values; bar.hidden = true; toast('Settings saved');
      if (opts.onSave) opts.onSave(r.values);
    } catch (e) {
      Object.entries(e.fields || {}).forEach(([k, v]) => { const el = $(`[data-err="${k}"]`, container); if (el) el.textContent = v; });
      toast(e.message, true);
    }
  });
  $('[data-discard]', container).addEventListener('click', () => renderSettings(container, module, opts));
  const reset = $('[data-reset]', container);
  if (reset) reset.addEventListener('click', async () => {
    if (!await confirmDialog('Reset settings', `Reset every ${spec.title} setting to its default? This can't be undone.`, 'Reset', 'danger')) return;
    await run(() => post(`settings/${module}/reset`), 'Settings reset');
    renderSettings(container, module, opts);
  });
}

// ------------------------------------------------------------------- tabs
function tabs(container, defs, initial) {
  container.innerHTML = `<div class="tabs" role="tablist">${defs.map((d) => `<button role="tab" data-tab="${d.id}">${esc(d.label)}</button>`).join('')}</div><div data-tab-body></div>`;
  const body = $('[data-tab-body]', container);
  const show = (id) => {
    $$('[data-tab]', container).forEach((b) => b.classList.toggle('active', b.dataset.tab === id));
    NX.clearTimers();
    const d = defs.find((x) => x.id === id) || defs[0];
    body.innerHTML = '<p class="loading">Loading…</p>';
    Promise.resolve(d.render(body)).catch((e) => { body.innerHTML = `<p class="notice">${esc(e.message)}</p>`; });
    history.replaceState(null, '', `#${NX.current}/${d.id}`);
  };
  $$('[data-tab]', container).forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  show(initial && defs.some((d) => d.id === initial) ? initial : defs[0].id);
}

// ------------------------------------------------------------------ charts
function chart(canvas, type, labels, datasets, extra) {
  if (!window.Chart) { canvas.parentElement.innerHTML = '<p class="faint">Charts need an internet connection to load Chart.js.</p>'; return null; }
  const palette = ['#8b5cf6', '#34d399', '#f87171', '#fbbf24', '#60a5fa', '#f472b6', '#22d3ee'];
  const c = new Chart(canvas, {
    type, data: { labels, datasets: datasets.map((d, i) => ({ borderColor: palette[i % 7], backgroundColor: type === 'line' ? palette[i % 7] + '33' : palette[i % 7], tension: .3, pointRadius: 0, borderWidth: 2, fill: type === 'line' && datasets.length === 1, ...d })) },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#a79fc4', boxWidth: 12 } } },
      scales: type === 'doughnut' ? {} : { x: { ticks: { color: '#7a7198', maxTicksLimit: 10 }, grid: { color: '#271e45' } }, y: { beginAtZero: true, ticks: { color: '#7a7198', precision: 0 }, grid: { color: '#271e45' } } },
      ...(extra || {}) },
  });
  NX.charts.push(c); return c;
}

// ------------------------------------------------------------------ router
NX.clearTimers = () => { NX.timers.forEach(clearInterval); NX.timers = []; };
NX.register = (id, title, render) => { NX.pages[id] = { title, render }; };
async function route() {
  const [page, sub] = (location.hash.slice(1) || 'overview').split('/');
  const p = NX.pages[page] || NX.pages.overview;
  NX.current = NX.pages[page] ? page : 'overview';
  NX.charts.forEach((c) => c.destroy()); NX.charts = []; NX.clearTimers();
  $$('#dash-nav a').forEach((a) => a.classList.toggle('active', a.dataset.page === NX.current));
  $('#page-title').textContent = p.title; document.title = `${p.title} · ${NX.meta.guild.name} · Nexora`;
  const c = $('#content'); c.innerHTML = '<p class="loading">Loading…</p>';
  $('#side').classList.remove('open');
  try { await p.render(c, sub); } catch (e) { c.innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
  window.scrollTo(0, 0);
}

async function refreshCounts() {
  try {
    const o = await get('overview');
    const set = (k, n) => { const el = $(`[data-count="${k}"]`); if (el) { el.textContent = n; el.hidden = !n; } };
    set('appeals', o.moderation.pending_appeals); set('flags', o.moderation.flags_pending);
    set('tickets', o.tickets.open); set('applications', o.applications.pending);
    return o;
  } catch (e) { return null; }
}

document.addEventListener('DOMContentLoaded', async () => {
  const app = $('#app');
  NX.gid = app.dataset.gid; NX.csrf = app.dataset.csrf;
  $('#side-toggle').addEventListener('click', () => $('#side').classList.toggle('open'));
  $$('[data-close]').forEach((b) => b.addEventListener('click', Dialog.close));
  try { NX.meta = await get('meta'); } catch (e) { $('#content').innerHTML = `<div class="notice">${esc(e.message)}</div>`; return; }
  NX.meta.channels.forEach((c) => { NX.maps.channels[c.id] = c; });
  NX.meta.voice_channels.forEach((c) => { NX.maps.channels[c.id] = c; });
  NX.meta.roles.forEach((r) => { NX.maps.roles[r.id] = r; });
  const g = NX.meta.guild;
  $('#guild-name').textContent = g.name;
  $('#guild-icon').outerHTML = g.icon ? `<img src="${esc(g.icon)}" alt="">` : `<span class="icon">${esc(g.name.slice(0, 2))}</span>`;
  if (!canWrite()) $('#access-pill').hidden = false;
  window.addEventListener('hashchange', route);
  route(); refreshCounts();
  setInterval(refreshCounts, 60000);
});
