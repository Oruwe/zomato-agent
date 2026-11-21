/* Meal Agent dashboard.
 *
 * No framework and no build step: this ships inside the same container as the API and
 * runs under a strict CSP (no inline script, no remote origins).
 *
 * Security note: restaurant names, dish names and calendar titles are attacker-
 * controlled -- a merchant can put markup in a dish description, and anyone who can
 * email the user can write a calendar title. Every interpolation goes through esc().
 * Never introduce an unescaped template hole in this file. Helpers that return markup
 * follow the same rule even though the innerHTML audit in tests/test_api.py only walks
 * assignments: numbers are formatted into locals first and escaped like anything else.
 */
'use strict';

const HEADERS = { 'Content-Type': 'application/json', 'X-Requested-With': 'zomato-agent' };
const REFRESH_MS = 20000;
const IST_OFFSET_MIN = 330;
const VIEWS = ['today', 'orders', 'wallet', 'security', 'taste', 'privacy'];

let STATE = null;
let SECURITY = {};
let RUNS = [];
let LATENCY = null;
let PRIVACY = null;
let busy = false;
let orderFilter = 'all';
let lastSyncAt = 0;
let repeatOffer = null;   // { runId, reason } while an "order again anyway" offer is open

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

/* Wall-clock arithmetic in the user's zone, not the browser's. The schedule carries the
 * offset; everything else is derived from it so a laptop on UTC still sees Bengaluru. */
function offsetOf(iso) {
  const m = /([+-])(\d{2}):(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  return (m[1] === '-' ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3]));
}

function userOffset(s) {
  const ev = (s && s.schedule && s.schedule.events) || [];
  for (const e of ev) {
    const off = offsetOf(e.start);
    if (off !== null) return off;
  }
  return IST_OFFSET_MIN;
}

function minutesOfDay(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(String(iso || ''));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function hhmmMinutes(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm || ''));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function localNow(off) { return new Date(Date.now() + off * 60000); }
function nowMinutes(off) { const d = localNow(off); return d.getUTCHours() * 60 + d.getUTCMinutes(); }
function localDate(off) { return localNow(off).toISOString().slice(0, 10); }

function fmtMinutes(total) {
  const t = ((Math.round(total) % 1440) + 1440) % 1440;
  const h = Math.floor(t / 60), m = t % 60;
  const h12 = ((h + 11) % 12) + 1;
  return String(h12).padStart(2, '0') + ':' + String(m).padStart(2, '0') + (h < 12 ? ' am' : ' pm');
}

function hourLabel(total) {
  const h = Math.floor(total / 60) % 24;
  const h12 = ((h + 11) % 12) + 1;
  return h12 + (h < 12 ? ' am' : ' pm');
}

function duration(mins) {
  const m = Math.max(0, Math.round(mins));
  if (m < 60) return m + ' min';
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h}h ${r}m` : `${h}h`;
}

function micros(us) {
  const v = Number(us || 0);
  if (v < 1) return v.toFixed(2) + 'µs';
  if (v < 1000) return Math.round(v) + 'µs';
  if (v < 1e6) return (v / 1000).toFixed(1) + 'ms';
  return (v / 1e6).toFixed(2) + 's';
}

function pctOf(used, cap) { return cap > 0 ? Math.min(100, (used / cap) * 100) : 0; }
function fillClass(pct) { return pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : ''; }
function plural(n, one, many) { return n === 1 ? one : (many || one + 's'); }
function cap1(text) { const t = String(text || ''); return t.charAt(0).toUpperCase() + t.slice(1); }

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
  if (!res.ok) {
    const detail = body && body.detail;
    const msg = typeof detail === 'string' ? detail
      : (Array.isArray(detail) && detail[0] && detail[0].msg) || ('request failed: ' + res.status);
    throw new Error(msg);
  }
  return body;
}

/* ---------- tooltip ----------
 * One floating readout for every [data-tip] mark: day-strip blocks, chart columns,
 * latency bars. Content goes in with textContent, so a hostile calendar title that
 * reached data-tip (escaped on the way in) cannot become markup on the way out. */

function showTip(target, x, y) {
  const tip = $('#tooltip');
  const raw = target.dataset.tip || '';
  const [head, ...rest] = raw.split('\n');
  tip.replaceChildren();
  const strong = document.createElement('strong');
  strong.textContent = head;
  tip.appendChild(strong);
  if (rest.length) tip.appendChild(document.createTextNode(rest.join(' · ')));
  tip.hidden = false;
  const pad = 12;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let left = x + pad, top = y - h - pad;
  if (left + w > window.innerWidth - 8) left = x - w - pad;
  if (top < 8) top = y + pad;
  tip.style.left = Math.max(8, left) + 'px';
  tip.style.top = top + 'px';
}

function hideTip() { $('#tooltip').hidden = true; }

document.addEventListener('pointermove', (ev) => {
  const t = ev.target.closest && ev.target.closest('[data-tip]');
  if (t) showTip(t, ev.clientX, ev.clientY); else hideTip();
});
document.addEventListener('focusin', (ev) => {
  const t = ev.target.closest && ev.target.closest('[data-tip]');
  if (!t) return;
  const r = t.getBoundingClientRect();
  showTip(t, r.left + r.width / 2, r.top);
});
document.addEventListener('focusout', hideTip);
window.addEventListener('scroll', hideTip, { passive: true });

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
    $('#logoutBtn').hidden = false;
    await refresh();
  } catch (err) {
    $('#loginErr').textContent = 'Incorrect password.';
  } finally {
    btn.disabled = false;
  }
});

$('#logoutBtn').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (_) { /* gone anyway */ }
  $('#password').value = '';
  showLogin();
});

/* ---------- navigation ---------- */

function activate(view) {
  if (!VIEWS.includes(view)) view = 'today';
  $$('nav.tabs button').forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.view === view)));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + view));
  if (location.hash.slice(1) !== view) history.replaceState(null, '', '#' + view);
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
  if (view === 'privacy') loadPrivacy();
}

$$('nav.tabs button').forEach((btn) =>
  btn.addEventListener('click', () => activate(btn.dataset.view)));

// Stat tiles link to the view that explains them; the back button should work too.
window.addEventListener('hashchange', () => activate(location.hash.slice(1)));

// 1-6 switch views, r refreshes. Never while typing.
document.addEventListener('keydown', (ev) => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const tag = (ev.target && ev.target.tagName) || '';
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || $('#app').hidden) return;
  const n = Number(ev.key);
  if (n >= 1 && n <= VIEWS.length) { activate(VIEWS[n - 1]); return; }
  if (ev.key === 'r') refresh().catch(() => {});
});

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

function renderSync(state) {
  const dot = $('#syncDot');
  dot.className = 'sync-dot' + (state === 'busy' ? ' busy' : state === 'stale' ? ' stale' : '');
  if (state === 'stale') {
    $('#syncLabel').textContent = 'Offline';
    $('#syncBtn').title = 'Could not reach the agent. Click to retry.';
    return;
  }
  if (lastSyncAt) {
    const off = STATE ? userOffset(STATE) : IST_OFFSET_MIN;
    $('#syncLabel').textContent = 'Updated ' + fmtMinutes(nowMinutes(off));
    $('#syncBtn').title = 'Refreshes every 20 seconds. Click to refresh now.';
  }
}

$('#syncBtn').addEventListener('click', () => refresh().catch(() => {}));

/* ---------- render: stat tiles ---------- */

/* label · value · note, optionally a thin meter. Every field is escaped here, so
 * callers can pass API-derived strings without thinking about it. */
function kpiTile(t) {
  const tag = t.href ? 'a' : 'div';
  const href = t.href ? ` href="${esc(t.href)}"` : '';
  const est = t.estimate ? ' <span class="est">estimate</span>' : '';
  const unit = t.unit ? ` <small>${esc(t.unit)}</small>` : '';
  const noteCls = t.tone ? ' ' + esc(t.tone) : '';
  let mini = '';
  if (typeof t.meter === 'number') {
    const width = esc(t.meter.toFixed(1));
    const cls = esc(fillClass(t.meter));
    mini = `<div class="mini" aria-hidden="true"><div class="${cls}" style="width:${width}%"></div></div>`;
  }
  return `<${tag} class="kpi"${href}>
      <span class="label">${esc(t.label)}${est}</span>
      <span class="value">${esc(t.value)}${unit}</span>
      <span class="note${noteCls}">${esc(t.note || '')}</span>
      ${mini}
    </${tag}>`;
}

function runsOrRecent(s) {
  return RUNS.length ? RUNS : ((s && s.recent_runs) || []);
}

function placedOn(runs, day) {
  return runs.filter((r) => r.state === 'order_placed' && String(r.created_at || '').slice(0, 10) === day);
}

const ZM_SOURCE = {
  declared: 'as you declared it',
  spent: 'after orders it paid for',
  corrected: 'corrected after a miss',
};

function renderKpis(s) {
  const w = s.wallet;
  const zm = s.zomato_money || {};
  const today = (s.plans && s.plans.date) || localDate(userOffset(s));
  const todays = placedOn(runsOrRecent(s), today);
  const spentToday = todays.reduce((a, r) => a + Number(r.amount_rupees || 0), 0);
  const blocked = Number((s.stats && s.stats.injections_blocked) || 0);

  const tiles = [
    kpiTile({
      label: 'Left today', value: rupeesShort(w.daily_remaining_rupees),
      note: 'of ' + rupeesShort(w.daily_cap_rupees) + ' daily cap',
      meter: pctOf(w.day_spent_rupees, w.daily_cap_rupees), href: '#wallet',
    }),
    kpiTile({
      label: 'Zomato Money', estimate: !!zm.known,
      value: zm.known ? rupeesShort(zm.inr) : 'Not set',
      note: zm.known ? (ZM_SOURCE[zm.source] || 'estimated') : 'Declare it to pay hands-free',
      tone: zm.known ? '' : 'bad', href: '#wallet',
    }),
    kpiTile({
      label: 'Ordered today', value: String(todays.length),
      unit: plural(todays.length, 'order'),
      note: todays.length ? rupees(spentToday) + ' spent' : 'Nothing yet',
      href: '#orders',
    }),
    kpiTile({
      label: 'Manipulation blocked', value: String(blocked),
      unit: plural(blocked, 'attempt'),
      note: blocked ? 'None got through to money' : 'Nothing suspicious seen',
      tone: 'good', href: '#security',
    }),
  ].join('');
  $('#kpiHost').innerHTML = tiles;
}

/* ---------- render: today ---------- */

function pendingPlan(s) {
  return ((s.plans && s.plans.items) || [])
    .filter((p) => p.status === 'pending')
    .sort((a, b) => a.deliver_by.localeCompare(b.deliver_by))[0];
}

function renderToday(s) {
  // A stated plan outranks an inferred window: show what they actually asked for.
  const planned = pendingPlan(s);
  if (planned) {
    $('#nextEyebrow').textContent = 'You planned';
    $('#nextSlot').textContent = planned.slot;
    $('#nextWhen').textContent =
      `You want it by ${planned.deliver_by}`
      + (planned.request ? ` · ${planned.request}` : '');
  } else {
    const gap = s.schedule.next_gap;
    $('#nextEyebrow').textContent = 'Next meal window';
    if (gap) {
      $('#nextSlot').textContent = gap.slot;
      $('#nextWhen').textContent =
        `Free from ${clockTime(gap.start)} · ${gap.minutes} minutes`;
    } else {
      $('#nextSlot').textContent = 'No meal window';
      $('#nextWhen').textContent = 'Your schedule has no free meal slot left today.';
    }
  }

  renderNextMeta(s, planned);
  renderDayStrip(s);
  renderTimeline(s);
  renderPayPreview(s);
}

/* The settlement chip says, before anything happens, whether this order will need
 * the user. It reads the server's own ladder decision rather than guessing here. */
function settlementChip(pv) {
  if (!pv || !pv.wire_type) return ['bad', 'No payment method is permitted'];
  if (pv.expect_wallet) return ['good', 'Zomato Money · nothing to approve'];
  if (pv.wire_type === 'cash_on_delivery') return ['', 'Cash on delivery · pay the rider'];
  return ['warn', 'UPI request · one tap from you'];
}

function renderNextMeta(s, planned) {
  const off = userOffset(s);
  const now = nowMinutes(off);
  const chips = [];

  if (planned) {
    const due = hhmmMinutes(planned.deliver_by);
    if (due !== null) {
      const left = due - now;
      chips.push(left > 0
        ? ['good', `Arrives by ${fmtMinutes(due)} · ${duration(left)} from now`]
        : ['bad', `The ${fmtMinutes(due)} deadline has passed`]);
    }
  } else if (s.schedule.next_gap) {
    const g = s.schedule.gaps.find((x) => x.start === s.schedule.next_gap.start);
    const end = g ? minutesOfDay(g.end) : null;
    if (end !== null) {
      chips.push(end > now
        ? ['', `Window closes ${fmtMinutes(end)}`]
        : ['bad', 'Window already closed']);
    }
  }

  chips.push(settlementChip(s.settlement_preview));
  if (s.config.dry_run) chips.push(['', 'Dry run · nothing will be bought']);

  const html = chips.map(([tone, text]) =>
    `<span class="chipline ${esc(tone)}">${esc(text)}</span>`).join('');
  const hint = planned
    ? '<p class="sub" style="margin:10px 0 0">Times mean arrival. The agent orders early enough to make it — delivery time plus a 10-minute buffer.</p>'
    : '';
  $('#nextMeta').innerHTML = `<div class="nextmeta">${html}</div>${hint}`;
}

/* Day strip: the whole day at a glance, scaled to the schedule. Events are neutral,
 * hostile invites are flagged, meal windows are the accent, a planned deadline is a pin
 * and "now" is the ink line. Every mark carries a tooltip. */
function renderDayStrip(s) {
  const host = $('#dayStrip');
  const events = s.schedule.events || [];
  const gaps = s.schedule.gaps || [];
  const plans = ((s.plans && s.plans.items) || []).filter((p) => p.status === 'pending');
  if (!events.length && !gaps.length) { host.innerHTML = ''; return; }

  const spans = [];
  events.forEach((e) => {
    const a = minutesOfDay(e.start);
    let b = minutesOfDay(e.end);
    if (a === null || b === null) return;
    if (b <= a) b = 1440;
    spans.push({ a, b, kind: e.risk_score > 0 ? 'risky' : 'event', label: e.summary,
      tip: `${e.summary}\n${clockTime(e.start)} – ${clockTime(e.end)}`
        + (e.risk_score > 0 ? '\nmanipulation attempt in this invite — ignored' : '') });
  });
  gaps.forEach((g) => {
    const a = minutesOfDay(g.start);
    let b = minutesOfDay(g.end);
    if (a === null || b === null) return;
    if (b <= a) b = 1440;
    spans.push({ a, b, kind: 'gap', label: g.slot,
      tip: `${cap1(g.slot)} window\n${clockTime(g.start)} – ${clockTime(g.end)}\n${g.minutes} minutes free` });
  });

  const off = userOffset(s);
  const now = nowMinutes(off);
  let lo = Math.min(7 * 60, ...spans.map((x) => x.a));
  let hi = Math.max(22 * 60, ...spans.map((x) => x.b));
  lo = Math.max(0, Math.floor(lo / 60) * 60);
  hi = Math.min(1440, Math.ceil(hi / 60) * 60);
  const span = hi - lo;
  const pos = (m) => ((Math.min(hi, Math.max(lo, m)) - lo) / span) * 100;
  const trackPx = host.clientWidth || 640;

  const blocks = spans.map((x) => {
    const left = pos(x.a), width = Math.max(0.6, pos(x.b) - left);
    const l = esc(left.toFixed(2)), wd = esc(width.toFixed(2));
    // Labels only where a few characters fit; the tooltip and the list below carry
    // the rest. Measured in pixels, so a phone does not get a row of "B…".
    const text = (width / 100) * trackPx >= 52 ? `<span>${esc(x.label)}</span>` : '';
    return `<div class="blk ${esc(x.kind)}" style="left:${l}%;width:${wd}%" tabindex="0" data-tip="${esc(x.tip)}">${text}</div>`;
  }).join('');

  const pins = plans.map((p) => {
    const m = hhmmMinutes(p.deliver_by);
    if (m === null || m < lo || m > hi) return '';
    const l = esc(pos(m).toFixed(2));
    const tip = `${cap1(p.slot)} — arrive by ${p.deliver_by}` + (p.request ? `\n${p.request}` : '');
    return `<div class="pin" style="left:${l}%" tabindex="0" data-tip="${esc(tip)}"></div>`;
  }).join('');

  const nowMark = (now >= lo && now <= hi)
    ? `<div class="now" style="left:${esc(pos(now).toFixed(2))}%" data-tip="${esc('Now\n' + fmtMinutes(now))}"></div>`
    : '';

  const ticks = [];
  const step = span > 12 * 60 ? 180 : 120;
  for (let m = lo; m <= hi; m += step) ticks.push(m);
  const axis = ticks.map((m) =>
    `<span style="left:${esc(pos(m).toFixed(2))}%">${esc(hourLabel(m))}</span>`).join('');

  const anyRisky = spans.some((x) => x.kind === 'risky');
  const legend = [
    '<span><i class="event"></i>Busy</span>',
    '<span><i class="gap"></i>Meal window</span>',
    plans.length ? '<span><i class="pin"></i>Planned arrival</span>' : '',
    anyRisky ? '<span><i class="risky"></i>Hostile invite</span>' : '',
    nowMark ? '<span><i class="now"></i>Now</span>' : '',
  ].join('');

  host.innerHTML = `<div class="daystrip" aria-label="Your day as a strip">
      <div class="track">${blocks}${pins}${nowMark}</div>
      <div class="axis">${axis}</div>
      <div class="legend">${legend}</div>
    </div>`;
}

function renderTimeline(s) {
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
  return `<span class="tag ${esc(cls)}">${esc(label)}</span>`;
}

/* Placing an order is not the same as paying for it. With UPI the user still has to
 * approve a collect request in their own app, and until they do, no food is coming.
 * Saying "Ordered!" and stopping there would be a lie by omission. */
function renderPayment(run) {
  const p = run && run.payment;
  if (!p || p.settled || !p.needs_user) return '';
  const link = p.action_url
    ? `<div style="margin-top:10px"><a class="btn sm" href="${esc(p.action_url)}">Open your UPI app</a></div>`
    : '';
  return `
    <div class="approval" style="margin-top:14px">
      <div style="font-weight:600">Waiting for your approval</div>
      <div class="why">${esc(p.message)}</div>
      ${link}
      <div class="actions" style="margin-top:10px">
        <button class="btn ghost sm" data-track="${esc(p.order_id)}">Check status</button>
      </div>
    </div>`;
}

function wirePaymentActions(host) {
  host.querySelectorAll('[data-track]').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        const st = await api(`/api/orders/${encodeURIComponent(b.dataset.track)}/track`);
        toast(st.message, !st.settled);
        if (st.settled) await refresh();
      } catch (err) { toast(err.message, true); }
      b.disabled = false;
    }));
}

/* ---------- run anatomy ---------- */

function stepOf(run, name) {
  const steps = (run && run.steps) || [];
  for (let i = steps.length - 1; i >= 0; i--) if (steps[i].step === name) return steps[i];
  return null;
}

/* What the settlement step decided, as a badge: tone + words. */
function settlementBadge(run) {
  const st = stepOf(run, 'settlement');
  const d = (st && st.detail) || {};
  const rec = stepOf(run, 'balance_reconciled');
  if (rec && !rec.ok) return ['bad', 'Zomato Money estimate corrected'];
  if (!st) return null;
  if (!st.ok || !d.wire_type) return ['bad', 'No permitted payment'];
  if (d.expect_wallet) return ['good', 'Zomato Money · no tap'];
  if (d.wire_type === 'cash_on_delivery') return ['', 'Cash on delivery'];
  return ['warn', 'UPI request · one tap'];
}

function runReasoning(run) {
  const sel = stepOf(run, 'select_items');
  if (sel && sel.detail && sel.detail.reasoning) return sel.detail.reasoning;
  if (run && run.suggestion) return run.suggestion;
  return '';
}

function badgesHtml(run) {
  const out = [];
  const sb = settlementBadge(run);
  if (sb) out.push(`<span class="chipline ${esc(sb[0])}">${esc(sb[1])}</span>`);
  if (run.deliver_by) out.push(`<span class="chipline">${esc('Arrive by ' + run.deliver_by)}</span>`);
  const inj = (run.injection_events || []).length;
  if (inj) out.push(`<span class="chipline bad">${esc(inj + ' manipulation ' + plural(inj, 'attempt') + ' blocked')}</span>`);
  if (run.dry_run && run.state === 'simulated') out.push('<span class="chipline">Dry run</span>');
  return out.length ? `<div class="badges">${out.join('')}</div>` : '';
}

const METHOD_LABEL = {
  zomato_money: 'Zomato Money',
  cash_on_delivery: 'Cash on delivery',
  upi: 'UPI collect request',
};

/* One audit step as a sentence. Machine detail stays available as the step code
 * beside it; the sentence is what a person deciding whether to trust this reads. */
function describeStep(st) {
  const d = st.detail || {};
  const ok = !!st.ok;
  const money = (p) => rupees(Number(p || 0) / 100);
  const sub = [];
  let text;
  let tone = ok ? 'ok' : 'bad';

  switch (st.step) {
    case 'resolve_address':
      text = ok ? 'Delivering to your saved Zomato address' : (d.error || 'No delivery address on your Zomato account');
      break;
    case 'read_schedule': {
      const g = (d.gaps || []).length;
      text = `Read your schedule: ${d.events || 0} ${plural(d.events || 0, 'event')}, ${g} meal ${plural(g, 'window')}`;
      if (d.injection_events) text += ` — ${d.injection_events} suspicious ${plural(d.injection_events, 'invite')} ignored`;
      break;
    }
    case 'select_slot':
      if (!ok) { text = describeEscalation(d.reason) || 'No meal window to order for'; break; }
      text = d.planned
        ? `Ordering for your planned ${d.slot}, due by ${d.deliver_by}` + (typeof d.minutes_left === 'number' ? ` (${duration(d.minutes_left)} left)` : '')
        : `Chose the ${d.slot} window` + (d.gap ? ` — ${d.gap}` : '');
      break;
    case 'duplicate_guard':
      text = ok ? 'Not a repeat of an earlier order' : `Already ordered ${d.slot || 'this meal'} today, so it stopped`;
      if (!ok) tone = 'warn';
      break;
    case 'compute_budget':
      text = ok ? `Budget for this order: ${money(d.budget_paise)}` : 'No budget left in your envelope';
      break;
    case 'deadline_filter':
      text = `${d.kept || 0} ${plural(d.kept || 0, 'place')} can arrive in the ${duration(d.minutes_to_deadline || 0)} left`;
      if (d.dropped) text += `; ${Array.isArray(d.dropped) ? d.dropped.length : d.dropped} dropped as too slow`;
      break;
    case 'fetch_candidates':
      text = `Searched “${d.keyword || 'food'}”: ${d.restaurants || 0} ${plural(d.restaurants || 0, 'restaurant')}, ${d.with_menus || 0} with menus`;
      break;
    case 'widened_search':
      text = `Searched again without the dish constraint` + (d.alternative ? ` and found ${d.alternative}` : '');
      break;
    case 'intent_unmet':
      text = d.suggestion || 'Could not get what you asked for at acceptable quality';
      tone = 'warn';
      break;
    case 'select_items':
      text = ok
        ? (d.reasoning || `Picked ${(d.dishes || []).join(', ')} from ${d.restaurant}`)
        : (describeEscalation(d.reason) || 'Nothing suitable found');
      if (ok && d.backend) sub.push(`Chosen by the ${d.backend === 'deterministic' ? 'built-in deterministic' : d.backend} planner, then checked against the real menu`);
      break;
    case 'settlement':
      text = d.reason || (ok ? 'Chose how to pay' : 'No permitted way to pay');
      (d.considered || []).forEach((c) => sub.push(
        `${c.used ? '✓' : '✕'} ${METHOD_LABEL[c.method] || c.method}: ${c.why}`));
      break;
    case 'settlement_revised':
      text = `The cart came to ${money(d.total_paise)}, above the ${money(d.estimated_paise)} estimate, so the “nothing to approve” promise was withdrawn`;
      tone = 'warn';
      break;
    case 'policy_cart':
    case 'policy_checkout': {
      const reasons = d.reasons || [];
      if (d.decision === 'escalate') {
        text = 'Needs your approval: ' + describeEscalation(reasons[0]);
        tone = 'warn';
      } else if (ok) {
        text = st.step === 'policy_cart' ? 'Cart passed the policy check' : 'Checkout passed the policy check';
      } else {
        text = 'Blocked by policy: ' + describeEscalation(reasons[0]);
      }
      break;
    }
    case 'create_cart':
      text = ok ? `Staged a cart for ${money(d.total_paise)}` : `Could not create the cart: ${d.error || 'unknown error'}`;
      break;
    case 'wallet_authorize':
      text = ok
        ? `Reserved ${money(d.amount_paise)} against your spending envelope`
        : `Envelope refused: ${describeEscalation(d.reason)}`;
      break;
    case 'mandate_check':
      text = ok ? `Standing authorisation has ${money(d.remaining_paise)} headroom` : (d.reason || 'No standing authorisation');
      break;
    case 'rail_charge':
      text = ok ? `Charged on the ${d.rail} rail` : `The ${d.rail || 'payment'} rail declined: ${d.error || d.status}`;
      break;
    case 'checkout':
      if (d.simulated) {
        // Two different stops share this record: dry-run mode, and an escalation that
        // released the hold and is waiting on a person.
        if (/^dry-run/i.test(String(d.reason || ''))) {
          text = 'Dry run — stopped before checkout, nothing bought';
          tone = 'ok';
        } else {
          text = 'Stopped before checkout and released the hold until you decide';
          tone = 'warn';
        }
        break;
      }
      text = ok
        ? `Order placed${d.amount_paise ? ' for ' + money(d.amount_paise) : ''}`
        : `Checkout failed: ${d.error || (d.payment && d.payment.message) || 'unknown error'}`;
      if (ok && d.payment && d.payment.message) sub.push(d.payment.message);
      break;
    case 'balance_reconciled':
      text = ok
        ? `Zomato Money covered it; estimate is now ${money(d.balance_paise)}`
        : `${d.corrected || 'Zomato Money did not cover it'}; estimate written down to ${money(d.balance_paise)}`;
      if (!ok) tone = 'warn';
      break;
    default: {
      text = cap1(String(st.step || 'step').replace(/_/g, ' '));
      const bits = Object.keys(d)
        .filter((k) => ['string', 'number', 'boolean'].includes(typeof d[k]))
        .slice(0, 4).map((k) => `${k.replace(/_/g, ' ')}: ${d[k]}`);
      if (bits.length) sub.push(bits.join(' · '));
    }
  }
  return { text, tone, sub };
}

function storyHtml(run) {
  const steps = run.steps || [];
  if (!steps.length) return '';
  const items = steps.map((st) => {
    const { text, tone, sub } = describeStep(st);
    const icon = tone === 'ok' ? '✓' : tone === 'warn' ? '!' : '✕';
    const icCls = tone === 'ok' ? '' : tone;
    const subs = sub.length
      ? `<ul class="sublist">${sub.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`
      : '';
    const t = st.elapsed_us ? micros(st.elapsed_us) : '';
    return `<li>
        <span class="ic ${esc(icCls)}">${esc(icon)}</span>
        <div class="txt">${esc(text)}${subs}<div class="code">${esc(st.step)}</div></div>
        <span class="t">${esc(t)}</span>
      </li>`;
  }).join('');
  const count = esc(steps.length);
  return `<details class="audit"><summary>Why this happened (${count} steps)</summary><ol class="story">${items}</ol></details>`;
}

function renderLastRun(run) {
  const host = $('#lastRunHost');
  if (!run) { host.innerHTML = ''; return; }
  const dishes = (run.dishes || []).map(esc).join(', ');
  const fallback = esc(run.escalation_reason ? describeEscalation(run.escalation_reason) : (run.error || '—'));
  const reasoning = runReasoning(run);
  const why = reasoning ? `<div class="reasoning">${esc(reasoning)}</div>` : '';
  const badges = badgesHtml(run);
  const offer = (repeatOffer && repeatOffer.runId === run.run_id)
    ? `<div class="approval" style="margin-top:14px">
         <div style="font-weight:600">Already ordered</div>
         <div class="why">${esc(repeatOffer.reason)}</div>
         <div class="actions"><button class="btn sm" id="forceBtn">Order it again anyway</button></div>
       </div>`
    : '';
  host.innerHTML = `
    <div class="item" style="margin-top:16px;border-top:1px solid var(--border);border-bottom:0;padding-top:14px;align-items:flex-start">
      <div class="grow">
        <div class="eyebrow" style="color:var(--faint)">Last run · ${esc(relativeDay(run.created_at) || 'just now')}</div>
        <div class="title">${esc(run.restaurant || 'No restaurant chosen')}</div>
        <div class="meta">${dishes || fallback}</div>
        ${badges}
      </div>
      <div style="text-align:right">
        <div class="amt money">${rupees(run.amount_rupees)}</div>
        <div style="margin-top:4px">${stateTag(run.state)}</div>
      </div>
    </div>
    ${why}
    ${renderPayment(run)}
    ${offer}`;
  wirePaymentActions(host);
  const force = $('#forceBtn');
  if (force) {
    force.addEventListener('click', () => {
      repeatOffer = null;
      placeOrder(true);
    });
  }
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
  if (/wallet_daily_cap_exceeded|daily_cap/.test(raw)) return "This would go over today's budget.";
  if (/wallet_monthly_cap_exceeded|monthly_cap/.test(raw)) return "This would go over this month's budget.";
  if (/restaurant_not_in_allowlist/.test(raw)) return 'This restaurant is not on your approved list.';
  if (/dietary/.test(raw)) return 'It contains something on your never-order list.';
  if (/^dry-run/i.test(raw)) return 'Dry run — nothing was ordered.';
  return raw;
}

/* ---------- render: Zomato account ---------- */

let loginHandle = null;

/* Orders go to the user's own Zomato account, so nothing can be ordered until it is
 * linked. This card is the gate: phone -> OTP -> pick an address. */
function renderZomato(z) {
  const host = $('#zomatoHost');
  const orderBtn = $('#orderBtn');

  if (z && z.linked && z.default_address_id) {
    host.innerHTML = '';
    orderBtn.disabled = busy;
    orderBtn.title = '';
    return;
  }

  // Ordering is impossible without a linked account and an address to deliver to.
  orderBtn.disabled = true;
  orderBtn.title = 'Link your Zomato account first';

  if (z && z.linked) {
    host.innerHTML = `
      <div class="approval">
        <h2 style="margin:0 0 2px;font-size:15px">Add a delivery address</h2>
        <div class="why">${esc(z.error || 'Your Zomato account has no saved address. Add one in the Zomato app, then refresh.')}</div>
        <div class="actions"><button class="btn sm" id="refreshAddr">I've added it — refresh</button></div>
      </div>`;
    $('#refreshAddr').addEventListener('click', async (ev) => {
      ev.target.disabled = true;
      try {
        await api('/api/zomato/refresh', { method: 'POST', body: '{}' });
        await refresh();
      } catch (err) { toast(err.message, true); ev.target.disabled = false; }
    });
    return;
  }

  // A refresh while someone is typing their OTP must not throw the form away.
  if ($('#loginStep')) return;

  host.innerHTML = `
    <div class="card" style="border-color:var(--accent)">
      <h2>Connect your Zomato account</h2>
      <p class="sub">The agent orders on your account, to your saved address. Zomato sends a one-time code to your registered number.</p>
      <div id="loginStep"></div>
    </div>`;
  renderPhoneStep();
}

function renderPhoneStep() {
  $('#loginStep').innerHTML = `
    <div class="inputrow">
      <input id="phoneInput" type="tel" inputmode="numeric" autocomplete="tel"
             placeholder="10-digit mobile number" maxlength="14">
      <button class="btn sm" id="sendOtp">Send code</button>
    </div>
    <div class="err" id="loginMsg" style="color:var(--danger);font-size:13px;margin-top:8px"></div>`;

  const send = async () => {
    const btn = $('#sendOtp');
    btn.disabled = true;
    $('#loginMsg').textContent = '';
    try {
      const res = await api('/api/zomato/login', {
        method: 'POST', body: JSON.stringify({ phone: $('#phoneInput').value }),
      });
      loginHandle = res.handle;
      renderOtpStep();
    } catch (err) {
      $('#loginMsg').textContent = err.message;
      btn.disabled = false;
    }
  };
  $('#sendOtp').addEventListener('click', send);
  $('#phoneInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
  $('#phoneInput').focus();
}

function renderOtpStep() {
  $('#loginStep').innerHTML = `
    <div class="inputrow">
      <input id="otpInput" inputmode="numeric" autocomplete="one-time-code"
             placeholder="6-digit code" maxlength="8">
      <button class="btn sm" id="verifyOtp">Verify</button>
    </div>
    <div style="font-size:12.5px;color:var(--muted);margin-top:8px">
      Sent to your Zomato number. <a href="#" id="restartLogin">Use a different number</a>
    </div>
    <div class="err" id="loginMsg" style="color:var(--danger);font-size:13px;margin-top:8px"></div>`;

  const verify = async () => {
    const btn = $('#verifyOtp');
    btn.disabled = true;
    $('#loginMsg').textContent = '';
    try {
      await api('/api/zomato/verify', {
        method: 'POST',
        body: JSON.stringify({ handle: loginHandle, code: $('#otpInput').value }),
      });
      toast('Zomato account connected.');
      await refresh();
    } catch (err) {
      $('#loginMsg').textContent = err.message;
      btn.disabled = false;
    }
  };
  $('#verifyOtp').addEventListener('click', verify);
  $('#otpInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') verify(); });
  $('#restartLogin').addEventListener('click', (e) => { e.preventDefault(); renderPhoneStep(); });
  $('#otpInput').focus();
}

function renderAccount(z, config) {
  const host = $('#accountHost');
  if (!z || !z.linked) {
    host.innerHTML = '<div class="empty">Not connected. Link your account from the Today tab.</div>';
    return;
  }
  const addresses = z.addresses || [];
  host.innerHTML = `
    <dl class="kv" style="margin-bottom:14px">
      <dt>Signed in as</dt><dd>${esc(z.name || 'Zomato user')} · ${esc(z.phone_masked || '')}</dd>
      <dt>Minimum rating</dt><dd class="num">${esc(config.min_restaurant_rating)} ★</dd>
    </dl>
    <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);margin-bottom:8px">Deliver to</div>
    ${addresses.length ? addresses.map((a) => `
      <div class="guard">
        <span class="grow">
          <span class="name">${esc(a.alias || 'Saved address')}</span>
          <div class="meta" style="font-size:12.5px;color:var(--muted)">${esc(a.address || '')}</div>
        </span>
        ${a.address_id === z.default_address_id
          ? '<span class="state on">selected</span>'
          : `<button class="btn ghost sm" data-addr="${esc(a.address_id)}">Use this</button>`}
      </div>`).join('')
      : '<div class="empty">No saved addresses on this Zomato account.</div>'}
    <div style="margin-top:14px"><button class="btn danger sm" id="unlinkBtn">Disconnect account</button></div>`;

  host.querySelectorAll('[data-addr]').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        await api('/api/zomato/address', {
          method: 'POST', body: JSON.stringify({ address_id: b.dataset.addr }),
        });
        toast('Delivery address updated.');
        await refresh();
      } catch (err) { toast(err.message, true); b.disabled = false; }
    }));

  $('#unlinkBtn').addEventListener('click', async () => {
    await api('/api/zomato/unlink', { method: 'POST', body: '{}' });
    toast('Disconnected.');
    await refresh();
  });
}

/* ---------- render: the day planner ---------- */

const PLAN_STATUS = {
  pending: ['', 'waiting'],
  ordered: ['placed', 'ordered'],
  missed: ['failed', 'missed'],
  skipped: ['rejected', 'removed'],
};

/* A time here is when the food should ARRIVE, not when to order. The agent works
 * backwards from it, so the copy has to say so plainly or people will enter the wrong
 * number and get cold food. */
function renderPlans(plans) {
  const host = $('#planHost');
  if (!plans) { host.innerHTML = ''; return; }

  // Re-rendering on every poll would wipe a half-typed plan. Keep what is in the form.
  const draft = $('#planWhat') ? {
    slot: $('#planSlot').value, time: $('#planTime').value, what: $('#planWhat').value,
    focused: document.activeElement && document.activeElement.id,
  } : null;

  const items = plans.items || [];
  const prompt = plans.needs_planning
    ? `<div class="prompt">
         <h2>What do you want today?</h2>
         <p>Tell the agent when you want food in your hands and it works backwards —
            ordering early enough to arrive by then, not at then.</p>
       </div>`
    : '';

  const rows = items.map((p) => {
    const [cls, label] = PLAN_STATUS[p.status] || ['', p.status];
    return `<div class="planrow">
        <span class="slot">${esc(p.slot)}</span>
        <span class="by money">${esc(p.deliver_by)}</span>
        <span class="what">${esc(p.request || 'anything good')}</span>
        <span class="tag ${esc(cls)}">${esc(label)}</span>
        ${p.status === 'pending'
          ? `<button class="x" data-drop="${esc(p.plan_id)}" title="Remove">×</button>`
          : ''}
      </div>`;
  }).join('');

  const options = (plans.slots || []).map(
    (s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');

  host.innerHTML = `
    ${prompt}
    <div class="card">
      <h2>Planned meals</h2>
      <p class="sub">Times are when the food should <strong>arrive</strong>.</p>
      ${rows || '<div class="empty" style="padding:18px">Nothing planned yet.</div>'}
      <div class="planform">
        <select id="planSlot" aria-label="Meal">${options}</select>
        <input type="time" id="planTime" value="13:00" aria-label="Arrive by">
        <input type="text" id="planWhat" placeholder="biryani with sides (optional)"
               maxlength="120" aria-label="What you want">
        <button class="btn sm" id="planAdd">Add</button>
      </div>
    </div>`;

  if (draft) {
    $('#planSlot').value = draft.slot;
    $('#planTime').value = draft.time;
    $('#planWhat').value = draft.what;
    if (draft.focused && $('#' + draft.focused)) $('#' + draft.focused).focus();
  }

  const add = async () => {
    const btn = $('#planAdd');
    btn.disabled = true;
    try {
      await api('/api/plans', {
        method: 'POST',
        body: JSON.stringify({
          slot: $('#planSlot').value,
          deliver_by: $('#planTime').value,
          request: $('#planWhat').value,
        }),
      });
      $('#planWhat').value = '';
      toast('Planned. The agent will order in time for it.');
      await refresh();
    } catch (err) {
      toast(err.message, true);
      btn.disabled = false;
    }
  };
  $('#planAdd').addEventListener('click', add);
  $('#planWhat').addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });

  host.querySelectorAll('[data-drop]').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        await api(`/api/plans/${encodeURIComponent(b.dataset.drop)}`, { method: 'DELETE' });
        await refresh();
      } catch (err) { toast(err.message, true); b.disabled = false; }
    }));
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
      ${badgesHtml(r)}
      <div class="actions" style="margin-top:13px">
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
    busy = false;
    await refresh();
  } catch (err) {
    toast(err.message, true);
    btn.disabled = false;
    btn.textContent = original;
  } finally {
    busy = false;
  }
}

/* ---------- render: how the next order pays ---------- */

/* The settlement ladder, top to bottom, with the rung a typical order lands on. The
 * decision comes from the server's own choose_settlement(); this only draws it. */
function ladderHtml(s) {
  const pv = s.settlement_preview || {};
  const allowed = new Set((s.config && s.config.allowed_payment_types) || []);
  const considered = {};
  (pv.considered || []).forEach((c) => { considered[c.method] = c; });

  const rungs = [
    { key: 'zomato_money', name: 'Zomato Money', blurb: 'Applied during a UPI checkout. Nobody taps anything.',
      enabled: allowed.has('upi') },
    { key: 'cash_on_delivery', name: 'Cash on delivery', blurb: 'Placed with no approval step; you pay the rider.',
      enabled: allowed.has('cash_on_delivery') && !!(s.config && s.config.cash_fallback_when_short) },
    { key: 'upi', name: 'UPI collect request', blurb: 'Zomato sends a request; NPCI requires your tap.',
      enabled: allowed.has('upi') },
  ];

  const items = rungs.map((r) => {
    const c = considered[r.key];
    let cls = r.enabled ? '' : 'off';
    let why = r.enabled ? r.blurb : 'Not permitted by your settings.';
    let tag = '';
    if (c) {
      why = cap1(c.why);
      if (c.used) {
        cls = 'chosen' + (r.key === 'upi' ? ' tap' : '');
        tag = `<span class="tag ${r.key === 'upi' ? 'awaiting' : 'placed'} tagr">${r.key === 'upi' ? 'one tap' : 'chosen'}</span>`;
      }
    } else if (r.enabled && pv.wire_type) {
      why = 'Not needed — an earlier rung settles it.';
    }
    return `<li class="${esc(cls)}"><div class="grow"><div class="name">${esc(r.name)}</div><div class="why">${esc(why)}</div></div>${tag}</li>`;
  }).join('');
  return `<ol class="ladder">${items}</ol>`;
}

function renderPayPreview(s) {
  const pv = s.settlement_preview || {};
  const basis = pv.basis === 'typical' ? 'your typical' : 'an example';
  const amount = rupees(pv.amount_rupees);
  const verdict = pv.reason || 'No permitted way to pay.';
  const [tone] = settlementChip(pv);
  $('#payPreviewHost').innerHTML = `
    <div class="chipline ${esc(tone)}" style="margin-bottom:10px">${esc('For ' + basis + ' ' + amount + ' order')}</div>
    <p style="margin:0 0 12px;font-size:14px">${esc(verdict)}</p>
    ${ladderHtml(s)}
    <p class="sub" style="margin:12px 0 0"><a href="#wallet">Update your Zomato Money balance →</a></p>`;
}

/* ---------- render: orders ---------- */

const ORDER_FILTERS = [
  ['all', 'All', () => true],
  ['order_placed', 'Ordered', (r) => r.state === 'order_placed'],
  ['awaiting', 'Needs you', (r) => r.state === 'awaiting_approval' || r.approval_status === 'pending'],
  ['simulated', 'Simulated', (r) => r.state === 'simulated'],
  ['rejected', 'Declined', (r) => r.state === 'rejected'],
  ['failed', 'Failed', (r) => r.state === 'failed'],
];

function renderOrders(s) {
  const runs = runsOrRecent(s);
  const host = $('#ordersHost');

  // Summary tiles. "Hands-free" is the number this product is built to raise: orders
  // that went out without anybody approving anything.
  const placed = runs.filter((r) => r.state === 'order_placed');
  const spent = placed.reduce((a, r) => a + Number(r.amount_rupees || 0), 0);
  const handsFree = placed.filter((r) => {
    const st = stepOf(r, 'settlement');
    return st && st.detail && st.detail.human_less;
  }).length;
  const needs = runs.filter((r) => r.approval_status === 'pending').length;
  $('#ordersKpis').innerHTML = [
    kpiTile({ label: 'Orders placed', value: String(placed.length), note: 'of ' + runs.length + ' ' + plural(runs.length, 'run') }),
    kpiTile({ label: 'Spent', value: rupeesShort(spent), note: placed.length ? 'avg ' + rupeesShort(spent / placed.length) + ' an order' : 'nothing yet' }),
    kpiTile({ label: 'Hands-free', value: String(handsFree), unit: 'of ' + placed.length,
      note: 'placed with no approval step', tone: handsFree ? 'good' : '' }),
    kpiTile({ label: 'Waiting on you', value: String(needs), note: needs ? 'see Today' : 'nothing pending',
      tone: needs ? 'bad' : '', href: needs ? '#today' : '' }),
  ].join('');

  const filters = ORDER_FILTERS.map(([key, label, fn]) => {
    const n = runs.filter(fn).length;
    const pressed = String(orderFilter === key);
    return `<button type="button" data-filter="${esc(key)}" aria-pressed="${esc(pressed)}">${esc(label)}<span class="n">${esc(n)}</span></button>`;
  }).join('');
  $('#orderFilters').innerHTML = filters;
  $$('#orderFilters [data-filter]').forEach((b) => b.addEventListener('click', () => {
    orderFilter = b.dataset.filter;
    renderOrders(STATE);
  }));

  if (!runs.length) {
    host.innerHTML = '<div class="empty"><div class="big">🍽️</div>No orders yet. Run the agent from the Today tab.</div>';
    return;
  }
  const match = (ORDER_FILTERS.find((f) => f[0] === orderFilter) || ORDER_FILTERS[0])[2];
  const shown = runs.filter(match);
  if (!shown.length) {
    host.innerHTML = '<div class="empty">Nothing matches this filter.</div>';
    return;
  }

  // Keep open audit trails open across the 20-second poll.
  const open = new Set($$('#ordersHost details[open]').map((d) => d.dataset.run));

  host.innerHTML = shown.map((r) => {
    const reasoning = runReasoning(r);
    const why = reasoning ? `<div class="reasoning">${esc(reasoning)}</div>` : '';
    const story = storyHtml(r).replace('<details class="audit"',
      `<details class="audit" data-run="${esc(r.run_id)}"${open.has(r.run_id) ? ' open' : ''}`);
    return `<div class="order">
        <div class="head">
          <div class="grow">
            <div class="title">${esc(r.restaurant || '—')}</div>
            <div class="meta">${esc(relativeDay(r.created_at))} · ${esc(r.slot || '—')} · ${esc((r.dishes || []).join(', ') || 'no items')}</div>
          </div>
          <div style="text-align:right">
            <div class="amt money">${rupees(r.amount_rupees)}</div>
            <div style="margin-top:4px">${stateTag(r.state)}</div>
          </div>
        </div>
        ${badgesHtml(r)}
        ${why}
        ${story}
      </div>`;
  }).join('');
}

/* ---------- render: wallet ---------- */

function meter(label, used, cap) {
  const pct = pctOf(used, cap);
  const cls = esc(fillClass(pct));
  const width = esc(pct.toFixed(1));
  return `<div class="meter">
      <div class="row">
        <span class="label">${esc(label)}</span>
        <span class="val money">${rupeesShort(used)} <span style="color:var(--faint);font-weight:500">of ${rupeesShort(cap)}</span></span>
      </div>
      <div class="track"><div class="fill ${cls}" style="width:${width}%"></div></div>
    </div>`;
}

function renderWallet(s) {
  const w = s.wallet;
  const approveAbove = s.config.human_approval_above_rupees;
  $('#metersHost').innerHTML =
    meter('Spent today', w.day_spent_rupees, w.daily_cap_rupees) +
    meter('Spent this month', w.month_spent_rupees, w.monthly_cap_rupees) +
    `<dl class="kv" style="margin-top:16px">
        <dt>Maximum per single order</dt><dd class="money">${rupeesShort(w.per_order_cap_rupees)}</dd>
        <dt>Asks you above</dt><dd class="money">${rupeesShort(approveAbove)}</dd>
        <dt>Held right now</dt><dd class="money">${rupees(Number(w.reserved_paise || 0) / 100)}</dd>
      </dl>`;

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

  renderSpendChart(s);
  renderZomatoMoney(s);
}

/* Fourteen days of placed-order spend, one column per day. Single series, so no legend:
 * the card title names it. Columns are HTML, not a scaled SVG, so labels stay legible
 * at phone width. Each column has a tooltip and the same numbers sit in a table. */
function spendSeries(runs, todayIso) {
  const byDay = new Map();
  runs.forEach((r) => {
    if (r.state !== 'order_placed') return;
    const d = String(r.created_at || '').slice(0, 10);
    const cur = byDay.get(d) || { total: 0, n: 0 };
    cur.total += Number(r.amount_rupees || 0);
    cur.n += 1;
    byDay.set(d, cur);
  });
  const base = new Date(todayIso + 'T00:00:00Z');
  const out = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date(base.getTime() - i * 86400000);
    const key = d.toISOString().slice(0, 10);
    const hit = byDay.get(key) || { total: 0, n: 0 };
    out.push({
      key, day: d.getUTCDate(), today: i === 0,
      long: d.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' }),
      total: hit.total, n: hit.n,
    });
  }
  return out;
}

function niceCeil(v) {
  if (v <= 0) return 100;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function renderSpendChart(s) {
  const today = (s.plans && s.plans.date) || localDate(userOffset(s));
  const series = spendSeries(runsOrRecent(s), today);
  const peak = Math.max(...series.map((d) => d.total));
  const total = series.reduce((a, d) => a + d.total, 0);
  const host = $('#spendChart');

  $('#spendSub').textContent = total
    ? `${rupees(total)} across ${series.reduce((a, d) => a + d.n, 0)} orders in the last 14 days. Daily cap ${rupeesShort(s.wallet.daily_cap_rupees)}.`
    : 'No placed orders in the last 14 days yet.';

  const yMax = niceCeil(peak * 1.15);
  const gridVals = [0, yMax / 2, yMax];
  const grid = gridVals.map((v) => {
    const bottom = esc(((v / yMax) * 100).toFixed(2));
    return `<div class="gl" style="bottom:${bottom}%"><span>${esc(rupeesShort(v))}</span></div>`;
  }).join('');

  const cols = series.map((d) => {
    const h = d.total > 0 ? Math.max(2, (d.total / yMax) * 100) : 0;
    const height = esc(h.toFixed(2));
    const showVal = d.total > 0 && (d.total === peak || d.today);
    const val = showVal ? `<span class="cv">${esc(rupeesShort(d.total))}</span>` : '';
    const tip = `${d.long}${d.today ? ' (today)' : ''}\n${d.total ? rupees(d.total) + ' · ' + d.n + ' ' + plural(d.n, 'order') : 'No orders'}`;
    const bar = d.total > 0 ? `<div class="cb" style="height:${height}%"></div>` : '<div class="cb zero"></div>';
    const cls = d.today ? 'col today' : 'col';
    return `<div class="${cls}" tabindex="0" data-tip="${esc(tip)}">${val}${bar}</div>`;
  }).join('');

  const labels = series.map((d) =>
    `<span class="${d.today ? 'today' : ''}">${esc(d.day)}</span>`).join('');

  const rows = series.slice().reverse().filter((d) => d.n).map((d) =>
    `<tr><td>${esc(d.long)}</td><td class="n">${esc(d.n)}</td><td class="n">${esc(rupees(d.total))}</td></tr>`).join('');
  const table = rows
    ? `<details class="tableview"><summary>Show as table</summary>
         <table class="data"><thead><tr><th>Day</th><th style="text-align:right">Orders</th><th style="text-align:right">Spent</th></tr></thead><tbody>${rows}</tbody></table>
       </details>`
    : '';

  host.innerHTML = `<div class="colchart" role="img" aria-label="Daily spend for the last 14 days">
      <div class="plot">${grid}<div class="cols">${cols}</div></div>
      <div class="xl">${labels}</div>
    </div>${table}`;
}

const ZM_EXPLAIN = {
  declared: 'The figure you gave the agent.',
  spent: 'Reduced by orders that Zomato Money paid for.',
  corrected: 'Written down after an order needed a tap the agent did not expect.',
};

function renderZomatoMoney(s) {
  const zm = s.zomato_money || {};
  const updated = zm.updated_at ? relativeDay(zm.updated_at) : '';
  const head = zm.known
    ? `<div class="balance"><span class="big">${esc(rupees(zm.inr))}</span><span class="est">estimate</span></div>
       <div class="balance"><span class="src">${esc(ZM_EXPLAIN[zm.source] || '')}${updated ? ' Updated ' + esc(updated) + '.' : ''}</span></div>`
    : `<div class="balance"><span class="big" style="color:var(--faint)">Not set</span></div>
       <div class="balance"><span class="src">Without a balance the agent cannot promise a hands-free payment, so it falls back to cash or a UPI request.</span></div>`;

  // Keep a half-typed figure across polls.
  const draft = $('#zmInput') ? $('#zmInput').value : '';
  const hadFocus = document.activeElement && document.activeElement.id === 'zmInput';

  $('#zmHost').innerHTML = `
    <h2>Balance</h2>
    ${head}
    <div class="inputrow" style="margin-top:14px">
      <input id="zmInput" inputmode="decimal" placeholder="What is in Zomato Money now? e.g. 2000" maxlength="9" aria-label="Zomato Money balance in rupees">
      <button class="btn sm" id="zmSave" type="button">Update</button>
    </div>
    <p class="sub" style="margin:12px 0 0">Zomato publishes no balance API, so the agent works from the figure you give it, says it is an estimate, and corrects it the moment an order proves it wrong. One wrong prediction is enough to stop making it.</p>
    <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);margin:18px 0 8px">${esc('Settlement ladder · ' + rupees((s.settlement_preview || {}).amount_rupees) + ' order')}</div>
    ${ladderHtml(s)}`;

  const input = $('#zmInput');
  input.value = draft;
  if (hadFocus) input.focus();

  const save = async () => {
    const raw = input.value.replace(/[₹,\s]/g, '');
    const v = Number(raw);
    if (!raw || !isFinite(v) || v < 0) { toast('Enter an amount in rupees, like 2000.', true); return; }
    const btn = $('#zmSave');
    btn.disabled = true;
    try {
      await api('/api/wallet/zomato-money', { method: 'POST', body: JSON.stringify({ balance_inr: v }) });
      input.value = '';
      toast('Zomato Money set to ' + rupees(v) + '.');
      await refresh();
    } catch (err) {
      toast(err.message, true);
      btn.disabled = false;
    }
  };
  $('#zmSave').addEventListener('click', save);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
}

/* ---------- render: autonomous payment ---------- */

const MANDATE_LABEL = {
  active: ['on', 'active'],
  pending: ['off', 'waiting for your approval'],
  exhausted: ['off', 'ceiling reached'],
  revoked: ['off', 'revoked'],
  failed: ['off', 'failed'],
};

function renderMandate(p) {
  const host = $('#mandateHost');
  if (!p) { host.innerHTML = ''; return; }

  // Zomato collects the bill itself, so unless the agent also debits its own rail there
  // is nothing for a standing authorisation to cover.
  if (!p.needs_mandate) {
    const how = p.settlement === 'cash_on_delivery' ? 'cash on delivery' : 'Zomato';
    host.innerHTML = `
      <div class="guard">
        <span class="grow"><span class="name">Settlement</span></span>
        <span class="state on">${esc(how)}</span>
      </div>
      <p class="sub" style="margin:12px 0 0">
        Zomato is the merchant and collects payment itself, so the agent moves no money of
        its own and no standing authorisation is needed.
      </p>`;
    return;
  }

  const m = p.mandate;
  if (!m || m.status === 'revoked') {
    host.innerHTML = `
      <div class="inputrow">
        <input id="mandateAmount" inputmode="decimal" placeholder="Ceiling, e.g. 5000"
               value="5000" maxlength="7">
        <button class="btn sm" id="authoriseBtn">Authorise</button>
      </div>
      <p class="sub" style="margin:12px 0 0">
        Capped at ₹${esc(p.upi_circle_cap_rupees)} — the UPI Circle delegation limit for
        an agent. Until you authorise, the agent will ask before every payment.
      </p>`;
    $('#authoriseBtn').addEventListener('click', async (ev) => {
      ev.target.disabled = true;
      try {
        const amount = Number($('#mandateAmount').value) || undefined;
        const res = await api('/api/payments/mandate', {
          method: 'POST', body: JSON.stringify({ max_amount_inr: amount }),
        });
        toast(res.requires_approval
          ? 'Approve the request in your UPI app to finish.'
          : 'Authorised — the agent can now pay on its own.');
        await refresh();
      } catch (err) { toast(err.message, true); ev.target.disabled = false; }
    });
    return;
  }

  const [cls, label] = MANDATE_LABEL[m.status] || ['off', m.status];
  const used = m.max_amount_rupees ? (m.debited_rupees / m.max_amount_rupees) * 100 : 0;
  // Computed here rather than inline: the XSS audit keeps template holes to escaped or
  // trivially-numeric expressions, and that strictness is worth more than terseness.
  const pct = Math.min(100, used).toFixed(1);
  const fillCls = used >= 90 ? 'danger' : used >= 70 ? 'warn' : '';
  host.innerHTML = `
    <div class="guard">
      <span class="grow"><span class="name">Authorisation</span></span>
      <span class="state ${esc(cls)}">${esc(label)}</span>
    </div>
    ${m.test_mode ? `<div class="guard">
      <span class="grow"><span class="name">Mode</span></span>
      <span class="state off">test — no real money moves</span>
    </div>` : ''}
    <div class="meter" style="margin-top:14px">
      <div class="row">
        <span class="label">Debited against this authorisation</span>
        <span class="val money">${rupeesShort(m.debited_rupees)} <span style="color:var(--faint);font-weight:500">of ${rupeesShort(m.max_amount_rupees)}</span></span>
      </div>
      <div class="track"><div class="fill ${esc(fillCls)}" style="width:${esc(pct)}%"></div></div>
    </div>
    <dl class="kv" style="margin-top:14px">
      <dt>Autonomous payments made</dt><dd class="num">${esc(m.debits)}</dd>
      <dt>Headroom left</dt><dd class="money">${rupees(m.remaining_rupees)}</dd>
    </dl>
    ${m.last_error ? `<div class="threat" style="margin-top:12px"><div class="src">Last failure</div><div style="font-size:13px;margin-top:4px">${esc(m.last_error)}</div></div>` : ''}
    <p class="sub" style="margin:14px 0 0">
      This authorisation covers the agent's own spend ledger. Zomato is the merchant and
      collects payment itself, so this does not settle the restaurant bill directly.
    </p>
    <div style="margin-top:12px;display:flex;gap:9px;flex-wrap:wrap">
      ${m.status === 'pending' ? '<button class="btn sm" id="confirmMandate">I approved it — confirm</button>' : ''}
      <button class="btn danger sm" id="revokeMandate">Revoke</button>
    </div>`;

  const confirm = $('#confirmMandate');
  if (confirm) {
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        await api('/api/payments/mandate/activate', { method: 'POST', body: '{}' });
        toast('Authorisation confirmed.');
        await refresh();
      } catch (err) { toast(err.message, true); confirm.disabled = false; }
    });
  }
  $('#revokeMandate').addEventListener('click', async () => {
    await api('/api/payments/mandate/revoke', { method: 'POST', body: '{}' });
    toast('Revoked. The agent will ask before paying again.');
    await refresh();
  });
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
  const events = sec.injection_events || [];
  const grouped = new Map();
  events.forEach((t) => {
    const key = t.source + '|' + (t.reasons || []).join(',');
    const hit = grouped.get(key);
    if (hit) { hit.count += 1; if (t.created_at > hit.created_at) hit.created_at = t.created_at; }
    else grouped.set(key, Object.assign({ count: 1 }, t));
  });
  const threats = Array.from(grouped.values())
    .sort((a, b) => (b.score - a.score) || (b.count - a.count));

  const fromCal = events.filter((t) => t.source === 'calendar').length;
  const fromMerchant = events.filter((t) => String(t.source || '').startsWith('zomato:')).length;
  const guardsOn = guards.filter((x) => x[1]).length;
  $('#securityKpis').innerHTML = [
    kpiTile({ label: 'Attempts blocked', value: String(sec.total_blocked || 0), note: threats.length + ' distinct ' + plural(threats.length, 'source'), tone: 'good' }),
    kpiTile({ label: 'Via calendar invites', value: String(fromCal), note: 'anyone who can email you' }),
    kpiTile({ label: 'Via restaurant listings', value: String(fromMerchant), note: 'merchant-written text' }),
    kpiTile({ label: 'Guards on', value: String(guardsOn), unit: 'of ' + guards.length,
      note: guardsOn === guards.length ? 'fully locked down' : 'some guards relaxed', tone: guardsOn === guards.length ? 'good' : '' }),
  ].join('');

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

  renderLatency();
}

const OP_LABELS = {
  'guardrails.sanitize': 'Sanitise untrusted text',
  'calendar.find_gaps': 'Find meal windows',
  'intent.extract': 'Read the dish from the calendar',
  'memory.recall': 'Recall your preferences',
  'zomato.search.miss': 'Restaurant search',
  'zomato.search.hit': 'Restaurant search (cached)',
  'zomato.menu.miss': 'Menu fetch',
  'zomato.menu.hit': 'Menu fetch (cached)',
  'planner.deterministic.select': 'Deterministic planner',
  'policy.validate': 'Policy check',
  'wallet.authorize': 'Wallet reserve',
  'wallet.commit': 'Wallet commit',
};

function renderLatency() {
  const host = $('#latencyHost');
  const ops = (LATENCY && LATENCY.operations) || {};
  const rows = Object.entries(ops)
    .filter(([, v]) => v && v.count)
    .sort((a, b) => b[1].p95_us - a[1].p95_us);
  if (!rows.length) {
    host.innerHTML = '<div class="empty">No timings yet. Run the agent once and they appear here.</div>';
    return;
  }
  const peak = Math.max(...rows.map(([, v]) => v.p95_us)) || 1;
  const bars = rows.map(([name, v]) => {
    const w = esc(Math.max(1, (v.p95_us / peak) * 100).toFixed(1));
    const label = OP_LABELS[name] || name;
    const tip = `${label}\np50 ${micros(v.p50_us)}\np95 ${micros(v.p95_us)}\nmax ${micros(v.max_us)}\n${v.count} calls`;
    return `<div class="hbar" tabindex="0" data-tip="${esc(tip)}">
        <div class="row"><span class="k">${esc(label)}</span><span class="v">${esc('p95 ' + micros(v.p95_us))}</span></div>
        <div class="hbar-track"><div class="bar" style="width:${w}%"></div></div>
      </div>`;
  }).join('');
  const tableRows = rows.map(([name, v]) =>
    `<tr><td>${esc(OP_LABELS[name] || name)}<span class="sub2">${esc(name)}</span></td><td class="n">${esc(micros(v.p50_us))}</td><td class="n">${esc(micros(v.p95_us))}</td><td class="n">${esc(Math.round(v.count))}</td></tr>`).join('');
  host.innerHTML = `<div class="hbars">${bars}</div>
    <details class="tableview"><summary>Show as table</summary>
      <div class="scroll-x"><table class="data"><thead><tr><th>Step</th><th style="text-align:right">p50</th><th style="text-align:right">p95</th><th style="text-align:right">Calls</th></tr></thead><tbody>${tableRows}</tbody></table></div>
    </details>`;
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

  const cuisines = (p.top_cuisines || []).slice(0, 6);
  const peak = Math.max(1, ...cuisines.map((c) => Number(c[1]) || 0));
  const bars = cuisines.map(([name, n]) => {
    const w = esc(((Number(n) / peak) * 100).toFixed(1));
    return `<div class="hbar">
        <div class="row"><span class="k">${esc(name)}</span><span class="v">${esc(n + ' ' + plural(Number(n), 'order'))}</span></div>
        <div class="hbar-track"><div class="bar soft" style="width:${w}%"></div></div>
      </div>`;
  }).join('');
  $('#cuisineBars').innerHTML = bars
    ? `<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);margin:18px 0 10px">Cuisines you order most</div><div class="hbars">${bars}</div>`
    : '';
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

$$('#view-taste .inputrow input').forEach((input) => {
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      input.closest('.inputrow').querySelector('[data-add]').click();
    }
  });
});

/* ---------- render: privacy ---------- */

async function loadPrivacy() {
  if (PRIVACY) { renderPrivacy(); return; }
  try {
    PRIVACY = await api('/api/privacy');
    renderPrivacy();
  } catch (err) {
    $('#privacyHost').innerHTML = `<div class="empty">${esc('Could not load the inventory: ' + err.message)}</div>`;
  }
}

function renderPrivacy() {
  const inv = (PRIVACY && PRIVACY.inventory) || [];
  const yn = (v, yes, no) => v ? `<span class="yes">${esc(yes)}</span>` : `<span class="no">${esc(no)}</span>`;
  const rows = inv.map((c) => `
    <tr>
      <td><strong>${esc(c.category)}</strong><span class="sub2">${esc(c.examples)}</span></td>
      <td>${esc(c.where)}<span class="sub2">${esc('Why: ' + c.why)}</span></td>
      <td>${yn(c.persisted, 'Yes', 'No')}</td>
      <td>${yn(c.sent_to_model, 'Yes', 'No')}</td>
    </tr>`).join('');
  $('#privacyHost').innerHTML = inv.length
    ? `<p class="sub">Held in code, not in a document that drifts: every “No” in the disk column is checked against the real files on each test run.</p>
       <div class="scroll-x"><table class="data">
         <thead><tr><th>What</th><th>Where it lives</th><th>On disk</th><th>Sent to model</th></tr></thead>
         <tbody>${rows}</tbody>
       </table></div>`
    : '<div class="empty">Nothing recorded.</div>';
}

$('#exportBtn').addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  try {
    const data = await api('/api/me/data');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'meal-agent-my-data.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast('Downloaded everything held about you.');
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
});

/* Erasure is irreversible, so it takes two deliberate clicks within a few seconds. */
let eraseArmed = null;
$('#eraseBtn').addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  if (!eraseArmed) {
    btn.textContent = 'Click again to erase everything';
    eraseArmed = setTimeout(() => { eraseArmed = null; btn.textContent = 'Erase my data'; }, 4000);
    return;
  }
  clearTimeout(eraseArmed);
  eraseArmed = null;
  btn.disabled = true;
  try {
    await api('/api/me/data', { method: 'DELETE' });
    toast('Erased. Your data has been deleted.');
    await refresh();
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
  btn.textContent = 'Erase my data';
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
      order_placed: (run.payment && run.payment.needs_user)
        ? run.payment.message
        : `Ordered from ${run.restaurant}.`,
      simulated: `Dry run: would order ${rupees(run.amount_rupees)} from ${run.restaurant}.`,
      awaiting_approval: 'Needs your approval — see the card above.',
      rejected: describeEscalation(run.escalation_reason) || 'Nothing suitable found.',
      failed: run.error || 'The run failed.',
    };
    const bad = run.state === 'failed' || run.state === 'rejected';
    toast(messages[run.state] || run.state, bad);
    // Already ordered this meal: offer a deliberate repeat rather than a dead end.
    repeatOffer = (run.state === 'rejected' && /already ordered/i.test(run.escalation_reason || ''))
      ? { runId: run.run_id, reason: run.escalation_reason }
      : null;
    renderLastRun(run);
    busy = false;
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy = false;
    btn.textContent = 'Order now';
    if (STATE) renderZomato(STATE.zomato); else btn.disabled = false;
  }
}

$('#orderBtn').addEventListener('click', () => placeOrder(false));

/* ---------- refresh loop ---------- */

async function refresh() {
  renderSync('busy');
  let state, security, runs, latency;
  try {
    [state, security, runs, latency] = await Promise.all([
      api('/api/state'),
      api('/api/security').catch(() => ({})),
      api('/api/runs?limit=200').catch(() => null),
      api('/metrics/latency').catch(() => null),
    ]);
  } catch (err) {
    renderSync('stale');
    throw err;
  }
  STATE = state;
  SECURITY = security || {};
  RUNS = (runs && runs.runs) || [];
  LATENCY = latency;
  lastSyncAt = Date.now();

  renderHeader(state);
  renderZomato(state.zomato);
  renderApprovals(state);
  renderKpis(state);
  renderPlans(state.plans);
  renderAccount(state.zomato, state.config);
  renderToday(state);
  renderLastRun(runsOrRecent(state)[0] || null);
  renderOrders(state);
  renderWallet(state);
  renderMandate(state.payments);
  renderSecurity(state, SECURITY);
  renderTaste(state);
  if ($('#view-privacy').classList.contains('active')) loadPrivacy();

  const pendingCount = (state.pending_approvals || []).length;
  const tab = document.querySelector('nav.tabs button[data-view="today"]');
  tab.innerHTML = 'Today' + (pendingCount ? ` <span class="badge">${esc(pendingCount)}</span>` : '');
  document.title = (pendingCount ? `(${pendingCount}) ` : '') + 'Meal Agent';
  renderSync('ok');
}

async function boot() {
  try {
    const session = await api('/api/session');
    $('#logoutBtn').hidden = !session.auth_required;
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
