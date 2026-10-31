/* Nexora dashboard pages. Every page reads and writes through the real API. */
'use strict';

const LOG_CATS = ['messages', 'members', 'roles', 'channels', 'moderation', 'voice', 'server'];
const statBox = (v, l) => `<div class="stat"><div class="v">${v}</div><div class="l">${esc(l)}</div></div>`;
const emptyBox = (text, btn) => `<div class="empty"><p>${esc(text)}</p>${btn || ''}</div>`;
const writeBtn = (attrs, label, cls) => (canWrite() ? `<button class="btn ${cls || ''}" ${attrs}>${label}</button>` : '');
const toLocalInput = (d) => { const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000); return z.toISOString().slice(0, 16); };
const fromDbToLocalInput = (s) => toLocalInput(new Date(String(s).replace(' ', 'T') + 'Z'));

/* ================================================================ overview */
NX.register('overview', 'Overview', async (c) => {
  const o = await get('overview');
  const miss = NX.meta.missing_permissions;
  c.innerHTML = `
    ${miss.length ? `<div class="notice">Nexora is missing permissions some features need: <strong>${miss.map((p) => esc(p.replace(/_/g, ' '))).join(', ')}</strong>. Give its role these permissions, or re-invite it.</div>` : ''}
    ${o.bot.ready ? '' : '<div class="notice">The bot is reconnecting to Discord. Changes are saved and apply once it\'s back.</div>'}
    <div class="stats">
      ${statBox(fmtNum(o.members.total), 'Members')}
      ${statBox((o.week.joins - o.week.leaves >= 0 ? '+' : '') + fmtNum(o.week.joins - o.week.leaves), `Net growth this week (${o.week.joins} joined, ${o.week.leaves} left)`)}
      ${statBox(fmtNum(o.week.messages), 'Messages this week')}
      ${statBox(fmtNum(o.week.voice_minutes), 'Voice minutes this week')}
    </div>
    <div class="grid-2">
      <section class="panel"><div class="panel-head"><h3>Recent events</h3><a href="#logs">Open logs</a></div>
        <div id="ov-feed">${o.recent_events.length ? o.recent_events.map((e) => `<div class="feed-item"><span class="cat-dot cat-${esc(e.category)}"></span><div>${esc(e.summary)}</div><span class="faint nowrap">${fmtAgo(e.created_at)}</span></div>`).join('') : emptyBox('No events recorded yet. They appear here as soon as something happens in your server.')}</div>
      </section>
      <div>
        <section class="panel"><h3>Needs attention</h3>
          <div class="kv mt">
            <dt>Open tickets</dt><dd><a href="#tickets">${o.tickets.open}</a></dd>
            <dt>Pending applications</dt><dd><a href="#applications">${o.applications.pending}</a></dd>
            <dt>Pending appeals</dt><dd><a href="#moderation/appeals">${o.moderation.pending_appeals}</a></dd>
            <dt>Automod items to review</dt><dd><a href="#automod">${o.moderation.flags_pending}</a></dd>
            <dt>Running giveaways</dt><dd><a href="#giveaways">${o.giveaways.running}</a></dd>
          </div>
        </section>
        <section class="panel"><h3>This week</h3>
          <div class="kv mt">
            <dt>Moderation cases</dt><dd>${o.moderation.cases_7d}</dd>
            <dt>Automod actions</dt><dd>${o.moderation.automod_7d}</dd>
            <dt>Active warnings</dt><dd>${o.moderation.active_warnings}</dd>
            <dt>Tickets closed</dt><dd>${o.tickets.closed_7d}</dd>
            <dt>Average ticket rating</dt><dd>${o.tickets.avg_rating != null ? o.tickets.avg_rating + ' / 5' : 'No ratings yet'}</dd>
            <dt>Applications (30 days)</dt><dd>${o.applications.total_30d}</dd>
            <dt>Giveaway entries (30 days)</dt><dd>${o.giveaways.entries_30d}</dd>
          </div>
        </section>
        <section class="panel"><h3>Staff activity, last 30 days</h3>
          ${o.staff_activity.length ? `<table class="data">${o.staff_activity.map((s) => `<tr><td>${esc(s.name)}</td><td class="right">${s.actions} actions</td></tr>`).join('')}</table>` : '<p class="muted">No staff actions yet.</p>'}
        </section>
        <section class="panel"><h3>Bot</h3><div class="kv mt">
          <dt>Status</dt><dd>${o.bot.ready ? pill('Online', 'ok') : pill('Offline', 'bad')}</dd>
          <dt>Latency</dt><dd>${o.bot.latency_ms != null ? o.bot.latency_ms + ' ms' : '—'}</dd>
          <dt>Uptime</dt><dd>${fmtDuration(o.bot.uptime)}</dd>
          <dt>Humans / bots</dt><dd>${fmtNum(o.members.humans)} / ${fmtNum(o.members.bots)}</dd>
          <dt>Boosts</dt><dd>${o.members.boosts}</dd></div>
        </section>
      </div>
    </div>`;
});

/* =============================================================== analytics */
NX.register('analytics', 'Analytics', async (c) => {
  const today = new Date().toISOString().slice(0, 10);
  c.innerHTML = `<div class="toolbar">
      <div><label>Range</label><select id="an-range"><option value="7">Last 7 days</option><option value="30" selected>Last 30 days</option><option value="90">Last 90 days</option><option value="365">Last year</option><option value="custom">Custom</option></select></div>
      <div id="an-custom" hidden class="inline-flex"><div><label>From</label><input type="date" id="an-start" max="${today}"></div><div><label>To</label><input type="date" id="an-end" value="${today}" max="${today}"></div><button class="btn" id="an-apply">Apply</button></div>
      <a class="btn" id="an-export">${icon('download')} Export CSV</a></div><div id="an-body"><p class="loading">Loading…</p></div>`;
  const load = async () => {
    const r = $('#an-range').value;
    const q = r === 'custom' ? `start=${$('#an-start').value}&end=${$('#an-end').value}` : `days=${r}`;
    $('#an-export').href = `/api/guilds/${NX.gid}/analytics/export?${q}`;
    NX.charts.forEach((x) => x.destroy()); NX.charts = [];
    const d = await get(`analytics?${q}`);
    const t = d.totals;
    $('#an-body').innerHTML = `<div class="stats">${statBox(fmtNum(t.joins), 'Joins')}${statBox(fmtNum(t.leaves), 'Leaves')}${statBox((t.net_growth >= 0 ? '+' : '') + fmtNum(t.net_growth), 'Net growth')}
        ${statBox(fmtNum(t.messages), 'Messages')}${statBox(fmtNum(t.voice_minutes), 'Voice minutes')}${statBox(fmtNum(t.mod_actions), 'Moderation actions')}
        ${statBox(fmtNum(t.tickets_opened), 'Tickets opened')}${statBox(fmtNum(t.applications), 'Applications')}${statBox(fmtNum(t.giveaway_entries), 'Giveaway entries')}</div>
      <div class="grid-2">
        <section class="panel"><h3>Member count</h3><div class="chart-box"><canvas id="ch-members"></canvas></div><p class="help">Recorded hourly while the bot is running.</p></section>
        <section class="panel"><h3>Joins and leaves</h3><div class="chart-box"><canvas id="ch-joins"></canvas></div></section>
        <section class="panel"><h3>Messages</h3><div class="chart-box"><canvas id="ch-msgs"></canvas></div><p class="help">Messages from members (bots excluded).</p></section>
        <section class="panel"><h3>Voice minutes</h3><div class="chart-box"><canvas id="ch-voice"></canvas></div></section>
        <section class="panel"><h3>Moderation, tickets and applications</h3><div class="chart-box"><canvas id="ch-staff"></canvas></div></section>
        <section class="panel"><h3>Moderation actions by type</h3><div class="chart-box"><canvas id="ch-modtypes"></canvas></div></section>
      </div>
      <div class="grid-2">
        <section class="panel"><h3>Most active staff</h3>${d.staff.length ? `<table class="data"><tr><th>Staff member</th><th class="right">Actions</th></tr>${d.staff.map((s) => `<tr><td>${esc(s.name)}</td><td class="right">${s.actions}</td></tr>`).join('')}</table>` : '<p class="muted">No staff actions in this range.</p>'}</section>
        <section class="panel"><h3>Feature usage (all time)</h3>${d.features.length ? `<table class="data"><tr><th>Feature</th><th class="right">Uses</th></tr>${d.features.map((f) => `<tr><td>${esc(f.feature)}</td><td class="right">${fmtNum(f.count)}</td></tr>`).join('')}</table>` : '<p class="muted">Nothing used yet.</p>'}</section>
      </div>`;
    const labels = d.days.map((x) => x.slice(5));
    chart($('#ch-members'), 'line', labels, [{ label: 'Members', data: d.series.member_count, spanGaps: true }]);
    chart($('#ch-joins'), 'bar', labels, [{ label: 'Joins', data: d.series.joins }, { label: 'Leaves', data: d.series.leaves, backgroundColor: '#f87171' }]);
    chart($('#ch-msgs'), 'line', labels, [{ label: 'Messages', data: d.series.messages }]);
    chart($('#ch-voice'), 'line', labels, [{ label: 'Voice minutes', data: d.series.voice_minutes, borderColor: '#fbbf24', backgroundColor: '#fbbf2433' }]);
    chart($('#ch-staff'), 'line', labels, [{ label: 'Mod actions', data: d.series.mod_actions }, { label: 'Tickets', data: d.series.tickets_opened }, { label: 'Applications', data: d.series.applications }, { label: 'Giveaway entries', data: d.series.giveaway_entries }]);
    if (d.mod_actions.length) chart($('#ch-modtypes'), 'doughnut', d.mod_actions.map((a) => a.action), [{ data: d.mod_actions.map((a) => a.n), backgroundColor: ['#8b5cf6', '#f87171', '#fbbf24', '#34d399', '#60a5fa', '#f472b6', '#22d3ee', '#a78bfa'] }]);
    else $('#ch-modtypes').parentElement.innerHTML = '<p class="muted">No moderation actions in this range.</p>';
  };
  $('#an-range').addEventListener('change', () => { const cu = $('#an-range').value === 'custom'; $('#an-custom').hidden = !cu; if (!cu) load(); });
  $('#an-apply').addEventListener('click', () => { if (!$('#an-start').value) return toast('Pick a start date', true); load().catch((e) => toast(e.message, true)); });
  await load();
});

/* ==================================================================== logs */
function logDetail(e) {
  const d = e.data || {};
  let diff = '';
  if ('before' in d || 'after' in d) {
    diff = `<h3 class="mt">Before and after</h3><div class="diffbox"><div class="before"><span class="faint">Before</span><pre>${esc(d.before == null ? 'Not available (message wasn\'t cached)' : d.before)}</pre></div><div class="after"><span class="faint">After</span><pre>${esc(d.after)}</pre></div></div>`;
  } else if (d.changes && Object.keys(d.changes).length) {
    diff = `<h3 class="mt">Changes</h3><table class="data"><tr><th>Setting</th><th>Before</th><th>After</th></tr>${Object.entries(d.changes).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v[0])}</td><td>${esc(v[1])}</td></tr>`).join('')}</table>`;
  }
  if (d.perms_added && d.perms_added.length) diff += `<p class="mt"><strong>Permissions granted:</strong> ${esc(d.perms_added.join(', '))}</p>`;
  if (d.perms_removed && d.perms_removed.length) diff += `<p><strong>Permissions removed:</strong> ${esc(d.perms_removed.join(', '))}</p>`;
  if (d.content != null) diff += `<h3 class="mt">Content</h3><div class="diffbox" style="grid-template-columns:1fr"><div class="before"><pre>${esc(d.content || '(no text)')}</pre></div></div>`;
  if (d.attachments && d.attachments.length) diff += `<h3 class="mt">Attachments</h3><ul>${d.attachments.map((a) => `<li>${esc(a.filename)} (${Math.round(a.size / 1024)} KB)</li>`).join('')}</ul><p class="help">Discord deletes attachments of deleted messages, so the files themselves usually can't be opened.</p>`;
  if (d.messages && d.messages.length) diff += `<h3 class="mt">Deleted messages</h3><pre style="white-space:pre-wrap;max-height:300px;overflow:auto">${esc(d.messages.join('\n'))}</pre>`;
  if (d.overwrites && d.overwrites.length) diff += `<h3 class="mt">Permission overwrite changes</h3><ul>${d.overwrites.map((o) => `<li>${esc(o.replace(/\*\*/g, ''))}</li>`).join('')}</ul>`;
  Dialog.open(`${e.event_type.replace(/_/g, ' ')}`, `<dl class="kv">
    <dt>When</dt><dd>${fmtTime(e.created_at)}</dd><dt>Category</dt><dd>${esc(e.category)}</dd>
    <dt>Summary</dt><dd>${esc(e.summary)}</dd>
    ${e.user_id ? `<dt>User</dt><dd>${userCell(e.user, e.user_id)} <span class="faint">${esc(e.user_id)}</span></dd>` : ''}
    ${e.moderator_id ? `<dt>Moderator</dt><dd>${userCell(e.moderator, e.moderator_id)}</dd>` : ''}
    ${e.channel_id ? `<dt>Channel</dt><dd>${e.channel_name ? '#' + esc(e.channel_name) : esc(e.channel_id)}</dd>` : ''}
    ${d.url ? `<dt>Message</dt><dd><a href="${esc(d.url)}" target="_blank" rel="noopener">Open in Discord</a></dd>` : ''}
    </dl>${diff}<details class="mt"><summary class="faint">Raw data</summary><pre style="white-space:pre-wrap;font-size:.8rem">${esc(JSON.stringify(d, null, 2))}</pre></details>`);
}

async function logFeed(body) {
  body.innerHTML = `<div class="toolbar">
      <div><label>Category</label><select id="lg-cat"><option value="">All categories</option>${LOG_CATS.map((x) => `<option>${x}</option>`).join('')}</select></div>
      <div><label>Event</label><select id="lg-type"><option value="">Any event</option></select></div>
      <div><label>Search</label><input id="lg-q" type="search" placeholder="Text in summary or content"></div>
      <div><label>User ID</label><input id="lg-user" placeholder="User ID"></div>
      <div><label>Moderator ID</label><input id="lg-mod" placeholder="Moderator ID"></div>
      <div><label>From</label><input id="lg-since" type="date"></div><div><label>To</label><input id="lg-until" type="date"></div>
      <button class="btn primary" id="lg-go">Search</button>
    </div>
    <div class="inline-flex" style="margin-bottom:12px"><label class="switch"><input type="checkbox" id="lg-live" checked><span></span><span class="sr-only">Live updates</span></label><label for="lg-live" style="margin:0">Live updates</label>
      <span class="faint" id="lg-total"></span><span class="spacer" style="flex:1"></span>
      <a class="btn small" id="lg-csv">${icon('download')} CSV</a><a class="btn small" id="lg-json">${icon('download')} JSON</a></div>
    <div class="panel table-scroll" style="padding:8px 12px"><table class="data"><thead><tr><th>Time</th><th>Category</th><th>Event</th><th>Summary</th><th>User</th></tr></thead><tbody id="lg-rows"></tbody></table>
    <div class="right mt"><button class="btn small" id="lg-more" hidden>Load older</button></div></div>`;
  let next = null; let newest = 0;
  const params = () => {
    const p = new URLSearchParams();
    [['category', '#lg-cat'], ['event_type', '#lg-type'], ['q', '#lg-q'], ['user_id', '#lg-user'], ['moderator_id', '#lg-mod'], ['since', '#lg-since'], ['until', '#lg-until']]
      .forEach(([k, s]) => { const v = $(s).value.trim(); if (v) p.set(k, v); });
    return p;
  };
  const rowHtml = (e) => `<tr class="clickable" data-id="${e.id}"><td class="nowrap faint">${fmtTime(e.created_at)}</td><td><span class="inline-flex"><span class="cat-dot cat-${esc(e.category)}" style="margin:0"></span>${esc(e.category)}</span></td><td class="nowrap">${esc(e.event_type.replace(/_/g, ' '))}</td><td>${esc(e.summary)}</td><td>${e.user_id ? userCell(e.user, e.user_id) : ''}</td></tr>`;
  const cache = {};
  const bind = () => $$('#lg-rows tr[data-id]').forEach((tr) => { tr.onclick = () => logDetail(cache[tr.dataset.id]); });
  const load = async (append) => {
    const p = params(); if (append && next) p.set('before_id', next);
    const d = await get('logs?' + p);
    d.events.forEach((e) => { cache[e.id] = e; });
    if (!append) {
      newest = d.events.length ? d.events[0].id : 0;
      const sel = $('#lg-type'); const cur = sel.value;
      sel.innerHTML = '<option value="">Any event</option>' + d.event_types.map((t) => `<option ${t === cur ? 'selected' : ''}>${esc(t)}</option>`).join('');
    }
    $('#lg-rows').innerHTML = (append ? $('#lg-rows').innerHTML : '') + (d.events.map(rowHtml).join('') || (append ? '' : `<tr><td colspan="5">${emptyBox('No events match these filters.')}</td></tr>`));
    next = d.next_before_id; $('#lg-more').hidden = !next; $('#lg-total').textContent = `${fmtNum(d.total)} matching events`;
    const qs = params().toString();
    $('#lg-csv').href = `/api/guilds/${NX.gid}/logs/export?format=csv&${qs}`; $('#lg-json').href = `/api/guilds/${NX.gid}/logs/export?format=json&${qs}`;
    bind();
  };
  $('#lg-go').addEventListener('click', () => load(false).catch((e) => toast(e.message, true)));
  $('#lg-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') load(false); });
  $('#lg-more').addEventListener('click', () => load(true));
  NX.timers.push(setInterval(async () => {
    if (!$('#lg-live') || !$('#lg-live').checked || document.hidden) return;
    const d = await get('logs?' + params() + '&limit=50').catch(() => null);
    if (!d) return;
    const fresh = d.events.filter((e) => e.id > newest);
    if (!fresh.length) return;
    fresh.forEach((e) => { cache[e.id] = e; });
    newest = fresh[0].id;
    const empty = $('#lg-rows .empty'); if (empty) $('#lg-rows').innerHTML = '';
    $('#lg-rows').insertAdjacentHTML('afterbegin', fresh.map(rowHtml).join(''));
    $('#lg-total').textContent = `${fmtNum(d.total)} matching events`; bind();
  }, 8000));
  await load(false);
}
NX.register('logs', 'Advanced Logging', async (c, sub) => tabs(c, [
  { id: 'feed', label: 'Event log', render: logFeed },
  { id: 'settings', label: 'Logging settings', render: (b) => renderSettings(b, 'logging') },
], sub));

/* ============================================================== moderation */
function modActionDialog(userId, preset) {
  const b = Dialog.open('Moderation action', `<div class="form-grid">
    <div class="field"><label>Action</label><select name="action">${['warn', 'timeout', 'untimeout', 'kick', 'ban', 'unban'].map((a) => `<option ${a === preset ? 'selected' : ''}>${a}</option>`).join('')}</select></div>
    <div class="field"><label>User ID</label><input name="user_id" value="${esc(userId || '')}" required></div>
    <div class="field"><label>Duration</label><input name="duration" placeholder="e.g. 1h, 7d (timeout / temp ban)"></div>
    <div class="field wide"><label>Reason</label><textarea name="reason" maxlength="500"></textarea></div></div>
    <p class="help">Your own Discord permissions and role position are checked, just like in Discord.</p>`, [
    { label: 'Cancel', cls: 'ghost' },
    { label: 'Apply', cls: 'danger', onClick: async (body) => { await run(() => post('moderation/action', readForm(body)), (r) => r.message); if (NX.onModChange) NX.onModChange(); } },
  ]);
  return b;
}

async function casesTab(body) {
  body.innerHTML = `<div class="toolbar"><div><label>User ID</label><input id="cs-user"></div>
    <div><label>Action</label><select id="cs-action"><option value="">All</option>${['warn', 'timeout', 'untimeout', 'kick', 'ban', 'tempban', 'unban', 'lock', 'unlock'].map((a) => `<option>${a}</option>`).join('')}</select></div>
    <button class="btn" id="cs-go">Filter</button>${writeBtn('id="cs-new"', 'New action', 'primary')}</div><div id="cs-body"></div>`;
  const load = async () => {
    const p = new URLSearchParams(); if ($('#cs-user').value) p.set('user_id', $('#cs-user').value.trim()); if ($('#cs-action').value) p.set('action', $('#cs-action').value);
    const d = await get('cases?' + p);
    $('#cs-body').innerHTML = `<div class="grid-2" style="grid-template-columns:3fr 1fr"><div class="panel table-scroll">${d.cases.length ? `<table class="data"><thead><tr><th>Case</th><th>Action</th><th>User</th><th>Moderator</th><th>Reason</th><th>Date</th></tr></thead><tbody>
      ${d.cases.map((x) => `<tr class="clickable" data-case="${x.case_number}"><td>#${x.case_number}</td><td>${statusPill(x.action)}${x.active ? '' : ' <span class="faint">inactive</span>'}</td><td>${userCell(x.user, x.user_id)}</td><td>${x.moderator_id ? userCell(x.moderator, x.moderator_id) : '<span class="faint">Automatic</span>'}</td><td class="truncate">${esc(x.reason || '—')}</td><td class="nowrap faint">${fmtTime(x.created_at)}</td></tr>`).join('')}</tbody></table>` : emptyBox('No cases yet.')}</div>
      <section class="panel"><h3>Top moderators, 30 days</h3>${d.moderators.map((m) => `<div class="feed-item" style="grid-template-columns:1fr auto"><span>${esc(m.name)}</span><strong>${m.actions}</strong></div>`).join('') || '<p class="muted">No actions.</p>'}</section></div>`;
    $$('[data-case]').forEach((tr) => tr.addEventListener('click', () => {
      const x = d.cases.find((k) => String(k.case_number) === tr.dataset.case);
      Dialog.open(`Case #${x.case_number}`, `<dl class="kv"><dt>Action</dt><dd>${esc(x.action)}</dd><dt>User</dt><dd>${userCell(x.user, x.user_id)} <span class="faint">${esc(x.user_id)}</span></dd>
        <dt>Moderator</dt><dd>${x.moderator_id ? userCell(x.moderator, x.moderator_id) : 'Automatic'}</dd><dt>Date</dt><dd>${fmtTime(x.created_at)}</dd>
        ${x.duration_seconds ? `<dt>Duration</dt><dd>${fmtDuration(x.duration_seconds)}</dd>` : ''}</dl>
        <label class="mt" style="margin-top:16px">Reason</label><textarea name="reason" ${canWrite() ? '' : 'disabled'}>${esc(x.reason || '')}</textarea>`,
      canWrite() ? [{ label: 'Close', cls: 'ghost' }, { label: 'View user', onClick: () => { location.hash = `moderation/user`; setTimeout(() => NX.openUser && NX.openUser(x.user_id), 300); } },
        { label: 'Save reason', cls: 'primary', onClick: async (b) => { await run(() => api('PATCH', `cases/${x.case_number}`, { reason: $('[name=reason]', b).value }), 'Reason updated'); load(); } }] : []);
    }));
  };
  NX.onModChange = load;
  $('#cs-go').addEventListener('click', load);
  const nb = $('#cs-new'); if (nb) nb.addEventListener('click', () => modActionDialog());
  await load();
}

async function warningsTab(body) {
  const d = await get('warnings');
  body.innerHTML = `<div class="panel table-scroll">${d.warnings.length ? `<table class="data"><thead><tr><th>ID</th><th>User</th><th>Reason</th><th>Moderator</th><th>Date</th><th></th></tr></thead><tbody>
    ${d.warnings.map((w) => `<tr><td>${w.id}</td><td>${userCell(w.user, w.user_id)}</td><td>${esc(w.reason || '—')}</td><td>${w.moderator_id ? userCell(w.moderator, w.moderator_id) : 'Automatic'}</td><td class="faint nowrap">${fmtTime(w.created_at)}</td>
    <td>${writeBtn(`data-rm="${w.id}"`, 'Remove', 'small danger')}</td></tr>`).join('')}</tbody></table>` : emptyBox('No active warnings.')}</div>`;
  $$('[data-rm]', body).forEach((b) => b.addEventListener('click', async () => { await run(() => del(`warnings/${b.dataset.rm}`), 'Warning removed'); warningsTab(body); }));
}

async function appealsTab(body) {
  body.innerHTML = `<div class="toolbar"><select id="ap-status"><option value="pending">Pending</option><option value="accepted">Accepted</option><option value="denied">Denied</option><option value="all">All</option></select></div><div id="ap-list"></div>`;
  const load = async () => {
    const d = await get('appeals?status=' + $('#ap-status').value);
    $('#ap-list').innerHTML = d.appeals.length ? d.appeals.map((a) => `<section class="panel"><div class="panel-head"><h3>Appeal #${a.id} ${statusPill(a.status)}</h3><span class="faint">${fmtTime(a.created_at)}</span></div>
      <dl class="kv"><dt>User</dt><dd>${userCell(a.user, a.user_id)} <span class="faint">${esc(a.user_id)}</span></dd><dt>Case</dt><dd>#${a.case_number || '—'} ${esc(a.case_action || '')}</dd><dt>Original reason</dt><dd>${esc(a.case_reason || '—')}</dd></dl>
      <p class="mt" style="white-space:pre-wrap">${esc(a.content)}</p>${a.response ? `<p class="muted">Response: ${esc(a.response)}</p>` : ''}
      ${a.status === 'pending' && canWrite() ? `<div class="inline-flex"><input data-resp="${a.id}" placeholder="Message to the user (optional)" style="flex:1;min-width:240px"><button class="btn primary" data-acc="${a.id}">Accept and reverse</button><button class="btn danger" data-den="${a.id}">Deny</button></div>` : ''}</section>`).join('') : emptyBox('No appeals here.');
    const act = (id, accept) => run(() => post(`appeals/${id}/resolve`, { accept, response: $(`[data-resp="${id}"]`).value }), (r) => r.message).then(load);
    $$('[data-acc]').forEach((b) => b.addEventListener('click', () => act(b.dataset.acc, true)));
    $$('[data-den]').forEach((b) => b.addEventListener('click', () => act(b.dataset.den, false)));
  };
  $('#ap-status').addEventListener('change', load); await load();
}

async function userTab(body) {
  body.innerHTML = `<div class="toolbar"><div style="flex:1;max-width:420px"><label>Find a member</label><input id="us-q" placeholder="Name or user ID"></div><button class="btn primary" id="us-go">Search</button></div><div id="us-results"></div><div id="us-profile"></div>`;
  const open = async (uid) => {
    const p = await get(`users/${uid}`);
    $('#us-results').innerHTML = '';
    $('#us-profile').innerHTML = `<section class="panel"><div class="panel-head"><h3>${userCell(p.user, uid)}</h3><div class="inline-flex">${canWrite() ? ['warn', 'timeout', 'kick', 'ban', 'unban'].map((a) => `<button class="btn small ${a === 'ban' || a === 'kick' ? 'danger' : ''}" data-act="${a}">${a[0].toUpperCase() + a.slice(1)}</button>`).join('') : ''}</div></div>
      <dl class="kv"><dt>User ID</dt><dd>${esc(uid)}</dd><dt>Account created</dt><dd>${fmtTime(p.created_at)}</dd><dt>In server</dt><dd>${p.in_server ? 'Yes, joined ' + fmtTime(p.joined_at) : 'No'}</dd>
      <dt>Roles</dt><dd>${p.roles.map((r) => esc('@' + r.name)).join(', ') || '—'}</dd><dt>Level</dt><dd>${p.level ? `${p.level.level} (${fmtNum(p.level.xp)} XP, ${fmtNum(p.level.messages)} messages)` : '—'}</dd><dt>Balance</dt><dd>${fmtNum(p.balance)}</dd></dl></section>
      <div class="grid-2"><section class="panel"><h3>Cases</h3>${p.cases.map((x) => `<div class="feed-item" style="grid-template-columns:auto 1fr auto"><strong>#${x.case_number}</strong><span>${esc(x.action)}: ${esc(x.reason || '—')}</span><span class="faint">${fmtAgo(x.created_at)}</span></div>`).join('') || '<p class="muted">No cases.</p>'}</section>
      <section class="panel"><h3>Moderator notes</h3>${p.notes.map((n) => `<div class="feed-item" style="grid-template-columns:1fr auto"><span>${esc(n.note)} <span class="faint">by ${esc(n.moderator && n.moderator.name || n.moderator_id)}</span></span><span class="faint">${fmtAgo(n.created_at)}</span></div>`).join('') || '<p class="muted">No notes.</p>'}
        ${canWrite() ? `<div class="inline-flex mt"><input id="us-note" placeholder="Add a private note" style="flex:1"><button class="btn" id="us-addnote">Add note</button></div>` : ''}</section></div>
      <section class="panel"><h3>Recent log activity</h3>${p.recent_logs.map((e) => `<div class="feed-item"><span class="cat-dot cat-${esc(e.category)}"></span><span>${esc(e.summary)}</span><span class="faint">${fmtAgo(e.created_at)}</span></div>`).join('') || '<p class="muted">Nothing logged.</p>'}</section>`;
    $$('[data-act]').forEach((b) => b.addEventListener('click', () => { NX.onModChange = () => open(uid); modActionDialog(uid, b.dataset.act); }));
    const nb = $('#us-addnote'); if (nb) nb.addEventListener('click', async () => { await run(() => post('notes', { user_id: uid, note: $('#us-note').value }), 'Note added'); open(uid); });
  };
  NX.openUser = open;
  const search = async () => {
    const q = $('#us-q').value.trim(); if (!q) return;
    if (/^\d{15,22}$/.test(q)) return open(q);
    const d = await get('members/search?q=' + encodeURIComponent(q));
    $('#us-results').innerHTML = d.members.length ? `<div class="panel">${d.members.map((m) => `<div class="feed-item clickable" style="grid-template-columns:1fr auto;cursor:pointer" data-uid="${esc(m.id)}">${userCell(m, m.id)}<span class="faint">${esc(m.id)}</span></div>`).join('')}</div>` : emptyBox('No members found.');
    $$('[data-uid]').forEach((el) => el.addEventListener('click', () => open(el.dataset.uid)));
  };
  $('#us-go').addEventListener('click', search);
  $('#us-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') search(); });
}
NX.register('moderation', 'Moderation', async (c, sub) => tabs(c, [
  { id: 'cases', label: 'Cases', render: casesTab }, { id: 'warnings', label: 'Warnings', render: warningsTab },
  { id: 'appeals', label: 'Appeals', render: appealsTab }, { id: 'user', label: 'User lookup', render: userTab },
  { id: 'settings', label: 'Settings', render: (b) => renderSettings(b, 'moderation') },
], sub));

/* ================================================================= automod */
async function flagsTab(body) {
  body.innerHTML = `<div class="toolbar"><select id="fl-status"><option value="pending">Waiting for review</option><option value="all">All</option><option value="actioned">Actioned automatically</option><option value="confirmed">Confirmed</option><option value="dismissed">False positives</option></select></div>
    <p class="help">"Correct" confirms the action. "False positive" removes the warning or case and lifts any timeout Nexora applied.</p><div id="fl-list" class="panel table-scroll"></div>`;
  const load = async () => {
    const d = await get('automod/flags?status=' + $('#fl-status').value);
    $('#fl-list').innerHTML = d.flags.length ? `<table class="data"><thead><tr><th>Rule</th><th>User</th><th>Content</th><th>Action</th><th>Status</th><th>When</th><th></th></tr></thead><tbody>${d.flags.map((f) => `<tr>
      <td class="nowrap">${esc(f.rule)}</td><td>${userCell(f.user, f.user_id)}</td><td class="truncate" title="${esc(f.content || '')}">${esc(f.content || '—')}</td><td>${esc(f.action_taken || '—')}</td><td>${statusPill(f.status)}</td><td class="faint nowrap">${fmtAgo(f.created_at)}</td>
      <td class="nowrap">${['pending', 'actioned'].includes(f.status) && canWrite() ? `<button class="btn small" data-ok="${f.id}">Correct</button> <button class="btn small danger" data-fp="${f.id}">False positive</button>` : ''}</td></tr>`).join('')}</tbody></table>` : emptyBox('Nothing to review.');
    const rev = (id, correct) => run(() => post(`automod/flags/${id}/review`, { correct }), (r) => r.message).then(load);
    $$('[data-ok]').forEach((b) => b.addEventListener('click', () => rev(b.dataset.ok, true)));
    $$('[data-fp]').forEach((b) => b.addEventListener('click', () => rev(b.dataset.fp, false)));
  };
  $('#fl-status').addEventListener('change', load); await load();
}
NX.register('automod', 'Automod', async (c, sub) => tabs(c, [
  { id: 'review', label: 'Review queue', render: flagsTab },
  { id: 'settings', label: 'Protection rules', render: (b) => renderSettings(b, 'automod') },
], sub));

/* ===================================================== question builder */
function questionBuilder(container, questions, opts) {
  const draw = () => {
    container.innerHTML = questions.map((q, i) => `<div class="q-row" data-q="${i}"><div class="q-top"><input data-k="label" maxlength="45" placeholder="Question (max 45 characters)" value="${esc(q.label)}">
      <select data-k="type"><option value="short" ${q.type === 'short' ? 'selected' : ''}>Short answer</option><option value="long" ${q.type === 'long' ? 'selected' : ''}>Long answer</option>${opts.choice ? `<option value="choice" ${q.type === 'choice' ? 'selected' : ''}>Multiple choice</option>` : ''}</select></div>
      ${q.type === 'choice' ? `<textarea data-k="options" placeholder="One option per line">${esc((q.options || []).join('\n'))}</textarea>` : `<input data-k="placeholder" maxlength="100" placeholder="Placeholder text (optional)" value="${esc(q.placeholder || '')}">`}
      <div class="row-actions"><label class="inline-flex" style="margin:0;font-weight:500"><input type="checkbox" data-k="required" ${q.required !== false ? 'checked' : ''}> Required</label><span style="flex:1"></span>
      <button type="button" class="btn small ghost" data-up="${i}" ${i === 0 ? 'disabled' : ''}>Move up</button><button type="button" class="btn small danger" data-rmq="${i}">Remove</button></div></div>`).join('') +
      (questions.length < opts.max ? `<button type="button" class="btn small" data-addq>${icon('plus')} Add question</button>` : `<p class="help">Maximum of ${opts.max} questions.</p>`);
    $$('[data-q]', container).forEach((row) => {
      const q = questions[+row.dataset.q];
      $$('[data-k]', row).forEach((el) => el.addEventListener('change', () => {
        const k = el.dataset.k;
        if (k === 'required') q.required = el.checked; else if (k === 'options') q.options = el.value.split('\n').map((s) => s.trim()).filter(Boolean); else q[k] = el.value;
        if (k === 'type') draw();
      }));
    });
    $$('[data-rmq]', container).forEach((b) => b.addEventListener('click', () => { questions.splice(+b.dataset.rmq, 1); draw(); }));
    $$('[data-up]', container).forEach((b) => b.addEventListener('click', () => { const i = +b.dataset.up; [questions[i - 1], questions[i]] = [questions[i], questions[i - 1]]; draw(); }));
    const add = $('[data-addq]', container); if (add) add.addEventListener('click', () => { questions.push({ label: '', type: 'short', required: true }); draw(); });
  };
  draw();
}

/* ================================================================= tickets */
function panelEditor(panel, reload) {
  const p = panel || { name: '', title: 'Support', description: 'Click the button below to open a ticket.', button_label: 'Open Ticket', welcome_message: 'Hello {user}! Staff will be with you shortly.', name_format: 'ticket-{number}', max_open_per_user: 1, enabled: 1, staff_role_ids: [], questions: [], color: '#7C3AED' };
  const qs = JSON.parse(JSON.stringify(p.questions || []));
  const body = Dialog.open(panel ? `Edit panel: ${p.name}` : 'New ticket panel', `<div class="form-grid">
    <div class="field"><label>Panel name</label><input name="name" maxlength="80" value="${esc(p.name)}" placeholder="e.g. Support"></div>
    <div class="field"><label>Post in channel</label>${channelSelect('channel_id', p.channel_id, 'Choose channel')}</div>
    <div class="field"><label>Create tickets in category</label><select name="category_id">${options(categoryList(), p.category_id, 'No category')}</select></div>
    <div class="field"><label>Max open tickets per member</label><input type="number" name="max_open_per_user" min="0" max="20" value="${p.max_open_per_user}"><div class="help">0 = unlimited</div></div>
    <div class="field wide"><label>Staff roles</label>${chipPicker('staff_role_ids', p.staff_role_ids, 'roles')}<div class="help">Empty uses the staff roles from Server Settings.</div></div>
    <div class="field"><label>Embed title</label><input name="title" maxlength="256" value="${esc(p.title)}"></div>
    <div class="field"><label>Embed color</label><input name="color" value="${esc(p.color || '#7C3AED')}" maxlength="7"></div>
    <div class="field wide"><label>Embed description</label><textarea name="description" maxlength="4000">${esc(p.description)}</textarea></div>
    <div class="field"><label>Button label</label><input name="button_label" maxlength="80" value="${esc(p.button_label)}"></div>
    <div class="field"><label>Button emoji</label><input name="button_emoji" maxlength="64" value="${esc(p.button_emoji || '')}" placeholder="🎫"></div>
    <div class="field"><label>Channel name format</label><input name="name_format" maxlength="90" value="${esc(p.name_format)}"><div class="help">{number} and {username} are available.</div></div>
    <div class="field">${switchHtml('enabled', p.enabled, 'Panel enabled')}</div>
    <div class="field wide"><label>Welcome message inside the ticket</label><textarea name="welcome_message" maxlength="2000">${esc(p.welcome_message)}</textarea></div>
    <div class="field wide"><label>Questions asked before opening (up to 5)</label><div id="pq"></div></div></div>`, [
    { label: 'Cancel', cls: 'ghost' },
    { label: panel ? 'Save panel' : 'Create panel', cls: 'primary', onClick: async (b) => {
      const data = { ...readForm(b), questions: qs };
      await run(() => (panel ? put(`ticket-panels/${panel.id}`, data) : post('ticket-panels', data)), panel ? 'Panel saved. Publish it to update the message in Discord.' : 'Panel created. Press Publish to post it.');
      reload();
    } },
  ]);
  hydrateChips(body); questionBuilder($('#pq', body), qs, { max: 5, choice: false });
}

async function panelsTab(body) {
  const d = await get('ticket-panels');
  body.innerHTML = `<div class="toolbar">${writeBtn('id="pn-new"', `${icon('plus')} New panel`, 'primary')}</div>${d.panels.length ? d.panels.map((p) => `<section class="panel"><div class="panel-head"><div><h3>${esc(p.name)} ${p.enabled ? '' : pill('Disabled')}</h3>
    <span class="muted">${chName(p.channel_id)} · ${p.questions.length} question(s) · staff: ${p.staff_role_ids.map(roleName).join(', ') || 'server staff roles'}</span></div>
    <div class="inline-flex">${writeBtn(`data-pub="${p.id}"`, p.message_id ? 'Update message' : 'Publish', 'primary small')}${writeBtn(`data-edit="${p.id}"`, 'Edit', 'small')}${writeBtn(`data-del="${p.id}"`, 'Delete', 'small danger')}</div></div></section>`).join('') : emptyBox('No panels yet. A panel is the message with the button members click to open a ticket.')}`;
  const reload = () => panelsTab(body);
  const nb = $('#pn-new', body); if (nb) nb.addEventListener('click', () => panelEditor(null, reload));
  $$('[data-edit]', body).forEach((b) => b.addEventListener('click', () => panelEditor(d.panels.find((p) => String(p.id) === b.dataset.edit), reload)));
  $$('[data-pub]', body).forEach((b) => b.addEventListener('click', async () => { await run(() => post(`ticket-panels/${b.dataset.pub}/publish`), 'Panel published in Discord'); reload(); }));
  $$('[data-del]', body).forEach((b) => b.addEventListener('click', async () => {
    if (await confirmDialog('Delete panel', 'Delete this panel and its message in Discord? Existing tickets are kept.', 'Delete', 'danger')) { await run(() => del(`ticket-panels/${b.dataset.del}`), 'Panel deleted'); reload(); }
  }));
}

async function ticketsTab(body) {
  body.innerHTML = `<div class="toolbar"><select id="tk-status"><option value="open">Open</option><option value="closed">Closed</option><option value="all">All</option></select></div><div id="tk-body"></div>`;
  const load = async () => {
    const d = await get('tickets?status=' + $('#tk-status').value);
    const s = d.stats;
    $('#tk-body').innerHTML = `<div class="stats">${statBox(fmtNum(s.total), 'Total tickets')}${statBox(fmtNum(s.open || 0), 'Open')}${statBox(s.avg_rating != null ? s.avg_rating + ' / 5' : '—', 'Average rating')}${statBox(s.avg_hours != null ? s.avg_hours + 'h' : '—', 'Average time to close')}</div>
      <div class="panel table-scroll">${d.tickets.length ? `<table class="data"><thead><tr><th>#</th><th>Panel</th><th>Opened by</th><th>Claimed by</th><th>Priority</th><th>Status</th><th>Opened</th><th>Rating</th></tr></thead><tbody>
      ${d.tickets.map((t) => `<tr class="clickable" data-t="${t.id}"><td>${t.number}</td><td>${esc(t.panel || '—')}</td><td>${userCell(t.opener, t.opener_id)}</td><td>${t.claimed_by ? userCell(t.claimer, t.claimed_by) : '<span class="faint">Unclaimed</span>'}</td>
      <td>${esc(t.priority)}</td><td>${statusPill(t.status)}</td><td class="faint nowrap">${fmtTime(t.created_at)}</td><td>${t.rating ? '★'.repeat(t.rating) : '—'}</td></tr>`).join('')}</tbody></table>` : emptyBox('No tickets here.')}</div>`;
    $$('[data-t]').forEach((tr) => tr.addEventListener('click', async () => {
      const t = await get('tickets/' + tr.dataset.t);
      Dialog.open(`Ticket #${t.number}`, `<dl class="kv"><dt>Status</dt><dd>${statusPill(t.status)}</dd><dt>Opened by</dt><dd>${userCell(t.opener, t.opener_id)}</dd><dt>Claimed by</dt><dd>${t.claimed_by ? userCell(t.claimer, t.claimed_by) : '—'}</dd>
        <dt>Opened</dt><dd>${fmtTime(t.created_at)}</dd>${t.closed_at ? `<dt>Closed</dt><dd>${fmtTime(t.closed_at)}</dd><dt>Close reason</dt><dd>${esc(t.close_reason || '—')}</dd>` : ''}
        <dt>Channel</dt><dd>${t.status === 'open' ? `<a href="https://discord.com/channels/${NX.gid}/${t.channel_id}" target="_blank" rel="noopener">Open in Discord</a>` : '—'}</dd>
        <dt>Rating</dt><dd>${t.rating ? '★'.repeat(t.rating) : '—'}</dd></dl>
        ${t.answers.length ? `<h3 class="mt">Answers</h3>${t.answers.map((a) => `<p><strong>${esc(a.question)}</strong><br>${esc(a.answer || '—')}</p>`).join('')}` : ''}
        ${t.status === 'open' && canWrite() ? '<label class="mt" style="margin-top:16px">Close reason</label><input name="reason" placeholder="Closed from dashboard">' : ''}`, [
        ...(t.has_transcript ? [{ label: 'View transcript', onClick: () => { window.open(`/dashboard/${NX.gid}/tickets/${t.id}/transcript`, '_blank', 'noopener'); return true; } }] : []),
        ...(t.status === 'open' && canWrite() ? [{ label: 'Close ticket', cls: 'danger', onClick: async (b) => { await run(() => post(`tickets/${t.id}/close`, { reason: $('[name=reason]', b).value }), 'Closing ticket…'); setTimeout(load, 2500); } }] : []),
      ]);
    }));
  };
  $('#tk-status').addEventListener('change', load); await load();
}
NX.register('tickets', 'Tickets', async (c, sub) => tabs(c, [
  { id: 'tickets', label: 'Tickets', render: ticketsTab }, { id: 'panels', label: 'Panels', render: panelsTab },
  { id: 'settings', label: 'Settings', render: (b) => renderSettings(b, 'tickets') },
], sub));

/* ============================================================ applications */
const FORM_KINDS = { staff: 'Staff', moderator: 'Moderator', administrator: 'Administrator', developer: 'Developer', media: 'Media team', partnership: 'Partnership', custom: 'Custom' };
function formEditor(form, reload) {
  const f = form || { name: '', kind: 'staff', description: '', questions: [{ label: 'How old are you?', type: 'short', required: true }, { label: 'Why do you want to join the team?', type: 'long', required: true }],
    cooldown_hours: 24, max_submissions: 0, required_votes: 1, enabled: 1, accept_role_ids: [], remove_role_ids: [], reviewer_role_ids: [],
    accept_message: 'Congratulations! Your {form} application in {server} was accepted.', deny_message: 'Thank you for applying. Your {form} application in {server} was not accepted this time.' };
  const qs = JSON.parse(JSON.stringify(f.questions));
  const body = Dialog.open(form ? `Edit form: ${f.name}` : 'New application form', `<div class="form-grid">
    <div class="field"><label>Form name</label><input name="name" maxlength="80" value="${esc(f.name)}" placeholder="e.g. Moderator application"></div>
    <div class="field"><label>Type</label><select name="kind">${options(Object.entries(FORM_KINDS).map(([id, label]) => ({ id, label })), f.kind)}</select></div>
    <div class="field wide"><label>Description shown on the Apply message</label><textarea name="description" maxlength="2000">${esc(f.description)}</textarea></div>
    <div class="field"><label>Apply button channel</label>${channelSelect('panel_channel_id', f.panel_channel_id, 'None')}</div>
    <div class="field"><label>Review channel</label>${channelSelect('review_channel_id', f.review_channel_id, 'Use default from settings')}</div>
    <div class="field"><label>Cooldown between applications (hours)</label><input type="number" name="cooldown_hours" min="0" max="8760" value="${f.cooldown_hours}"></div>
    <div class="field"><label>Max submissions per member</label><input type="number" name="max_submissions" min="0" max="1000" value="${f.max_submissions}"><div class="help">0 = unlimited</div></div>
    <div class="field"><label>Reviewer votes needed</label><input type="number" name="required_votes" min="1" max="25" value="${f.required_votes}"></div>
    <div class="field">${switchHtml('enabled', f.enabled, 'Accepting applications')}</div>
    <div class="field wide"><label>Reviewer roles</label>${chipPicker('reviewer_role_ids', f.reviewer_role_ids, 'roles')}<div class="help">Members with Manage Server can always review.</div></div>
    <div class="field wide"><label>Roles given when accepted</label>${chipPicker('accept_role_ids', f.accept_role_ids, 'roles')}</div>
    <div class="field wide"><label>Roles removed when accepted</label>${chipPicker('remove_role_ids', f.remove_role_ids, 'roles')}</div>
    <div class="field wide"><label>Acceptance message (DM)</label><textarea name="accept_message" maxlength="2000">${esc(f.accept_message)}</textarea></div>
    <div class="field wide"><label>Rejection message (DM)</label><textarea name="deny_message" maxlength="2000">${esc(f.deny_message)}</textarea><div class="help">{form}, {server} and {user} are available.</div></div>
    <div class="field wide"><label>Questions</label><div id="fq"></div></div></div>`, [
    { label: 'Cancel', cls: 'ghost' },
    { label: form ? 'Save form' : 'Create form', cls: 'primary', onClick: async (b) => {
      const data = { ...readForm(b), questions: qs };
      await run(() => (form ? put(`application-forms/${form.id}`, data) : post('application-forms', data)), 'Form saved'); reload();
    } },
  ]);
  hydrateChips(body); questionBuilder($('#fq', body), qs, { max: 100, choice: true });
}

async function formsTab(body) {
  const d = await get('application-forms');
  body.innerHTML = `<div class="toolbar">${writeBtn('id="fm-new"', `${icon('plus')} New form`, 'primary')}</div>${d.forms.length ? d.forms.map((f) => `<section class="panel"><div class="panel-head"><div><h3>${esc(f.name)} ${f.enabled ? pill('Open', 'ok') : pill('Closed')}</h3>
    <span class="muted">${esc(FORM_KINDS[f.kind] || f.kind)} · ${f.questions.length} questions · ${f.submissions} submissions (${f.pending} pending, ${f.accepted} accepted)</span></div>
    <div class="inline-flex">${writeBtn(`data-pub="${f.id}"`, f.panel_message_id ? 'Update Apply message' : 'Post Apply button', 'small primary')}${writeBtn(`data-edit="${f.id}"`, 'Edit', 'small')}${writeBtn(`data-del="${f.id}"`, 'Delete', 'small danger')}</div></div></section>`).join('') : emptyBox('No application forms yet.')}`;
  const reload = () => formsTab(body);
  const nb = $('#fm-new', body); if (nb) nb.addEventListener('click', () => formEditor(null, reload));
  $$('[data-edit]', body).forEach((b) => b.addEventListener('click', () => formEditor(d.forms.find((f) => String(f.id) === b.dataset.edit), reload)));
  $$('[data-pub]', body).forEach((b) => b.addEventListener('click', async () => { await run(() => post(`application-forms/${b.dataset.pub}/publish`), 'Apply button posted'); reload(); }));
  $$('[data-del]', body).forEach((b) => b.addEventListener('click', async () => {
    if (await confirmDialog('Delete form', 'Delete this form? Past submissions stay in the history.', 'Delete', 'danger')) { await run(() => del(`application-forms/${b.dataset.del}`), 'Form deleted'); reload(); }
  }));
}

async function submissionsTab(body) {
  const forms = (await get('application-forms')).forms;
  body.innerHTML = `<div class="toolbar"><select id="sb-status"><option value="pending">Pending</option><option value="accepted">Accepted</option><option value="denied">Denied</option><option value="all">All</option></select>
    <select id="sb-form"><option value="">All forms</option>${forms.map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join('')}</select></div><div id="sb-body"></div>`;
  const load = async () => {
    const d = await get(`applications?status=${$('#sb-status').value}${$('#sb-form').value ? '&form_id=' + $('#sb-form').value : ''}`);
    const s = d.stats;
    $('#sb-body').innerHTML = `<div class="stats">${statBox(fmtNum(s.total), 'Submissions')}${statBox(fmtNum(s.pending || 0), 'Pending')}${statBox(fmtNum(s.accepted || 0), 'Accepted')}${statBox(fmtNum(s.denied || 0), 'Denied')}${statBox(s.avg_hours != null ? s.avg_hours + 'h' : '—', 'Average review time')}</div>
      <div class="panel table-scroll">${d.submissions.length ? `<table class="data"><thead><tr><th>#</th><th>Form</th><th>Applicant</th><th>Status</th><th>Votes</th><th>Submitted</th></tr></thead><tbody>${d.submissions.map((x) => `<tr class="clickable" data-s="${x.id}">
      <td>${x.id}</td><td>${esc(x.form)}</td><td>${userCell(x.user, x.user_id)}</td><td>${statusPill(x.status)}</td><td>${x.votes_accept} ✓ / ${x.votes_deny} ✗</td><td class="faint nowrap">${fmtTime(x.created_at)}</td></tr>`).join('')}</tbody></table>` : emptyBox('No submissions here.')}</div>`;
    $$('[data-s]').forEach((tr) => tr.addEventListener('click', () => openSubmission(tr.dataset.s, load)));
  };
  $('#sb-status').addEventListener('change', load); $('#sb-form').addEventListener('change', load); await load();
}
async function openSubmission(id, reload) {
  const s = await get('applications/' + id);
  const pending = s.status === 'pending' && canWrite();
  Dialog.open(`${s.form} #${s.id}`, `<dl class="kv"><dt>Applicant</dt><dd>${userCell(s.user, s.user_id)} <span class="faint">${esc(s.user_id)}</span></dd><dt>Status</dt><dd>${statusPill(s.status)}</dd><dt>Submitted</dt><dd>${fmtTime(s.created_at)}</dd>
    ${s.decided_at ? `<dt>Decided</dt><dd>${fmtTime(s.decided_at)} by ${esc(s.reviewer && s.reviewer.name || s.reviewer_id)}</dd>` : ''}${s.decision_reason ? `<dt>Reason</dt><dd>${esc(s.decision_reason)}</dd>` : ''}</dl>
    <h3 class="mt">Answers</h3>${s.answers.map((a, i) => `<div style="margin-bottom:14px"><strong>${i + 1}. ${esc(a.question)}</strong><div style="white-space:pre-wrap">${esc(a.answer || '—')}</div></div>`).join('')}
    <h3 class="mt">Votes</h3>${s.votes.map((v) => `<div>${esc(v.reviewer && v.reviewer.name || v.reviewer_id)}: ${v.vote === 'accept' ? '✓ accept' : '✗ deny'}</div>`).join('') || '<p class="muted">No votes yet.</p>'}
    <h3 class="mt">Reviewer notes</h3>${s.notes.map((n) => `<p><strong>${esc(n.reviewer && n.reviewer.name || n.reviewer_id)}</strong> <span class="faint">${fmtAgo(n.created_at)}</span><br>${esc(n.note)}</p>`).join('') || '<p class="muted">No notes.</p>'}
    ${canWrite() ? '<div class="inline-flex"><input name="note" placeholder="Add a note for other reviewers" style="flex:1"><button class="btn" type="button" id="sb-note">Add note</button></div>' : ''}
    ${s.history.length ? `<h3 class="mt">Previous applications</h3>${s.history.map((h) => `<div>${esc(h.form)} · ${statusPill(h.status)} · ${fmtTime(h.created_at)}</div>`).join('')}` : ''}
    ${pending ? '<label class="mt" style="margin-top:16px">Reason (sent with a rejection)</label><input name="reason" maxlength="1000">' : ''}`,
  pending ? [{ label: 'Deny', cls: 'danger', onClick: async (b) => { await run(() => post(`applications/${id}/vote`, { accept: false, reason: $('[name=reason]', b).value }), (r) => r.message); reload(); } },
    { label: 'Accept', cls: 'primary', onClick: async () => { await run(() => post(`applications/${id}/vote`, { accept: true }), (r) => r.message); reload(); } }] : []);
  const nb = $('#sb-note'); if (nb) nb.addEventListener('click', async () => { await run(() => post(`applications/${id}/notes`, { note: $('#dlg [name=note]').value }), 'Note added'); openSubmission(id, reload); });
}
NX.register('applications', 'Applications', async (c, sub) => tabs(c, [
  { id: 'review', label: 'Review', render: submissionsTab }, { id: 'forms', label: 'Forms', render: formsTab },
  { id: 'settings', label: 'Settings', render: (b) => renderSettings(b, 'applications') },
], sub));

/* =============================================================== giveaways */
function giveawayCreate(reload) {
  const bonus = [];
  const body = Dialog.open('New giveaway', `<div class="form-grid">
    <div class="field wide"><label>Prize</label><input name="prize" maxlength="200" placeholder="e.g. Discord Nitro"></div>
    <div class="field"><label>Channel</label>${channelSelect('channel_id', '', 'Choose channel')}</div>
    <div class="field"><label>Duration</label><input name="duration" placeholder="e.g. 1d, 12h, 1w" value="1d"></div>
    <div class="field"><label>Winners</label><input type="number" name="winners" min="1" max="50" value="1"></div>
    <div class="field"><label>Start later (optional)</label><input type="datetime-local" name="starts_at"></div>
    <div class="field wide"><label>Description</label><textarea name="description" maxlength="2000"></textarea></div>
    <div class="field wide"><label>Required roles (member needs any one)</label>${chipPicker('required_role_ids', [], 'roles')}</div>
    <div class="field"><label>Minimum account age (days)</label><input type="number" name="min_account_age_days" min="0" value="0"></div>
    <div class="field"><label>Minimum level</label><input type="number" name="min_level" min="0" value="0"></div>
    <div class="field wide"><label>Bonus entries</label><div data-rows="bonus"></div><div class="help">Members get their single highest bonus.</div></div></div>`, [
    { label: 'Cancel', cls: 'ghost' },
    { label: 'Start giveaway', cls: 'primary', onClick: async (b) => {
      const d = readForm(b); d.bonus_roles = bonus;
      if (d.starts_at) d.starts_at = new Date(d.starts_at).toISOString(); else delete d.starts_at;
      await run(() => post('giveaways', d), 'Giveaway created'); reload();
    } },
  ]);
  hydrateChips(body);
  rowsEditor($('[data-rows="bonus"]', body), { columns: [{ key: 'role_id', type: 'role', label: 'Role', default: null }, { key: 'entries', type: 'int', label: 'Extra entries', default: 1 }] }, bonus, () => {});
}
async function giveawaysTab(body) {
  body.innerHTML = `<div class="toolbar"><select id="gw-status"><option value="all">All</option><option value="running">Running</option><option value="scheduled">Scheduled</option><option value="ended">Ended</option><option value="cancelled">Cancelled</option></select>${writeBtn('id="gw-new"', `${icon('plus')} New giveaway`, 'primary')}</div><div id="gw-body"></div>`;
  const load = async () => {
    const d = await get('giveaways?status=' + $('#gw-status').value);
    const s = d.stats;
    $('#gw-body').innerHTML = `<div class="stats">${statBox(fmtNum(s.total), 'Giveaways')}${statBox(fmtNum(s.running || 0), 'Running')}${statBox(fmtNum(s.scheduled || 0), 'Scheduled')}${statBox(fmtNum(s.entries), 'Total entries')}</div>` +
      (d.giveaways.length ? d.giveaways.map((g) => `<section class="panel"><div class="panel-head"><div><h3>${esc(g.prize)} ${statusPill(g.status)}</h3>
      <span class="muted">#${g.id} · ${g.channel_name ? '#' + esc(g.channel_name) : 'deleted channel'} · ${g.winner_count} winner(s) · ${g.entry_count} entrants (${g.ticket_count} entries) · ${g.status === 'scheduled' ? 'starts ' + fmtTime(g.starts_at) : (g.status === 'running' ? 'ends ' : 'ended ') + fmtTime(g.ends_at)}</span>
      ${g.status === 'ended' ? `<div class="mt">Winners: ${g.winners.map((w) => esc(w.name || w.tag || w.id)).join(', ') || 'none'}</div>` : ''}</div>
      <div class="inline-flex">${canWrite() ? (g.status === 'running' ? `<button class="btn small" data-act="end" data-id="${g.id}">End now</button><button class="btn small" data-act="extend" data-id="${g.id}">Extend</button><button class="btn small danger" data-act="cancel" data-id="${g.id}">Cancel</button>`
        : g.status === 'scheduled' ? `<button class="btn small danger" data-act="cancel" data-id="${g.id}">Cancel</button>` : g.status === 'ended' ? `<button class="btn small" data-act="reroll" data-id="${g.id}">Reroll</button>` : '') : ''}</div></div></section>`).join('') : emptyBox('No giveaways yet.'));
    $$('[data-act]').forEach((b) => b.addEventListener('click', async () => {
      const a = b.dataset.act; let payload = {};
      if (a === 'extend') { const by = prompt('Extend by (e.g. 1h, 1d)'); if (!by) return; payload = { by }; }
      if (a === 'reroll') { const n = prompt('How many new winners?', '1'); if (!n) return; payload = { count: n }; }
      if (a === 'cancel' && !await confirmDialog('Cancel giveaway', 'Cancel this giveaway? No winners will be picked.', 'Cancel giveaway', 'danger')) return;
      await run(() => post(`giveaways/${b.dataset.id}/${a}`, payload), (r) => r.message); load();
    }));
  };
  $('#gw-status').addEventListener('change', load);
  const nb = $('#gw-new'); if (nb) nb.addEventListener('click', () => giveawayCreate(load));
  await load();
}
NX.register('giveaways', 'Giveaways', async (c, sub) => tabs(c, [
  { id: 'list', label: 'Giveaways', render: giveawaysTab }, { id: 'settings', label: 'Settings', render: (b) => renderSettings(b, 'giveaways') },
], sub));

/* ================================================================= welcome */
NX.register('welcome', 'Welcome', async (c) => renderSettings(c, 'welcome'));

/* =================================================================== roles */
function menuEditor(menu, reload) {
  const m = menu || { kind: 'button', title: 'Pick your roles', description: '', options: [], max_values: 0 };
  const opts = JSON.parse(JSON.stringify(m.options));
  const body = Dialog.open(menu ? 'Edit role menu' : 'New role menu', `<div class="form-grid">
    <div class="field"><label>Type</label><select name="kind">${options([{ id: 'button', label: 'Buttons' }, { id: 'dropdown', label: 'Dropdown' }, { id: 'reaction', label: 'Reactions' }], m.kind)}</select></div>
    <div class="field"><label>Channel</label>${channelSelect('channel_id', m.channel_id, 'Choose channel')}</div>
    <div class="field"><label>Max roles per member</label><input type="number" name="max_values" min="0" max="25" value="${m.max_values}"><div class="help">0 = unlimited, 1 = pick only one</div></div>
    <div class="field wide"><label>Title</label><input name="title" maxlength="256" value="${esc(m.title)}"></div>
    <div class="field wide"><label>Description</label><textarea name="description" maxlength="2000">${esc(m.description)}</textarea></div>
    <div class="field wide"><label>Roles</label><div id="mo"></div><div class="help">Only roles below Nexora's highest role can be used.</div></div></div>`, [
    { label: 'Cancel', cls: 'ghost' },
    { label: 'Save menu', cls: 'primary', onClick: async (b) => { await run(() => (menu ? put(`role-menus/${menu.id}`, { ...readForm(b), options: opts }) : post('role-menus', { ...readForm(b), options: opts })), 'Menu saved. Publish it to update Discord.'); reload(); } },
  ]);
  const draw = () => {
    $('#mo', body).innerHTML = opts.map((o, i) => `<div class="row-edit" data-o="${i}" style="grid-template-columns:2fr 2fr 1fr 2fr auto;display:grid;align-items:end">
      <div><label class="help">Role</label><select data-k="role_id">${options(roleList(true), o.role_id, 'Choose role')}</select></div>
      <div><label class="help">Label</label><input data-k="label" maxlength="80" value="${esc(o.label || '')}" placeholder="Role name"></div>
      <div><label class="help">Emoji</label><input data-k="emoji" maxlength="64" value="${esc(o.emoji || '')}"></div>
      <div><label class="help">Description</label><input data-k="description" maxlength="100" value="${esc(o.description || '')}"></div>
      <button class="btn small danger" type="button" data-rm="${i}">Remove</button></div>`).join('') + `<button type="button" class="btn small" id="mo-add">${icon('plus')} Add role</button>`;
    $$('[data-o]', body).forEach((row) => $$('[data-k]', row).forEach((el) => el.addEventListener('change', () => { opts[+row.dataset.o][el.dataset.k] = el.value; })));
    $$('[data-rm]', body).forEach((b) => b.addEventListener('click', () => { opts.splice(+b.dataset.rm, 1); draw(); }));
    $('#mo-add', body).addEventListener('click', () => { if (opts.length < 25) { opts.push({ role_id: '', label: '', emoji: '', description: '' }); draw(); } });
  };
  draw();
}
NX.register('roles', 'Roles', async (c) => {
  const d = await get('role-menus');
  c.innerHTML = `<p class="panel-desc">Role menus let members give themselves roles with buttons, a dropdown or reactions. Auto roles for new members are under Welcome. Temporary roles use <code>/role temp</code>.</p>
    <div class="toolbar">${writeBtn('id="rm-new"', `${icon('plus')} New role menu`, 'primary')}</div>
    ${d.menus.length ? d.menus.map((m) => `<section class="panel"><div class="panel-head"><div><h3>${esc(m.title)} ${pill(m.kind)}</h3><span class="muted">${chName(m.channel_id)} · ${m.options.map((o) => roleName(o.role_id)).join(', ')}</span></div>
    <div class="inline-flex">${writeBtn(`data-pub="${m.id}"`, m.message_id ? 'Update message' : 'Publish', 'small primary')}${writeBtn(`data-edit="${m.id}"`, 'Edit', 'small')}${writeBtn(`data-del="${m.id}"`, 'Delete', 'small danger')}</div></div></section>`).join('') : emptyBox('No role menus yet.')}`;
  const reload = () => route();
  const nb = $('#rm-new'); if (nb) nb.addEventListener('click', () => menuEditor(null, reload));
  $$('[data-edit]').forEach((b) => b.addEventListener('click', () => menuEditor(d.menus.find((m) => String(m.id) === b.dataset.edit), reload)));
  $$('[data-pub]').forEach((b) => b.addEventListener('click', async () => { await run(() => post(`role-menus/${b.dataset.pub}/publish`), 'Role menu published'); reload(); }));
  $$('[data-del]').forEach((b) => b.addEventListener('click', async () => { if (await confirmDialog('Delete role menu', 'Delete this menu and its Discord message?', 'Delete', 'danger')) { await run(() => del(`role-menus/${b.dataset.del}`), 'Menu deleted'); reload(); } }));
});

/* ======================================================= leveling/economy */
async function boardTab(body, board) {
  const d = await get('leaderboard?board=' + board);
  body.innerHTML = `<p class="help">Public page: <a href="/leaderboard/${NX.gid}?board=${board === 'economy' ? 'economy' : 'xp'}" target="_blank" rel="noopener">/leaderboard/${NX.gid}</a> (visible while the module is enabled).</p>
    <div class="panel table-scroll">${d.rows.length ? `<table class="data"><thead><tr><th>Rank</th><th>Member</th>${board === 'economy' ? '<th>Balance</th>' : '<th>Level</th><th>XP</th><th>Messages</th>'}</tr></thead><tbody>${d.rows.map((r, i) => `<tr><td>${i + 1}</td><td>${userCell(r.user, r.user_id)}</td>${board === 'economy' ? `<td>${fmtNum(r.value)}</td>` : `<td>${r.level}</td><td>${fmtNum(r.value)}</td><td>${fmtNum(r.messages)}</td>`}</tr>`).join('')}</tbody></table>` : emptyBox('Nobody has earned anything yet.')}</div>`;
}
NX.register('leveling', 'Leveling', async (c, sub) => tabs(c, [
  { id: 'settings', label: 'Settings', render: (b) => renderSettings(b, 'leveling') }, { id: 'leaderboard', label: 'Leaderboard', render: (b) => boardTab(b, 'xp') },
], sub));

function crudPage(kind, cfg) {
  return async (body) => {
    const d = await get('collections/' + kind);
    body.innerHTML = `${cfg.intro ? `<p class="panel-desc">${cfg.intro}</p>` : ''}<div class="toolbar">${writeBtn('id="cr-new"', `${icon('plus')} ${cfg.newLabel}`, 'primary')}</div>
      <div class="panel table-scroll">${d.items.length ? `<table class="data"><thead><tr>${cfg.cols.map((col) => `<th>${col[0]}</th>`).join('')}<th></th></tr></thead><tbody>${d.items.map((it) => `<tr>${cfg.cols.map((col) => `<td>${col[1](it)}</td>`).join('')}
      <td class="nowrap right">${(cfg.extraActions || []).map((a) => writeBtn(`data-x="${a.id}" data-id="${it.id}"`, a.label, 'small')).join(' ')} ${writeBtn(`data-edit="${it.id}"`, 'Edit', 'small')} ${writeBtn(`data-del="${it.id}"`, 'Delete', 'small danger')}</td></tr>`).join('')}</tbody></table>` : emptyBox(cfg.empty)}</div>`;
    const reload = () => crudPage(kind, cfg)(body);
    const edit = (item) => {
      const b = Dialog.open(item ? 'Edit' : cfg.newLabel, `<div class="form-grid">${cfg.form(item || {})}</div>`, [
        { label: 'Cancel', cls: 'ghost' },
        { label: 'Save', cls: 'primary', onClick: async (bb) => { const data = cfg.collect ? cfg.collect(bb) : readForm(bb); await run(() => (item ? put(`collections/${kind}/${item.id}`, data) : post(`collections/${kind}`, data)), 'Saved'); reload(); } },
      ]);
      hydrateChips(b); if (cfg.afterOpen) cfg.afterOpen(b, item || {});
    };
    const nb = $('#cr-new', body); if (nb) nb.addEventListener('click', () => edit(null));
    $$('[data-edit]', body).forEach((b) => b.addEventListener('click', () => edit(d.items.find((x) => String(x.id) === b.dataset.edit))));
    $$('[data-del]', body).forEach((b) => b.addEventListener('click', async () => { if (await confirmDialog('Delete', 'Delete this item?', 'Delete', 'danger')) { await run(() => del(`collections/${kind}/${b.dataset.del}`), 'Deleted'); reload(); } }));
    $$('[data-x]', body).forEach((b) => b.addEventListener('click', () => cfg.extraActions.find((a) => a.id === b.dataset.x).run(d.items.find((x) => String(x.id) === b.dataset.id), reload)));
  };
}
const shopCrud = crudPage('shop', {
  newLabel: 'New shop item', empty: 'The shop is empty. Items can give a role when bought.',
  cols: [['Item', (i) => `<strong>${esc(i.name)}</strong><div class="faint">${esc(i.description)}</div>`], ['Price', (i) => fmtNum(i.price)], ['Role', (i) => esc(i.role_id ? roleName(i.role_id) : '—')], ['Stock', (i) => (i.stock < 0 ? 'Unlimited' : i.stock)], ['Status', (i) => (i.enabled ? pill('On sale', 'ok') : pill('Hidden'))]],
  form: (i) => `<div class="field"><label>Name</label><input name="name" maxlength="80" value="${esc(i.name || '')}"></div><div class="field"><label>Price</label><input type="number" name="price" min="0" value="${i.price || 100}"></div>
    <div class="field wide"><label>Description</label><input name="description" maxlength="300" value="${esc(i.description || '')}"></div>
    <div class="field"><label>Role given (optional)</label>${roleSelect('role_id', i.role_id, 'No role', true)}</div><div class="field"><label>Stock</label><input type="number" name="stock" min="-1" value="${i.stock == null ? -1 : i.stock}"><div class="help">-1 = unlimited</div></div>
    <div class="field">${switchHtml('enabled', i.enabled !== 0, 'On sale')}</div>`,
});
NX.register('economy', 'Economy', async (c, sub) => tabs(c, [
  { id: 'settings', label: 'Settings', render: (b) => renderSettings(b, 'economy') }, { id: 'shop', label: 'Shop', render: shopCrud },
  { id: 'leaderboard', label: 'Leaderboard', render: (b) => boardTab(b, 'economy') },
], sub));

/* ============================================================= suggestions */
async function suggestionsTab(body) {
  body.innerHTML = `<div class="toolbar"><select id="sg-status"><option value="all">All</option><option value="open">Open</option><option value="approved">Approved</option><option value="considering">Under consideration</option><option value="implemented">Implemented</option><option value="denied">Denied</option></select></div><div id="sg-list"></div>`;
  const load = async () => {
    const d = await get('suggestions?status=' + $('#sg-status').value);
    $('#sg-list').innerHTML = d.suggestions.length ? d.suggestions.map((s) => `<section class="panel"><div class="panel-head"><h3>#${s.number} ${statusPill(s.status)}</h3><span class="faint">👍 ${s.up} · 👎 ${s.down} · ${fmtAgo(s.created_at)}</span></div>
      <p style="white-space:pre-wrap">${esc(s.content)}</p><div class="faint">by ${esc(s.user && s.user.name || s.user_id)}</div>${s.response ? `<p class="mt"><strong>Response:</strong> ${esc(s.response)}</p>` : ''}
      ${canWrite() ? `<div class="inline-flex mt"><select data-st="${s.number}">${['approved', 'considering', 'implemented', 'denied', 'open'].map((x) => `<option ${x === s.status ? 'selected' : ''}>${x}</option>`).join('')}</select><input data-resp="${s.number}" placeholder="Response (optional)" style="flex:1;min-width:200px" value="${esc(s.response || '')}"><button class="btn" data-go="${s.number}">Update</button></div>` : ''}</section>`).join('') : emptyBox('No suggestions yet.');
    $$('[data-go]').forEach((b) => b.addEventListener('click', async () => { const n = b.dataset.go; await run(() => post(`suggestions/${n}/respond`, { status: $(`[data-st="${n}"]`).value, response: $(`[data-resp="${n}"]`).value }), (r) => r.message.replace(/\*/g, '')); load(); }));
  };
  $('#sg-status').addEventListener('change', load); await load();
}
NX.register('suggestions', 'Suggestions', async (c, sub) => tabs(c, [
  { id: 'list', label: 'Suggestions', render: suggestionsTab }, { id: 'settings', label: 'Settings', render: (b) => renderSettings(b, 'suggestions') },
], sub));

/* =================================================================== polls */
NX.register('polls', 'Polls', async (c) => {
  const d = await get('polls');
  c.innerHTML = `${canWrite() ? `<section class="panel"><h3>Create a poll</h3><div class="form-grid mt" id="pl-form">
    <div class="field wide"><label>Question</label><input name="question" maxlength="250"></div><div class="field"><label>Channel</label>${channelSelect('channel_id', '', 'Choose channel')}</div>
    <div class="field"><label>Duration (optional)</label><input name="duration" placeholder="e.g. 1d"></div><div class="field">${switchHtml('multi', false, 'Allow multiple choices')}</div>
    <div class="field wide"><label>Options (one per line, 2-20)</label><textarea name="options"></textarea></div></div><button class="btn primary mt" id="pl-go">Post poll</button></section>` : ''}
    ${d.polls.length ? d.polls.map((p) => { const total = p.results.reduce((a, b) => a + b, 0) || 1; return `<section class="panel"><div class="panel-head"><h3>${esc(p.question)} ${statusPill(p.status)}</h3>${p.status === 'open' ? writeBtn(`data-end="${p.id}"`, 'End poll', 'small') : ''}</div>
      ${p.options.map((o, i) => `<div style="margin-bottom:8px"><div class="inline-flex" style="justify-content:space-between;display:flex"><span>${esc(o)}</span><span class="faint">${p.results[i]} (${Math.round(p.results[i] / total * 100)}%)</span></div><div style="height:6px;background:var(--raised);border-radius:3px"><div style="height:6px;border-radius:3px;background:var(--violet);width:${p.results[i] / total * 100}%"></div></div></div>`).join('')}
      <p class="faint">${chName(p.channel_id)} · ${fmtTime(p.created_at)}${p.ends_at ? ' · ends ' + fmtTime(p.ends_at) : ''}</p></section>`; }).join('') : emptyBox('No polls yet.')}`;
  const go = $('#pl-go');
  if (go) go.addEventListener('click', async () => { const f = readForm($('#pl-form')); f.options = f.options.split('\n').map((s) => s.trim()).filter(Boolean); await run(() => post('polls', f), 'Poll posted'); route(); });
  $$('[data-end]').forEach((b) => b.addEventListener('click', async () => { await run(() => post(`polls/${b.dataset.end}/end`), 'Poll ended'); route(); }));
});

/* ========================================================= more community */
NX.register('community', 'More community', async (c, sub) => tabs(c, [
  { id: 'starboard', label: 'Starboard', render: (b) => renderSettings(b, 'starboard') }, { id: 'counting', label: 'Counting', render: (b) => renderSettings(b, 'counting') },
  { id: 'birthdays', label: 'Birthdays', render: (b) => renderSettings(b, 'birthdays') }, { id: 'stats', label: 'Stat channels', render: (b) => renderSettings(b, 'server_stats') },
  { id: 'ai', label: 'AI assistant', render: async (b) => { await renderSettings(b, 'ai'); if (!NX.meta.ai_available) b.insertAdjacentHTML('afterbegin', '<div class="notice">The bot owner hasn\'t set ANTHROPIC_API_KEY, so /ask won\'t work until they do.</div>'); } },
], sub));

/* ========================================================= custom commands */
const commandsCrud = crudPage('commands', {
  newLabel: 'New command', empty: 'No custom commands yet.',
  intro: 'Custom commands reply when someone types the server prefix plus the name, e.g. <code>!rules</code>. Change the prefix in Server Settings.',
  cols: [['Command', (i) => `<span class="cmd-name">${esc(i.name)}</span>`], ['Response', (i) => `<span class="truncate" style="display:block">${esc(i.response || (i.embed ? '(embed) ' + (i.embed.title || '') : ''))}</span>`], ['Uses', (i) => fmtNum(i.uses)], ['Status', (i) => (i.enabled ? pill('On', 'ok') : pill('Off'))]],
  form: (i) => `<div class="field"><label>Name</label><input name="name" maxlength="32" value="${esc(i.name || '')}" placeholder="rules"></div>
    <div class="field">${switchHtml('enabled', i.enabled !== 0, 'Enabled')}</div><div class="field">${switchHtml('delete_trigger', !!i.delete_trigger, 'Delete the triggering message')}</div>
    <div class="field wide"><label>Text response</label><textarea name="response" maxlength="2000">${esc(i.response || '')}</textarea><div class="help">{user}, {server}, {args}, {channel} and {member_count} are available.</div></div>
    <div class="field wide"><label>Only these roles can use it (optional)</label>${chipPicker('required_role_ids', i.required_role_ids || [], 'roles')}</div>
    <div class="field"><label>Embed title (optional)</label><input name="e_title" maxlength="256" value="${esc(i.embed && i.embed.title || '')}"></div>
    <div class="field"><label>Embed color</label><input name="e_color" maxlength="7" value="${esc(i.embed && i.embed.color || '#7C3AED')}"></div>
    <div class="field wide"><label>Embed description</label><textarea name="e_description" maxlength="4000">${esc(i.embed && i.embed.description || '')}</textarea></div>`,
  collect: (b) => { const f = readForm(b); const out = { name: f.name, enabled: f.enabled, delete_trigger: f.delete_trigger, response: f.response, required_role_ids: f.required_role_ids };
    if (f.e_title || f.e_description) out.embed = { title: f.e_title, description: f.e_description, color: f.e_color || '#7C3AED' }; return out; },
});
NX.register('commands', 'Custom Commands', async (c) => commandsCrud(c));

/* ================================================================== embeds */
function blankEmbed() { return { content: '', title: '', description: '', url: '', color: '#7C3AED', image: '', thumbnail: '', timestamp: false, author: { name: '', url: '', icon_url: '' }, footer: { text: '', icon_url: '' }, fields: [], buttons: [] }; }
function embedPreview(e) {
  const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\*(.+?)\*/g, '<em>$1</em>').replace(/`(.+?)`/g, '<code>$1</code>');
  const hasEmbed = e.title || e.description || e.fields.length || e.image || e.author.name || e.footer.text || e.thumbnail;
  return `<div class="dp-msg"><div class="dp-avatar">N</div><div><span class="dp-name">Nexora</span><span class="dp-bot">BOT</span>
    ${e.content ? `<div style="white-space:pre-wrap">${md(e.content)}</div>` : ''}
    ${hasEmbed ? `<div class="dp-embed" style="border-color:${esc(e.color)}"><div>
      ${e.author.name ? `<div class="dp-author">${e.author.icon_url ? `<img src="${esc(e.author.icon_url)}" alt="">` : ''}${esc(e.author.name)}</div>` : ''}
      ${e.title ? `<div class="dp-title">${md(e.title)}</div>` : ''}${e.description ? `<div class="dp-desc">${md(e.description)}</div>` : ''}</div>
      ${e.thumbnail ? `<img class="dp-thumb" src="${esc(e.thumbnail)}" alt="">` : ''}
      ${e.fields.length ? `<div class="dp-fields">${e.fields.map((f) => `<div class="dp-field ${f.inline ? 'inline' : ''}"><b>${md(f.name)}</b>${md(f.value)}</div>`).join('')}</div>` : ''}
      ${e.image ? `<img class="dp-image" src="${esc(e.image)}" alt="">` : ''}
      ${e.footer.text || e.timestamp ? `<div class="dp-footer">${e.footer.icon_url ? `<img src="${esc(e.footer.icon_url)}" alt="">` : ''}${esc(e.footer.text)}${e.timestamp ? (e.footer.text ? ' • ' : '') + 'Today at ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}</div>` : ''}</div>` : ''}
    ${e.buttons.length ? `<div class="dp-buttons">${e.buttons.map((b) => `<span>${esc(b.label)} ↗</span>`).join('')}</div>` : ''}</div></div>`;
}
NX.register('embeds', 'Embeds', async (c) => {
  const saved = (await get('collections/embeds')).items;
  let e = blankEmbed(); let editingId = null;
  c.innerHTML = `<div class="toolbar"><div><label>Saved embeds</label><select id="eb-load"><option value="">New embed</option>${saved.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></div>
    ${canWrite() ? '<div><label>Save as</label><input id="eb-name" maxlength="64" placeholder="Name"></div><button class="btn" id="eb-save">Save</button><button class="btn danger" id="eb-del" hidden>Delete saved</button>' : ''}</div>
    <div class="embed-builder"><div id="eb-form"></div><div><div class="discord-preview" id="eb-prev"></div>
    ${canWrite() ? `<section class="panel mt"><h3>Send to Discord</h3><div class="inline-flex mt">${channelSelect('send_channel', '', 'Choose channel')}<button class="btn primary" id="eb-send">Send now</button></div><p class="help">Saved embeds can also be sent with <code>/embed</code> in Discord, or scheduled on the Announcements page.</p></section>` : ''}</div></div>`;
  const draw = () => {
    $('#eb-form').innerHTML = `<section class="panel"><h3>Message</h3><textarea data-p="content" maxlength="2000" placeholder="Text above the embed (optional)">${esc(e.content)}</textarea></section>
      <section class="panel"><h3>Author</h3><div class="form-grid mt"><div class="field"><label>Name</label><input data-p="author.name" maxlength="256" value="${esc(e.author.name)}"></div><div class="field"><label>Icon URL</label><input data-p="author.icon_url" value="${esc(e.author.icon_url)}"></div><div class="field"><label>Link</label><input data-p="author.url" value="${esc(e.author.url)}"></div></div></section>
      <section class="panel"><h3>Body</h3><div class="form-grid mt"><div class="field wide"><label>Title</label><input data-p="title" maxlength="256" value="${esc(e.title)}"></div><div class="field"><label>Title link</label><input data-p="url" value="${esc(e.url)}"></div>
        <div class="field"><label>Color</label><div class="inline-flex"><input type="color" data-p="color" value="${esc(e.color)}"><span class="faint">${esc(e.color)}</span></div></div>
        <div class="field wide"><label>Description</label><textarea data-p="description" maxlength="4096" style="min-height:140px">${esc(e.description)}</textarea><div class="help">Supports Discord markdown and placeholders like {server} and {member_count}.</div></div></div></section>
      <section class="panel"><div class="panel-head"><h3>Fields</h3><button class="btn small" type="button" id="eb-addf" ${e.fields.length >= 25 ? 'disabled' : ''}>${icon('plus')} Add field</button></div>${e.fields.map((f, i) => `<div class="row-edit"><div class="form-grid"><div class="field"><label class="help">Name</label><input data-f="${i}" data-k="name" maxlength="256" value="${esc(f.name)}"></div>
        <div class="field">${'<label class="help">Inline</label>'}<input type="checkbox" data-f="${i}" data-k="inline" ${f.inline ? 'checked' : ''}></div><div class="field wide"><label class="help">Value</label><textarea data-f="${i}" data-k="value" maxlength="1024">${esc(f.value)}</textarea></div></div><div class="row-actions"><button type="button" class="btn small danger" data-rmf="${i}">Remove</button></div></div>`).join('')}</section>
      <section class="panel"><h3>Images</h3><div class="form-grid mt"><div class="field"><label>Image URL</label><input data-p="image" value="${esc(e.image)}"></div><div class="field"><label>Thumbnail URL</label><input data-p="thumbnail" value="${esc(e.thumbnail)}"></div></div></section>
      <section class="panel"><h3>Footer</h3><div class="form-grid mt"><div class="field"><label>Text</label><input data-p="footer.text" maxlength="2048" value="${esc(e.footer.text)}"></div><div class="field"><label>Icon URL</label><input data-p="footer.icon_url" value="${esc(e.footer.icon_url)}"></div>
        <div class="field">${switchHtml('eb_ts', e.timestamp, 'Show timestamp')}</div></div></section>
      <section class="panel"><div class="panel-head"><h3>Link buttons</h3><button class="btn small" type="button" id="eb-addb" ${e.buttons.length >= 5 ? 'disabled' : ''}>${icon('plus')} Add button</button></div>${e.buttons.map((b, i) => `<div class="row-edit" style="display:grid;grid-template-columns:1fr 2fr auto;align-items:end"><div><label class="help">Label</label><input data-b="${i}" data-k="label" maxlength="80" value="${esc(b.label)}"></div><div><label class="help">URL</label><input data-b="${i}" data-k="url" value="${esc(b.url)}"></div><button type="button" class="btn small danger" data-rmb="${i}">Remove</button></div>`).join('')}</section>`;
    $$('[data-p]', c).forEach((el) => el.addEventListener('input', () => { const [a, b] = el.dataset.p.split('.'); if (b) e[a][b] = el.value; else e[a] = el.value; preview(); }));
    $$('[data-f]', c).forEach((el) => el.addEventListener('input', () => { e.fields[+el.dataset.f][el.dataset.k] = el.type === 'checkbox' ? el.checked : el.value; preview(); }));
    $$('[data-b]', c).forEach((el) => el.addEventListener('input', () => { e.buttons[+el.dataset.b][el.dataset.k] = el.value; preview(); }));
    $('#sw-eb_ts').addEventListener('change', (ev) => { e.timestamp = ev.target.checked; preview(); });
    $('#eb-addf').addEventListener('click', () => { e.fields.push({ name: 'Field', value: 'Value', inline: false }); draw(); });
    $('#eb-addb').addEventListener('click', () => { e.buttons.push({ label: 'Website', url: 'https://' }); draw(); });
    $$('[data-rmf]', c).forEach((b) => b.addEventListener('click', () => { e.fields.splice(+b.dataset.rmf, 1); draw(); }));
    $$('[data-rmb]', c).forEach((b) => b.addEventListener('click', () => { e.buttons.splice(+b.dataset.rmb, 1); draw(); }));
    preview();
  };
  const preview = () => { $('#eb-prev').innerHTML = embedPreview(e); };
  $('#eb-load').addEventListener('change', (ev) => {
    const s = saved.find((x) => String(x.id) === ev.target.value);
    e = s ? { ...blankEmbed(), ...s.data, author: { ...blankEmbed().author, ...(s.data.author || {}) }, footer: { ...blankEmbed().footer, ...(s.data.footer || {}) } } : blankEmbed();
    editingId = s ? s.id : null; if ($('#eb-name')) $('#eb-name').value = s ? s.name : ''; if ($('#eb-del')) $('#eb-del').hidden = !s; draw();
  });
  if (canWrite()) {
    $('#eb-save').addEventListener('click', async () => {
      const name = $('#eb-name').value.trim(); if (!name) return toast('Give the embed a name first', true);
      await run(() => (editingId ? put(`collections/embeds/${editingId}`, { name, data: e }) : post('collections/embeds', { name, data: e })), 'Embed saved'); route();
    });
    $('#eb-del').addEventListener('click', async () => { if (editingId && await confirmDialog('Delete saved embed', 'Delete this saved embed?', 'Delete', 'danger')) { await run(() => del(`collections/embeds/${editingId}`), 'Deleted'); route(); } });
    $('#eb-send').addEventListener('click', async () => { const ch = $('[name=send_channel]').value; if (!ch) return toast('Choose a channel', true); await run(() => post('embeds/send', { channel_id: ch, data: e }), 'Sent to Discord'); });
  }
  draw();
});

/* =========================================================== announcements */
const scheduledCrud = crudPage('scheduled', {
  newLabel: 'Schedule a message', empty: 'Nothing scheduled.',
  intro: 'Send announcements at a set time, once or on repeat. Times use your local time zone.',
  cols: [['Channel', (i) => esc(chName(i.channel_id))], ['Message', (i) => `<span class="truncate" style="display:block">${esc(i.content || (i.embed && i.embed.title) || '(embed)')}</span>`], ['Next send', (i) => fmtTime(i.next_run_at)], ['Repeat', (i) => (i.repeat_hours ? `every ${i.repeat_hours}h` : 'once')], ['Status', (i) => (i.enabled ? pill('Active', 'ok') : pill('Done or paused'))]],
  form: (i) => `<div class="field"><label>Channel</label>${channelSelect('channel_id', i.channel_id, 'Choose channel')}</div>
    <div class="field"><label>Send at</label><input type="datetime-local" name="next_run_at" value="${i.next_run_at ? fromDbToLocalInput(i.next_run_at) : toLocalInput(new Date(Date.now() + 3600e3))}"></div>
    <div class="field"><label>Repeat every (hours, 0 = once)</label><input type="number" name="repeat_hours" min="0" max="8760" value="${i.repeat_hours || 0}"><div class="help">24 = daily, 168 = weekly</div></div>
    <div class="field">${switchHtml('enabled', i.enabled !== 0, 'Active')}</div>
    <div class="field wide"><label>Message text</label><textarea name="content" maxlength="2000">${esc(i.content || '')}</textarea></div>
    <div class="field">${switchHtml('use_embed', !!i.embed, 'Include an embed')}</div><div class="field"><label>Embed color</label><input name="e_color" value="${esc(i.embed && i.embed.color || '#7C3AED')}" maxlength="7"></div>
    <div class="field wide"><label>Embed title</label><input name="e_title" maxlength="256" value="${esc(i.embed && i.embed.title || '')}"></div>
    <div class="field wide"><label>Embed description</label><textarea name="e_description" maxlength="4000">${esc(i.embed && i.embed.description || '')}</textarea></div>`,
  collect: (b) => { const f = readForm(b); return { channel_id: f.channel_id, next_run_at: f.next_run_at ? new Date(f.next_run_at).toISOString() : '', repeat_hours: f.repeat_hours, enabled: f.enabled, content: f.content, use_embed: f.use_embed, embed: { title: f.e_title, description: f.e_description, color: f.e_color || '#7C3AED' } }; },
});
NX.register('announcements', 'Announcements', async (c) => scheduledCrud(c));

/* ============================================================== automation */
function automationEditor(auto, reload) {
  const A = NX.meta.automation;
  const a = auto ? JSON.parse(JSON.stringify(auto)) : { name: '', trigger: 'member_join', conditions: [], actions: [{ type: 'send_message', channel: '', content: 'Welcome {user}!' }], enabled: 1 };
  const valueInput = (cond, i) => {
    const kind = (A.conditions[cond.type] || {}).value;
    if (kind === 'role') return `<select data-c="${i}" data-k="value">${options(roleList(), cond.value, 'Choose role')}</select>`;
    if (kind === 'channel') return `<select data-c="${i}" data-k="value">${options(channelList(), cond.value, 'Choose channel')}</select>`;
    if (kind === 'bool') return `<select data-c="${i}" data-k="value"><option value="true" ${cond.value ? 'selected' : ''}>Yes</option><option value="false" ${!cond.value ? 'selected' : ''}>No</option></select>`;
    return `<input data-c="${i}" data-k="value" ${kind === 'int' ? 'type="number"' : ''} value="${esc(cond.value == null ? '' : cond.value)}">`;
  };
  const actionFields = (act, i) => A.actions[act.type].fields.map((f) => {
    const v = act[f]; const lab = { channel: 'Channel', role: 'Role', content: 'Text', title: 'Title', color: 'Color', amount: 'Amount', panel: 'Ticket panel ID' }[f];
    let inp;
    if (f === 'channel') inp = `<select data-a="${i}" data-k="${f}">${options(channelList(), v, 'Choose channel')}</select>`;
    else if (f === 'role') inp = `<select data-a="${i}" data-k="${f}">${options(roleList(true), v, 'Choose role')}</select>`;
    else if (f === 'content') inp = `<textarea data-a="${i}" data-k="${f}" maxlength="2000">${esc(v || '')}</textarea>`;
    else inp = `<input data-a="${i}" data-k="${f}" ${['amount', 'panel'].includes(f) ? 'type="number"' : ''} value="${esc(v == null ? (f === 'color' ? '#7C3AED' : '') : v)}">`;
    return `<div class="field ${f === 'content' ? 'wide' : ''}"><label class="help">${lab}</label>${inp}</div>`;
  }).join('');
  const body = Dialog.open(auto ? `Edit automation: ${a.name}` : 'New automation', '<div id="au-form"></div>', [
    { label: 'Cancel', cls: 'ghost' },
    { label: 'Save automation', cls: 'primary', onClick: async () => { await run(() => (auto ? put(`automations/${auto.id}`, a) : post('automations', a)), 'Automation saved'); reload(); } },
  ]);
  const draw = () => {
    $('#au-form', body).innerHTML = `<div class="form-grid"><div class="field"><label>Name</label><input id="au-name" maxlength="80" value="${esc(a.name)}" placeholder="e.g. Reward level 10"></div>
      <div class="field"><label>When this happens</label><select id="au-trigger">${options(Object.entries(A.triggers).map(([id, label]) => ({ id, label })), a.trigger)}</select></div>
      <div class="field">${switchHtml('au_enabled', a.enabled, 'Enabled')}</div></div>
      <h3 class="mt">Only if (all must be true)</h3>${a.conditions.map((cnd, i) => `<div class="row-edit" style="display:grid;grid-template-columns:1fr 1fr auto;align-items:end"><div><select data-c="${i}" data-k="type">${options(Object.entries(A.conditions).map(([id, c]) => ({ id, label: c.label })), cnd.type)}</select></div><div>${valueInput(cnd, i)}</div><button class="btn small danger" type="button" data-rmc="${i}">Remove</button></div>`).join('') || '<p class="muted">No conditions: runs every time.</p>'}
      <button class="btn small" type="button" id="au-addc">${icon('plus')} Add condition</button>
      <h3 class="mt" style="margin-top:20px">Do this</h3>${a.actions.map((act, i) => `<div class="row-edit"><div class="inline-flex" style="display:flex"><strong style="white-space:nowrap">${i + 1}.</strong><select data-a="${i}" data-k="type">${options(Object.entries(A.actions).map(([id, x]) => ({ id, label: x.label })), act.type)}</select><button class="btn small danger" type="button" data-rma="${i}">Remove</button></div><div class="form-grid">${actionFields(act, i)}</div></div>`).join('')}
      <button class="btn small" type="button" id="au-adda" ${a.actions.length >= 10 ? 'disabled' : ''}>${icon('plus')} Add action</button>
      <p class="help mt">Text can use {user}, {server}, {role}, {level}, {form}, {channel} and other values from the trigger.</p>`;
    $('#au-name', body).addEventListener('input', (ev) => { a.name = ev.target.value; });
    $('#au-trigger', body).addEventListener('change', (ev) => { a.trigger = ev.target.value; });
    $('#sw-au_enabled', body).addEventListener('change', (ev) => { a.enabled = ev.target.checked; });
    $$('[data-c]', body).forEach((el) => el.addEventListener('change', () => { const cnd = a.conditions[+el.dataset.c]; cnd[el.dataset.k] = el.dataset.k === 'value' && el.value === 'true' ? true : el.dataset.k === 'value' && el.value === 'false' ? false : el.value; if (el.dataset.k === 'type') { cnd.value = ''; draw(); } }));
    $$('[data-a]', body).forEach((el) => el.addEventListener('change', () => { const act = a.actions[+el.dataset.a]; if (el.dataset.k === 'type') { a.actions[+el.dataset.a] = { type: el.value }; draw(); } else act[el.dataset.k] = el.value; }));
    $$('[data-rmc]', body).forEach((b) => b.addEventListener('click', () => { a.conditions.splice(+b.dataset.rmc, 1); draw(); }));
    $$('[data-rma]', body).forEach((b) => b.addEventListener('click', () => { a.actions.splice(+b.dataset.rma, 1); draw(); }));
    $('#au-addc', body).addEventListener('click', () => { a.conditions.push({ type: 'has_role', value: '' }); draw(); });
    $('#au-adda', body).addEventListener('click', () => { a.actions.push({ type: 'send_message' }); draw(); });
  };
  draw();
}
NX.register('automation', 'Automation', async (c) => {
  const d = await get('automations');
  const A = NX.meta.automation;
  c.innerHTML = `<p class="panel-desc">Automations run actions when something happens in your server. Each run is recorded so you can see what happened.</p><div class="toolbar">${writeBtn('id="au-new"', `${icon('plus')} New automation`, 'primary')}</div>
    ${d.automations.length ? d.automations.map((a) => `<section class="panel"><div class="panel-head"><div><h3>${esc(a.name)} ${a.enabled ? pill('On', 'ok') : pill('Off')} ${a.errors_24h ? pill(a.errors_24h + ' errors today', 'bad') : ''}</h3>
      <span class="muted">When ${esc((A.triggers[a.trigger] || a.trigger).toLowerCase())}${a.conditions.length ? `, if ${a.conditions.length} condition(s)` : ''}: ${a.actions.map((x) => esc((A.actions[x.type] || {}).label || x.type).toLowerCase()).join(', then ')}</span>
      <div class="faint">Ran ${fmtNum(a.runs)} times${a.last_run_at ? ', last ' + fmtAgo(a.last_run_at) : ''}</div></div>
      <div class="inline-flex"><button class="btn small" data-runs="${a.id}">History</button>${writeBtn(`data-tog="${a.id}"`, a.enabled ? 'Turn off' : 'Turn on', 'small')}${writeBtn(`data-edit="${a.id}"`, 'Edit', 'small')}${writeBtn(`data-del="${a.id}"`, 'Delete', 'small danger')}</div></div></section>`).join('') : emptyBox('No automations yet. Try: when a member reaches level 10, give them a role and announce it.')}`;
  const reload = () => route();
  const nb = $('#au-new'); if (nb) nb.addEventListener('click', () => automationEditor(null, reload));
  $$('[data-edit]').forEach((b) => b.addEventListener('click', () => automationEditor(d.automations.find((x) => String(x.id) === b.dataset.edit), reload)));
  $$('[data-tog]').forEach((b) => b.addEventListener('click', async () => { await run(() => post(`automations/${b.dataset.tog}/toggle`), 'Updated'); reload(); }));
  $$('[data-del]').forEach((b) => b.addEventListener('click', async () => { if (await confirmDialog('Delete automation', 'Delete this automation and its history?', 'Delete', 'danger')) { await run(() => del(`automations/${b.dataset.del}`), 'Deleted'); reload(); } }));
  $$('[data-runs]').forEach((b) => b.addEventListener('click', async () => {
    const r = await get(`automations/${b.dataset.runs}/runs`);
    Dialog.open('Run history', r.runs.length ? `<table class="data"><tr><th>When</th><th>Result</th><th>Detail</th></tr>${r.runs.map((x) => `<tr><td class="nowrap">${fmtTime(x.created_at)}</td><td>${statusPill(x.status)}</td><td>${esc(x.detail)}</td></tr>`).join('')}</table>` : emptyBox('This automation hasn\'t run yet.'));
  }));
});

/* ================================================================ settings */
NX.register('settings', 'Server Settings', async (c, sub) => tabs(c, [
  { id: 'general', label: 'General', render: (b) => renderSettings(b, 'general') },
  { id: 'backup', label: 'Backup and restore', render: async (b) => {
    b.innerHTML = `<section class="panel"><h3>Export configuration</h3><p class="panel-desc">Downloads every setting, ticket panel, application form, role menu, custom command, automation, shop item and saved embed as a JSON file.</p>
      ${canWrite() ? `<a class="btn primary" href="/api/guilds/${NX.gid}/config/export">${icon('download')} Download backup</a>` : '<p class="muted">Read-only access.</p>'}</section>
      ${canWrite() ? `<section class="panel"><h3>Import configuration</h3><p class="panel-desc">Restore a backup from this or another server. Channel and role settings from another server won't match and will need updating.</p>
      <input type="file" id="imp-file" accept="application/json,.json"><div class="mt">${switchHtml('imp_replace', false, 'Replace existing panels, forms, menus and commands instead of adding to them')}</div><button class="btn primary mt" id="imp-go">Import</button></section>` : ''}`;
    const go = $('#imp-go', b);
    if (go) go.addEventListener('click', async () => {
      const f = $('#imp-file', b).files[0]; if (!f) return toast('Choose a backup file', true);
      let data; try { data = JSON.parse(await f.text()); } catch (e) { return toast('That file isn\'t valid JSON', true); }
      data.replace = $('#sw-imp_replace', b).checked;
      if (!await confirmDialog('Import configuration', 'This overwrites module settings with the backup. Continue?', 'Import', 'danger')) return;
      await run(() => post('config/import', data), (r) => `Imported ${r.modules.length} modules. ${r.note}` + (r.skipped && r.skipped.length ? ` Skipped ${r.skipped.length}: ${r.skipped.slice(0, 3).join('; ')}${r.skipped.length > 3 ? '…' : ''}` : ''));
    });
  } },
  { id: 'audit', label: 'Dashboard audit log', render: async (b) => {
    const d = await get('audit');
    b.innerHTML = `<p class="panel-desc">Every change made through the dashboard, newest first.</p><div class="panel table-scroll">${d.entries.length ? `<table class="data"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th></tr></thead><tbody>${d.entries.map((e) => `<tr><td class="nowrap faint">${fmtTime(e.created_at)}</td><td>${esc(e.user_name)}</td><td>${esc(e.action)}</td><td class="faint">${esc(e.detail || '')}</td></tr>`).join('')}</tbody></table>` : emptyBox('No dashboard changes yet.')}</div>`;
  } },
], sub));
