/* Read-only demo: answers the dashboard's data calls from snapshot.json, so no server is needed.
   Made-up data. Time stamps are moved forward so "2 h ago" still reads sensibly. */
(function () {
  var real = window.fetch.bind(window), snap = null, loading = null, delta = 0;
  var ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?$/;
  var TIME = { since: 1, until: 1, from: 1, to: 1, as_of: 1 };

  try { if (!localStorage.getItem('aa.agent')) localStorage.setItem('aa.agent', 'support-bot'); } catch (e) { /* storage blocked: the dashboard picks an agent itself */ }

  function load() {
    if (!loading) {
      loading = real('snapshot.json').then(function (r) { return r.json(); }).then(function (s) {
        snap = s; delta = Date.now() - Date.parse(s.generated_at);
      });
    }
    return loading;
  }
  function shift(v) {
    if (typeof v === 'string') {
      if (!ISO.test(v)) return v;
      var t = Date.parse(/(Z|[+-]\d\d:\d\d)$/.test(v) ? v : v + 'Z');
      return isNaN(t) ? v : new Date(t + delta).toISOString();
    }
    if (Array.isArray(v)) return v.map(shift);
    if (v && typeof v === 'object') { var o = {}; for (var k in v) o[k] = shift(v[k]); return o; }
    return v;
  }
  function keyFor(url) {
    var u = new URL(url, location.href), pairs = [];
    u.searchParams.forEach(function (val, name) { if (!TIME[name]) pairs.push([name, val]); });
    pairs.sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0; });
    var q = pairs.map(function (p) { return encodeURIComponent(p[0]).replace(/%20/g, '+') + '=' + encodeURIComponent(p[1]).replace(/%20/g, '+'); }).join('&');
    return u.pathname + (q ? '?' + q : '');
  }
  function reply(body, status) {
    return new Response(JSON.stringify(body), { status: status, headers: { 'Content-Type': 'application/json' } });
  }

  window.fetch = function (input, opts) {
    var url = typeof input === 'string' ? input : input.url;
    if (url.indexOf('/v1/') !== 0) return real(input, opts);
    if (((opts && opts.method) || 'GET').toUpperCase() !== 'GET') {
      return Promise.resolve(reply({ detail: 'This is a read-only demo. To try it for real, follow the quick start on the project site.' }, 405));
    }
    return load().then(function () {
      var k = keyFor(url), hit = snap.responses[k];
      if (hit === undefined) hit = snap.byPath[k.split('?')[0]];
      return hit === undefined ? reply({ detail: 'This view was not recorded in the demo.' }, 404) : reply(shift(hit), 200);
    });
  };

  document.addEventListener('DOMContentLoaded', function () {
    function dropDeadLinks() {  // links that only work on a running server; the dashboard draws them after load
      document.querySelectorAll('a').forEach(function (a) {
        if (/^(Open the chat app|API reference)$/.test(a.textContent.trim())) a.remove();
      });
    }
    dropDeadLinks();
    new MutationObserver(dropDeadLinks).observe(document.body, { childList: true, subtree: true });
    var bar = document.createElement('div');
    bar.setAttribute('role', 'note');
    bar.style.cssText = 'position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:99999;background:#13211c;color:#e6efe9;' +
      'border:1px solid #3a5247;border-radius:8px;padding:8px 12px 8px 14px;font:14px/1.4 system-ui,sans-serif;max-width:92vw;display:flex;gap:12px;align-items:center;box-shadow:0 4px 16px rgba(0,0,0,.3)';
    bar.innerHTML = '<span>Read-only demo with made-up data. <a href="../#start" style="color:#9fd2ba">Run it yourself</a> &middot; <a href="../" style="color:#9fd2ba">Back to site</a></span>';
    var x = document.createElement('button');
    x.type = 'button'; x.setAttribute('aria-label', 'Hide this note'); x.textContent = '\u00d7';
    x.style.cssText = 'background:none;border:0;color:#e6efe9;font-size:20px;line-height:1;cursor:pointer;padding:0 4px';
    x.onclick = function () { bar.remove(); };
    bar.appendChild(x);
    document.body.appendChild(bar);
  });
})();
