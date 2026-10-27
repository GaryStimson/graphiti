/* Memory Manager: single-page app (no build step). All data is inserted with textContent /
 * DOM nodes, never innerHTML, because memories are agent-written and must be treated as
 * untrusted text. */
'use strict';

const main = document.getElementById('main');
const topbar = document.getElementById('topbar');
const state = { authed: false, filters: { categories: [], agents: [] }, token: 0, cy: null };

/* ------------------------------------------------------------------ helpers */

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'memory-ui', ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError('Cannot reach the server (offline?)', 0);
  }
  if (res.status === 401 && !path.startsWith('/auth/')) {
    state.authed = false;
    showLogin();
    throw new ApiError('Please sign in', 401);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join('; ');
    throw new ApiError(detail || res.statusText || 'Request failed', res.status);
  }
  return data;
}

function toast(message, isError = false) {
  const el = h('div', { class: `toast${isError ? ' err' : ''}`, role: 'status' }, message);
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3200);
}

function fail(e) {
  if (e.status !== 401) toast(e.message || String(e), true);
}

function fmt(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function fmtDay(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { dateStyle: 'medium' });
}

const STATUS_LABEL = { current: 'Current', superseded: 'Superseded', not_yet_valid: 'Upcoming' };
const qs = (params) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== '' && v !== null && v !== undefined) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : '';
};

function dialog({ title, body, actions }) {
  return new Promise((resolve) => {
    const dlg = h('dialog', {}, h('h2', {}, title), body,
      h('div', { class: 'dialog-actions' }, actions.map((a) =>
        h('button', {
          class: `btn ${a.kind || ''}`, type: 'button',
          onclick: async () => {
            if (a.onClick) {
              const keep = await a.onClick(dlg);
              if (keep === false) return;
            }
            dlg.close();
            resolve(a.value);
          },
        }, a.label))));
    dlg.addEventListener('close', () => { dlg.remove(); resolve(null); });
    document.body.append(dlg);
    dlg.showModal();
  });
}

const flagBadges = (flags) => (flags || []).map((f) => h('span', { class: 'badge flag', title: `Possible ${f.kind} (${f.hint})` }, `⚠ ${f.kind}`));
const link = (href, text) => h('a', { href }, text);
const chips = (items, hrefFor) => h('div', { class: 'chips' }, (items || []).map((c) =>
  hrefFor ? h('a', { class: 'chip', href: hrefFor(c) }, c) : h('span', { class: 'chip' }, c)));

function pager(data, onPage) {
  if (data.pages <= 1) return null;
  return h('div', { class: 'pager' },
    h('button', { class: 'btn small', disabled: data.page <= 1, onclick: () => onPage(data.page - 1) }, '‹ Prev'),
    h('span', { class: 'muted small' }, `Page ${data.page} of ${data.pages} · ${data.total} items`),
    h('button', { class: 'btn small', disabled: data.page >= data.pages, onclick: () => onPage(data.page + 1) }, 'Next ›'));
}

function select(options, value, onChange, label) {
  const el = h('select', { 'aria-label': label, onchange: (e) => onChange(e.target.value) },
    options.map(([v, text]) => h('option', { value: v, selected: v === value }, text)));
  return el;
}

/* ------------------------------------------------------------------ routing */

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  const parts = path.split('/').filter(Boolean);
  return { parts, params: Object.fromEntries(new URLSearchParams(query || '')) };
}

function go(path, params = {}) {
  location.hash = `#/${path}${qs(params)}`;
}

async function route() {
  if (!state.authed) return;
  const token = ++state.token;
  const { parts, params } = parseHash();
  const name = parts[0] || '';
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === name));
  main.classList.toggle('wide', name === 'graph');
  if (state.cy) { state.cy.destroy(); state.cy = null; }
  main.replaceChildren(h('p', { class: 'muted pad' }, 'Loading…'));
  try {
    const view = {
      '': viewOverview, notes: viewNotes, facts: viewFacts, graph: viewGraph,
      review: viewReview, add: viewAdd, history: viewHistory,
    }[name] || viewOverview;
    const node = await view(parts, params);
    if (token !== state.token) return;
    main.replaceChildren(node);
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  } catch (e) {
    if (token !== state.token) return;
    main.replaceChildren(h('div', { class: 'banner err' }, e.message || 'Something went wrong'));
    fail(e);
  }
}

const reload = () => route();

/* ------------------------------------------------------------------ login / session */

function showLogin(message) {
  topbar.hidden = true;
  state.authed = false;
  const error = h('p', { class: 'banner err', hidden: !message, role: 'alert' }, message || '');
  const input = h('input', { type: 'password', id: 'password', autocomplete: 'current-password', required: true, 'aria-label': 'Password' });
  const button = h('button', { class: 'btn primary', type: 'submit' }, 'Sign in');
  const form = h('form', {
    onsubmit: async (e) => {
      e.preventDefault();
      button.disabled = true;
      try {
        await api('/auth/login', { method: 'POST', body: { password: input.value } });
        input.value = '';
        await start();
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
        input.select();
      } finally {
        button.disabled = false;
      }
    },
  }, error, h('div', { class: 'field' }, h('label', { for: 'password' }, 'Password'), input), button);
  main.classList.remove('wide');
  main.replaceChildren(h('div', { class: 'login' },
    h('img', { src: '/icons/icon-192.png', width: 84, height: 84, alt: '' }),
    h('h1', {}, 'Memory Manager'),
    h('p', { class: 'muted' }, 'Sign in to view and manage your memories.'),
    form));
  input.focus();
}

async function start() {
  let me;
  try {
    me = await api('/auth/me');
  } catch (e) {
    showLogin(e.message);
    return;
  }
  if (!me.configured) {
    showLogin('The UI password is not configured on the server (set UI_PASSWORD in .env).');
    return;
  }
  if (!me.authenticated) {
    showLogin();
    return;
  }
  state.authed = true;
  topbar.hidden = false;
  api('/api/review?days=7').then((r) => {
    const n = r.sensitive_notes.length + r.sensitive_facts.length;
    const badge = document.getElementById('review-badge');
    badge.hidden = n === 0;
    badge.textContent = String(n);
    badge.title = `${n} possible sensitive item(s)`;
  }).catch(() => {});
  route();
}

document.getElementById('logout').addEventListener('click', async () => {
  try { await api('/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  location.hash = '';
  showLogin();
});

/* ------------------------------------------------------------------ shared pieces */

async function loadFilters() {
  const stats = await api('/api/stats');
  state.filters = stats.filters;
  return stats;
}

function factCard(f, { showActions = true } = {}) {
  const old = f.status === 'superseded';
  return h('div', { class: `card${f.flags.length ? ' flagged' : ''}` },
    h('div', { class: `fact-text${old ? ' old' : ''}` }, f.fact),
    h('div', { class: 'meta' },
      h('span', { class: `badge ${f.status}` }, STATUS_LABEL[f.status] || f.status),
      flagBadges(f.flags),
      h('span', {}, link(`#/graph${qs({ center: f.subject.uuid })}`, f.subject.name), ' → ', link(`#/graph${qs({ center: f.object.uuid })}`, f.object.name)),
      f.valid_at ? h('span', {}, `from ${fmtDay(f.valid_at)}`) : null,
      f.invalid_at ? h('span', {}, `until ${fmtDay(f.invalid_at)}`) : null,
      h('span', {}, `recorded ${fmt(f.recorded_at)}`),
      f.agents.length ? h('span', {}, `by ${f.agents.join(', ')}`) : null),
    f.categories.length ? h('div', { class: 'mt' }, chips(f.categories, (c) => `#/facts${qs({ category: c })}`)) : null,
    f.sources && f.sources.length ? h('div', { class: 'meta' }, 'Source: ', f.sources.map((s) => link(`#/notes/${s.note_id}`, s.title || 'note'))) : null,
    showActions ? factActions(f) : null);
}

function factActions(f) {
  return h('div', { class: 'actions' },
    h('button', { class: 'btn small', onclick: () => editFact(f) }, 'Edit wording'),
    f.status === 'superseded'
      ? h('button', { class: 'btn small', onclick: () => patchFact(f, 'restore') }, 'Restore')
      : h('button', { class: 'btn small', onclick: () => endFact(f) }, 'No longer true'),
    h('button', { class: 'btn small danger', onclick: () => deleteFact(f) }, 'Delete'));
}

function noteCard(n) {
  return h('div', { class: `card clickable${n.flags.length ? ' flagged' : ''}`, onclick: () => go(`notes/${n.note_id}`) },
    h('h3', {}, n.title || '(untitled)'),
    h('div', { class: 'meta' },
      n.flags.length ? flagBadges(n.flags) : null,
      h('span', {}, fmt(n.recorded_at)),
      n.agent ? h('span', {}, `by ${n.agent}`) : null,
      h('span', {}, `${n.facts} fact${n.facts === 1 ? '' : 's'}`),
      n.parts > 1 ? h('span', {}, `${n.parts} parts`) : null,
      n.categories.length ? h('span', {}, n.categories.join(', ')) : null),
    h('p', { class: 'excerpt' }, n.excerpt));
}

/* ------------------------------------------------------------------ actions */

async function patchFact(f, action, at) {
  try {
    await api(`/api/facts/${f.uuid}`, { method: 'PATCH', body: { action, at } });
    toast(action === 'restore' ? 'Fact restored' : 'Fact ended');
    reload();
  } catch (e) { fail(e); }
}

async function endFact(f) {
  const date = h('input', { type: 'date' });
  const ok = await dialog({
    title: 'Mark as no longer true',
    body: h('div', {},
      h('p', {}, h('strong', {}, f.fact)),
      h('p', { class: 'muted small' }, 'The fact stays in the history but is no longer returned as current.'),
      h('div', { class: 'field' }, h('label', {}, 'No longer true from (optional, default now)'), date)),
    actions: [{ label: 'Cancel', value: false }, { label: 'Mark as ended', kind: 'primary', value: true }],
  });
  if (ok) await patchFact(f, 'end', date.value ? new Date(date.value).toISOString() : undefined);
}

async function deleteFact(f) {
  let plan;
  try { plan = await api(`/api/facts/${f.uuid}/delete-plan`); } catch (e) { fail(e); return; }
  const ok = await dialog({
    title: 'Delete this fact?',
    body: h('div', {},
      h('p', {}, h('strong', {}, f.fact)),
      plan.entities_deleted.length ? h('p', { class: 'small' }, 'Also removes entities left with no facts: ', plan.entities_deleted.map((e) => e.name).join(', ')) : null,
      h('p', { class: 'muted small' }, 'The source note is kept. A recovery copy is saved on the server.')),
    actions: [{ label: 'Cancel', value: false }, { label: 'Delete fact', kind: 'danger', value: true }],
  });
  if (!ok) return;
  try {
    await api(`/api/facts/${f.uuid}`, { method: 'DELETE' });
    toast('Fact deleted');
    reload();
  } catch (e) { fail(e); }
}

async function deleteNote(note) {
  let plan;
  try { plan = await api(`/api/notes/${note.note_id}/delete-plan`); } catch (e) { fail(e); return; }
  const list = (items, text) => (items.length ? h('ul', {}, items.map((i) => h('li', {}, text(i)))) : h('p', { class: 'muted small' }, 'None'));
  const ok = await dialog({
    title: 'Delete this note?',
    body: h('div', {},
      h('p', {}, h('strong', {}, note.title)),
      h('h3', {}, `Facts that will be deleted (${plan.facts_deleted.length})`),
      list(plan.facts_deleted, (f) => f.fact),
      h('h3', {}, `Facts kept (also supported by other notes) (${plan.facts_kept.length})`),
      list(plan.facts_kept, (f) => f.fact),
      h('h3', {}, `Entities that will be deleted (${plan.entities_deleted.length})`),
      list(plan.entities_deleted, (e) => e.name),
      h('p', { class: 'muted small' }, 'A recovery copy of the note and its facts is saved on the server.')),
    actions: [{ label: 'Cancel', value: false }, { label: 'Delete note', kind: 'danger', value: true }],
  });
  if (!ok) return;
  try {
    await api(`/api/notes/${note.note_id}`, { method: 'DELETE' });
    toast('Note deleted');
    go('notes');
  } catch (e) { fail(e); }
}

async function editFact(f) {
  const text = h('textarea', { 'aria-label': 'Corrected fact' }, f.fact);
  text.value = f.fact;
  const cats = h('input', { type: 'text', value: f.categories.join(', '), 'aria-label': 'Categories' });
  const dropSource = h('input', { type: 'checkbox' });
  const sensitive = h('input', { type: 'checkbox' });
  const sensitiveRow = h('label', { class: 'check', hidden: true }, sensitive, 'This looks sensitive. Save it anyway.');
  const error = h('p', { class: 'banner err', hidden: true, role: 'alert' });
  await dialog({
    title: 'Edit fact wording',
    body: h('div', {},
      h('p', { class: 'muted small' }, 'The old fact is deleted and your corrected sentence is added as a new memory, so the server extracts it again. It appears after a few seconds.'),
      error,
      h('div', { class: 'field' }, h('label', {}, 'Corrected statement (one clear sentence, e.g. "My mortgage lender is Nationwide.")'), text),
      h('div', { class: 'field' }, h('label', {}, 'Categories (comma separated)'), cats),
      f.sources && f.sources.length
        ? h('label', { class: 'check' }, dropSource, `Also delete the source note${f.sources.length > 1 ? 's' : ''} (${f.sources.map((s) => s.title).join(', ')}) so the old wording is not re-extracted.`)
        : null,
      sensitiveRow),
    actions: [
      { label: 'Cancel', value: false },
      {
        label: 'Delete and re-add', kind: 'primary', value: true,
        onClick: async () => {
          error.hidden = true;
          try {
            await api(`/api/facts/${f.uuid}/rewrite`, {
              method: 'POST',
              body: {
                new_text: text.value.trim(),
                categories: cats.value.split(',').map((c) => c.trim()).filter(Boolean),
                delete_source_notes: dropSource.checked,
                allow_sensitive: sensitive.checked,
              },
            });
            toast('Fact replaced. The new version appears after processing.');
            setTimeout(reload, 400);
          } catch (e) {
            error.textContent = e.message;
            error.hidden = false;
            if (/sensitive/i.test(e.message)) sensitiveRow.hidden = false;
            return false;
          }
          return true;
        },
      },
    ],
  });
}

/* ------------------------------------------------------------------ views */

async function viewOverview() {
  const [stats, review] = await Promise.all([loadFilters(), api('/api/review?days=7')]);
  const stat = (n, l, href) => h('a', { class: 'stat', href }, h('div', { class: 'n' }, n), h('div', { class: 'l' }, l));
  const flagged = review.sensitive_notes.length + review.sensitive_facts.length;
  return h('div', {},
    h('h1', {}, 'Overview'),
    flagged ? h('div', { class: 'banner' }, `⚠ ${flagged} item(s) may contain sensitive data. `, link('#/review', 'Review them')) : null,
    h('div', { class: 'stats' },
      stat(stats.notes, 'Notes', '#/notes'),
      stat(stats.facts.current, 'Current facts', '#/facts'),
      stat(stats.facts.not_yet_valid, 'Upcoming facts', '#/facts?status=not_yet_valid'),
      stat(stats.facts.superseded, 'Superseded facts', '#/facts?status=superseded'),
      stat(stats.entities, 'Entities', '#/graph')),
    h('h2', {}, 'Categories'),
    Object.keys(stats.categories).length ? h('div', { class: 'chips' }, Object.entries(stats.categories).map(([c, n]) => h('a', { class: 'chip', href: `#/notes${qs({ category: c })}` }, `${c} · ${n}`))) : h('p', { class: 'muted' }, 'No notes yet.'),
    h('h2', {}, 'Agents'),
    Object.keys(stats.agents).length ? h('div', { class: 'chips' }, Object.entries(stats.agents).map(([a, n]) => h('a', { class: 'chip', href: `#/notes${qs({ agent: a })}` }, `${a} · ${n}`))) : h('p', { class: 'muted' }, 'None yet.'),
    h('h2', {}, `Added in the last 7 days (${review.recent_count})`),
    review.recent_count
      ? Object.entries(review.recent_by_agent).map(([agent, notes]) => h('div', {}, h('h3', {}, `${agent} (${notes.length})`), notes.slice(0, 4).map(noteCard)))
      : h('p', { class: 'muted' }, 'Nothing new this week.'),
    h('h2', {}, 'Data'),
    h('div', { class: 'row' },
      h('a', { class: 'btn', href: '/api/export', download: '' }, 'Export everything (JSON)'),
      h('span', { class: 'muted small' }, 'Deleted items are also saved to a recovery folder on the server.')),
    h('p', { class: 'muted small' }, `Updated ${fmt(stats.generated_at)}`));
}

async function viewNotes(parts, params) {
  if (parts[1]) return viewNote(parts[1]);
  await loadFilters();
  const p = { query: '', category: '', agent: '', days: '', page: '1', ...params };
  const data = await api(`/api/notes${qs(p)}`);
  const apply = (patch) => go('notes', { ...p, page: '1', ...patch });
  const search = h('input', { type: 'search', placeholder: 'Search notes…', value: p.query, 'aria-label': 'Search notes', onkeydown: (e) => { if (e.key === 'Enter') apply({ query: e.target.value }); } });
  return h('div', {},
    h('h1', {}, 'Notes'),
    h('div', { class: 'filters' }, search,
      select([['', 'All categories'], ...state.filters.categories.map((c) => [c, c])], p.category, (v) => apply({ category: v }), 'Category'),
      select([['', 'All agents'], ...state.filters.agents.map((a) => [a, a])], p.agent, (v) => apply({ agent: v }), 'Agent'),
      select([['', 'Any time'], ['1', 'Last 24 hours'], ['7', 'Last 7 days'], ['30', 'Last 30 days']], p.days, (v) => apply({ days: v }), 'Time')),
    data.items.length ? data.items.map(noteCard) : h('p', { class: 'empty' }, 'No notes match.'),
    pager(data, (page) => go('notes', { ...p, page })));
}

async function viewNote(id) {
  const n = await api(`/api/notes/${id}`);
  return h('div', {},
    h('p', {}, link('#/notes', '‹ All notes')),
    h('h1', {}, n.title || '(untitled)'),
    n.flags.length ? h('div', { class: 'banner' }, '⚠ This note may contain sensitive data: ', n.flags.map((f) => f.kind).join(', '), '. Memory should not hold passwords, card numbers or one-time codes.') : null,
    h('div', { class: 'meta' }, h('span', {}, fmt(n.recorded_at)), n.agent ? h('span', {}, `by ${n.agent}`) : null, h('span', {}, `${n.parts} part${n.parts === 1 ? '' : 's'}`)),
    n.categories.length ? h('div', { class: 'mv' }, chips(n.categories, (c) => `#/notes${qs({ category: c })}`)) : null,
    h('div', { class: 'note-body' }, n.content),
    h('div', { class: 'actions' }, h('button', { class: 'btn danger', onclick: () => deleteNote(n) }, 'Delete note')),
    h('h2', {}, `Facts from this note (${n.facts_list.length})`),
    n.facts_list.length ? n.facts_list.map((f) => factCard({ ...f, sources: [] })) : h('p', { class: 'muted' }, 'No facts were extracted from this note.'),
    h('h2', {}, 'Entities mentioned'),
    n.entities.length ? h('div', { class: 'chips' }, n.entities.map((e) => h('a', { class: 'chip', href: `#/graph${qs({ center: e.uuid })}` }, e.name))) : h('p', { class: 'muted' }, 'None.'));
}

async function viewFacts(parts, params) {
  await loadFilters();
  const p = { status: '', query: '', category: '', agent: '', entity: '', page: '1', ...params };
  const data = await api(`/api/facts${qs(p)}`);
  const apply = (patch) => go('facts', { ...p, page: '1', ...patch });
  const search = h('input', { type: 'search', placeholder: 'Search facts…', value: p.query, 'aria-label': 'Search facts', onkeydown: (e) => { if (e.key === 'Enter') apply({ query: e.target.value }); } });
  return h('div', {},
    h('h1', {}, 'Facts'),
    h('div', { class: 'filters' }, search,
      select([['', 'Current + upcoming'], ['current', 'Current only'], ['not_yet_valid', 'Upcoming'], ['superseded', 'Superseded'], ['all', 'Everything']], p.status, (v) => apply({ status: v }), 'Status'),
      select([['', 'All categories'], ...state.filters.categories.map((c) => [c, c])], p.category, (v) => apply({ category: v }), 'Category'),
      select([['', 'All agents'], ...state.filters.agents.map((a) => [a, a])], p.agent, (v) => apply({ agent: v }), 'Agent')),
    p.entity ? h('p', {}, 'Filtered to one entity. ', link('#/facts', 'Clear')) : null,
    data.items.length ? data.items.map((f) => factCard(f)) : h('p', { class: 'empty' }, 'No facts match.'),
    pager(data, (page) => go('facts', { ...p, page })));
}

const LABEL_COLOURS = { Person: '#2dd4bf', Organization: '#818cf8', Location: '#fbbf24', Asset: '#f472b6', Event: '#fb923c', Goal: '#a3e635', Document: '#60a5fa', Procedure: '#c084fc', Preference: '#f87171', Topic: '#94a3b8' };
const colourFor = (labels) => (labels || []).map((l) => LABEL_COLOURS[l]).find(Boolean) || '#64748b';

async function viewGraph(parts, params) {
  await loadFilters();
  const p = { center: '', query: '', hops: '1', category: '', limit: '60', status: 'current,not_yet_valid', hub: '', ...params };
  const focused = Boolean(p.center || p.query);
  const hideHub = p.hub ? p.hub === 'hide' : !focused;
  const apply = (patch) => go('graph', { ...p, ...patch });
  const wanted = new Set(p.status.split(',').filter(Boolean));
  const toggle = (key, text) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: wanted.has(key), onchange: (e) => { e.target.checked ? wanted.add(key) : wanted.delete(key); apply({ status: [...wanted].join(',') || 'current' }); } }), text);
  const search = h('input', { type: 'search', placeholder: 'Find an entity…', value: p.query, 'aria-label': 'Find an entity', onkeydown: (e) => { if (e.key === 'Enter') apply({ query: e.target.value, center: '' }); } });
  const canvas = h('div', { id: 'cy', role: 'img', 'aria-label': 'Knowledge graph' });
  const panel = h('div', { class: 'panel' }, h('p', { class: 'muted' }, 'Click a node or a line to see details and manage it. Double-click a node to centre the graph on it.'));
  const info = h('p', { class: 'muted small' });

  const wrap = h('div', {},
    h('div', { class: 'graph-head' },
      h('h1', {}, 'Knowledge graph'),
      h('div', { class: 'filters' }, search,
        select([['1', '1 hop'], ['2', '2 hops'], ['3', '3 hops']], p.hops, (v) => apply({ hops: v }), 'Hops'),
        select([['', 'All categories'], ...state.filters.categories.map((c) => [c, c])], p.category, (v) => apply({ category: v }), 'Category'),
        select([['30', '30 nodes'], ['60', '60 nodes'], ['120', '120 nodes'], ['300', '300 nodes']], p.limit, (v) => apply({ limit: v }), 'Node limit'),
        p.center || p.query ? h('button', { class: 'btn', onclick: () => go('graph') }, 'Show overview') : null,
        h('button', { class: 'btn', onclick: () => { if (state.cy) { const a = document.createElement('a'); a.href = state.cy.png({ full: true, scale: 2, bg: getComputedStyle(document.body).backgroundColor }); a.download = 'memory-graph.png'; a.click(); } } }, 'Save PNG')),
      h('div', { class: 'row' }, toggle('current', 'Current'), toggle('not_yet_valid', 'Upcoming'), toggle('superseded', 'Superseded'),
        h('label', { class: 'check', title: 'The most connected entity (usually you) links to almost everything and hides the structure' }, h('input', { type: 'checkbox', checked: hideHub, onchange: (e) => apply({ hub: e.target.checked ? 'hide' : 'show' }) }), 'Hide the most connected entity')),
      h('div', { class: 'legend' },
        Object.entries({ Person: LABEL_COLOURS.Person, Organization: LABEL_COLOURS.Organization, Location: LABEL_COLOURS.Location, Asset: LABEL_COLOURS.Asset, Other: '#64748b' }).map(([k, c]) => h('span', {}, h('i', { style: { background: c } }), k)),
        h('span', {}, h('span', { class: 'dot' }), 'upcoming'), h('span', {}, h('span', { class: 'dash' }), 'superseded')),
      info),
    h('div', { class: 'graph-wrap' }, canvas, panel));

  const data = await api(`/api/graph${qs({ center: p.center, query: p.query, hops: p.hops, category: p.category, limit: p.limit, status: [...wanted].join(',') })}`);
  info.textContent = data.nodes.length
    ? `${data.nodes.length} entities, ${data.edges.length} facts${data.truncated ? ` (showing the ${data.nodes.length} most connected of ${data.total_nodes}; refine with search or raise the limit)` : ''}`
    : 'Nothing to show for these filters.';

  // Cytoscape needs the container in the DOM; draw after the view is attached.
  requestAnimationFrame(() => requestAnimationFrame(() => drawGraph(canvas, panel, data, apply, hideHub)));
  return wrap;
}

function withoutHub(data) {
  const hub = data.nodes.reduce((a, b) => (b.degree > (a ? a.degree : -1) ? b : a), null);
  if (!hub || data.nodes.length < 4) return { data, hub: null };
  const edges = data.edges.filter((e) => e.source !== hub.id && e.target !== hub.id);
  const used = new Set(edges.flatMap((e) => [e.source, e.target]));
  const nodes = data.nodes.filter((n) => n.id !== hub.id && used.has(n.id));
  return { data: { ...data, nodes, edges }, hub };
}

function drawGraph(canvas, panel, fullData, apply, hideHub) {
  if (!fullData.nodes.length || !document.body.contains(canvas)) return;
  let data = fullData;
  if (hideHub) {
    const reduced = withoutHub(fullData);
    data = reduced.data;
    if (reduced.hub) canvas.title = `Hidden: ${reduced.hub.label}`;
  }
  const css = getComputedStyle(document.body);
  const text = css.color;
  const muted = css.getPropertyValue('--muted').trim() || '#64748b';
  const info = css.getPropertyValue('--info').trim() || '#3b82f6';
  const surface = css.getPropertyValue('--surface').trim() || '#131c2e';
  const cy = cytoscape({
    container: canvas,
    elements: [
      ...data.nodes.map((n) => ({ data: { id: n.id, label: n.label, deg: n.degree, labels: n.labels, summary: n.summary, seed: n.seed, colour: colourFor(n.labels) } })),
      ...data.edges.map((e) => ({ data: { id: e.id, source: e.source, target: e.target, label: e.label, fact: e.fact, status: e.status } })),
    ],
    style: [
      { selector: 'node', style: { label: 'data(label)', 'background-color': 'data(colour)', width: 'mapData(deg, 1, 12, 16, 40)', height: 'mapData(deg, 1, 12, 16, 40)', color: text, 'font-size': 13, 'text-outline-color': surface, 'text-outline-width': 3, 'text-valign': 'bottom', 'text-margin-y': 4, 'text-wrap': 'ellipsis', 'text-max-width': 110, 'min-zoomed-font-size': 9 } },
      { selector: 'node[deg >= 4]', style: { 'min-zoomed-font-size': 0, 'font-size': 16, 'font-weight': 600 } },
      { selector: 'node[?seed]', style: { 'border-width': 3, 'border-color': text } },
      { selector: 'edge', style: { width: 1.6, 'line-color': muted, 'target-arrow-color': muted, 'target-arrow-shape': 'triangle', 'curve-style': 'bezier', label: '', 'font-size': 9, color: muted, 'text-rotation': 'autorotate', 'min-zoomed-font-size': 9 } },
      { selector: 'edge.hover, edge:selected', style: { label: 'data(label)', width: 2.6, 'z-index': 10 } },
      { selector: 'edge[status = "superseded"]', style: { 'line-style': 'dashed', opacity: 0.6 } },
      { selector: 'edge[status = "not_yet_valid"]', style: { 'line-style': 'dotted', 'line-color': info, 'target-arrow-color': info } },
      { selector: ':selected', style: { 'overlay-color': '#2dd4bf', 'overlay-opacity': 0.25, 'overlay-padding': 6 } },
    ],
    layout: { name: 'cose', animate: false, nodeRepulsion: () => 250000, idealEdgeLength: () => 60, nodeOverlap: 16, gravity: 0.5, numIter: 1500, componentSpacing: 90, padding: 30, randomize: true },
    wheelSensitivity: 0.3,
  });
  state.cy = cy;

  cy.on('tap', 'node', (evt) => showNode(panel, evt.target.data(), apply));
  cy.on('dbltap', 'node', (evt) => apply({ center: evt.target.id(), query: '' }));
  cy.on('tap', 'edge', (evt) => showEdge(panel, evt.target.data()));
  cy.on('mouseover', 'edge', (evt) => evt.target.addClass('hover'));
  cy.on('mouseout', 'edge', (evt) => evt.target.removeClass('hover'));
  cy.on('tap', (evt) => { if (evt.target === cy) panel.replaceChildren(h('p', { class: 'muted' }, 'Click a node or a line to see details.')); });
}

function showNode(panel, d, apply) {
  const summary = d.summary && d.summary.length > 320 ? `${d.summary.slice(0, 320)}…` : d.summary;
  panel.replaceChildren(
    h('h3', {}, d.label),
    d.labels && d.labels.length ? chips(d.labels) : null,
    h('p', { class: 'muted small' }, `${d.deg} fact${d.deg === 1 ? '' : 's'} shown`),
    h('div', { class: 'actions' },
      h('button', { class: 'btn small primary', onclick: () => apply({ center: d.id, query: '', hub: 'show' }) }, 'Centre here'),
      h('a', { class: 'btn small', href: `#/facts${qs({ entity: d.id, status: 'all' })}` }, 'List facts')),
    summary ? h('p', { class: 'excerpt' }, summary) : null);
}

async function showEdge(panel, d) {
  panel.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
  try {
    const f = await api(`/api/facts/${d.id}`);
    panel.replaceChildren(factCard(f));
  } catch (e) {
    panel.replaceChildren(h('p', { class: 'banner err' }, e.message));
  }
}

async function viewReview(parts, params) {
  const days = params.days || '7';
  const r = await api(`/api/review${qs({ days })}`);
  const section = (title, items, render, hint) => h('div', {}, h('h2', {}, `${title} (${items.length})`), hint ? h('p', { class: 'muted small' }, hint) : null,
    items.length ? items.map(render) : h('p', { class: 'muted' }, 'Nothing to flag.'));
  const sensitive = r.sensitive_notes.length + r.sensitive_facts.length;
  return h('div', {},
    h('h1', {}, 'Review'),
    h('div', { class: 'filters' }, select([['1', 'Last 24 hours'], ['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days']], days, (v) => go('review', { days: v }), 'Period')),
    sensitive ? h('div', { class: 'banner' }, '⚠ Memory should never hold passwords, card numbers, one-time codes or API keys. Delete the items below, then change any exposed credential.') : null,
    section('Possibly sensitive notes', r.sensitive_notes, noteCard),
    section('Possibly sensitive facts', r.sensitive_facts, (f) => factCard(f)),
    h('h2', {}, `Added in this period (${r.recent_count})`),
    r.recent_count ? Object.entries(r.recent_by_agent).map(([agent, notes]) => h('div', {}, h('h3', {}, `${agent} (${notes.length})`), notes.map(noteCard))) : h('p', { class: 'muted' }, 'Nothing added in this period.'),
    section('Duplicate current facts', r.duplicate_facts, (group) => h('div', { class: 'card' }, group.map((f) => factCard(f))), 'The same statement stored more than once.'),
    section('Facts without a source note', r.facts_without_source, (f) => factCard(f), 'Usually written outside remember(); check they are correct.'),
    section('Notes with no extracted facts', r.notes_without_facts, noteCard, 'The server found nothing to remember in these, or processing has not finished.'));
}

async function viewAdd() {
  await loadFilters();
  const content = h('textarea', { placeholder: 'e.g. My mortgage fixed rate ends in March 2027 and the lender is Nationwide.', 'aria-label': 'Memory text' });
  const title = h('input', { type: 'text', 'aria-label': 'Title (optional)', placeholder: 'Title (optional)', maxlength: 120 });
  const cats = h('input', { type: 'text', 'aria-label': 'Categories', placeholder: 'finance, property', list: 'cat-list' });
  const sensitive = h('input', { type: 'checkbox' });
  const sensitiveRow = h('label', { class: 'check', hidden: true }, sensitive, 'This looks sensitive. Save it anyway.');
  const error = h('p', { class: 'banner err', hidden: true, role: 'alert' });
  const button = h('button', { class: 'btn primary', type: 'submit' }, 'Remember this');
  return h('div', {},
    h('h1', {}, 'Add a memory'),
    h('p', { class: 'muted' }, 'Sent to the memory server exactly like an agent would, so facts are extracted and older conflicting facts are superseded. Processing takes a few seconds.'),
    h('form', {
      onsubmit: async (e) => {
        e.preventDefault();
        error.hidden = true;
        button.disabled = true;
        try {
          const res = await api('/api/remember', {
            method: 'POST',
            body: {
              content: content.value.trim(),
              title: title.value.trim() || null,
              categories: cats.value.split(',').map((c) => c.trim()).filter(Boolean),
              allow_sensitive: sensitive.checked,
            },
          });
          toast(res.message || 'Queued');
          content.value = '';
          title.value = '';
          sensitive.checked = false;
          sensitiveRow.hidden = true;
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
          if (/sensitive/i.test(err.message)) sensitiveRow.hidden = false;
        } finally {
          button.disabled = false;
        }
      },
    }, error,
    h('div', { class: 'field' }, h('label', {}, 'Memory'), content),
    h('div', { class: 'field' }, h('label', {}, 'Title'), title),
    h('div', { class: 'field' }, h('label', {}, 'Categories (comma separated, optional)'), cats,
      h('datalist', { id: 'cat-list' }, state.filters.categories.map((c) => h('option', { value: c })))),
    sensitiveRow, button));
}

async function viewHistory() {
  const data = await api('/api/changes');
  const label = { delete_note: 'Deleted note', delete_fact: 'Deleted fact', end_fact: 'Ended fact', restore_fact: 'Restored fact', rewrite_fact: 'Rewrote fact', remember: 'Added memory' };
  return h('div', {},
    h('h1', {}, 'Change history'),
    h('p', { class: 'muted' }, 'Changes made from this app. Deleted notes and facts are also saved as JSON files in the server\'s recovery folder (the ui data volume, "trash").'),
    data.items.length ? data.items.map((c) => h('div', { class: 'card' }, h('div', {}, h('strong', {}, label[c.action] || c.action), ' ', c.summary), h('div', { class: 'meta' }, fmt(c.at), c.trash ? h('span', {}, `recovery file: ${c.trash}`) : null))) : h('p', { class: 'empty' }, 'No changes yet.'));
}

/* ------------------------------------------------------------------ PWA */

let installPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  installPrompt = e;
  document.getElementById('install').hidden = false;
});
document.getElementById('install').addEventListener('click', async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  document.getElementById('install').hidden = true;
});
window.addEventListener('appinstalled', () => { document.getElementById('install').hidden = true; toast('App installed'); });

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}

window.addEventListener('hashchange', route);
start();
