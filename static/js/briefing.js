/**
 * Briefing Module — the PM Briefing panel.
 *
 * A read-only daily "what's going on" tile that aggregates four existing
 * data surfaces into one view via GET /api/briefing:
 *   1. email urgency (flagged/unread messages)
 *   2. calendar events for today + the next 7 days
 *   3. open backlog items (data/backlog.json)
 *   4. scheduled task scheduler status (active tasks + recent runs)
 *
 * Everything is rendered with textContent to stay XSS-safe (subjects, event
 * summaries and backlog titles are user/untrusted content).
 */

import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const API_BASE = window.location.origin;

let _modal = null;
let _open = false;

// ── CSS (self-contained; kept out of the monolithic style.css) ──

const _CSS_ID = 'briefing-css';
const _CSS = `
#briefing-modal .modal-content {
  width: 520px; max-width: 94vw; max-height: 86vh;
  display: flex; flex-direction: column;
}
#briefing-modal .modal-body {
  overflow-y: auto; padding: 12px 16px 16px; display: flex; flex-direction: column; gap: 14px;
}
.briefing-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.briefing-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 10px; border-radius: 999px; font-size: 12px; line-height: 1.2;
  background: var(--bg-soft, rgba(128,128,128,.12)); color: var(--tx, #bbb);
  border: 1px solid transparent;
}
.briefing-chip b { font-size: 13px; }
.briefing-chip.is-urgent { border-color: rgba(230,80,60,.5); color: #ff8a70; }
.briefing-chip.is-today { border-color: rgba(90,150,255,.45); color: #9cc0ff; }
.briefing-section { border-top: 1px solid rgba(128,128,128,.18); padding-top: 10px; }
.briefing-section h5 {
  margin: 0 0 8px; font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--tx-soft, #888); display: flex; align-items: center; justify-content: space-between;
}
.briefing-muted { color: var(--tx-soft, #888); font-size: 12px; }
.briefing-row {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 5px 6px; border-radius: 6px; font-size: 13px; line-height: 1.35;
}
.briefing-row:hover { background: rgba(128,128,128,.1); }
.briefing-time { flex-shrink: 0; font-variant-numeric: tabular-nums; color: var(--tx-soft, #888); min-width: 46px; }
.briefing-score {
  flex-shrink: 0; min-width: 18px; height: 18px; border-radius: 4px; text-align: center;
  font-size: 11px; font-weight: 700; line-height: 18px; margin-top: 1px; color: #fff;
}
.briefing-score.s2 { background: #d97a46; }
.briefing-score.s3 { background: #cf5540; }
.briefing-score.s1 { background: #6b7fd1; }
.briefing-tag {
  display: inline-block; margin: 1px 4px 0 0; padding: 1px 6px; border-radius: 4px;
  background: rgba(128,128,128,.15); color: var(--tx-soft, #999); font-size: 10px; text-transform: uppercase;
}
.briefing-prio {
  flex-shrink: 0; min-width: 22px; height: 22px; border-radius: 5px; text-align: center;
  font-size: 11px; font-weight: 700; line-height: 22px; margin-top: 1px;
}
.briefing-prio.p1 { background: #cf5540; color: #fff; }
.briefing-prio.p2 { background: #d97a46; color: #fff; }
.briefing-prio.p3 { background: #b98a24; color: #fff; }
.briefing-prio.other { background: rgba(128,128,128,.18); color: var(--tx-soft, #999); }
.briefing-empty { color: var(--tx-soft, #888); font-size: 12px; padding: 4px 6px; }
.briefing-refresh {
  border: 0; background: rgba(128,128,128,.14); color: var(--tx, #ccc); cursor: pointer;
  width: 22px; height: 22px; border-radius: 6px; font-size: 13px; line-height: 1; margin-left: 8px;
  vertical-align: -3px;
}
.briefing-refresh:hover { background: rgba(128,128,128,.28); }
.briefing-refresh:disabled { opacity: .4; cursor: default; }
.briefing-footer {
  margin-top: 2px; font-size: 11px; color: var(--tx-soft, #777);
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
}
.briefing-task-status { font-size: 10px; text-transform: uppercase; letter-spacing: .04em; }
.briefing-task-status.ok { color: #7cc47c; }
.briefing-task-status.err { color: #ff8a70; }
.briefing-task-status.queued { color: #9cc0ff; }
@media (max-width: 640px) {
  #briefing-modal .modal-content { width: 100vw; max-width: 100vw; height: 100vh; max-height: 100vh; }
}
`;

function _ensureCss() {
  if (document.getElementById(_CSS_ID)) return;
  const style = document.createElement('style');
  style.id = _CSS_ID;
  style.textContent = _CSS;
  document.head.appendChild(style);
}

// ── Small helpers ──

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function _fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
}

function _fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleDateString('de-DE', { weekday: 'short', day: '2-digit', month: '2-digit' });
}

function _el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

// ── Modal lifecycle ──

function _getModal() {
  if (_modal) return _modal;
  _ensureCss();
  _modal = document.createElement('div');
  _modal.id = 'briefing-modal';
  _modal.className = 'modal';
  _modal.style.display = 'none';
  _modal.innerHTML = `
    <div class="modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M9 18h6"/><path d="M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10.47c.63.6 1 1.44 1 2.53h6c0-1.09.37-1.93 1-2.53A6 6 0 0 0 12 3z"/></svg>Briefing<button class="briefing-refresh" id="briefing-refresh" title="Aktualisieren">↻</button></h4>
        <button class="close-btn" id="briefing-close">✖</button>
      </div>
      <div class="modal-body" id="briefing-body"></div>
    </div>`;
  document.body.appendChild(_modal);
  _modal.querySelector('#briefing-close').addEventListener('click', closeBriefing);
  _modal.querySelector('#briefing-refresh').addEventListener('click', () => { refreshBriefing(false); });
  _modal.addEventListener('click', (e) => { if (e.target === _modal) closeBriefing(); });
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(_modal, { content, header });
  return _modal;
}

async function refreshBriefing(silent) {
  _open = true;
  const modal = _getModal();
  const body = modal.querySelector('#briefing-body');
  const refreshBtn = modal.querySelector('#briefing-refresh');
  if (!silent) {
    body.innerHTML = '<div class="briefing-empty">Briefing wird geladen …</div>';
    if (refreshBtn) refreshBtn.disabled = true;
  }
  let data = null;
  try {
    const res = await fetch(`${API_BASE}/api/briefing`, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    body.innerHTML = '';
    body.appendChild(_el('div', 'briefing-empty', `Briefing konnte nicht geladen werden (${err.message || err}).`));
  } finally {
    if (refreshBtn) refreshBtn.disabled = false;
  }
  if (data) _render(body, data);
}

function openBriefing() {
  if (_open) return;
  // Restore a minimized instance in place, preserving rendered content.
  if (Modals.isMinimized('briefing-modal')) {
    Modals.restore('briefing-modal');
    _open = true;
    return;
  }
  _open = true;
  const modal = _getModal();
  modal.classList.remove('hidden', 'modal-minimized');
  const content = modal.querySelector('.modal-content');
  if (content) {
    content.classList.remove('modal-closing', 'sheet-ready');
    content.style.transform = '';
    content.style.transition = '';
    content.style.animation = '';
    content.style.opacity = '';
  }
  modal.style.display = 'flex';
  Modals.register('briefing-modal', {
    railBtnId: 'rail-briefing',
    sidebarBtnId: 'tool-briefing-btn',
    closeFn: () => { _open = false; },
    restoreFn: () => {},
  });
  refreshBriefing(true);
}

function closeBriefing() {
  if (!_open) return;
  _open = false;
  const modal = _getModal();
  modal.classList.add('hidden');
  modal.style.display = 'none';
}

function isBriefingOpen() {
  return _open;
}

// ── Rendering ──

function _renderChips(body, data) {
  const s = data.summary || {};
  const row = _el('div', 'briefing-chips');
  const chip = (label, value, cls) => {
    const c = _el('span', `briefing-chip${cls ? ' ' + cls : ''}`);
    c.appendChild(_el('b', null, String(value)));
    c.appendChild(_el('span', null, label));
    return c;
  };
  row.appendChild(chip('ungelesen', s.email_unread || 0, (s.email_unread || 0) > 0 ? '' : ''));
  row.appendChild(chip('dringend', s.email_urgent || 0, (s.email_urgent || 0) > 0 ? 'is-urgent' : ''));
  row.appendChild(chip('Termine heute', s.appointments_today || 0, (s.appointments_today || 0) > 0 ? 'is-today' : ''));
  row.appendChild(chip('Backlog offen', s.backlog_open || 0));
  row.appendChild(chip('Tasks aktiv', s.tasks_active || 0));
  body.appendChild(row);
}

function _renderEmail(body, data) {
  const email = data.email || {};
  const flagged = email.flagged || [];
  const sec = _el('div', 'briefing-section');
  const h = _el('h5', null, 'E‑Mail');
  h.appendChild(_el('span', 'briefing-muted',
    flagged.length > 0 ? `${email.total_unread || 0} ungelesen, ${email.total_urgent || 0} als dringend eingestuft` : ''));
  sec.appendChild(h);
  if (!flagged.length) {
    sec.appendChild(_el('div', 'briefing-empty', 'Keine dringenden oder ungelesenen E‑Mails.'));
  } else {
    const list = _el('div', null);
    flagged.forEach(m => {
      const row = _el('div', 'briefing-row');
      const score = _el('span', `briefing-score s${Math.min(3, Math.max(1, m.score || 1))}`, String(m.score || 0));
      row.appendChild(score);
      const mid = _el('div', null);
      mid.appendChild(_el('div', null, m.subject));
      mid.appendChild(_el('div', 'briefing-muted', m.from));
      if ((m.tags || []).length) {
        const tags = _el('div', null);
        m.tags.forEach(t => tags.appendChild(_el('span', 'briefing-tag', t)));
        mid.appendChild(tags);
      }
      row.appendChild(mid);
      list.appendChild(row);
    });
    sec.appendChild(list);
  }
  body.appendChild(sec);
}

function _renderCalendar(body, data) {
  const cal = (data.calendar || {}).today || [];
  const sec = _el('div', 'briefing-section');
  const h = _el('h5', null, 'Kalender · heute');
  h.appendChild(_el('span', 'briefing-muted',
    new Date().toLocaleDateString('de-DE', { weekday: 'long', day: '2-digit', month: 'long' })));
  sec.appendChild(h);
  if (!cal.length) {
    sec.appendChild(_el('div', 'briefing-empty', 'Keine Termine für heute.'));
  } else {
    const list = _el('div', null);
    cal.forEach(ev => {
      const row = _el('div', 'briefing-row');
      row.appendChild(_el('span', 'briefing-time', ev.all_day ? 'ganztägig' : _fmtTime(ev.dtstart)));
      const mid = _el('div', null);
      mid.appendChild(_el('div', null, ev.summary || '(ohne Titel)'));
      const meta = [];
      if (ev.location) meta.push(ev.location);
      if (ev.calendar) meta.push(ev.calendar);
      if (meta.length) mid.appendChild(_el('div', 'briefing-muted', meta.join(' · ')));
      row.appendChild(mid);
      list.appendChild(row);
    });
    sec.appendChild(list);
  }
  const upcoming = (data.calendar || {}).upcoming || [];
  if (upcoming.length) {
    const hUp = _el('h5', null, 'Nächste 7 Tage');
    sec.appendChild(hUp);
    const list = _el('div', null);
    upcoming.forEach(ev => {
      const row = _el('div', 'briefing-row');
      row.appendChild(_el('span', 'briefing-time', ev.all_day ? 'ganztägig' : _fmtTime(_localIso(ev))));
      const mid = _el('div', null);
      mid.appendChild(_el('div', null, ev.summary || '(ohne Titel)'));
      mid.appendChild(_el('div', 'briefing-muted', _fmtDate(_localIso(ev))));
      row.appendChild(mid);
      list.appendChild(row);
    });
    sec.appendChild(list);
  }
  body.appendChild(sec);
}

// Events are serialized with a trailing Z when stored as UTC; convert to a
// comparable format for display formatting.
function _localIso(ev) {
  const raw = ev.dtstart;
  if (!raw) return '';
  if (raw.endsWith('Z')) return raw;
  const d = new Date(raw);
  if (isNaN(d.getTime())) return raw;
  return d.toISOString();
}

function _renderBacklog(body, data) {
  const items = data.backlog || [];
  const sec = _el('div', 'briefing-section');
  const h = _el('h5', null, 'Backlog');
  h.appendChild(_el('span', 'briefing-muted',
    `${items.length} von ${(data.summary || {}).backlog_open || 0} offen`));
  sec.appendChild(h);
  if (!items.length) {
    sec.appendChild(_el('div', 'briefing-empty', 'Backlog leer.'));
  } else {
    const list = _el('div', null);
    items.forEach(b => {
      const row = _el('div', 'briefing-row');
      const prio = _el('span', `briefing-prio ${b.priority <= 3 ? 'p' + b.priority : 'other'}`,
        b.priority <= 9 ? String(b.priority) : '');
      if (b.priority <= 3) row.appendChild(prio);
      const mid = _el('div', null);
      mid.appendChild(_el('div', null, b.title));
      if (b.estimated_effort) mid.appendChild(_el('div', 'briefing-muted', b.estimated_effort));
      row.appendChild(mid);
      list.appendChild(row);
    });
    sec.appendChild(list);
  }
  body.appendChild(sec);
}

function _renderTasks(body, data) {
  const tasks = (data.tasks || {}).active || [];
  const dueSoon = data.tasks.due_soon || [];
  const sec = _el('div', 'briefing-section');
  const h = _el('h5', null, 'Geplante Tasks');
  h.appendChild(_el('span', 'briefing-muted',
    `${dueSoon.length} von ${tasks.length} fällig in den nächsten 24 h`));
  sec.appendChild(h);
  if (!tasks.length) {
    sec.appendChild(_el('div', 'briefing-empty', 'Keine aktiven Tasks.'));
  } else {
    const list = _el('div', null);
    tasks.forEach(t => {
      const row = _el('div', 'briefing-row');
      const time = t.next_run ? (t.schedule === 'daily' || t.schedule === 'weekly' || t.schedule === 'monthly'
        ? t.scheduled_time || _fmtTime(t.next_run)
        : _fmtTime(t.next_run)) : '—';
      row.appendChild(_el('span', 'briefing-time', time));
      const mid = _el('div', null);
      mid.appendChild(_el('div', null, t.name));
      if (t.cron_expression) mid.appendChild(_el('div', 'briefing-muted', `cron ${t.cron_expression}`));
      row.appendChild(mid);
      list.appendChild(row);
    });
    sec.appendChild(list);
  }
  // Recent runs — last few task executions with their status.
  const runs = (data.tasks || {}).recent_runs || [];
  if (runs.length) {
    sec.appendChild(_el('h5', null, 'Letzte Läufe'));
    const list = _el('div', null);
    runs.forEach(r => {
      const row = _el('div', 'briefing-row');
      row.appendChild(_el('span', 'briefing-time', _fmtTime(r.started_at) || ''));
      const mid = _el('div', null);
      mid.appendChild(_el('div', null, r.task_name || r.task_id));
      const stWrap = _el('div', null);
      stWrap.appendChild(_el('span',
        `briefing-task-status ${r.status === 'success' ? 'ok' : r.status === 'error' ? 'err' : 'queued'}`,
        r.status == null ? '' : r.status === 'success' ? 'erfolgreich' : r.status === 'error' ? 'fehlgeschlagen' : r.status));
      mid.appendChild(stWrap);
      if (r.error) mid.appendChild(_el('div', 'briefing-muted', r.error));
      row.appendChild(mid);
      list.appendChild(row);
    });
    sec.appendChild(list);
  }
  body.appendChild(sec);
}

function _renderFooter(body, data) {
  const footer = _el('div', 'briefing-footer');
  footer.appendChild(_el('span', null, 'Quelle: E‑Mail · Kalender · Backlog · Tasks'));
  const ts = data.generated_at ? new Date(data.generated_at) : null;
  footer.appendChild(_el('span', null,
    ts && !isNaN(ts.getTime()) ? `Aktualisiert ${ts.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })}` : ''));
  body.appendChild(footer);
}

function _render(body, data) {
  body.innerHTML = '';
  _renderChips(body, data);
  _renderEmail(body, data);
  _renderCalendar(body, data);
  _renderBacklog(body, data);
  _renderTasks(body, data);
  _renderFooter(body, data);
}