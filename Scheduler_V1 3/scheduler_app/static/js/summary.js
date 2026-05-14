(function () {
  const TAB_DEFS = [
    { key: 'overview', label: 'Overview' },
    { key: 'machines', label: 'Machine Performance' },
    { key: 'heatmap', label: 'Capacity Heatmap' },
    { key: 'watchlist', label: 'Planner Watchlist' },
    { key: 'data-quality', label: 'Data Quality' },
    { key: 'ps-progress', label: 'PS / Order Progress' },
  ];

  const state = {
    tab: 'overview',
    range: 'week',
    from: '',
    to: '',
    machineType: 'all',
    weekKey: '',
    monthKey: '',
    weekMonthKey: '',
  };

  const ui = {
    openPanel: null,
    customOpen: false,
  };

  let summaryDataCache = null;
  let heatmapSelection = null;
  let loadToken = 0;

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function fmt(value, digits = 0) {
    if (value === null || value === undefined || value === '') return '—';
    const num = Number(value);
    return Number.isFinite(num) ? num.toFixed(digits) : '—';
  }

  function fmtHours(value) {
    return fmt(value, 1);
  }

  function fmtPct(value, digits = 1) {
    if (value === null || value === undefined || value === '') return '—';
    const num = Number(value);
    return Number.isFinite(num) ? `${num.toFixed(digits)}%` : '—';
  }

  function safeText(value) {
    return escapeHtml(value === null || value === undefined || value === '' ? '—' : String(value));
  }

  function todayISO() {
    const d = new Date();
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function isoDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function parseISODateLocal(value) {
    if (!value) return null;
    const text = String(value).trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
    const [year, month, day] = text.split('-').map(Number);
    const date = new Date(year, month - 1, day);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function addDaysISO(dateISO, delta) {
    const date = parseISODateLocal(dateISO);
    if (!date) return dateISO;
    date.setDate(date.getDate() + Number(delta || 0));
    return isoDate(date);
  }

  function firstDayOfCurrentMonthISO() {
    const now = new Date();
    return isoDate(new Date(now.getFullYear(), now.getMonth(), 1));
  }

  function lastDayOfCurrentMonthISO() {
    const now = new Date();
    return isoDate(new Date(now.getFullYear(), now.getMonth() + 1, 0));
  }

  function mondayOfCurrentIsoWeekISO() {
    const today = parseISODateLocal(todayISO()) || new Date();
    const day = today.getDay() || 7; // ISO: Monday=1 ... Sunday=7
    const monday = new Date(today);
    monday.setDate(today.getDate() - (day - 1));
    return isoDate(monday);
  }

  function saturdayOfCurrentIsoWeekISO() {
    const monday = parseISODateLocal(mondayOfCurrentIsoWeekISO());
    if (!monday) return todayISO();
    const saturday = new Date(monday);
    saturday.setDate(monday.getDate() + 5);
    return isoDate(saturday);
  }

  function normalizeRange(from, to) {
    const a = parseISODateLocal(from);
    const b = parseISODateLocal(to);
    if (!a || !b) return null;
    const start = a.getTime() <= b.getTime() ? a : b;
    const end = a.getTime() <= b.getTime() ? b : a;
    return { from: isoDate(start), to: isoDate(end) };
  }

  function startOfISOWeekISO(iso) {
    const date = parseISODateLocal(iso);
    if (!date) return null;
    const day = date.getDay() || 7;
    const monday = new Date(date);
    monday.setDate(date.getDate() - (day - 1));
    return isoDate(monday);
  }

  function mondayToSaturdayForISODate(iso) {
    const monday = startOfISOWeekISO(iso);
    if (!monday) return null;
    return {
      from: monday,
      to: addDaysISO(monday, 5),
    };
  }

  function isoWeekKey(iso) {
    const date = parseISODateLocal(iso);
    if (!date) return '';
    const work = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    work.setHours(0, 0, 0, 0);
    const day = work.getDay() || 7;
    work.setDate(work.getDate() + 4 - day);
    const isoYear = work.getFullYear();
    const yearStart = new Date(isoYear, 0, 1);
    const weekNo = Math.ceil((((work - yearStart) / 86400000) + 1) / 7);
    return `${isoYear}-W${String(weekNo).padStart(2, '0')}`;
  }

  function rangeForISOWeekKey(key) {
    const match = String(key || '').match(/^(\d{4})-W(\d{2})$/);
    if (!match) return null;
    const year = Number(match[1]);
    const week = Number(match[2]);
    if (!Number.isFinite(year) || !Number.isFinite(week) || week < 1) return null;
    const jan4 = new Date(year, 0, 4);
    const jan4Day = jan4.getDay() || 7;
    const week1Monday = new Date(jan4);
    week1Monday.setDate(jan4.getDate() - (jan4Day - 1));
    const monday = new Date(week1Monday);
    monday.setDate(week1Monday.getDate() + ((week - 1) * 7));
    return {
      from: isoDate(monday),
      to: addDaysISO(isoDate(monday), 5),
    };
  }

  function monthKeyForDate(iso) {
    const date = parseISODateLocal(iso);
    if (!date) return '';
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
  }

  function monthRange(monthKey) {
    const match = String(monthKey || '').match(/^(\d{4})-(\d{2})$/);
    if (!match) return null;
    const year = Number(match[1]);
    const monthIndex = Number(match[2]) - 1;
    if (!Number.isFinite(year) || !Number.isFinite(monthIndex)) return null;
    const start = new Date(year, monthIndex, 1);
    const end = new Date(year, monthIndex + 1, 0);
    return { from: isoDate(start), to: isoDate(end) };
  }

  function weeksForMonth(monthKey) {
    const match = String(monthKey || '').match(/^(\d{4})-(\d{2})$/);
    if (!match) return [];
    const year = Number(match[1]);
    const monthIndex = Number(match[2]) - 1;
    const firstOfMonth = new Date(year, monthIndex, 1);
    const lastOfMonth = new Date(year, monthIndex + 1, 0);
    const firstMonday = parseISODateLocal(startOfISOWeekISO(isoDate(firstOfMonth)));
    if (!firstMonday) return [];
    const monthStartTime = new Date(year, monthIndex, 1).getTime();
    const monthEndTime = new Date(year, monthIndex + 1, 0, 23, 59, 59, 999).getTime();
    const weeks = [];
    let monday = new Date(firstMonday);
    let index = 1;
    for (let guard = 0; guard < 8; guard += 1) {
      const from = isoDate(monday);
      const to = addDaysISO(from, 5);
      const fromDate = parseISODateLocal(from);
      const toDate = parseISODateLocal(to);
      if (!fromDate || !toDate) break;
      const overlap = toDate.getTime() >= monthStartTime && fromDate.getTime() <= monthEndTime;
      if (overlap) {
        weeks.push({
          label: `Week ${index} · ${from} → ${to}`,
          weekKey: isoWeekKey(from),
          from,
          to,
        });
        index += 1;
      }
      monday.setDate(monday.getDate() + 7);
      if (monday.getMonth() !== monthIndex && monday.getTime() > monthEndTime) break;
    }
    return weeks;
  }

  function rangeFromPreset(kind) {
    const today = todayISO();
    if (kind === 'week') return mondayToSaturdayForISODate(today) || { from: today, to: today };
    if (kind === 'month') return monthRange(monthKeyForDate(today)) || { from: firstDayOfCurrentMonthISO(), to: lastDayOfCurrentMonthISO() };
    return mondayToSaturdayForISODate(today) || { from: today, to: today };
  }

  function getRows(data) {
    return Array.isArray(data?.rows) ? data.rows : [];
  }

  function getTotals(data) {
    return data?.totals || {};
  }

  function getWatchlist(data) {
    return data?.watchlist || {};
  }

  function getDataQuality(data) {
    return data?.data_quality || {};
  }

  function getHeatmap(data) {
    return Array.isArray(data?.heatmap) ? data.heatmap : [];
  }

  function heatmapStatusClass(status) {
    const value = String(status || '').toUpperCase();
    if (value === 'NO_LOAD' || value === 'NO_CAPACITY') return 'summary-status-muted';
    if (value === 'UNDERLOADED') return 'summary-status-info';
    if (value === 'HEALTHY') return 'summary-status-ok';
    if (value === 'FULL') return 'summary-status-warn';
    if (value === 'CAPACITY_CONFLICT' || value === 'OVER_CAPACITY') return 'summary-status-low';
    return 'summary-status-low';
  }

  function heatmapCellFillColor(cell) {
    const status = String(cell?.status || '').toUpperCase();
    if (status === 'CAPACITY_CONFLICT' || status === 'OVER_CAPACITY') return 'rgba(239,68,68,0.84)';
    if (status === 'FULL') return 'rgba(245,158,11,0.78)';
    if (status === 'HEALTHY') return 'rgba(34,197,94,0.66)';
    if (status === 'UNDERLOADED') return 'rgba(59,130,246,0.62)';
    return 'rgba(100,116,139,0.42)';
  }

  function heatmapCellBg(cell) {
    const pct = Number(cell?.visible_load_pct ?? cell?.load_pct ?? cell?.utilization_pct ?? 0);
    const status = String(cell?.status || '').toUpperCase();
    if (status === 'CAPACITY_CONFLICT' || status === 'OVER_CAPACITY') return 'rgba(239,68,68,0.10)';
    if (status === 'FULL') return 'rgba(245,158,11,0.10)';
    if (status === 'HEALTHY') return 'rgba(34,197,94,0.08)';
    if (status === 'UNDERLOADED') return 'rgba(59,130,246,0.08)';
    if (status === 'NO_LOAD' || status === 'NO_CAPACITY') return 'rgba(100,116,139,0.10)';
    return pct >= 100 ? 'rgba(239,68,68,0.10)' : pct >= 90 ? 'rgba(245,158,11,0.10)' : pct >= 70 ? 'rgba(34,197,94,0.08)' : 'rgba(59,130,246,0.08)';
  }

  function heatmapCellPercentText(cell) {
    const raw = Number(cell?.raw_load_pct ?? cell?.load_pct ?? cell?.utilization_pct ?? 0);
    const visible = Number(cell?.visible_load_pct ?? cell?.load_pct ?? cell?.utilization_pct ?? 0);
    if (raw > 100) return `${Math.round(raw)}%`;
    return `${Math.round(Number.isFinite(visible) ? visible : 0)}%`;
  }

  function heatmapCellTitle(cell) {
    return [
      `Planned: ${fmt(Number(cell?.planned_minutes || 0), 0)} min / ${fmtHours(Number(cell?.planned_hours || 0))} hrs`,
      `Capacity: ${fmt(Number(cell?.capacity_minutes || 0), 0)} min / ${fmtHours(Number(cell?.available_hours || 0))} hrs`,
      `Setup: ${fmt(Number(cell?.setup_minutes || 0), 0)} min / ${fmtHours(Number(cell?.setup_hours || 0))} hrs`,
      `Production: ${fmt(Number(cell?.production_minutes || 0), 0)} min / ${fmtHours(Number(cell?.production_hours || 0))} hrs`,
      `Visible load: ${fmtPct(Number(cell?.visible_load_pct ?? cell?.load_pct ?? cell?.utilization_pct ?? 0))}`,
      `Raw load: ${fmtPct(Number(cell?.raw_load_pct ?? 0))}`,
      `Status: ${cell?.status_label || '—'}`,
    ].join(' · ');
  }

  function getProcessSheets(data) {
    return Array.isArray(data?.process_sheets) ? data.process_sheets : [];
  }

  function getMachineSnapshot(data) {
    const snapshot = data?.machine_snapshot || {};
    const rows = getRows(data);
    const candidates = rows.filter(row => Number(row.available_hours || 0) > 0);
    const fallback = candidates.length ? candidates : rows;
    const sortedDesc = [...fallback].sort((a, b) => Number(b.oee2_pct || 0) - Number(a.oee2_pct || 0));
    const sortedAsc = [...fallback].sort((a, b) => Number(a.oee2_pct || 0) - Number(b.oee2_pct || 0));
    return {
      highest_oee2: Array.isArray(snapshot.highest_oee2) && snapshot.highest_oee2.length ? snapshot.highest_oee2 : sortedDesc.slice(0, 3),
      lowest_oee2: Array.isArray(snapshot.lowest_oee2) && snapshot.lowest_oee2.length ? snapshot.lowest_oee2 : sortedAsc.slice(0, 3),
    };
  }

  function getDueProcessSheets(data) {
    const due = data?.due_process_sheets || {};
    const watchlist = getWatchlist(data);
    if (Object.prototype.hasOwnProperty.call(due, 'late') || Object.prototype.hasOwnProperty.call(due, 'due_soon')) {
      return {
        late: Array.isArray(due.late) ? due.late : [],
        due_soon: Array.isArray(due.due_soon) ? due.due_soon : [],
      };
    }
    return {
      late: Array.isArray(watchlist.late_ps) ? watchlist.late_ps : [],
      due_soon: Array.isArray(watchlist.due_soon_ps) ? watchlist.due_soon_ps : [],
    };
  }

  function getMachineTypes(data) {
    const rows = getRows(data);
    const fromPayload = Array.isArray(data?.machine_types) ? data.machine_types : [];
    const fromRows = rows.map(row => row.machine_category).filter(Boolean);
    return [...new Set([...fromPayload, ...fromRows].map(value => String(value || '').trim()).filter(Boolean))].sort();
  }

  function dashboardTotals(data) {
    const totals = getTotals(data);
    const rows = getRows(data);
    const watchlist = getWatchlist(data);
    const processSheets = getProcessSheets(data);
    const details = rows.flatMap(row => Array.isArray(row.details) ? row.details : []);

    const machineCount = Number(totals.machine_count ?? rows.length ?? 0) || 0;
    const calendarHours = Number(totals.calendar_hours ?? totals.total_calendar_hours ?? rows.reduce((sum, row) => sum + Number(row.calendar_hours || 0), 0)) || 0;
    const availableHours = Number(totals.available_hours ?? totals.total_available_hours ?? rows.reduce((sum, row) => sum + Number(row.available_hours || 0), 0)) || 0;
    const plannedHours = Number(totals.planned_hours ?? totals.total_planned_hours ?? rows.reduce((sum, row) => sum + Number(row.planned_hours || 0), 0)) || 0;
    const effectiveHours = Number(totals.effective_hours ?? totals.actual_used_hours ?? totals.total_actual_used_hours ?? rows.reduce((sum, row) => sum + Number(row.effective_hours || row.actual_used_hours || 0), 0)) || 0;
    const actualUsedHours = effectiveHours;
    const opsTotal = Number(totals.ops_total ?? details.length ?? 0) || 0;
    const opsFinished = Number(totals.ops_finished ?? details.filter(detail => !!detail.op_finished).length) || 0;
    const psTotal = Number(totals.ps_total ?? processSheets.length ?? 0) || 0;
    const psFinished = Number(totals.ps_finished ?? processSheets.filter(ps => !!ps.ps_finished).length) || 0;
    const latePsCount = Number(totals.late_ps_count ?? watchlist.late_ps?.length ?? 0) || 0;
    const atRiskPsCount = Number(totals.at_risk_ps_count ?? watchlist.due_soon_ps?.length ?? 0) || 0;
    const bottleneckMachine = totals.bottleneck_machine?.machine_code || totals.bottleneck_machine || rows[0]?.machine_code || '—';
    const nextAvailableMachine = totals.next_available_machine?.machine_code || totals.next_available_machine || rows[0]?.next_available || '—';
    const idleHours = Number(totals.idle_hours ?? rows.reduce((sum, row) => sum + Number(row.idle_hours || 0), 0)) || 0;
    const oee1 = Number(totals.oee1_pct ?? (calendarHours > 0 ? (actualUsedHours / calendarHours) * 100 : 0)) || 0;
    const oee2 = Number(totals.oee2_pct ?? (plannedHours > 0 ? (actualUsedHours / plannedHours) * 100 : 0)) || 0;
    const utilization = availableHours > 0 ? (plannedHours / availableHours) * 100 : 0;

    return {
      machineCount,
      calendarHours,
      availableHours,
      plannedHours,
      actualUsedHours,
      effectiveHours,
      idleHours,
      utilization,
      opsTotal,
      opsFinished,
      psTotal,
      psFinished,
      latePsCount,
      atRiskPsCount,
      bottleneckMachine,
      nextAvailableMachine,
      oee1,
      oee2,
      calculationInputs: totals.calculation_inputs || {
        calendar_hours: calendarHours,
        available_hours: availableHours,
        planned_hours: plannedHours,
        effective_hours: effectiveHours,
      },
    };
  }

  function operationStatusLabel(row) {
    const text = String(row?.execution_status || row?.planning_status || row?.status || '').trim().toUpperCase();
    if (!text) return '—';
    if (text === 'COMPLETED' || text === 'DONE') return 'Finished';
    if (text === 'IN_PROGRESS' || text === 'IN PROGRESS') return 'In progress';
    if (text === 'PLANNED') return 'Planned';
    if (text === 'NOT_STARTED' || text === 'NOT STARTED') return 'Not started';
    if (text === 'CANCELLED' || text === 'CANCELED') return 'Cancelled';
    return text.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, ch => ch.toUpperCase());
  }

  function processSheetStatusLabel(row) {
    const text = String(row?.ps_status || row?.ps_planner_status || row?.status || '').trim().toUpperCase();
    if (!text) return '—';
    if (text === 'COMPLETED' || text === 'DONE') return 'Finished';
    if (text === 'IN_PROGRESS' || text === 'IN PROGRESS') return 'In progress';
    if (text === 'PLANNED') return 'Planned';
    if (text === 'NOT_STARTED' || text === 'NOT STARTED') return 'Not started';
    if (text === 'CANCELLED' || text === 'CANCELED') return 'Cancelled';
    return text.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, ch => ch.toUpperCase());
  }

  function psVisibleParts(ps) {
    const backend = String(ps?.ps_id || '').trim();
    const baseLabel = String(ps?.display_ps_id || ps?.source_ps_id || '').trim() || backend.split('::')[0] || backend || '—';
    const partialLabel = String(ps?.display_partial_no || ps?.pp_partial_no || '').trim() || (backend.includes('::') ? backend.split('::').pop() : '1');
    return { baseLabel, partialLabel };
  }

  function dueVisibleParts(row) {
    const backend = String(row?.ps_id || '').trim();
    const baseLabel = String(row?.display_ps_id || row?.source_ps_id || '').trim() || backend.split('::')[0] || backend || '—';
    const partialLabel = String(row?.display_partial_no || row?.pp_partial_no || '').trim() || (backend.includes('::') ? backend.split('::').pop() : '1');
    return { baseLabel, partialLabel };
  }

  function displayOpText(value, fallbackName = '') {
    if (!value) return safeText(fallbackName || '—');
    if (typeof value === 'string') return safeText(value);
    if (typeof value === 'object') {
      return safeText(value.source_op_no || value.operation_name || fallbackName || '—');
    }
    return safeText(fallbackName || '—');
  }

  function displayMachineText(value, fallbackName = '') {
    if (!value) return safeText(fallbackName || '—');
    if (typeof value === 'string') return safeText(value);
    if (typeof value === 'object') {
      return safeText(value.machine_code || fallbackName || '—');
    }
    return safeText(fallbackName || '—');
  }

  function renderEmpty(message) {
    return `<div class="summary-empty"><p>${safeText(message)}</p></div>`;
  }

  function renderBanner(message, kind = 'loading') {
    const banner = $('summary-banner');
    if (!banner) return;
    banner.hidden = false;
    banner.className = `summary-status-banner ${kind ? `is-${kind}` : ''}`.trim();
    if (kind === 'loading') {
      banner.innerHTML = `<div class="spinner"></div><span>${safeText(message)}</span>`;
    } else {
      banner.innerHTML = `<span>${safeText(message)}</span>`;
    }
  }

  function clearBanner() {
    const banner = $('summary-banner');
    if (!banner) return;
    banner.className = 'summary-status-banner';
    banner.innerHTML = '';
    banner.hidden = true;
  }

  function setRangeHint() {
    const hint = $('range-hint');
    if (!hint) return;
    if (state.range === 'week') {
      const weekKey = state.weekKey || isoWeekKey(state.from);
      const weekNo = String(weekKey || '').includes('-W') ? Number(String(weekKey).split('-W')[1]) : null;
      hint.textContent = `Showing ISO Week ${Number.isFinite(weekNo) ? weekNo : '—'}: ${state.from} → ${state.to}`;
      return;
    }
    if (state.range === 'month') {
      const monthKey = state.monthKey || monthKeyForDate(state.from);
      const labelDate = monthRange(monthKey);
      const title = monthKey ? new Date(Number(monthKey.slice(0, 4)), Number(monthKey.slice(5, 7)) - 1, 1).toLocaleDateString('en', { month: 'long', year: 'numeric' }) : 'Month';
      hint.textContent = `Showing ${title}: ${state.from} → ${state.to}`;
      if (!labelDate) return;
      return;
    }
    hint.textContent = `Showing custom range: ${state.from} → ${state.to}`;
  }

  function setRangeDisplay() {
    const display = $('range-display');
    if (!display) return;
    display.hidden = true;
  }

  function setMachineTypeHint(options) {
    const hint = $('machine-type-hint');
    if (!hint) return;
    if (!options.length) {
      hint.textContent = 'No machine types available.';
      return;
    }
    hint.textContent = state.machineType === 'all'
      ? `${Math.max(0, options.length - 1)} machine types available.`
      : `Filtered to ${state.machineType}.`;
  }

  function syncRangeButtons() {
    document.querySelectorAll('[data-summary-range]').forEach(btn => {
      const mode = btn.dataset.summaryRange;
      const active = ui.openPanel ? mode === ui.openPanel : mode === state.range;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      btn.setAttribute('aria-expanded', ui.openPanel === mode ? 'true' : 'false');
    });
  }

  function syncTabButtons() {
    document.querySelectorAll('[data-summary-tab]').forEach(btn => {
      const active = btn.dataset.summaryTab === state.tab;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.setAttribute('tabindex', active ? '0' : '-1');
    });
    document.querySelectorAll('[data-summary-panel]').forEach(panel => {
      const active = panel.dataset.summaryPanel === state.tab;
      panel.hidden = !active;
      panel.classList.toggle('active', active);
    });
  }

  function updateUrl() {
    const params = new URLSearchParams();
    if (state.tab) params.set('tab', state.tab);
    if (state.range) params.set('range', state.range);
    if (state.from) params.set('from', state.from);
    if (state.to) params.set('to', state.to);
    if (state.range === 'week' && (state.weekKey || state.from)) {
      params.set('week', state.weekKey || isoWeekKey(state.from));
    }
    if (state.range === 'month' && (state.monthKey || state.from)) {
      params.set('month', state.monthKey || monthKeyForDate(state.from));
    }
    if (state.machineType && state.machineType !== 'all') params.set('category', state.machineType);
    const query = params.toString();
    history.replaceState(null, '', query ? `/summary?${query}` : '/summary');
  }

  function setTab(tab, update = true) {
    const next = TAB_DEFS.some(item => item.key === tab) ? tab : 'overview';
    state.tab = next;
    syncTabButtons();
    if (update) updateUrl();
  }

  function setCustomInputs(fromValue, toValue) {
    const fromInput = $('summary-custom-from');
    const toInput = $('summary-custom-to');
    if (fromInput) fromInput.value = fromValue || '';
    if (toInput) toInput.value = toValue || '';
  }

  function setWeekInputs(monthKey) {
    const input = $('summary-week-month');
    if (input) input.value = monthKey || '';
  }

  function setMonthInputs(monthKey) {
    const input = $('summary-month-input');
    if (input) input.value = monthKey || '';
  }

  function ensureRangePanelsPortal() {
    const portal = $('summary-custom-portal');
    ['summary-week-panel', 'summary-month-panel', 'summary-custom-panel'].forEach(id => {
      const panel = $(id);
      if (panel && portal && panel.parentElement !== portal) portal.appendChild(panel);
    });
  }

  function getPanelForMode(mode) {
    if (mode === 'week') return $('summary-week-panel');
    if (mode === 'month') return $('summary-month-panel');
    return $('summary-custom-panel');
  }

  function positionRangePanel(mode) {
    const panel = getPanelForMode(mode);
    const toggle = document.querySelector(`[data-summary-range="${mode}"]`);
    if (!panel || panel.hidden || !toggle) return;
    const anchor = toggle.getBoundingClientRect();
    const panelWidth = panel.offsetWidth || 380;
    const panelHeight = panel.offsetHeight || 280;
    const margin = 16;
    const left = Math.max(margin, Math.min(anchor.left, window.innerWidth - panelWidth - margin));
    const top = Math.max(margin, Math.min(anchor.bottom + 10, window.innerHeight - panelHeight - margin));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  }

  function renderWeekOptions(monthKey) {
    const host = $('summary-week-options');
    if (!host) return;
    const selectedMonthKey = monthKey || monthKeyForDate(todayISO());
    const options = weeksForMonth(selectedMonthKey);
    state.weekMonthKey = selectedMonthKey;
    if (!options.length) {
      host.innerHTML = '<div class="summary-empty"><p>No weeks available for this month.</p></div>';
      return;
    }
    host.innerHTML = options.map((option, index) => {
      const active = state.range === 'week' && state.weekKey === option.weekKey;
      return `
        <button type="button" class="summary-week-option ${active ? 'active' : ''}" data-summary-week-choice="${escapeHtml(option.weekKey)}" data-summary-week-from="${escapeHtml(option.from)}" data-summary-week-to="${escapeHtml(option.to)}" data-summary-week-month="${escapeHtml(selectedMonthKey)}">
          <strong>Week ${index + 1} · ${safeText(option.from)} → ${safeText(option.to)}</strong>
        </button>`;
    }).join('');
  }

  function openRangePopover(mode) {
    ensureRangePanelsPortal();
    ui.openPanel = mode;
    ui.customOpen = mode === 'custom';
    document.querySelectorAll('.summary-range-popover').forEach(panel => {
      const active = panel.id === `summary-${mode}-panel`;
      panel.hidden = !active;
      panel.setAttribute('aria-hidden', active ? 'false' : 'true');
      if (active) panel.setAttribute('tabindex', '-1');
      else panel.removeAttribute('tabindex');
    });
    if (mode === 'week') {
      const monthKey = state.weekMonthKey || monthKeyForDate(state.from) || monthKeyForDate(todayISO());
      setWeekInputs(monthKey);
      renderWeekOptions(monthKey);
    } else if (mode === 'month') {
      const monthKey = state.monthKey || monthKeyForDate(state.from) || monthKeyForDate(todayISO());
      setMonthInputs(monthKey);
    } else {
      const current = mondayToSaturdayForISODate(todayISO()) || rangeFromPreset('week');
      setCustomInputs(current.from, current.to);
    }
    syncRangeButtons();
    window.requestAnimationFrame(() => {
      positionRangePanel(mode);
      const focusTarget = mode === 'week' ? $('summary-week-month') : mode === 'month' ? $('summary-month-input') : $('summary-custom-from');
      focusTarget?.focus?.();
    });
  }

  function closeRangePopover() {
    ui.openPanel = null;
    ui.customOpen = false;
    document.querySelectorAll('.summary-range-popover').forEach(panel => {
      panel.hidden = true;
      panel.removeAttribute('tabindex');
      panel.setAttribute('aria-hidden', 'true');
    });
    syncRangeButtons();
  }

  function openCustomRangePanel() {
    openRangePopover('custom');
  }

  function closeCustomRangePanel() {
    closeRangePopover();
  }

  function applyCustomRange() {
    const fromValue = $('summary-custom-from')?.value || '';
    const toValue = $('summary-custom-to')?.value || '';
    const normalized = normalizeRange(fromValue, toValue);
    if (!normalized) {
      renderBanner('Choose both a From and To date.', 'error');
      return;
    }
    state.range = 'custom';
    state.from = normalized.from;
    state.to = normalized.to;
    state.weekKey = isoWeekKey(state.from);
    state.monthKey = monthKeyForDate(state.from);
    state.weekMonthKey = state.monthKey;
    closeRangePopover();
    syncRangeButtons();
    setRangeHint();
    setRangeDisplay();
    updateUrl();
    loadSummary(true);
  }

  function resetCustomRange() {
    const preset = rangeFromPreset('week');
    state.range = 'week';
    state.from = preset.from;
    state.to = preset.to;
    state.weekKey = isoWeekKey(state.from);
    state.weekMonthKey = monthKeyForDate(state.from);
    closeRangePopover();
    syncRangeButtons();
    setRangeHint();
    setRangeDisplay();
    updateUrl();
    loadSummary(true);
  }

  function changeRangeMode(kind) {
    if (kind === 'week' || kind === 'month' || kind === 'custom') {
      if (ui.openPanel === kind) closeRangePopover();
      else openRangePopover(kind);
    }
  }

  function setMachineType(kind) {
    state.machineType = kind || 'all';
    syncMachineTypeButtons(summaryDataCache);
    setMachineTypeHint(getMachineTypeOptions(summaryDataCache));
    updateUrl();
    loadSummary(true);
  }

  function getMachineTypeOptions(data) {
    const types = getMachineTypes(data);
    return [{ value: 'all', label: 'All Types' }, ...types.map(type => ({ value: type, label: type }))];
  }

  function syncMachineTypeButtons(data) {
    const wrap = $('machine-type-chips');
    if (!wrap) return;
    const options = getMachineTypeOptions(data);
    if (!options.some(item => item.value === state.machineType)) {
      state.machineType = 'all';
    }
    wrap.innerHTML = options.map(item => `
      <button type="button" class="chip-btn ${item.value === state.machineType ? 'active' : ''}" data-machine-type="${escapeHtml(item.value)}">${escapeHtml(item.label)}</button>
    `).join('');
    setMachineTypeHint(options);
  }

  function renderKpiCard(label, value, sub, extraClass = '') {
    return `
      <div class="summary-kpi ${extraClass}">
        <div class="label">${safeText(label)}</div>
        <div class="value">${safeText(value)}</div>
        ${sub ? `<div class="sub">${safeText(sub)}</div>` : ''}
      </div>`;
  }

  function renderOeeCard(title, pct, config = {}) {
    const pctValue = Number(pct || 0);
    const clampedPct = Math.max(0, Math.min(100, pctValue));
    const effective = Math.max(0, Number(config.effectiveHours || config.effective || 0));
    const calendar = Math.max(0, Number(config.calendarHours || config.calendar || 0));
    const planned = Math.max(0, Number(config.plannedHours || config.planned || 0));
    const formula = config.formula || '';
    const explanation = config.explanation || '';
    const rawTitle = `${title}: ${Number.isFinite(pctValue) ? pctValue.toFixed(1) : '0.0'}%`;
    const denominator = title === 'OEE1' ? calendar : planned;
    const numerator = effective;
    const ratio = denominator > 0 ? Math.max(0, Math.min(100, (numerator / denominator) * 100)) : 0;
    const remainder = Math.max(0, 100 - ratio);
    const miniRows = title === 'OEE1'
      ? [
          ['Effective Hrs', effective],
          ['Calendar Hrs', calendar],
          ['Unused Calendar Hrs', Math.max(0, calendar - effective)],
        ]
      : [
          ['Effective Hrs', effective],
          ['Planned Hrs', planned],
          ['Planned but not effective', Math.max(0, planned - effective)],
        ];
    const segments = title === 'OEE1'
      ? [
          { cls: 'summary-oee-fill', label: 'Effective', value: effective, size: Math.max(0.001, ratio) },
          { cls: 'summary-oee-gap', label: 'Unused calendar', value: Math.max(0, calendar - effective), size: Math.max(0.001, remainder) },
        ]
      : [
          { cls: 'summary-oee-fill', label: 'Effective', value: effective, size: Math.max(0.001, ratio) },
          { cls: 'summary-oee-gap', label: 'Planned but not effective', value: Math.max(0, planned - effective), size: Math.max(0.001, remainder) },
        ];
    return `
      <div class="summary-oee-card" title="${escapeHtml(rawTitle)}">
        <div class="summary-oee-head">
          <div>
            <div class="mini-label">${safeText(title)}</div>
            <div class="summary-oee-formula">${safeText(formula)}</div>
          </div>
          <div class="summary-oee-score">${safeText(fmtPct(clampedPct))}</div>
        </div>
        <div class="summary-oee-body">
          <div class="summary-oee-donut" style="--pct:${clampedPct}">
            <div class="summary-oee-donut-center">
              <strong>${safeText(Number.isFinite(clampedPct) ? clampedPct.toFixed(0) : '0')}%</strong>
              <span>${safeText(title)}</span>
            </div>
          </div>
          <div class="summary-oee-breakdown">
            <div class="summary-oee-bar summary-oee-bar--${safeText(title.toLowerCase())}">
              ${segments.map(item => `<span class="${item.cls}" style="flex:${Math.max(0.001, item.size)}" title="${safeText(item.label)}: ${safeText(fmtHours(item.value))}"></span>`).join('')}
            </div>
            <div class="summary-oee-metrics">
              ${miniRows.map(([label, value]) => `<div><span>${safeText(label)}</span><strong>${safeText(fmtHours(value))}</strong></div>`).join('')}
            </div>
            <div class="summary-oee-status">${safeText(explanation)}</div>
          </div>
        </div>
      </div>`;
  }

  function renderSnapshotRow(row) {
    return `
      <div class="summary-snapshot-item">
        <div class="summary-snapshot-title">
          <strong>${safeText(row.machine_code || 'Machine')}</strong>
          <span>${safeText(row.machine_category || '')}</span>
        </div>
        <div class="summary-snapshot-metrics">
          <div><span>OEE2</span><strong>${safeText(fmtPct(row.oee2_pct || 0))}</strong></div>
          <div><span>Calendar Hrs</span><strong>${safeText(fmtHours(row.calendar_hours || row.available_hours || 0))}</strong></div>
          <div><span>Planned Hrs</span><strong>${safeText(fmtHours(row.planned_hours || 0))}</strong></div>
          <div><span>Effective Hrs</span><strong>${safeText(fmtHours(row.effective_hours || row.actual_used_hours || 0))}</strong></div>
        </div>
        <div class="util-bar"><div class="util-fill" style="width:${Math.max(0, Math.min(100, Number(row.oee2_pct || 0)))}%;background:var(--accent)"></div></div>
      </div>`;
  }

  function renderDuePsCard(row) {
    const parts = dueVisibleParts(row);
    const statusClass = String(row.risk_label || '').toLowerCase() === 'late' ? 'summary-status-low' : 'summary-status-warn';
    return `
      <div class="summary-due-item">
        <div class="summary-due-head">
          <div>
            <strong>${safeText(parts.baseLabel)}</strong>
            <div class="mini-label">Partial ${safeText(parts.partialLabel)}</div>
          </div>
          <div class="${statusClass}">${safeText(row.risk_label || 'Due soon')}</div>
        </div>
        <div class="summary-due-grid">
          <div><span>Due</span><strong>${safeText(row.due_date || '—')}</strong></div>
          <div><span>Projected</span><strong>${safeText(row.projected_end_date || '—')}</strong></div>
          <div><span>Last Op</span><strong>${safeText(row.last_op_no || '—')}${row.last_op_name ? ` / ${safeText(row.last_op_name)}` : ''}</strong></div>
          <div><span>Last Op State</span><strong>${safeText(row.last_op_finished ? 'Finished' : (row.last_op_planned ? 'Planned' : 'Not planned'))}</strong></div>
          <div><span>Qty</span><strong>${safeText(`${fmt(row.finished_qty || 0, 0)} / ${fmt(row.total_qty || 0, 0)}`)}</strong></div>
          <div><span>Machine</span><strong>${displayMachineText(row.latest_machine, '—')}</strong></div>
          <div><span>PS State</span><strong>${safeText(row.ps_planning_state || row.planner_status || row.status || '—')}</strong></div>
          <div><span>Risk</span><strong>${safeText(row.risk_reason || row.risk_label || 'Due soon')}</strong></div>
        </div>
      </div>`;
  }

  function renderOverview(data) {
    const rows = getRows(data);
    const totals = dashboardTotals(data);
    const machineSnapshot = getMachineSnapshot(data);
    const dueSheets = getDueProcessSheets(data);
    const highest = Array.isArray(machineSnapshot.highest_oee2) ? machineSnapshot.highest_oee2 : [];
    const lowest = Array.isArray(machineSnapshot.lowest_oee2) ? machineSnapshot.lowest_oee2 : [];
    const late = Array.isArray(dueSheets.late) ? dueSheets.late : [];
    const soon = Array.isArray(dueSheets.due_soon) ? dueSheets.due_soon : [];

    if (!rows.length && !highest.length && !lowest.length && !late.length && !soon.length) {
      return renderEmpty('No summary data for this range.');
    }

    const oee1Pct = Number(totals.oee1 || totals.oee1_pct || 0);
    const oee2Pct = Number(totals.oee2 || totals.oee2_pct || 0);
    const calendarHours = Number(totals.calendarHours || totals.calendar_hours || totals.total_calendar_hours || 0);
    const plannedHours = Number(totals.plannedHours || totals.planned_hours || totals.total_planned_hours || 0);
    const effectiveHours = Number(totals.effectiveHours || totals.effective_hours || totals.actual_used_hours || totals.total_actual_used_hours || 0);
    const oeeInputs = totals.calculationInputs || {
      calendar_hours: calendarHours,
      planned_hours: plannedHours,
      effective_hours: effectiveHours,
    };

    return `
      <section class="summary-section">
        <div class="label section-label">Production Dashboard</div>
        <div class="summary-dashboard-grid summary-dashboard-grid--overview">
          ${renderKpiCard('Calendar Hrs', fmtHours(calendarHours), `${fmt(totals.machineCount || totals.machine_count || rows.length || 0, 0)} machines · 24/7 time in range`, 'hero')}
          ${renderKpiCard('Planned Hrs', fmtHours(plannedHours), 'Scheduled productive time')}
          ${renderKpiCard('Effective Hrs', fmtHours(effectiveHours), 'Actual/effective used hours')}
          ${renderOeeCard('OEE1', oee1Pct, { effectiveHours: oeeInputs.effective_hours ?? effectiveHours, calendarHours: oeeInputs.calendar_hours ?? calendarHours, formula: 'OEE1 = Effective Hrs / Calendar Hrs', explanation: 'Measures effective usage against 24/7 total available time.' })}
          ${renderOeeCard('OEE2', oee2Pct, { effectiveHours: oeeInputs.effective_hours ?? effectiveHours, plannedHours, formula: 'OEE2 = Effective Hrs / Planned Production Hrs', explanation: 'Measures effective usage against planned production time.' })}
        </div>
      </section>
      <section class="summary-section">
        <div class="label section-label">Machine Snapshot</div>
        <div class="summary-snapshot-grid">
          <div class="summary-snapshot-column">
            <div class="mini-label">Highest used</div>
            <div class="summary-list">
              ${highest.length ? highest.slice(0, 3).map(renderSnapshotRow).join('') : renderEmpty('No machines in range.')}
            </div>
          </div>
          <div class="summary-snapshot-column">
            <div class="mini-label">Lowest used</div>
            <div class="summary-list">
              ${lowest.length ? lowest.slice(0, 3).map(renderSnapshotRow).join('') : renderEmpty('No machines in range.')}
            </div>
          </div>
        </div>
      </section>
      <section class="summary-section">
        <div class="label section-label">Process Sheet Due Status</div>
        <div class="summary-due-grid-wrap">
          <div class="summary-due-column">
            <div class="mini-label">Late PS</div>
            <div class="summary-list">
              ${late.length ? late.map(renderDuePsCard).join('') : renderEmpty('No late process sheets processed in this range.')}
            </div>
          </div>
          <div class="summary-due-column">
            <div class="mini-label">Soon-to-be-due PS</div>
            <div class="summary-list">
              ${soon.length ? soon.map(renderDuePsCard).join('') : renderEmpty('No soon-to-be-due process sheets processed in this range.')}
            </div>
          </div>
        </div>
      </section>`;
  }

  function machineFocus(row) {
    const util = Number(row?.utilization_pct || 0);
    const idle = Number(row?.idle_hours || 0);
    const orderCompletion = Number(row?.order_completion_pct || 0);
    if (util >= 85) return { text: 'Near cap', cls: 'summary-status-warn' };
    if (util < 35) return { text: 'Underused', cls: 'summary-status-low' };
    if (idle > Number(row?.planned_hours || 0) * 0.3) return { text: 'Idle heavy', cls: 'summary-status-warn' };
    if (orderCompletion < 90) return { text: 'Behind target', cls: 'summary-status-warn' };
    return { text: 'Healthy', cls: 'summary-status-ok' };
  }

  function renderMachinePerformance(data) {
    const rows = getRows(data);
    if (!rows.length) return renderEmpty('No machine performance rows.');

    const machineRows = rows.map(row => {
      const focus = machineFocus(row);
      const oee1 = Math.max(0, Math.min(100, Number(row.oee1_pct || 0)));
      const oee2 = Math.max(0, Math.min(100, Number(row.oee2_pct || 0)));
      const detailId = `machine-details-${String(row.machine_code || '').replace(/[^a-zA-Z0-9_-]/g, '_')}`;
      const details = Array.isArray(row.details) ? row.details : [];
      return `
        <tr class="machine-main-row">
          <td class="machine-toggle">
            <button type="button" class="btn btn-ghost" data-machine-detail-toggle="${escapeHtml(detailId)}">Toggle</button>
            <strong style="margin-left:8px">${safeText(row.machine_code || '-')}</strong>
          </td>
          <td>${safeText(row.machine_category || '-')}</td>
          <td>${safeText(row.shift_profile || '-')}</td>
          <td style="text-align:right">${fmt(row.calendar_hours || row.available_hours || 0, 1)}</td>
          <td style="text-align:right">${fmt(row.planned_hours || 0, 1)}</td>
          <td style="text-align:right">${fmt(row.effective_hours || row.actual_used_hours || 0, 1)}</td>
          <td>
            <div class="summary-util-badge ${oee1 >= 90 ? 'summary-status-warn' : oee1 >= 70 ? 'summary-status-ok' : 'summary-status-low'}">${fmtPct(row.oee1_pct || 0)}</div>
            <div class="util-bar"><div class="util-fill" style="width:${oee1}%;background:var(--accent)"></div></div>
          </td>
          <td>
            <div class="summary-util-badge ${oee2 >= 90 ? 'summary-status-warn' : oee2 >= 70 ? 'summary-status-ok' : 'summary-status-low'}">${fmtPct(row.oee2_pct || 0)}</div>
            <div class="util-bar"><div class="util-fill" style="width:${oee2}%;background:${oee2 >= 85 ? 'var(--yellow)' : 'var(--accent)'}"></div></div>
          </td>
          <td style="text-align:right">${fmt(row.output_qty || 0, 0)}</td>
          <td style="text-align:right">${fmt(row.reject_qty || 0, 0)}</td>
          <td><span class="${focus.cls}">${safeText(focus.text)}</span></td>
        </tr>
        <tr class="machine-detail-row" id="${escapeHtml(detailId)}" hidden>
          <td colspan="11">
            <div class="machine-detail-panel">
              <table>
                <thead>
                  <tr>
                    <th>PS / Partial</th>
                    <th>Operation</th>
                    <th>Scheduled Time</th>
                    <th>End Time</th>
                    <th>Productive Minutes</th>
                    <th>Scheduled Qty</th>
                    <th>Output Qty</th>
                    <th>Reject Qty</th>
                    <th>Effective Hrs</th>
                    <th>Operation Status</th>
                    <th>PS Status</th>
                  </tr>
                </thead>
                <tbody>
                  ${details.length ? details.map(item => `
                    <tr>
                      <td>
                        <strong>${safeText(item.display_ps_id || item.source_ps_id || '—')}</strong>
                        <div style="font-size:11px;color:var(--text3)">${safeText(item.display_partial_no ? `Partial ${item.display_partial_no}` : '')}</div>
                      </td>
                      <td>${safeText(item.source_op_no || item.operation_name || '—')}</td>
                      <td>${safeText(item.start_datetime || item.start_time || '—')}</td>
                      <td>${safeText(item.end_datetime || item.end_time || '—')}</td>
                      <td style="text-align:right">${fmt(item.planned_hours || 0, 1)}</td>
                      <td style="text-align:right">${fmt(item.op_scheduled_qty || 0, 0)}</td>
                      <td style="text-align:right">${fmt(item.output_qty || 0, 0)}</td>
                      <td style="text-align:right">${fmt(item.reject_qty || 0, 0)}</td>
                      <td style="text-align:right">${fmtHours(item.actual_used_hours || 0)}</td>
                      <td>${safeText(operationStatusLabel(item))}</td>
                      <td>${safeText(processSheetStatusLabel(item))}</td>
                    </tr>`).join('') : '<tr><td colspan="11" style="color:var(--text3);padding:10px">No rows in range.</td></tr>'}
                </tbody>
              </table>
            </div>
          </td>
        </tr>`;
    }).join('');

    return `
      <section class="summary-section summary-table-card summary-machine-table">
        <div style="padding:16px 16px 8px"><div class="label">Machine Performance</div></div>
        <table>
          <thead>
            <tr>
              <th>Machine</th>
              <th>Category</th>
              <th>Shift</th>
              <th>Calendar Hrs</th>
              <th>Planned Hrs</th>
              <th>Effective Hrs</th>
              <th>OEE1</th>
              <th>OEE2</th>
              <th>Output</th>
              <th>Reject</th>
              <th>Focus</th>
            </tr>
          </thead>
          <tbody>${machineRows}</tbody>
        </table>
      </section>`;
  }

  function renderHeatmapSelection(data) {
    const host = $('summary-heatmap-detail');
    if (!host) return '';
    const heatmap = getHeatmap(data);
    if (!heatmapSelection) {
      const html = renderEmpty('Click a heatmap cell to show scheduled jobs.');
      host.innerHTML = html;
      return html;
    }
    const cell = heatmap.find(item => Number(item.machine_id || 0) === Number(heatmapSelection.machineId || 0) && String(item.plan_date || '') === String(heatmapSelection.planDate || ''));
    const rows = getRows(data);
    const machineRow = rows.find(row => Number(row.machine_id || 0) === Number(heatmapSelection.machineId || 0));
    const details = Array.isArray(cell?.details) ? cell.details : [];
    const machineName = cell?.machine_code || machineRow?.machine_code || '—';
    const html = `
      <div class="summary-panel summary-detail-panel">
        <div class="summary-mini-grid" style="margin-bottom:12px">
          <div>
            <span>Planned</span>
            <strong>${safeText(fmt(Number(cell?.planned_minutes || 0), 0))} min / ${safeText(fmtHours(Number(cell?.planned_hours || 0))) } hrs</strong>
          </div>
          <div>
            <span>Capacity</span>
            <strong>${safeText(fmt(Number(cell?.capacity_minutes || 0), 0))} min / ${safeText(fmtHours(Number(cell?.available_hours || 0)))} hrs</strong>
          </div>
          <div>
            <span>Setup</span>
            <strong>${safeText(fmt(Number(cell?.setup_minutes || 0), 0))} min / ${safeText(fmtHours(Number(cell?.setup_hours || 0)))} hrs</strong>
          </div>
          <div>
            <span>Production</span>
            <strong>${safeText(fmt(Number(cell?.production_minutes || 0), 0))} min / ${safeText(fmtHours(Number(cell?.production_hours || 0)))} hrs</strong>
          </div>
          <div>
            <span>Raw load</span>
            <strong>${safeText(fmtPct(Number(cell?.raw_load_pct ?? 0)))}</strong>
          </div>
          <div>
            <span>Status</span>
            <strong class="${heatmapStatusClass(cell?.status)}">${safeText(cell?.status_label || '—')}</strong>
          </div>
        </div>
        <table>
          <thead>
            <tr><th colspan="11">Jobs for ${safeText(machineName)} on ${safeText(heatmapSelection.planDate)}</th></tr>
            <tr>
              <th>PS / Partial</th>
              <th>Operation</th>
              <th>Scheduled Time</th>
              <th>End Time</th>
              <th>Productive Minutes</th>
              <th>Scheduled Qty</th>
              <th>Output Qty</th>
              <th>Reject Qty</th>
              <th>Effective Hrs</th>
              <th>Operation Status</th>
              <th>PS Status</th>
            </tr>
          </thead>
          <tbody>
            ${details.length ? details.map(item => `
              <tr>
                <td>
                  <strong>${safeText(item.display_ps_id || item.source_ps_id || '—')}</strong>
                  <div style="font-size:11px;color:var(--text3)">${safeText(item.display_partial_no ? `Partial ${item.display_partial_no}` : '')}</div>
                </td>
                <td>${safeText(item.source_op_no || item.operation_name || '—')}</td>
                <td>${safeText(item.start_datetime || item.start_time || '—')}</td>
                <td>${safeText(item.end_datetime || item.end_time || '—')}</td>
                <td style="text-align:right">${fmt(item.planned_hours || 0, 1)}</td>
                <td style="text-align:right">${fmt(item.op_scheduled_qty || 0, 0)}</td>
                <td style="text-align:right">${fmt(item.output_qty || 0, 0)}</td>
                <td style="text-align:right">${fmt(item.reject_qty || 0, 0)}</td>
                <td style="text-align:right">${fmtHours(item.actual_used_hours || 0)}</td>
                <td>${safeText(operationStatusLabel(item))}</td>
                <td>${safeText(processSheetStatusLabel(item))}</td>
              </tr>
            `).join('') : '<tr><td colspan="11" style="color:var(--text3);padding:10px">No scheduled jobs for this cell.</td></tr>'}
          </tbody>
        </table>
      </div>`;
    host.innerHTML = html;
    return html;
  }

  function renderHeatmap(data) {
    const rows = getRows(data);
    const heatmap = getHeatmap(data);
    const dateCols = Array.isArray(data?.dates) && data.dates.length
      ? data.dates
      : [...new Set(heatmap.map(item => item.plan_date).filter(Boolean))].sort();

    if (!rows.length || !dateCols.length || !heatmap.length) {
      return renderEmpty('No heatmap data.');
    }

    return `
      <section class="summary-section">
        <div class="label section-label">Capacity Heatmap</div>
        <div class="summary-heatmap-wrap">
          <table class="summary-heatmap">
            <thead>
              <tr>
                <th>Machine</th>
                ${dateCols.map(day => `<th>${safeText(String(day).slice(5))}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${rows.map(row => `
                <tr>
                  <td>
                    <strong>${safeText(row.machine_code)}</strong>
                    <div style="font-size:11px;color:var(--text3)">${safeText(row.machine_category)} · ${safeText(row.shift_profile)}</div>
                  </td>
                  ${dateCols.map(day => {
                    const cell = heatmap.find(item => Number(item.machine_id || 0) === Number(row.machine_id || 0) && String(item.plan_date || '') === String(day));
                    const pct = Number((cell?.visible_load_pct ?? cell?.load_pct ?? cell?.utilization_pct) || 0);
                    const rawPct = Number((cell?.raw_load_pct ?? cell?.load_pct ?? cell?.utilization_pct) || 0);
                    const bg = heatmapCellBg(cell);
                    const fill = heatmapCellFillColor(cell);
                    const text = heatmapCellPercentText(cell);
                    const title = heatmapCellTitle(cell);
                    const statusLabel = cell?.status_label || '—';
                    return `
                      <td style="text-align:center">
                        <button type="button" class="summary-heatmap-cell-btn" data-heatmap-machine="${escapeHtml(row.machine_id)}" data-heatmap-date="${escapeHtml(day)}" title="${escapeHtml(title)}" style="background:${bg}">
                          <div class="summary-heatmap-cell-fill" style="width:${Math.max(0, Math.min(100, pct))}%;background:${fill}"></div>
                          <div class="summary-heatmap-cell-content">
                            <div class="pct ${rawPct > 100 || String(cell?.status || '').toUpperCase() === 'CAPACITY_CONFLICT' ? 'is-over' : ''}">${safeText(text)}</div>
                            <div class="meta">${safeText(cell ? `${cell.ps_count} PS · ${cell.block_count || cell.ops_count || 0} blocks` : '—')}</div>
                            <div class="meta">${safeText(statusLabel)}</div>
                          </div>
                        </button>
                      </td>`;
                  }).join('')}
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
        <div class="summary-filter-hint" style="margin-top:8px">Load = planned setup + production minutes for this machine/day ÷ available capacity minutes for this machine/day.</div>
        <div id="summary-heatmap-detail" class="summary-detail-panel"></div>
      </section>`;
  }

  function renderListGroup(title, items, renderItem, emptyMessage) {
    const list = Array.isArray(items) ? items : [];
    return `
      <div class="summary-list-card">
        <div class="mini-label">${safeText(title)}</div>
        <div class="summary-list">
          ${list.length ? list.map(renderItem).join('') : `<div class="summary-empty"><p>${safeText(emptyMessage)}</p></div>`}
        </div>
      </div>`;
  }

  function renderWatchlist(data) {
    const watchlist = getWatchlist(data);
    const late = watchlist.late_ps || [];
    const dueSoon = watchlist.due_soon_ps || [];
    const over = watchlist.over_capacity_machines || [];
    const under = watchlist.underused_machines || [];
    const missing = watchlist.missing_actuals || [];
    const notPlanned = watchlist.not_fully_planned_ps || [];

    if (!late.length && !dueSoon.length && !over.length && !under.length && !missing.length && !notPlanned.length) {
      return renderEmpty('No watchlist items.');
    }

    return `
      <section class="summary-section summary-panel" style="padding:16px;border-radius:24px">
        <div class="label section-label">Planner Watchlist</div>
        <div class="summary-watch-grid">
          ${renderListGroup('Late PS', late, item => `
            <div class="summary-list-item">
              <div>
                <div class="item-title">${safeText(item.display_ps_id || item.source_ps_id || item.ps_id || 'PS')}</div>
                <div class="item-meta">${safeText(item.due_date || '—')}</div>
              </div>
              <div class="item-value">${safeText(item.risk_label || 'Late')}</div>
            </div>`, 'No late process sheets.')}
          ${renderListGroup('Due Soon', dueSoon, item => `
            <div class="summary-list-item">
              <div>
                <div class="item-title">${safeText(item.display_ps_id || item.source_ps_id || item.ps_id || 'PS')}</div>
                <div class="item-meta">${safeText(item.due_date || '—')}</div>
              </div>
              <div class="item-value">${safeText(item.risk_label || 'Due soon')}</div>
            </div>`, 'No due-soon process sheets.')}
          ${renderListGroup('Machines Over Capacity', over, item => `
            <div class="summary-list-item">
              <div>
                <div class="item-title">${safeText(item.machine_code || 'Machine')}</div>
                <div class="item-meta">${safeText(item.machine_category || '')}</div>
              </div>
              <div class="item-value">${safeText(fmtPct(item.utilization_pct || 0))}</div>
            </div>`, 'No over-capacity machines.')}
          ${renderListGroup('Machines Underused', under, item => `
            <div class="summary-list-item">
              <div>
                <div class="item-title">${safeText(item.machine_code || 'Machine')}</div>
                <div class="item-meta">${safeText(item.machine_category || '')}</div>
              </div>
              <div class="item-value">${safeText(fmtPct(item.utilization_pct || 0))}</div>
            </div>`, 'No underused machines.')}
          ${renderListGroup('Missing Actuals', missing, item => `
            <div class="summary-list-item">
              <div>
                <div class="item-title">${safeText(item.display_ps_id || item.source_ps_id || item.machine_code || 'PS')}</div>
                <div class="item-meta">${safeText(item.plan_date || '—')}</div>
              </div>
              <div class="item-value">${safeText(item.risk_label || 'Missing actuals')}</div>
            </div>`, 'No missing-actual rows.')}
          ${renderListGroup('Not Fully Planned PS', notPlanned, item => `
            <div class="summary-list-item">
              <div>
                <div class="item-title">${safeText(item.display_ps_id || item.source_ps_id || item.ps_id || 'PS')}</div>
                <div class="item-meta">${safeText(item.due_date || '—')}</div>
              </div>
              <div class="item-value">${safeText(item.risk_label || 'Not fully planned')}</div>
            </div>`, 'No not-fully-planned process sheets.')}
        </div>
      </section>`;
  }

  function renderDataQuality(data) {
    const dq = getDataQuality(data);
    const cards = [
      ['Check cycle time', dq.cycle_time_high_variance],
      ['Actual far below plan', dq.cycle_time_low_variance],
      ['Output over scheduled', dq.output_over_scheduled],
      ['Output under scheduled', dq.output_under_scheduled],
      ['Missing cycle time', dq.missing_cycle_time],
      ['Missing setup time', dq.missing_setup_time],
    ];

    const hasAny = cards.some(([, items]) => Array.isArray(items) && items.length);
    if (!hasAny) {
      return renderEmpty('No data-quality issues.');
    }

    return `
      <section class="summary-section summary-panel" style="padding:16px;border-radius:24px">
        <div class="label section-label">Data Quality</div>
        <div class="summary-watch-grid">
          ${cards.map(([title, items]) => renderListGroup(title, items, item => `
            <div class="summary-list-item">
              <div>
                <div class="item-title">${safeText(item.display_ps_id || item.source_ps_id || item.machine_code || 'Op')}</div>
                <div class="item-meta">${safeText(item.source_op_no || item.operation_name || item.plan_date || '—')}</div>
              </div>
              <div class="item-value">${safeText(item.risk_label || item.flag || '')}</div>
            </div>`, 'No items.')).join('')}
        </div>
      </section>`;
  }

  function renderPsProgress(data) {
    const processSheets = getProcessSheets(data);
    if (!processSheets.length) {
      return renderEmpty('No process sheets in range.');
    }

    const rows = [...processSheets].sort((a, b) => {
      const aFinished = a.ps_finished ? 1 : 0;
      const bFinished = b.ps_finished ? 1 : 0;
      if (aFinished !== bFinished) return aFinished - bFinished;
      const aDue = String(a.due_date || '');
      const bDue = String(b.due_date || '');
      return aDue.localeCompare(bDue);
    });

    return `
      <section class="summary-section summary-table-card">
        <div style="padding:16px 16px 8px"><div class="label">PS / Order Progress</div></div>
        <table>
          <thead>
            <tr>
              <th>PS</th>
              <th>Due Date</th>
              <th>Finished Qty / Total Qty</th>
              <th>Completion %</th>
              <th>Planned Qty / Total Qty</th>
              <th>Current Op</th>
              <th>Next Op</th>
              <th>Current Machine</th>
              <th>Planned Finish</th>
              <th>Risk Label</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(ps => {
              const parts = psVisibleParts(ps);
              return `
                <tr>
                  <td>
                    <strong>${safeText(parts.baseLabel)}</strong>
                    <div style="font-size:11px;color:var(--text3)">Partial ${safeText(parts.partialLabel)}</div>
                  </td>
                  <td>${safeText(ps.due_date || '—')}</td>
                  <td style="text-align:right">${fmt(ps.finished_qty || 0, 0)} / ${fmt(ps.total_qty || 0, 0)}</td>
                  <td style="text-align:right">${ps.total_qty ? fmtPct((Number(ps.finished_qty || 0) / Number(ps.total_qty || 0)) * 100) : '—'}</td>
                  <td style="text-align:right">${fmt(ps.planned_qty || 0, 0)} / ${fmt(ps.total_qty || 0, 0)}</td>
                  <td>${safeText(ps.current_op?.source_op_no || ps.current_op?.operation_name || '—')}</td>
                  <td>${safeText(ps.next_op?.source_op_no || ps.next_op?.operation_name || '—')}</td>
                  <td>${safeText(ps.current_machine || '—')}</td>
                  <td>${safeText(ps.planned_finish || '—')}</td>
                  <td><span class="${String(ps.risk_label || '').toLowerCase() === 'late' ? 'summary-status-low' : String(ps.risk_label || '').toLowerCase() === 'due soon' ? 'summary-status-warn' : 'summary-status-ok'}">${safeText(ps.risk_label || 'On track')}</span></td>
                </tr>`;
            }).join('')}
          </tbody>
        </table>
      </section>`;
  }

  function renderHeatmap(data) {
    const rows = getRows(data);
    const heatmap = getHeatmap(data);
    const dateCols = Array.isArray(data?.dates) && data.dates.length
      ? data.dates
      : [...new Set(heatmap.map(item => item.plan_date).filter(Boolean))].sort();

    if (!rows.length || !dateCols.length || !heatmap.length) {
      return renderEmpty('No heatmap data.');
    }

    return `
      <section class="summary-section">
        <div class="label section-label">Capacity Heatmap</div>
        <div class="summary-heatmap-wrap">
          <table class="summary-heatmap">
            <thead>
              <tr>
                <th>Machine</th>
                ${dateCols.map(day => `<th>${safeText(String(day).slice(5))}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${rows.map(row => `
                <tr>
                  <td>
                    <strong>${safeText(row.machine_code)}</strong>
                    <div style="font-size:11px;color:var(--text3)">${safeText(row.machine_category)} · ${safeText(row.shift_profile)}</div>
                  </td>
                  ${dateCols.map(day => {
                    const cell = heatmap.find(item => Number(item.machine_id || 0) === Number(row.machine_id || 0) && String(item.plan_date || '') === String(day));
                    const pct = Number(cell?.utilization_pct || 0);
                    const bg = pct >= 100 ? 'rgba(239,68,68,0.28)' : pct >= 90 ? 'rgba(245,158,11,0.25)' : pct >= 70 ? 'rgba(59,130,246,0.18)' : 'rgba(34,197,94,0.10)';
                    const title = `Planned ${fmtHours(cell?.planned_hours || 0)}h / Available ${fmtHours(cell?.available_hours || 0)}h / Util ${fmtPct(pct)}`;
                    return `
                      <td style="text-align:center">
                        <button type="button" class="summary-heatmap-cell-btn" data-heatmap-machine="${escapeHtml(row.machine_id)}" data-heatmap-date="${escapeHtml(day)}" title="${escapeHtml(title)}" style="background:${bg}">
                          <div class="pct">${Number.isFinite(pct) ? pct.toFixed(0) : '0'}%</div>
                          <div class="meta">${safeText(cell ? `${cell.ps_count} PS · ${cell.ops_count} ops` : '—')}</div>
                        </button>
                      </td>`;
                  }).join('')}
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
        <div id="summary-heatmap-detail" class="summary-detail-panel"></div>
      </section>`;
  }

  function renderPanelContent(data) {
    const tabs = {
      overview: renderOverview(data),
      machines: renderMachinePerformance(data),
      heatmap: renderHeatmap(data),
      watchlist: renderWatchlist(data),
      'data-quality': renderDataQuality(data),
      'ps-progress': renderPsProgress(data),
    };
    TAB_DEFS.forEach(tab => {
      const panel = $(`summary-tab-${tab.key}`);
      if (panel) panel.innerHTML = tabs[tab.key] || renderEmpty('No summary data for this range.');
    });
    syncTabButtons();
    renderHeatmapSelection(data);
  }

  function showSummaryFatalError(message) {
    renderBanner(message || 'Summary failed to load.', 'error');
    TAB_DEFS.forEach(tab => {
      const panel = $(`summary-tab-${tab.key}`);
      if (panel) panel.innerHTML = renderEmpty(message || 'Summary failed to load.');
    });
  }

  async function apiGetJSON(path) {
    const response = await fetch(path, {
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
      },
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const text = await response.text();
    return text ? JSON.parse(text) : {};
  }

  function normalizeLoadStateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const urlTab = TAB_DEFS.some(item => item.key === params.get('tab')) ? params.get('tab') : 'overview';
    const urlRange = params.get('range') || 'week';
    const urlCategory = params.get('category') || 'all';
    const urlFrom = params.get('from');
    const urlTo = params.get('to');
    const urlWeek = params.get('week') || '';
    const urlMonth = params.get('month') || '';

    let range = ['week', 'month', 'custom'].includes(urlRange) ? urlRange : 'week';
    let from = '';
    let to = '';
    let weekKey = '';
    let monthKey = '';

    if (urlFrom && urlTo) {
      const normalized = normalizeRange(urlFrom, urlTo);
      if (normalized) {
        from = normalized.from;
        to = normalized.to;
      }
    }

    if (range === 'week') {
      const weekRange = (!from || !to)
        ? (urlWeek ? rangeForISOWeekKey(urlWeek) : rangeFromPreset('week'))
        : null;
      if (!from || !to) {
        from = weekRange?.from || rangeFromPreset('week').from;
        to = weekRange?.to || rangeFromPreset('week').to;
      }
      weekKey = urlWeek || isoWeekKey(from);
      monthKey = monthKeyForDate(from);
    } else if (range === 'month') {
      const monthRangeValue = (!from || !to)
        ? (urlMonth ? monthRange(urlMonth) : monthRange(monthKeyForDate(todayISO())))
        : null;
      if (!from || !to) {
        from = monthRangeValue?.from || firstDayOfCurrentMonthISO();
        to = monthRangeValue?.to || lastDayOfCurrentMonthISO();
      }
      monthKey = urlMonth || monthKeyForDate(from);
      weekKey = isoWeekKey(from);
    } else if (range === 'custom') {
      if (!from || !to) {
        const fallback = rangeFromPreset('week');
        from = fallback.from;
        to = fallback.to;
        range = 'week';
      }
      weekKey = isoWeekKey(from);
      monthKey = monthKeyForDate(from);
    }

    state.tab = urlTab;
    state.range = range;
    state.from = from;
    state.to = to;
    state.machineType = urlCategory || 'all';
    state.weekKey = weekKey;
    state.monthKey = monthKey;
    state.weekMonthKey = monthKeyForDate(from);
  }

  function updateCustomPanelVisibility() {
    ensureRangePanelsPortal();
    ['summary-week-panel', 'summary-month-panel', 'summary-custom-panel'].forEach(id => {
      const panel = $(id);
      const active = ui.openPanel && id === `summary-${ui.openPanel}-panel`;
      if (panel) {
        panel.hidden = !active;
        panel.setAttribute('aria-hidden', active ? 'false' : 'true');
      }
    });
    if (ui.openPanel === 'week') {
      const monthKey = state.weekMonthKey || monthKeyForDate(state.from) || monthKeyForDate(todayISO());
      setWeekInputs(monthKey);
      renderWeekOptions(monthKey);
    } else if (ui.openPanel === 'month') {
      const monthKey = state.monthKey || monthKeyForDate(state.from) || monthKeyForDate(todayISO());
      setMonthInputs(monthKey);
    } else if (ui.openPanel === 'custom') {
      const source = mondayToSaturdayForISODate(todayISO()) || rangeFromPreset('week');
      setCustomInputs(source.from, source.to);
    }
    syncRangeButtons();
  }

  async function loadSummary(updateHistory = true) {
    if (!state.from || !state.to) {
      const fallback = rangeFromPreset('week');
      state.from = fallback.from;
      state.to = fallback.to;
      state.range = 'week';
    }
    const token = ++loadToken;
    renderBanner('Loading summary…', 'loading');

    const params = new URLSearchParams({
      view: 'machines',
      from: state.from,
      to: state.to,
    });
    if (state.machineType && state.machineType !== 'all') {
      params.set('category', state.machineType);
    }

    try {
      const data = await apiGetJSON(`/api/trial/summary?${params.toString()}`);
      if (token !== loadToken) return;
      summaryDataCache = data || {};
      heatmapSelection = null;
      syncMachineTypeButtons(summaryDataCache);
      setRangeHint();
      setRangeDisplay();
      renderPanelContent(summaryDataCache);
      updateCustomPanelVisibility();
      clearBanner();
      if (updateHistory) updateUrl();
    } catch (err) {
      if (token !== loadToken) return;
      console.error('[Summary load failed]', err);
      showSummaryFatalError(`Failed to load summary: ${err && err.message ? err.message : String(err)}`);
    }
  }

  function toggleMachineDetail(detailId) {
    const row = $(detailId);
    if (!row) return;
    row.hidden = !row.hidden;
  }

  function showHeatmapSelection(machineId, planDate) {
    heatmapSelection = {
      machineId: Number(machineId || 0),
      planDate: String(planDate || ''),
    };
    if (summaryDataCache) renderHeatmapSelection(summaryDataCache);
  }

  function bindEvents() {
    document.addEventListener('click', event => {
      const tabBtn = event.target.closest('[data-summary-tab]');
      if (tabBtn) {
        event.preventDefault();
        closeRangePopover();
        setTab(tabBtn.dataset.summaryTab, true);
        return;
      }

      const rangeBtn = event.target.closest('[data-summary-range]');
      if (rangeBtn) {
        event.preventDefault();
        changeRangeMode(rangeBtn.dataset.summaryRange);
        return;
      }

      const weekChoice = event.target.closest('[data-summary-week-choice]');
      if (weekChoice) {
        event.preventDefault();
        state.range = 'week';
        state.from = weekChoice.dataset.summaryWeekFrom || state.from;
        state.to = weekChoice.dataset.summaryWeekTo || state.to;
        state.weekKey = weekChoice.dataset.summaryWeekChoice || isoWeekKey(state.from);
        state.monthKey = weekChoice.dataset.summaryWeekMonth || monthKeyForDate(state.from);
        state.weekMonthKey = state.monthKey;
        closeRangePopover();
        syncRangeButtons();
        setRangeHint();
        setRangeDisplay();
        updateUrl();
        loadSummary(true);
        return;
      }

      const monthApply = event.target.closest('[data-summary-month-apply]');
      if (monthApply) {
        event.preventDefault();
        const monthValue = $('summary-month-input')?.value || '';
        const normalized = monthRange(monthValue);
        if (!normalized) {
          renderBanner('Choose a valid month.', 'error');
          return;
        }
        state.range = 'month';
        state.from = normalized.from;
        state.to = normalized.to;
        state.monthKey = monthValue;
        state.weekKey = isoWeekKey(state.from);
        state.weekMonthKey = monthValue;
        closeRangePopover();
        syncRangeButtons();
        setRangeHint();
        setRangeDisplay();
        updateUrl();
        loadSummary(true);
        return;
      }

      const weekMonthInput = event.target.closest('#summary-week-month');
      if (weekMonthInput) {
        return;
      }

      const customApply = event.target.closest('[data-summary-custom-apply]');
      if (customApply) {
        event.preventDefault();
        applyCustomRange();
        return;
      }

      const customCancel = event.target.closest('[data-summary-custom-cancel]');
      if (customCancel) {
        event.preventDefault();
        closeRangePopover();
        return;
      }

      const machineBtn = event.target.closest('[data-machine-type]');
      if (machineBtn) {
        event.preventDefault();
        closeRangePopover();
        setMachineType(machineBtn.dataset.machineType);
        return;
      }

      const heatmapCell = event.target.closest('[data-heatmap-machine][data-heatmap-date]');
      if (heatmapCell) {
        event.preventDefault();
        closeRangePopover();
        showHeatmapSelection(heatmapCell.dataset.heatmapMachine, heatmapCell.dataset.heatmapDate);
        return;
      }

      const detailToggle = event.target.closest('[data-machine-detail-toggle]');
      if (detailToggle) {
        event.preventDefault();
        closeRangePopover();
        toggleMachineDetail(detailToggle.dataset.machineDetailToggle);
      }

      if (ui.openPanel && !event.target.closest('.summary-range-popover, [data-summary-range]')) {
        closeRangePopover();
      }
    });

    document.addEventListener('change', event => {
      if (event.target && event.target.id === 'summary-week-month' && ui.openPanel === 'week') {
        renderWeekOptions(event.target.value || monthKeyForDate(todayISO()));
        positionRangePanel('week');
      }
    });

    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && ui.openPanel) {
        event.preventDefault();
        const previousPanel = ui.openPanel;
        closeRangePopover();
        document.querySelector(`[data-summary-range="${previousPanel}"]`)?.focus?.();
      }
    });

    window.addEventListener('resize', () => {
      if (ui.openPanel) positionRangePanel(ui.openPanel);
    });

    window.addEventListener('scroll', () => {
      if (ui.openPanel) positionRangePanel(ui.openPanel);
    }, { passive: true });
  }

  function exposeGlobals() {
    window.changeRangeMode = changeRangeMode;
    window.setMachineType = setMachineType;
    window.setSummaryTab = setTab;
    window.showHeatmapSelection = showHeatmapSelection;
    window.toggleMachineDetail = toggleMachineDetail;
  }

  async function init() {
    ensureRangePanelsPortal();
    normalizeLoadStateFromUrl();
    syncRangeButtons();
    setRangeHint();
    setRangeDisplay();
    syncMachineTypeButtons(summaryDataCache);
    syncTabButtons();
    updateCustomPanelVisibility();
    bindEvents();
    exposeGlobals();
    await loadSummary(true);
  }

  window.addEventListener('error', event => {
    console.error('[Summary fatal error]', event.error || event.message || event);
    showSummaryFatalError(event.error && event.error.message ? event.error.message : 'Summary failed to render.');
  });

  window.addEventListener('unhandledrejection', event => {
    console.error('[Summary unhandled rejection]', event.reason);
    const message = event.reason && event.reason.message ? event.reason.message : 'Summary failed to load.';
    showSummaryFatalError(message);
  });

  init();
})();
