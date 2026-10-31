// Small enhancements for public pages.
document.addEventListener('DOMContentLoaded', function () {
  var search = document.getElementById('cmd-search');
  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      document.querySelectorAll('[data-group]').forEach(function (group) {
        var visible = 0;
        group.querySelectorAll('[data-cmd]').forEach(function (row) {
          var show = !q || row.dataset.cmd.toLowerCase().indexOf(q) !== -1;
          row.hidden = !show; if (show) visible++;
        });
        group.hidden = visible === 0;
      });
    });
  }
  var status = document.querySelector('[data-status-refresh]');
  if (status) {
    setInterval(function () {
      fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
        document.querySelectorAll('[data-status]').forEach(function (el) {
          var ok = s[el.dataset.status];
          el.className = 'pill ' + (ok ? 'ok' : 'bad');
          el.textContent = ok ? 'Operational' : 'Offline';
        });
        var lat = document.querySelector('[data-latency]');
        if (lat) lat.textContent = s.latency_ms != null ? s.latency_ms + ' ms' : 'n/a';
      }).catch(function () {});
    }, 30000);
  }
  var servers = document.getElementById('server-list');
  if (servers) loadServers(servers, false);
  var refresh = document.getElementById('refresh-servers');
  if (refresh) refresh.addEventListener('click', function () { loadServers(servers, true); });
});

function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

function loadServers(el, refresh) {
  el.innerHTML = '<p class="loading">Loading your servers…</p>';
  fetch('/api/guilds' + (refresh ? '?refresh=1' : ''), { credentials: 'same-origin' })
    .then(function (r) { return r.json().then(function (b) { if (!r.ok) throw new Error(b.error || 'Failed'); return b; }); })
    .then(function (data) {
      if (!data.guilds.length) {
        el.innerHTML = '<div class="empty"><p>You don\'t manage any servers yet. You need the Manage Server permission, or a dashboard role given by an admin.</p></div>';
        return;
      }
      el.innerHTML = '<div class="server-grid">' + data.guilds.map(function (g) {
        var icon = g.icon ? '<img class="icon" src="' + esc(g.icon) + '" alt="">' : '<span class="icon">' + esc(g.name.slice(0, 2)) + '</span>';
        if (!g.bot_in) {
          var href = data.invite_url ? esc(data.invite_url + '&guild_id=' + g.id + '&disable_guild_select=true') : '#';
          return '<a class="server invite" href="' + href + '" target="_blank" rel="noopener">' + icon + '<div><div class="name">' + esc(g.name) + '</div><div class="faint">Add Nexora</div></div></a>';
        }
        return '<a class="server" href="/dashboard/' + g.id + '">' + icon + '<div><div class="name">' + esc(g.name) + '</div><div class="faint">' +
          (g.access === 'read' ? 'Read-only · ' : '') + (g.members || 0).toLocaleString() + ' members</div></div></a>';
      }).join('') + '</div>';
    })
    .catch(function (e) { el.innerHTML = '<p class="notice">' + esc(e.message) + '</p>'; });
}
