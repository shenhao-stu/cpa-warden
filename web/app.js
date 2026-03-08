/* ============================================================
   CPA Warden – Web UI Application
   Pure vanilla JS, no build step, GitHub Pages compatible.
   ============================================================ */

(function () {
  'use strict';

  // --------------- State ---------------
  const state = {
    baseUrl: '',
    token: '',
    files: [],
    filtered: [],
    selected: new Set(),
    page: 1,
    pageSize: 50,
    sortKey: 'name',
    sortDir: 'asc',
    search: '',
    filterType: '',
    filterProvider: '',
    filterStatus: '',
  };

  const STORAGE_KEY = 'cpa_warden_connections';

  // --------------- DOM refs ---------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // --------------- Theme ---------------
  function initTheme() {
    const saved = localStorage.getItem('cpa_warden_theme');
    if (saved === 'dark' || (!saved && matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.setAttribute('data-theme', 'dark');
    }
    updateThemeIcon();
  }

  function toggleTheme() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
    localStorage.setItem('cpa_warden_theme', isDark ? 'light' : 'dark');
    updateThemeIcon();
  }

  function updateThemeIcon() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const sun = $('.icon-sun');
    const moon = $('.icon-moon');
    if (sun && moon) {
      sun.style.display = isDark ? 'none' : 'block';
      moon.style.display = isDark ? 'block' : 'none';
    }
  }

  // --------------- Toast ---------------
  function toast(msg, type = 'info') {
    const container = $('#toast-container');
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => { el.remove(); }, 4000);
  }

  // --------------- Modal ---------------
  function showModal(title, body, footer) {
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = body;
    $('#modal-footer').innerHTML = '';
    if (footer) {
      $('#modal-footer').appendChild(footer);
    }
    $('#modal-overlay').classList.add('active');
  }

  function hideModal() {
    $('#modal-overlay').classList.remove('active');
  }

  // --------------- API ---------------
  async function api(method, path, body, raw) {
    const url = state.baseUrl.replace(/\/+$/, '') + path;
    const headers = { 'Authorization': `Bearer ${state.token}` };
    const opts = { method, headers };
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    if (raw) return res;
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${text}`);
    }
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('json')) return res.json();
    return res.text();
  }

  // --------------- Connections Storage ---------------
  function loadConnections() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    } catch {
      return [];
    }
  }

  function saveConnection(url, token) {
    const conns = loadConnections().filter((c) => c.url !== url);
    conns.unshift({ url, token: btoa(token) });
    if (conns.length > 10) conns.length = 10;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(conns));
  }

  function removeConnection(url) {
    const conns = loadConnections().filter((c) => c.url !== url);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(conns));
    renderSavedConnections();
  }

  function renderSavedConnections() {
    const conns = loadConnections();
    const container = $('#saved-connections');
    const list = $('#saved-list');
    if (conns.length === 0) {
      container.style.display = 'none';
      return;
    }
    container.style.display = 'block';
    list.innerHTML = '';
    for (const c of conns) {
      const item = document.createElement('div');
      item.className = 'saved-item';
      const displayUrl = c.url.replace(/^https?:\/\//, '').replace(/\/+$/, '');
      item.innerHTML = `<span class="saved-url">${esc(displayUrl)}</span><button class="saved-remove" title="Remove">&times;</button>`;
      item.querySelector('.saved-url').addEventListener('click', () => {
        $('#base-url').value = c.url;
        $('#token').value = atob(c.token);
      });
      item.querySelector('.saved-remove').addEventListener('click', (e) => {
        e.stopPropagation();
        removeConnection(c.url);
      });
      list.appendChild(item);
    }
  }

  // --------------- Screens ---------------
  function showScreen(id) {
    for (const s of $$('.screen')) s.classList.remove('active');
    $(`#${id}`).classList.add('active');
  }

  // --------------- Connect ---------------
  async function connect(baseUrl, token) {
    state.baseUrl = baseUrl.replace(/\/+$/, '');
    state.token = token;
    const data = await api('GET', '/v0/management/auth-files');
    state.files = data.files || [];
    return true;
  }

  // --------------- Dashboard Init ---------------
  function enterDashboard() {
    const displayUrl = state.baseUrl.replace(/^https?:\/\//, '');
    $('#topbar-url').textContent = displayUrl;
    showScreen('dashboard-screen');
    applyFilters();
    renderOverview();
    loadConfig();
    loadApiKeys();
  }

  // --------------- Overview ---------------
  function renderOverview() {
    const files = state.files;
    const total = files.length;
    const active = files.filter((f) => f.status === 'active' && !f.disabled && !f.unavailable).length;
    const disabled = files.filter((f) => f.disabled).length;
    const unavailable = files.filter((f) => f.unavailable).length;

    $('#stat-total').textContent = total.toLocaleString();
    $('#stat-active').textContent = active.toLocaleString();
    $('#stat-disabled').textContent = disabled.toLocaleString();
    $('#stat-unavailable').textContent = unavailable.toLocaleString();

    renderTypeChart(files);
    renderStatusChart(files);
    renderProviderChart(files);
    renderActivity(files);
  }

  function renderTypeChart(files) {
    const counts = countBy(files, 'type');
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    const max = sorted.length ? sorted[0][1] : 1;
    const colors = ['#6366f1', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4'];
    let html = '<div class="bar-chart">';
    sorted.forEach(([key, val], i) => {
      const pct = (val / max) * 100;
      const color = colors[i % colors.length];
      html += `<div class="bar-row">
        <span class="bar-label">${esc(key)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div>
        <span class="bar-value">${val.toLocaleString()}</span>
      </div>`;
    });
    html += '</div>';
    $('#chart-types').innerHTML = html;
  }

  function renderStatusChart(files) {
    const active = files.filter((f) => !f.disabled && !f.unavailable).length;
    const disabled = files.filter((f) => f.disabled).length;
    const unavailable = files.filter((f) => f.unavailable).length;
    const data = [
      { label: 'Active', value: active, color: '#10b981' },
      { label: 'Disabled', value: disabled, color: '#f59e0b' },
      { label: 'Unavailable', value: unavailable, color: '#ef4444' },
    ].filter((d) => d.value > 0);
    const total = data.reduce((s, d) => s + d.value, 0) || 1;

    const size = 100;
    const strokeWidth = 20;
    const radius = (size - strokeWidth) / 2;
    const circumference = 2 * Math.PI * radius;
    let offset = 0;

    let svg = `<svg class="donut-svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">`;
    for (const d of data) {
      const pct = d.value / total;
      const dashLen = pct * circumference;
      svg += `<circle cx="${size / 2}" cy="${size / 2}" r="${radius}" fill="none" stroke="${d.color}" stroke-width="${strokeWidth}" stroke-dasharray="${dashLen} ${circumference - dashLen}" stroke-dashoffset="${-offset}" transform="rotate(-90 ${size / 2} ${size / 2})"/>`;
      offset += dashLen;
    }
    svg += '</svg>';

    let legend = '<div class="donut-legend">';
    for (const d of data) {
      legend += `<div class="legend-item"><span class="legend-dot" style="background:${d.color}"></span><span>${d.label}</span><span class="legend-count">${d.value.toLocaleString()}</span></div>`;
    }
    legend += '</div>';

    $('#chart-status').innerHTML = `<div class="donut-chart">${svg}${legend}</div>`;
  }

  function renderProviderChart(files) {
    const counts = countBy(files, 'provider');
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    const max = sorted.length ? sorted[0][1] : 1;
    const colors = ['#06b6d4', '#8b5cf6', '#f59e0b', '#ef4444', '#10b981', '#6366f1'];
    let html = '<div class="bar-chart">';
    sorted.forEach(([key, val], i) => {
      const pct = (val / max) * 100;
      const color = colors[i % colors.length];
      html += `<div class="bar-row">
        <span class="bar-label">${esc(key || 'unknown')}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div>
        <span class="bar-value">${val.toLocaleString()}</span>
      </div>`;
    });
    html += '</div>';
    $('#chart-providers').innerHTML = html;
  }

  function renderActivity(files) {
    const sorted = [...files]
      .filter((f) => f.updated_at || f.created_at)
      .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at))
      .slice(0, 20);
    if (sorted.length === 0) {
      $('#recent-activity').innerHTML = '<p class="placeholder">No recent activity.</p>';
      return;
    }
    let html = '';
    for (const f of sorted) {
      const time = formatTime(f.updated_at || f.created_at);
      const dotColor = f.unavailable ? 'var(--danger)' : f.disabled ? 'var(--warning)' : 'var(--success)';
      const statusText = f.unavailable ? 'unavailable' : f.disabled ? 'disabled' : f.status || 'active';
      html += `<div class="activity-item">
        <span class="activity-dot" style="background:${dotColor}"></span>
        <span style="flex:1">${esc(f.email || f.name)} &mdash; <em>${statusText}</em></span>
        <span class="activity-time">${time}</span>
      </div>`;
    }
    $('#recent-activity').innerHTML = html;
  }

  // --------------- Accounts Table ---------------
  function applyFilters() {
    let arr = state.files;
    const s = state.search.toLowerCase();
    if (s) {
      arr = arr.filter((f) =>
        (f.name || '').toLowerCase().includes(s) ||
        (f.email || '').toLowerCase().includes(s) ||
        (f.account || '').toLowerCase().includes(s)
      );
    }
    if (state.filterType) arr = arr.filter((f) => f.type === state.filterType);
    if (state.filterProvider) arr = arr.filter((f) => f.provider === state.filterProvider);
    if (state.filterStatus) {
      if (state.filterStatus === 'active') arr = arr.filter((f) => !f.disabled && !f.unavailable);
      else if (state.filterStatus === 'disabled') arr = arr.filter((f) => f.disabled);
      else if (state.filterStatus === 'unavailable') arr = arr.filter((f) => f.unavailable);
    }
    arr.sort((a, b) => {
      const va = (a[state.sortKey] || '');
      const vb = (b[state.sortKey] || '');
      const cmp = typeof va === 'string' ? va.localeCompare(vb) : va - vb;
      return state.sortDir === 'asc' ? cmp : -cmp;
    });
    state.filtered = arr;
    state.page = 1;
    state.selected.clear();
    populateFilterDropdowns();
    renderTable();
  }

  function populateFilterDropdowns() {
    populateSelect('#filter-type', uniqueValues(state.files, 'type'), 'All Types');
    populateSelect('#filter-provider', uniqueValues(state.files, 'provider'), 'All Providers');
  }

  function populateSelect(sel, values, placeholder) {
    const el = $(sel);
    const current = el.value;
    el.innerHTML = `<option value="">${placeholder}</option>`;
    for (const v of values) {
      el.innerHTML += `<option value="${esc(v)}">${esc(v || 'unknown')}</option>`;
    }
    el.value = current;
  }

  function renderTable() {
    const { filtered, page, pageSize } = state;
    const start = (page - 1) * pageSize;
    const pageItems = filtered.slice(start, start + pageSize);
    const tbody = $('#accounts-body');
    $('#account-count').textContent = `${filtered.length} account${filtered.length !== 1 ? 's' : ''}`;

    if (pageItems.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="placeholder">No accounts found.</td></tr>';
      renderPagination();
      updateBulkButtons();
      return;
    }

    let html = '';
    for (const f of pageItems) {
      const checked = state.selected.has(f.name) ? 'checked' : '';
      const statusClass = f.unavailable ? 'unavailable' : f.disabled ? 'disabled' : 'active';
      const statusText = f.unavailable ? 'unavailable' : f.disabled ? 'disabled' : f.status || 'active';
      html += `<tr>
        <td class="td-check"><input type="checkbox" class="row-check" data-name="${esc(f.name)}" ${checked}></td>
        <td class="cell-name" title="${esc(f.name)}">${esc(f.name)}</td>
        <td class="cell-email" title="${esc(f.email || '')}">${esc(f.email || '-')}</td>
        <td><span class="badge badge-type">${esc(f.type || '-')}</span></td>
        <td>${esc(f.provider || '-')}</td>
        <td><span class="badge badge-${statusClass}">${statusText}</span></td>
        <td>${esc(f.account_type || '-')}</td>
        <td>${formatTime(f.updated_at || f.created_at)}</td>
        <td class="actions-cell">
          <button class="btn btn-ghost btn-sm" onclick="CPA.viewAccount('${esc(f.name)}')">View</button>
          <button class="btn btn-ghost btn-sm" onclick="CPA.downloadAccount('${esc(f.name)}')">DL</button>
          <button class="btn btn-danger btn-sm" onclick="CPA.deleteAccount('${esc(f.name)}')">Del</button>
        </td>
      </tr>`;
    }
    tbody.innerHTML = html;
    renderPagination();
    updateBulkButtons();
  }

  function renderPagination() {
    const totalPages = Math.ceil(state.filtered.length / state.pageSize) || 1;
    const container = $('#pagination');
    if (totalPages <= 1) { container.innerHTML = ''; return; }

    let html = `<button ${state.page <= 1 ? 'disabled' : ''} data-page="${state.page - 1}">&laquo;</button>`;
    const range = pagRange(state.page, totalPages);
    for (const p of range) {
      if (p === '...') {
        html += '<button disabled>...</button>';
      } else {
        html += `<button data-page="${p}" ${p === state.page ? 'class="active"' : ''}>${p}</button>`;
      }
    }
    html += `<button ${state.page >= totalPages ? 'disabled' : ''} data-page="${state.page + 1}">&raquo;</button>`;
    container.innerHTML = html;
  }

  function pagRange(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
    const pages = [1];
    const start = Math.max(2, current - 1);
    const end = Math.min(total - 1, current + 1);
    if (start > 2) pages.push('...');
    for (let i = start; i <= end; i++) pages.push(i);
    if (end < total - 1) pages.push('...');
    pages.push(total);
    return pages;
  }

  function updateBulkButtons() {
    const n = state.selected.size;
    $('#bulk-delete-btn').style.display = n ? '' : 'none';
    $('#bulk-disable-btn').style.display = n ? '' : 'none';
    $('#bulk-enable-btn').style.display = n ? '' : 'none';
    $('#check-all').checked = n > 0 && n === state.filtered.length;
  }

  // --------------- Account Actions ---------------
  window.CPA = {};

  CPA.viewAccount = function (name) {
    const f = state.files.find((x) => x.name === name);
    if (!f) return;
    const safeData = { ...f };
    delete safeData.id_token;
    const body = `<pre style="font-size:0.8rem;overflow:auto;max-height:400px;background:var(--bg);padding:12px;border-radius:var(--radius)">${esc(JSON.stringify(safeData, null, 2))}</pre>`;
    showModal('Account Details', body);
  };

  CPA.downloadAccount = async function (name) {
    try {
      const res = await api('GET', `/v0/management/auth-files/download?name=${encodeURIComponent(name)}`, null, true);
      if (!res.ok) throw new Error(`${res.status}`);
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
      toast('Downloaded ' + name, 'success');
    } catch (err) {
      toast('Download failed: ' + err.message, 'error');
    }
  };

  CPA.deleteAccount = function (name) {
    const frag = document.createDocumentFragment();
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = 'Cancel';
    cancel.onclick = hideModal;
    const confirm = document.createElement('button');
    confirm.className = 'btn btn-danger';
    confirm.textContent = 'Delete';
    confirm.onclick = async () => {
      hideModal();
      try {
        await api('DELETE', `/v0/management/auth-files?name=${encodeURIComponent(name)}`);
        state.files = state.files.filter((f) => f.name !== name);
        applyFilters();
        renderOverview();
        toast('Deleted ' + name, 'success');
      } catch (err) {
        toast('Delete failed: ' + err.message, 'error');
      }
    };
    frag.appendChild(cancel);
    frag.appendChild(confirm);
    showModal('Confirm Delete', `<p>Are you sure you want to delete <strong>${esc(name)}</strong>?</p><p class="text-muted">This action cannot be undone.</p>`, frag);
  };

  async function bulkAction(action) {
    const names = [...state.selected];
    if (names.length === 0) return;
    const verb = action === 'delete' ? 'delete' : action === 'disable' ? 'disable' : 'enable';
    const frag = document.createDocumentFragment();
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = 'Cancel';
    cancel.onclick = hideModal;
    const confirm = document.createElement('button');
    confirm.className = `btn btn-${action === 'delete' ? 'danger' : action === 'disable' ? 'warning' : 'success'}`;
    confirm.textContent = `${verb.charAt(0).toUpperCase() + verb.slice(1)} ${names.length} accounts`;
    confirm.onclick = async () => {
      hideModal();
      let ok = 0;
      let fail = 0;
      for (const name of names) {
        try {
          if (action === 'delete') {
            await api('DELETE', `/v0/management/auth-files?name=${encodeURIComponent(name)}`);
            state.files = state.files.filter((f) => f.name !== name);
          }
          ok++;
        } catch {
          fail++;
        }
      }
      state.selected.clear();
      applyFilters();
      renderOverview();
      toast(`${verb}: ${ok} succeeded, ${fail} failed`, ok > 0 ? 'success' : 'error');
    };
    frag.appendChild(cancel);
    frag.appendChild(confirm);
    showModal(`Confirm Bulk ${verb.charAt(0).toUpperCase() + verb.slice(1)}`, `<p>${verb.charAt(0).toUpperCase() + verb.slice(1)} <strong>${names.length}</strong> selected accounts?</p>`, frag);
  }

  // --------------- Upload ---------------
  function initUpload() {
    const dropzone = $('#dropzone');
    const fileInput = $('#file-input');

    dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      handleFiles(e.dataTransfer.files);
    });
    dropzone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      handleFiles(fileInput.files);
      fileInput.value = '';
    });
  }

  async function handleFiles(fileList) {
    const queue = $('#upload-queue');
    for (const file of fileList) {
      if (!file.name.endsWith('.json')) {
        toast('Only .json files are accepted', 'error');
        continue;
      }
      const item = document.createElement('div');
      item.className = 'upload-item';
      item.innerHTML = `<span class="filename">${esc(file.name)}</span><span class="upload-status pending">Uploading...</span>`;
      queue.prepend(item);

      try {
        const fd = new FormData();
        fd.append('file', file);
        await api('POST', '/v0/management/auth-files', fd);
        item.querySelector('.upload-status').textContent = 'Success';
        item.querySelector('.upload-status').className = 'upload-status success';
        toast('Uploaded ' + file.name, 'success');
      } catch (err) {
        item.querySelector('.upload-status').textContent = 'Failed';
        item.querySelector('.upload-status').className = 'upload-status error';
        toast('Upload failed: ' + err.message, 'error');
      }
    }
    await refreshData();
  }

  // --------------- OAuth ---------------
  function initOAuth() {
    for (const btn of $$('.oauth-btn')) {
      btn.addEventListener('click', () => startOAuth(btn.dataset.provider));
    }
  }

  async function startOAuth(provider) {
    const endpoints = {
      codex: '/v0/management/codex-auth-url',
      anthropic: '/v0/management/anthropic-auth-url',
      'gemini-cli': '/v0/management/gemini-cli-auth-url',
      qwen: '/v0/management/qwen-auth-url',
      iflow: '/v0/management/iflow-auth-url',
      antigravity: '/v0/management/antigravity-auth-url',
    };
    const ep = endpoints[provider];
    if (!ep) { toast('Unknown provider', 'error'); return; }

    try {
      const data = await api('GET', ep);
      const url = data.url || data.auth_url || data.verification_uri;
      const oauthState = data.state;
      if (url) {
        window.open(url, '_blank');
        toast('OAuth window opened. Complete login in the browser.', 'info');
        if (oauthState) pollOAuthStatus(oauthState, provider);
      } else {
        showModal('OAuth Response', `<pre style="font-size:0.8rem;overflow:auto">${esc(JSON.stringify(data, null, 2))}</pre>`);
      }
    } catch (err) {
      toast('OAuth failed: ' + err.message, 'error');
    }
  }

  async function pollOAuthStatus(oauthState, provider) {
    const statusEl = $('#oauth-status');
    const content = $('#oauth-status-content');
    statusEl.style.display = 'block';
    content.innerHTML = `<p>Waiting for <strong>${esc(provider)}</strong> login to complete...</p><p class="text-muted">State: ${esc(oauthState)}</p>`;

    for (let i = 0; i < 60; i++) {
      await sleep(3000);
      try {
        const data = await api('GET', `/v0/management/get-auth-status?state=${encodeURIComponent(oauthState)}`);
        if (data.status === 'ok') {
          content.innerHTML = `<p style="color:var(--success)">Login successful!</p>`;
          toast('OAuth login completed!', 'success');
          await refreshData();
          return;
        }
        if (data.status === 'error') {
          content.innerHTML = `<p style="color:var(--danger)">Login failed: ${esc(data.message || 'Unknown error')}</p>`;
          return;
        }
      } catch {
        // continue polling
      }
    }
    content.innerHTML = '<p class="text-muted">Polling timed out.</p>';
  }

  // --------------- Settings ---------------
  async function loadConfig() {
    try {
      const res = await api('GET', '/v0/management/config.yaml', null, true);
      if (res.ok) {
        const text = await res.text();
        $('#config-yaml').value = text;
        $('#config-loading').style.display = 'none';
        $('#config-editor').style.display = 'block';
      } else {
        // Try JSON fallback
        const data = await api('GET', '/v0/management/config');
        $('#config-yaml').value = JSON.stringify(data, null, 2);
        $('#config-loading').style.display = 'none';
        $('#config-editor').style.display = 'block';
      }
    } catch (err) {
      $('#config-loading').textContent = 'Failed to load configuration: ' + err.message;
    }
  }

  async function loadApiKeys() {
    try {
      const data = await api('GET', '/v0/management/api-keys');
      const keys = data.keys || data;
      let html = '';
      if (Array.isArray(keys) && keys.length > 0) {
        html = '<div style="display:flex;flex-direction:column;gap:8px">';
        for (const k of keys) {
          const display = typeof k === 'string' ? k : (k.key || k.name || JSON.stringify(k));
          html += `<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--border);border-radius:var(--radius);font-family:var(--font-mono);font-size:0.8rem">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis">${esc(maskKey(String(display)))}</span>
          </div>`;
        }
        html += '</div>';
      } else if (typeof keys === 'object') {
        html = `<pre style="font-size:0.8rem;overflow:auto;background:var(--bg);padding:12px;border-radius:var(--radius)">${esc(JSON.stringify(keys, null, 2))}</pre>`;
      } else {
        html = '<p class="text-muted">No API keys configured.</p>';
      }
      $('#api-keys-content').innerHTML = html;
    } catch (err) {
      $('#api-keys-content').innerHTML = `<p class="text-muted">Failed to load: ${esc(err.message)}</p>`;
    }
  }

  // --------------- Refresh ---------------
  async function refreshData() {
    try {
      const data = await api('GET', '/v0/management/auth-files');
      state.files = data.files || [];
      applyFilters();
      renderOverview();
      toast('Data refreshed', 'success');
    } catch (err) {
      toast('Refresh failed: ' + err.message, 'error');
    }
  }

  // --------------- Utils ---------------
  function esc(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
  }

  function countBy(arr, key) {
    const m = {};
    for (const item of arr) {
      const v = item[key] || '';
      m[v] = (m[v] || 0) + 1;
    }
    return m;
  }

  function uniqueValues(arr, key) {
    return [...new Set(arr.map((x) => x[key]).filter(Boolean))].sort();
  }

  function formatTime(str) {
    if (!str) return '-';
    try {
      const d = new Date(str);
      const now = new Date();
      const diff = now - d;
      if (diff < 60000) return 'just now';
      if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
      if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
      if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';
      return d.toLocaleDateString();
    } catch {
      return str;
    }
  }

  function maskKey(key) {
    if (key.length <= 8) return key;
    return key.slice(0, 4) + '***' + key.slice(-4);
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // --------------- Event Binding ---------------
  function bindEvents() {
    // Connect form
    $('#connect-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('#connect-btn');
      const text = btn.querySelector('.btn-text');
      const loading = btn.querySelector('.btn-loading');
      text.style.display = 'none';
      loading.style.display = '';
      btn.disabled = true;

      try {
        await connect($('#base-url').value, $('#token').value);
        if ($('#remember').checked) {
          saveConnection($('#base-url').value, $('#token').value);
        }
        enterDashboard();
      } catch (err) {
        toast('Connection failed: ' + err.message, 'error');
      } finally {
        text.style.display = '';
        loading.style.display = 'none';
        btn.disabled = false;
      }
    });

    // Password toggle
    $('#toggle-password').addEventListener('click', () => {
      const input = $('#token');
      const eyeOn = $('.icon-eye');
      const eyeOff = $('.icon-eye-off');
      if (input.type === 'password') {
        input.type = 'text';
        eyeOn.style.display = 'none';
        eyeOff.style.display = 'block';
      } else {
        input.type = 'password';
        eyeOn.style.display = 'block';
        eyeOff.style.display = 'none';
      }
    });

    // Theme
    $('#theme-toggle').addEventListener('click', toggleTheme);

    // Disconnect
    $('#disconnect-btn').addEventListener('click', () => {
      state.baseUrl = '';
      state.token = '';
      state.files = [];
      state.filtered = [];
      state.selected.clear();
      showScreen('connect-screen');
    });

    // Refresh
    $('#refresh-btn').addEventListener('click', refreshData);

    // Tabs
    for (const tab of $$('.tab')) {
      tab.addEventListener('click', () => {
        for (const t of $$('.tab')) t.classList.remove('active');
        tab.classList.add('active');
        for (const p of $$('.tab-panel')) p.classList.remove('active');
        $(`#tab-${tab.dataset.tab}`).classList.add('active');
      });
    }

    // Search & Filters
    let searchTimeout;
    $('#search-input').addEventListener('input', (e) => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => {
        state.search = e.target.value;
        applyFilters();
      }, 200);
    });

    $('#filter-type').addEventListener('change', (e) => { state.filterType = e.target.value; applyFilters(); });
    $('#filter-provider').addEventListener('change', (e) => { state.filterProvider = e.target.value; applyFilters(); });
    $('#filter-status').addEventListener('change', (e) => { state.filterStatus = e.target.value; applyFilters(); });

    // Sort
    for (const th of $$('.sortable')) {
      th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          state.sortKey = key;
          state.sortDir = 'asc';
        }
        for (const s of $$('.sortable')) { s.classList.remove('asc', 'desc'); }
        th.classList.add(state.sortDir);
        applyFilters();
      });
    }

    // Check all
    $('#check-all').addEventListener('change', (e) => {
      if (e.target.checked) {
        state.filtered.forEach((f) => state.selected.add(f.name));
      } else {
        state.selected.clear();
      }
      renderTable();
    });

    // Row checks (delegated)
    $('#accounts-body').addEventListener('change', (e) => {
      if (e.target.classList.contains('row-check')) {
        const name = e.target.dataset.name;
        if (e.target.checked) state.selected.add(name);
        else state.selected.delete(name);
        updateBulkButtons();
      }
    });

    // Select all button
    $('#select-all-btn').addEventListener('click', () => {
      if (state.selected.size === state.filtered.length) {
        state.selected.clear();
      } else {
        state.filtered.forEach((f) => state.selected.add(f.name));
      }
      renderTable();
    });

    // Bulk actions
    $('#bulk-delete-btn').addEventListener('click', () => bulkAction('delete'));
    $('#bulk-disable-btn').addEventListener('click', () => bulkAction('disable'));
    $('#bulk-enable-btn').addEventListener('click', () => bulkAction('enable'));

    // Pagination (delegated)
    $('#pagination').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-page]');
      if (!btn || btn.disabled) return;
      state.page = parseInt(btn.dataset.page, 10);
      renderTable();
      $('.table-wrapper').scrollTop = 0;
    });

    // Modal close
    $('#modal-close').addEventListener('click', hideModal);
    $('#modal-overlay').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) hideModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') hideModal();
    });

    // Settings
    $('#save-config-btn').addEventListener('click', async () => {
      try {
        const text = $('#config-yaml').value;
        // Try YAML first, then JSON
        try {
          await api('PUT', '/v0/management/config.yaml', text);
        } catch {
          const json = JSON.parse(text);
          await api('PUT', '/v0/management/config', json);
        }
        toast('Configuration saved', 'success');
      } catch (err) {
        toast('Save failed: ' + err.message, 'error');
      }
    });

    $('#reload-config-btn').addEventListener('click', loadConfig);

    // Logs
    $('#view-logs-btn').addEventListener('click', async () => {
      const el = $('#logs-content');
      const viewer = $('#log-viewer');
      el.style.display = el.style.display === 'none' ? 'block' : 'none';
      if (el.style.display === 'block') {
        try {
          const data = await api('GET', '/v0/management/logs');
          viewer.textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
        } catch (err) {
          viewer.textContent = 'Failed to load logs: ' + err.message;
        }
      }
    });

    $('#clear-logs-btn').addEventListener('click', async () => {
      try {
        await api('DELETE', '/v0/management/logs');
        toast('Logs cleared', 'success');
        $('#log-viewer').textContent = '';
      } catch (err) {
        toast('Clear logs failed: ' + err.message, 'error');
      }
    });

    $('#export-usage-btn').addEventListener('click', async () => {
      try {
        const data = await api('GET', '/v0/management/usage/export');
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'cpa_usage_export.json';
        a.click();
        URL.revokeObjectURL(a.href);
        toast('Usage data exported', 'success');
      } catch (err) {
        toast('Export failed: ' + err.message, 'error');
      }
    });
  }

  // --------------- Init ---------------
  function init() {
    initTheme();
    renderSavedConnections();
    bindEvents();
    initUpload();
    initOAuth();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
