/* utils.js - shared helpers */

// Toast notifications
function toastDismissKey(msg, type) {
  return `toast.dismissed.${String(type || 'info').toLowerCase()}.${String(msg || '').trim()}`;
}

function toastReadDismissal(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (_err) {
    return null;
  }
}

function toastWriteDismissal(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_err) {
    return false;
  }
  return true;
}

function toast(msg, type = 'info', duration = 0, options = {}) {
  const tc = document.getElementById('toast-container');
  if (!tc) return null;

  const dismissible = options.dismissible !== false;
  const persistDismissal = options.persistDismissal !== false;
  const key = String(options.dismissKey || toastDismissKey(msg, type)).trim();

  if (persistDismissal && key && toastReadDismissal(key) === '1') {
    return null;
  }

  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.dataset.toastKey = key;

  const text = document.createElement('div');
  text.className = 'toast-message';
  text.textContent = msg;
  el.appendChild(text);

  if (dismissible) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toast-dismiss';
    btn.setAttribute('aria-label', 'Dismiss notification');
    btn.innerHTML = '&times;';
    btn.addEventListener('click', () => {
      if (persistDismissal && key) {
        toastWriteDismissal(key, '1');
      }
      el.classList.add('is-closing');
      setTimeout(() => el.remove(), 120);
    });
    el.appendChild(btn);
  }

  tc.appendChild(el);

  if (duration !== 0) {
    setTimeout(() => {
      if (!el.isConnected) return;
      el.classList.add('is-closing');
      setTimeout(() => el.remove(), 120);
    }, duration);
  }

  return el;
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

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

