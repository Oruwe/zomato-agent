/* Meal Agent dashboard.
 *
 * No framework and no build step: this ships inside the same container as the API and
 * runs under a strict CSP (no inline script, no remote origins).
 *
 * Security note: restaurant names, dish names and calendar titles are attacker-
 * controlled -- a merchant can put markup in a dish description, and anyone who can
 * email the user can write a calendar title. Every interpolation goes through esc().
 * Never introduce an unescaped template hole in this file.
 */
'use strict';

const HEADERS = { 'Content-Type': 'application/json', 'X-Requested-With': 'zomato-agent' };
const REFRESH_MS = 20000;

let STATE = null;
let busy = false;

/* ---------- helpers ---------- */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function rupees(n) {
  const v = Number(n || 0);
  return '₹' + v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function rupeesShort(n) {
  const v = Math.round(Number(n || 0));
  return '₹' + v.toLocaleString('en-IN');
}

/* The API sends ISO timestamps carrying the user's own offset (+05:30 for Bengaluru).
 * Formatting with the *browser's* timezone would show 09:00 IST as 03:30 for anyone
 * whose machine is not on IST -- including every server-side render and CI screenshot.
 * So shift by the offset in the string itself and format in UTC. */
function clockTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const m = /([+-])(\d{2}):(\d{2})$/.exec(iso);
  let shifted = d;
  if (m) {
    const sign = m[1] === '-' ? -1 : 1;
    const offsetMin = sign * (Number(m[2]) * 60 + Number(m[3]));
    shifted = new Date(d.getTime() + offsetMin * 60000);
  } else if (/Z$/.test(iso)) {
    shifted = d;
  }
  return shifted.toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', hour12: true, timeZone: 'UTC',
  });
}

function relativeDay(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return 'today ' + clockTime(iso);
  if (days === 1) return 'yesterday';
  if (days < 7) return days + ' days ago';
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

let toastTimer = null;
function toast(message, bad) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3600);
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({ headers: HEADERS }, options || {}));
  if (res.status === 401) { showLogin(); throw new Error('not authenticated'); }
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty body is fine */ }
  if (!res.ok) throw new Error((body && body.detail) || ('request failed: ' + res.status));
  return body;
}

/* ---------- auth ---------- */

function showLogin() {
  $('#loginWrap').hidden = false;
  $('#app').hidden = true;
}

function showApp() {
  $('#loginWrap').hidden = true;
  $('#app').hidden = false;
}

$('#loginForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = $('#loginBtn');
  btn.disabled = true;
  $('#loginErr').textContent = '';
  try {
    await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ password: $('#password').value }),
    });
    showApp();
    await refresh();
  } catch (err) {
    $('#loginErr').textContent = 'Incorrect password.';
  } finally {
    btn.disabled = false;
  }
});

/* ---------- navigation ---------- */

function activate(view) {
  $$('nav.tabs button').forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.view === view)));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + view));
  if (location.hash.slice(1) !== view) history.replaceState(null, '', '#' + view);
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
}

$$('nav.tabs button').forEach((btn) =>
  btn.addEventListener('click', () => activate(btn.dataset.view)));

/* ---------- render: header ---------- */

function renderHeader(s) {
  const w = s.wallet;
  $('#budgetPill').textContent = rupeesShort(w.daily_remaining_rupees) + ' left today';

  const mode = $('#modePill');
  if (s.config.live_money_enabled) {
    mode.className = 'pill live';
    mode.textContent = 'LIVE MONEY';
  } else if (s.config.dry_run) {
    mode.className = 'pill dry';
    mode.textContent = 'Dry run';
  } else {
    mode.className = 'pill';
    mode.textContent = 'Approval only';
  }
}

/* ---------- render: today ---------- */

function renderToday(s) {
  const gap = s.schedule.next_gap;
  if (gap) {
    $('#nextSlot').textContent = gap.slot;
    $('#nextWhen').textContent =
      `Free from ${clockTime(gap.start)} · ${gap.minutes} minutes`;
  } else {
    $('#nextSlot').textContent = 'No meal window';
    $('#nextWhen').textContent = 'Your schedule has no free meal slot left today.';
  }

  // Timeline: events and gaps merged and ordered by start time.
  const rows = [];
  s.schedule.events.forEach((e) => rows.push({
    at: e.start, kind: 'event', title: e.summary,
    meta: `${clockTime(e.start)} – ${clockTime(e.end)}`,
    risky: e.risk_score > 0, reasons: e.risk_reasons,
  }));
  s.schedule.gaps.forEach((g) => rows.push({
    at: g.start, kind: 'gap', title: g.slot,
    meta: `${g.minutes} minutes free`,
  }));
  rows.sort((a, b) => new Date(a.at) - new Date(b.at));

  const host = $('#timeline');
  if (!rows.length) {
    host.innerHTML = '<div class="empty"><div class="big">🗓️</div>Nothing scheduled today.</div>';
    return;
  }
  host.innerHTML = rows.map((r) => {
    const cls = r.kind === 'gap' ? 'gap' : (r.risky ? 'risky' : '');
    const warn = r.risky
      ? `<div class="meta" style="color:var(--danger)">⚠ manipulation attempt in this invite — ignored</div>`
      : '';
    const title = r.kind === 'gap'
      ? `<span style="text-transform:capitalize">${esc(r.title)}</span> window`
      : esc(r.title);
    return `<div class="slotrow ${cls}">
        <div class="time">${esc(clockTime(r.at))}</div>
        <div class="what">
          <div class="title">${title}</div>
          <div class="meta">${esc(r.meta)}</div>${warn}
        </div>
      </div>`;
  }).join('');
}

function stateTag(state) {
  const map = {
    order_placed: ['placed', 'Ordered'],
    simulated: ['simulated', 'Simulated'],
    awaiting_approval: ['awaiting', 'Needs you'],
    rejected: ['rejected', 'Declined'],
    failed: ['failed', 'Failed'],
  };
  const [cls, label] = map[state] || ['rejected', state];
  return `<span class="tag ${cls}">${esc(label)}</span>`;
}

function renderLastRun(run) {
  if (!run) { $('#lastRunHost').innerHTML = ''; return; }
  const dishes = (run.dishes || []).map(esc).join(', ');
  const inj = (run.injection_events || []).length;
  $('#lastRunHost').innerHTML = `
    <div class="item" style="margin-top:14px;border-top:1px solid var(--border);border-bottom:0;padding-top:14px">
      <div class="grow">
        <div class="title">${esc(run.restaurant || 'No restaurant chosen')}</div>
        <div class="meta">${dishes || esc(run.escalation_reason ? describeEscalation(run.escalation_reason) : (run.error || '—'))}</div>
        ${inj ? `<div class="meta" style="color:var(--danger)">${inj} manipulation attempt${inj > 1 ? 's' : ''} blocked</div>` : ''}
      </div>
      <div style="text-align:right">
        <div class="amt money">${rupees(run.amount_rupees)}</div>
        <div style="margin-top:4px">${stateTag(run.state)}</div>
      </div>
    </div>`;
}

/* Policy reasons arrive as machine codes like
 * `checkout:above_human_approval_threshold:70700>15000`. Those are the right shape for
 * logs and the audit trail, and the wrong thing to show someone deciding whether to
 * spend money. Translate to a sentence, falling back to the raw code so a newly added
 * rule is never silently unexplained. */
function describeEscalation(reason) {
  const raw = String(reason || '').trim();
  if (!raw) return 'Needs your confirmation.';

  let m = /above_human_approval_threshold:(\d+)>(\d+)/.exec(raw);
  if (m) {
    return `${rupees(Number(m[1]) / 100)} is over your ${rupeesShort(Number(m[2]) / 100)} auto-approve limit.`;
  }
  m = /over_per_order_cap:(\d+)>(\d+)/.exec(raw);
  if (m) {
    return `${rupees(Number(m[1]) / 100)} is over your ${rupeesShort(Number(m[2]) / 100)} per-order limit.`;
  }
  if (/autonomous_checkout_disabled/.test(raw)) {
    return 'Unattended ordering is switched off, so this one is up to you.';
  }
  m = /outside_order_window:([\d:]+)/.exec(raw);
  if (m) return `It is ${m[1]}, outside the hours you allow ordering.`;
  if (/wallet_daily_cap_exceeded/.test(raw)) return "This would go over today's budget.";
  if (/wallet_monthly_cap_exceeded/.test(raw)) return "This would go over this month's budget.";
  if (/restaurant_not_in_allowlist/.test(raw)) return 'This restaurant is not on your approved list.';
  if (/^dry-run/i.test(raw)) return 'Dry run — nothing was ordered.';
  return raw;
}

/* ---------- render: approvals ---------- */

function renderApprovals(s) {
  const host = $('#approvalsHost');
  const pending = s.pending_approvals || [];
  if (!pending.length) { host.innerHTML = ''; return; }

  host.innerHTML = pending.map((r) => `
    <div class="approval">
      <h2 style="margin:0 0 2px;font-size:15px">Approve this order?</h2>
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-top:9px">
        <div>
          <div style="font-weight:600">${esc(r.restaurant || 'Unknown restaurant')}</div>
          <div class="meta" style="font-size:13px;color:var(--muted)">${esc((r.dishes || []).join(', '))}</div>
        </div>
        <div class="amt money" style="font-size:19px;font-weight:680">${rupees(r.amount_rupees)}</div>
      </div>
      <div class="why">${esc(describeEscalation(r.escalation_reason))}</div>
      <div class="actions">
        <button class="btn ok sm" data-approve="${esc(r.run_id)}">Approve &amp; order</button>
        <button class="btn danger sm" data-reject="${esc(r.run_id)}">Decline</button>
      </div>
    </div>`).join('');

  host.querySelectorAll('[data-approve]').forEach((b) =>
    b.addEventListener('click', () => decide(b.dataset.approve, 'approve', b)));
  host.querySelectorAll('[data-reject]').forEach((b) =>
    b.addEventListener('click', () => decide(b.dataset.reject, 'reject', b)));
}

async function decide(runId, action, btn) {
  if (busy) return;
  busy = true;
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>';
  try {
    await api(`/api/approvals/${encodeURIComponent(runId)}/${action}`, {
      method: 'POST', body: JSON.stringify({ reason: 'via dashboard' }),
    });
    toast(action === 'approve' ? 'Order placed.' : 'Order declined.');
    await refresh();
  } catch (err) {
    toast(err.message, true);
    btn.disabled = false;
    btn.textContent = original;
  } finally {
    busy = false;
  }
}

/* ---------- render: orders ---------- */

function renderOrders(s) {
  const runs = s.recent_runs || [];
  const host = $('#ordersHost');
  if (!runs.length) {
    host.innerHTML = '<div class="empty"><div class="big">🍽️</div>No orders yet. Run the agent from the Today tab.</div>';
    return;
  }
  host.innerHTML = runs.map((r) => {
    const steps = (r.steps || []).map((st) => `
      <div class="step">
        <span class="${st.ok ? 'ok' : 'bad'}">${st.ok ? '✓' : '✕'}</span>
        <span class="name">${esc(st.step)}</span>
        <span>${st.elapsed_us ? Math.round(st.elapsed_us) + 'µs' : ''}</span>
      </div>`).join('');
    return `<div class="item">
        <div class="grow">
          <div class="title">${esc(r.restaurant || '—')}</div>
          <div class="meta">${esc(relativeDay(r.created_at))} · ${esc(r.slot || '—')} · ${esc((r.dishes || []).join(', ') || 'no items')}</div>
          ${steps ? `<details class="audit"><summary>Why this happened (${(r.steps || []).length} steps)</summary>${steps}</details>` : ''}
        </div>
        <div style="text-align:right">
          <div class="amt money">${rupees(r.amount_rupees)}</div>
          <div style="margin-top:4px">${stateTag(r.state)}</div>
        </div>
      </div>`;
  }).join('');
}

/* ---------- render: wallet ---------- */

function meter(label, used, cap) {
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  const cls = pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : '';
  return `<div class="meter">
      <div class="row">
        <span class="label">${esc(label)}</span>
        <span class="val money">${rupeesShort(used)} <span style="color:var(--faint);font-weight:500">of ${rupeesShort(cap)}</span></span>
      </div>
      <div class="track"><div class="fill ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
    </div>`;
}

function renderWallet(s) {
  const w = s.wallet;
  $('#metersHost').innerHTML =
    meter('Spent today', w.day_spent_rupees, w.daily_cap_rupees) +
    meter('Spent this month', w.month_spent_rupees, w.monthly_cap_rupees) +
    `<div class="meter"><div class="row">
        <span class="label">Maximum per single order</span>
        <span class="val money">${rupeesShort(w.per_order_cap_rupees)}</span>
      </div></div>`;

  const st = s.stats;
  $('#walletKv').innerHTML = [
    ['Orders placed', st.orders_placed],
    ['Simulated (dry run)', st.simulated],
    ['Awaiting your approval', st.awaiting_approval],
    ['Declined by policy', st.rejected],
    ['Failed', st.failed],
    ['Total spent', rupees((st.total_spent_paise || 0) / 100)],
    ['Payment rail', s.config.payment_rail],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd class="num">${esc(v)}</dd>`).join('');
}

/* ---------- render: security ---------- */

function renderSecurity(s, sec) {
  const g = sec.guards || {};
  const guards = [
    ['Dry run (never spends)', g.dry_run, g.dry_run ? 'on' : 'off'],
    ['Unattended checkout', !g.allow_autonomous_checkout, g.allow_autonomous_checkout ? 'allowed' : 'blocked'],
    ['Dashboard password', g.auth_enabled, g.auth_enabled ? 'set' : 'not set'],
  ];
  $('#guardsHost').innerHTML = guards.map(([name, good, label]) => `
      <div class="guard">
        <span class="grow"><span class="name">${esc(name)}</span></span>
        <span class="state ${good ? 'on' : 'off'}">${esc(label)}</span>
      </div>`).join('') + `
      <div class="guard">
        <span class="grow"><span class="name">Per-order limit</span></span>
        <span class="state on money">${rupeesShort(g.per_order_cap_rupees)}</span>
      </div>
      <div class="guard">
        <span class="grow"><span class="name">Asks you above</span></span>
        <span class="state on money">${rupeesShort(g.human_approval_above_rupees)}</span>
      </div>`;

  const llm = sec.llm;
  if (!llm || !llm.key_count) {
    $('#llmHost').innerHTML =
      '<div class="empty">No Gemini key configured. The agent is using its built-in deterministic planner — it still works, with simpler choices.</div>';
  } else {
    $('#llmHost').innerHTML = `
      <dl class="kv">
        <dt>Keys healthy</dt><dd class="num">${esc(llm.available_keys)} of ${esc(llm.key_count)}</dd>
        <dt>Model chain</dt><dd>${esc((llm.models || []).join(' → '))}</dd>
      </dl>` +
      (llm.keys || []).map((k) => `
        <div class="guard">
          <span class="grow"><span class="name">${esc(k.fingerprint)}</span></span>
          <span class="state ${k.disabled ? 'off' : k.cooling_down ? 'off' : 'on'}">
            ${k.disabled ? 'disabled' : k.cooling_down ? 'cooling down' : 'ready'}
          </span>
        </div>`).join('');
  }

  // The same hostile restaurant or invite is re-detected on every run. Showing one row
  // per occurrence buries the signal, so collapse by source and count the sightings.
  const grouped = new Map();
  (sec.injection_events || []).forEach((t) => {
    const key = t.source + '|' + (t.reasons || []).join(',');
    const hit = grouped.get(key);
    if (hit) { hit.count += 1; if (t.created_at > hit.created_at) hit.created_at = t.created_at; }
    else grouped.set(key, Object.assign({ count: 1 }, t));
  });
  const threats = Array.from(grouped.values())
    .sort((a, b) => (b.score - a.score) || (b.count - a.count));

  $('#threatsHost').innerHTML = threats.length
    ? threats.map((t) => `
        <div class="threat">
          <div class="src">
            ${esc(threatSource(t.source))} · severity ${esc(t.score)}
            ${t.count > 1 ? `<span style="font-weight:500;color:var(--muted)"> · seen ${esc(t.count)}×</span>` : ''}
          </div>
          <div style="font-size:13px;margin-top:5px">${esc(describeReasons(t.reasons))}</div>
          <details class="audit"><summary>Rules matched</summary>
            <div class="reasons">${esc((t.reasons || []).join(' · '))}</div>
          </details>
        </div>`).join('')
    : '<div class="empty"><div class="big">🛡️</div>No manipulation attempts recorded yet.</div>';
}

/* ---------- render: taste ---------- */

/* Map internal rule ids to plain language. Unknown ids fall through unchanged rather
 * than being hidden, so a newly added rule is never silently unexplained. */
const RULE_LABELS = {
  instruction_override: 'tried to override the agent\'s instructions',
  role_hijack: 'tried to impersonate the system',
  delimiter_forgery: 'tried to break out of its data boundary',
  payment_manipulation: 'tried to change spending limits or payment details',
  upi_vpa_redirect: 'tried to redirect payment to another UPI address',
  tool_injection: 'tried to trigger checkout directly',
  exfiltration: 'tried to extract secrets or instructions',
  urgency_social_engineering: 'tried to pressure the agent into skipping confirmation',
};

function describeReasons(reasons) {
  const labels = new Set();
  (reasons || []).forEach((r) => {
    const rule = String(r).split(':').pop();
    labels.add(RULE_LABELS[rule] || rule.replace(/_/g, ' '));
  });
  const list = Array.from(labels);
  if (!list.length) return 'Suspicious content.';
  const text = list.join('; ');
  return text.charAt(0).toUpperCase() + text.slice(1) + '.';
}

function threatSource(source) {
  const s = String(source || '');
  if (s.startsWith('zomato:')) return 'Restaurant listing #' + s.split(':')[1];
  if (s === 'calendar') return 'Calendar invite';
  return s;
}

function chips(values, hard) {
  if (!values || !values.length) {
    return '<span class="chip empty">none set</span>';
  }
  return values.map((v) =>
    `<span class="chip ${hard ? 'hard' : ''}">${esc(v)}</span>`).join('');
}

function renderTaste(s) {
  const p = s.preferences;
  $('#dietaryChips').innerHTML = chips(p.dietary_constraints, true);
  $('#dislikeChips').innerHTML = chips(p.disliked, false);
  $('#likeChips').innerHTML = chips((p.top_cuisines || []).map((c) => c[0]), false);

  $('#learnedKv').innerHTML = [
    ['Orders on record', p.order_count],
    ['Typical spend', rupees(p.typical_spend_rupees)],
    ['Favourite dishes', (p.top_dishes || []).map((d) => d[0]).join(', ') || '—'],
    ['Preferred places', (p.top_restaurants || []).map((r) => r[0]).join(', ') || '—'],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
}

$$('[data-add]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const field = btn.dataset.add;
    const input = { dietary: '#dietaryInput', likes: '#likeInput', dislikes: '#dislikeInput' }[field];
    const el = $(input);
    const value = el.value.trim();
    if (!value) return;
    const payload = { likes: [], dislikes: [], dietary: [] };
    payload[field] = value.split(',').map((v) => v.trim()).filter(Boolean);
    btn.disabled = true;
    try {
      await api('/api/memory/preference', { method: 'POST', body: JSON.stringify(payload) });
      el.value = '';
      toast(field === 'dietary' ? 'Constraint saved — it applies to the next order.' : 'Saved.');
      await refresh();
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });
});

$$('.inputrow input').forEach((input) => {
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      input.closest('.inputrow').querySelector('[data-add]').click();
    }
  });
});

/* ---------- run the agent ---------- */

async function placeOrder(force) {
  if (busy) return;
  busy = true;
  const btn = $('#orderBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Thinking';
  try {
    const run = await api('/api/run', {
      method: 'POST', body: JSON.stringify({ force: !!force }),
    });
    const messages = {
      order_placed: `Ordered from ${run.restaurant}.`,
      simulated: `Dry run: would order ${rupees(run.amount_rupees)} from ${run.restaurant}.`,
      awaiting_approval: 'Needs your approval — see the card above.',
      rejected: run.escalation_reason || 'Nothing suitable found.',
      failed: run.error || 'The run failed.',
    };
    const bad = run.state === 'failed' || run.state === 'rejected';
    toast(messages[run.state] || run.state, bad);
    renderLastRun(run);
    // Already ordered this meal: offer a deliberate repeat rather than a dead end.
    if (run.state === 'rejected' && /already ordered/i.test(run.escalation_reason || '')) {
      showRepeatOffer(run.escalation_reason);
    }
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy = false;
    btn.disabled = false;
    btn.textContent = 'Order now';
  }
}

function showRepeatOffer(reason) {
  const host = $('#lastRunHost');
  const box = document.createElement('div');
  box.className = 'approval';
  box.style.marginTop = '14px';
  box.innerHTML = `
    <div style="font-weight:600">Already ordered</div>
    <div class="why">${esc(reason)}</div>
    <div class="actions"><button class="btn sm" id="forceBtn">Order it again anyway</button></div>`;
  host.appendChild(box);
  box.querySelector('#forceBtn').addEventListener('click', () => {
    box.remove();
    placeOrder(true);
  });
}

$('#orderBtn').addEventListener('click', () => placeOrder(false));

/* ---------- refresh loop ---------- */

async function refresh() {
  const [state, security] = await Promise.all([
    api('/api/state'),
    api('/api/security').catch(() => ({})),
  ]);
  STATE = state;

  renderHeader(state);
  renderToday(state);
  renderApprovals(state);
  renderOrders(state);
  renderWallet(state);
  renderSecurity(state, security);
  renderTaste(state);

  const pendingCount = (state.pending_approvals || []).length;
  const tab = document.querySelector('nav.tabs button[data-view="today"]');
  tab.innerHTML = 'Today' + (pendingCount ? ` <span class="badge">${pendingCount}</span>` : '');
}

async function boot() {
  try {
    const session = await api('/api/session');
    if (session.auth_required && !session.authenticated) { showLogin(); return; }
  } catch (_) {
    showLogin();
    return;
  }
  showApp();
  const initial = location.hash.slice(1);
  if (initial && document.getElementById('view-' + initial)) activate(initial);
  try {
    await refresh();
  } catch (err) {
    toast('Could not reach the agent: ' + err.message, true);
  }
  // Poll so approvals raised by a scheduled run appear without a manual reload.
  setInterval(() => { if (!busy && !document.hidden) refresh().catch(() => {}); }, REFRESH_MS);
}

boot();
