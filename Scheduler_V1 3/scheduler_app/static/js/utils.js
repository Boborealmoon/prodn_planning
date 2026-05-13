/* utils.js - shared helpers */

// Toast notifications
function toast(msg, type = 'info', duration = 3500) {
  const tc = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  tc.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// API helpers
async function api(url, options = {}) {
  try {
    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      ...options
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : {};
    } catch (parseErr) {
      const looksHtml = /^\s*<!doctype html|^\s*<html[\s>]/i.test(text || '');
      data = { error: looksHtml ? `HTTP ${res.status}: endpoint not found. Restart the app and refresh the page.` : (text || `HTTP ${res.status}`) };
    }
    if (!res.ok) {
      const err = new Error(data.error || `HTTP ${res.status}`);
      err.details = data.details || null;
      err.status = res.status;
      throw err;
    }
    return data;
  } catch (e) {
    throw e;
  }
}
const GET = (url) => api(url);
const POST = (url, body) => api(url, { method: 'POST', body: JSON.stringify(body) });
const PATCH = (url, body) => api(url, { method: 'PATCH', body: JSON.stringify(body) });
const PUT = (url, body) => api(url, { method: 'PUT', body: JSON.stringify(body) });
const DEL = (url, body = {}) => api(url, { method: 'DELETE', body: JSON.stringify(body) });

// Modal helpers
function openModal(title, bodyHtml, size = '') {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = bodyHtml;
  const box = document.getElementById('global-modal-box');
  box.className = 'modal-box ' + size;
  document.getElementById('global-modal').style.display = 'flex';
}
function openChoiceModal(title, bodyHtml, choices = []) {
  return new Promise((resolve) => {
    const buttons = choices.map((choice, idx) => {
      const cls = choice.className || 'btn btn-primary';
      return `<button class="${cls}" id="choice-modal-btn-${idx}">${choice.label}</button>`;
    }).join(' ');
    openModal(title, `
      <div style="display:grid;gap:12px">
        <div>${bodyHtml}</div>
        <div class="modal-footer" style="padding:0;margin-top:16px;justify-content:flex-end">
          ${buttons}
        </div>
      </div>`);
    setTimeout(() => {
      choices.forEach((choice, idx) => {
        const btn = document.getElementById(`choice-modal-btn-${idx}`);
        if (!btn) return;
        btn.addEventListener('click', () => {
          closeModal();
          resolve(choice.value);
        }, { once: true });
      });
    }, 0);
  });
}
function closeModal() {
  document.getElementById('global-modal').style.display = 'none';
  document.getElementById('modal-body').innerHTML = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
document.addEventListener('DOMContentLoaded', () => {
  const modal = document.getElementById('global-modal');
  if (!modal) return;
  modal.addEventListener('click', e => {
    if (e.target === modal) closeModal();
  });
});

// Format helpers
function fmt(v, dec = 0) {
  if (v == null || v === '') return '–';
  return Number(v).toFixed(dec);
}
function fmtDate(d) {
  if (!d) return '–';
  return d.split('T')[0];
}
function minToHHMM(m) {
  const h = Math.floor(m / 60);
  const mn = m % 60;
  return `${String(h).padStart(2,'0')}:${String(mn).padStart(2,'0')}`;
}

// Planner status badge
function plannerStatusBadge(s) {
  const map = {
    'NEEDS_REVIEW': ['badge-orange','Needs Review'],
    'UNPLANNED': ['badge-gray','Unplanned'],
    'COMPLETED': ['badge-green','Completed'],
    'PARTIALLY_PLANNED': ['badge-yellow','Partially Planned'],
    'PLANNED': ['badge-blue','Planned'],
  };
  const [cls, label] = map[s] || ['badge-gray', s];
  return `<span class="badge ${cls}">${label}</span>`;
}

// Warning chips
function warningChips(warnings) {
  if (!warnings || !warnings.length) return '';
  const map = {
    'OVERDUE': ['warn-overdue', 'Overdue'],
    'MATERIAL_PENDING': ['warn-material', 'Mat Pending'],
    'MATERIAL_SHORTAGE': ['warn-shortage', 'Shortage'],
    'NO_MACHINIST': ['warn-staff', 'No Machinist'],
    'UNPLANNED': ['warn-unplanned', 'Unplanned'],
    'PARTIALLY_PLANNED': ['warn-unplanned', 'Partial Plan'],
    'PLANNED': ['warn-unplanned', 'Planned'],
    'COMPLETION_REVIEW': ['warn-overdue', 'Review Needed'],
  };
  return warnings.map(w => {
    const [cls, label] = map[w] || ['warn-unplanned', w];
    return `<span class="warn-chip ${cls}">${label}</span>`;
  }).join(' ');
}

// Loading placeholder
function loadingEl() {
  return `<div class="loading"><div class="spinner"></div> Loading...</div>`;
}

// Confirm dialog
function confirm2(msg) {
  return window.confirm(msg);
}

// Debounce
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// Simple drag-and-drop for sortable lists
function makeSortable(container, onSort) {
  let dragged = null;
  container.querySelectorAll('[draggable]').forEach(el => {
    el.addEventListener('dragstart', () => { dragged = el; el.classList.add('dragging'); });
    el.addEventListener('dragend', () => { dragged = null; el.classList.remove('dragging'); if (onSort) onSort(); });
    el.addEventListener('dragover', e => {
      e.preventDefault();
      if (dragged && dragged !== el) {
        const rect = el.getBoundingClientRect();
        const mid = rect.top + rect.height / 2;
        if (e.clientY < mid) el.before(dragged);
        else el.after(dragged);
      }
    });
  });
}
