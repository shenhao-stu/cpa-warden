/* ============================================================
   CPA Warden - Web UI Application
   Pure vanilla JS, no build step, IIFE pattern.
   All UI text in Chinese. No inline event handlers.
   ============================================================ */

(function () {
  'use strict';

  // --------------- Constants ---------------
  var STORAGE_INSTANCES = 'cpa_warden_instances';
  var STORAGE_THEME = 'cpa_warden_theme';
  var STORAGE_SETTINGS = 'cpa_warden_settings';

  // --------------- State ---------------
  var state = {
    currentInstanceId: null,
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
    concurrency: 10,
    maintainRunning: false,
  };

  // --------------- DOM Helpers ---------------
  function $(sel) {
    return document.querySelector(sel);
  }

  function $$(sel) {
    return document.querySelectorAll(sel);
  }

  function escapeHtml(str) {
    var el = document.createElement('span');
    el.textContent = String(str);
    return el.innerHTML;
  }

  // --------------- Theme ---------------
  function initTheme() {
    var saved = localStorage.getItem(STORAGE_THEME);
    if (saved === 'dark' || (!saved && matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.setAttribute('data-theme', 'dark');
    }
    updateThemeIcon();
  }

  function toggleTheme() {
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
    localStorage.setItem(STORAGE_THEME, isDark ? 'light' : 'dark');
    updateThemeIcon();
  }

  function updateThemeIcon() {
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    var sun = $('.icon-sun');
    var moon = $('.icon-moon');
    if (sun && moon) {
      sun.style.display = isDark ? 'none' : 'block';
      moon.style.display = isDark ? 'block' : 'none';
    }
  }

  // --------------- Toast ---------------
  function toast(msg, type) {
    type = type || 'info';
    var container = $('#toast-container');
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(function () {
      el.remove();
    }, 4000);
  }

  // --------------- Modal ---------------
  function showModal(title, bodyHtml, footerFragment) {
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = bodyHtml;
    var footer = $('#modal-footer');
    footer.innerHTML = '';
    if (footerFragment) {
      footer.appendChild(footerFragment);
    }
    $('#modal-overlay').classList.add('active');
  }

  function hideModal() {
    $('#modal-overlay').classList.remove('active');
  }

  // --------------- API ---------------
  function api(method, path, body, raw) {
    var url = state.baseUrl.replace(/\/+$/, '') + path;
    var headers = { Authorization: 'Bearer ' + state.token };
    var opts = { method: method, headers: headers };
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (res) {
      if (raw) return res;
      if (!res.ok) {
        return res.text().then(function (text) {
          throw new Error(res.status + ': ' + text);
        });
      }
      var ct = res.headers.get('content-type') || '';
      if (ct.indexOf('json') !== -1) return res.json();
      return res.text();
    });
  }

  // --------------- Instance Storage ---------------
  function loadInstances() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_INSTANCES) || '[]');
    } catch (e) {
      return [];
    }
  }

  function saveInstances(list) {
    localStorage.setItem(STORAGE_INSTANCES, JSON.stringify(list));
  }

  function generateId() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  function addInstance(name, url, token) {
    var instances = loadInstances();
    var id = generateId();
    instances.push({
      id: id,
      name: name,
      url: url.replace(/\/+$/, ''),
      token: btoa(token),
    });
    saveInstances(instances);
    return id;
  }

  function removeInstance(id) {
    var instances = loadInstances().filter(function (inst) {
      return inst.id !== id;
    });
    saveInstances(instances);
  }

  function getInstanceById(id) {
    return loadInstances().find(function (inst) {
      return inst.id === id;
    }) || null;
  }

  // --------------- Settings Storage ---------------
  function loadSettings() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_SETTINGS) || '{}');
    } catch (e) {
      return {};
    }
  }

  function saveSettings(settings) {
    localStorage.setItem(STORAGE_SETTINGS, JSON.stringify(settings));
  }

  function applySettings() {
    var settings = loadSettings();
    if (settings.pageSize) {
      state.pageSize = parseInt(settings.pageSize, 10);
      var pageSizeEl = $('#page-size-select');
      if (pageSizeEl) pageSizeEl.value = String(state.pageSize);
    }
    if (settings.concurrency) {
      state.concurrency = parseInt(settings.concurrency, 10);
      var concurrencyEl = $('#concurrency-select');
      if (concurrencyEl) concurrencyEl.value = String(state.concurrency);
    }
  }

  // --------------- Screens ---------------
  function showScreen(id) {
    var screens = $$('.screen');
    for (var i = 0; i < screens.length; i++) {
      screens[i].classList.remove('active');
    }
    $('#' + id).classList.add('active');
  }

  // --------------- Connect Screen Rendering ---------------
  function renderInstanceList() {
    var instances = loadInstances();
    var list = $('#instance-list');
    var form = $('#connect-form');
    list.innerHTML = '';

    if (instances.length === 0) {
      form.style.display = 'block';
      return;
    }

    for (var i = 0; i < instances.length; i++) {
      var inst = instances[i];
      var item = document.createElement('div');
      item.className = 'instance-item';
      item.setAttribute('data-instance-id', inst.id);

      var nameSpan = document.createElement('span');
      nameSpan.className = 'instance-name';
      nameSpan.textContent = inst.name;

      var urlSpan = document.createElement('span');
      urlSpan.className = 'instance-url';
      urlSpan.textContent = inst.url.replace(/^https?:\/\//, '');

      var removeBtn = document.createElement('button');
      removeBtn.className = 'instance-remove';
      removeBtn.setAttribute('data-action', 'remove-instance');
      removeBtn.setAttribute('data-instance-id', inst.id);
      removeBtn.textContent = '\u00D7';
      removeBtn.title = '\u5220\u9664';

      item.appendChild(nameSpan);
      item.appendChild(urlSpan);
      item.appendChild(removeBtn);
      list.appendChild(item);
    }
  }

  // --------------- Connect ---------------
  function connectToInstance(id) {
    var inst = getInstanceById(id);
    if (!inst) {
      toast('\u5B9E\u4F8B\u4E0D\u5B58\u5728', 'error');
      return Promise.reject(new Error('Instance not found'));
    }
    state.currentInstanceId = id;
    state.baseUrl = inst.url;
    state.token = atob(inst.token);

    return api('GET', '/v0/management/auth-files').then(function (data) {
      state.files = data.files || [];
      enterDashboard();
    });
  }

  // --------------- Dashboard Init ---------------
  function enterDashboard() {
    renderInstanceSwitcher();
    showScreen('dashboard-screen');
    applyFilters();
    renderOverview();
    renderMaintainSummary();
    renderSettingsInstances();
  }

  function renderInstanceSwitcher() {
    var switcher = $('#instance-switcher');
    var instances = loadInstances();
    switcher.innerHTML = '';
    for (var i = 0; i < instances.length; i++) {
      var opt = document.createElement('option');
      opt.value = instances[i].id;
      opt.textContent = instances[i].name;
      if (instances[i].id === state.currentInstanceId) {
        opt.selected = true;
      }
      switcher.appendChild(opt);
    }
  }

  // --------------- Overview ---------------
  function renderOverview() {
    var files = state.files;
    var total = files.length;
    var active = files.filter(function (f) {
      return !f.disabled && !f.unavailable;
    }).length;
    var disabled = files.filter(function (f) {
      return f.disabled;
    }).length;
    var unavailable = files.filter(function (f) {
      return f.unavailable;
    }).length;

    $('#stat-total').textContent = total.toLocaleString();
    $('#stat-active').textContent = active.toLocaleString();
    $('#stat-disabled').textContent = disabled.toLocaleString();
    $('#stat-unavailable').textContent = unavailable.toLocaleString();

    renderTypeChart(files);
    renderStatusChart(files);
    renderProviderChart(files);
    renderActivity(files);
  }

  function countBy(arr, key) {
    var m = {};
    for (var i = 0; i < arr.length; i++) {
      var v = arr[i][key] || '';
      m[v] = (m[v] || 0) + 1;
    }
    return m;
  }

  function renderTypeChart(files) {
    var counts = countBy(files, 'type');
    var sorted = Object.entries(counts).sort(function (a, b) {
      return b[1] - a[1];
    });
    var max = sorted.length ? sorted[0][1] : 1;
    var colors = ['#6366f1', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4'];
    var html = '<div class="bar-chart">';
    for (var i = 0; i < sorted.length; i++) {
      var key = sorted[i][0];
      var val = sorted[i][1];
      var pct = (val / max) * 100;
      var color = colors[i % colors.length];
      html += '<div class="bar-row">' +
        '<span class="bar-label">' + escapeHtml(key || 'unknown') + '</span>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
        '<span class="bar-value">' + val.toLocaleString() + '</span>' +
        '</div>';
    }
    html += '</div>';
    $('#chart-types').innerHTML = html;
  }

  function renderStatusChart(files) {
    var active = files.filter(function (f) { return !f.disabled && !f.unavailable; }).length;
    var disabled = files.filter(function (f) { return f.disabled; }).length;
    var unavailable = files.filter(function (f) { return f.unavailable; }).length;
    var data = [
      { label: '\u6D3B\u8DC3', value: active, color: '#10b981' },
      { label: '\u5DF2\u7981\u7528', value: disabled, color: '#f59e0b' },
      { label: '\u4E0D\u53EF\u7528', value: unavailable, color: '#ef4444' },
    ].filter(function (d) { return d.value > 0; });
    var total = data.reduce(function (s, d) { return s + d.value; }, 0) || 1;

    var size = 100;
    var strokeWidth = 20;
    var radius = (size - strokeWidth) / 2;
    var circumference = 2 * Math.PI * radius;
    var offset = 0;

    var svg = '<svg class="donut-svg" width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '">';
    for (var i = 0; i < data.length; i++) {
      var d = data[i];
      var pct = d.value / total;
      var dashLen = pct * circumference;
      svg += '<circle cx="' + (size / 2) + '" cy="' + (size / 2) + '" r="' + radius + '" fill="none" stroke="' + d.color + '" stroke-width="' + strokeWidth + '" stroke-dasharray="' + dashLen + ' ' + (circumference - dashLen) + '" stroke-dashoffset="' + (-offset) + '" transform="rotate(-90 ' + (size / 2) + ' ' + (size / 2) + ')"/>';
      offset += dashLen;
    }
    svg += '</svg>';

    var legend = '<div class="donut-legend">';
    for (var j = 0; j < data.length; j++) {
      legend += '<div class="legend-item"><span class="legend-dot" style="background:' + data[j].color + '"></span><span>' + data[j].label + '</span><span class="legend-count">' + data[j].value.toLocaleString() + '</span></div>';
    }
    legend += '</div>';

    $('#chart-status').innerHTML = '<div class="donut-chart">' + svg + legend + '</div>';
  }

  function renderProviderChart(files) {
    var counts = countBy(files, 'provider');
    var sorted = Object.entries(counts).sort(function (a, b) { return b[1] - a[1]; });
    var max = sorted.length ? sorted[0][1] : 1;
    var colors = ['#06b6d4', '#8b5cf6', '#f59e0b', '#ef4444', '#10b981', '#6366f1'];
    var html = '<div class="bar-chart">';
    for (var i = 0; i < sorted.length; i++) {
      var key = sorted[i][0];
      var val = sorted[i][1];
      var pct = (val / max) * 100;
      var color = colors[i % colors.length];
      html += '<div class="bar-row">' +
        '<span class="bar-label">' + escapeHtml(key || 'unknown') + '</span>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
        '<span class="bar-value">' + val.toLocaleString() + '</span>' +
        '</div>';
    }
    html += '</div>';
    $('#chart-providers').innerHTML = html;
  }

  function renderActivity(files) {
    var sorted = files
      .filter(function (f) { return f.updated_at || f.created_at; })
      .slice()
      .sort(function (a, b) {
        return new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at);
      })
      .slice(0, 20);

    if (sorted.length === 0) {
      $('#recent-activity').innerHTML = '<p class="placeholder">\u6682\u65E0\u6700\u8FD1\u6D3B\u52A8</p>';
      return;
    }

    var container = $('#recent-activity');
    container.innerHTML = '';
    for (var i = 0; i < sorted.length; i++) {
      var f = sorted[i];
      var time = formatTime(f.updated_at || f.created_at);
      var dotColor = f.unavailable ? 'var(--danger)' : f.disabled ? 'var(--warning)' : 'var(--success)';
      var statusText = f.unavailable ? '\u4E0D\u53EF\u7528' : f.disabled ? '\u5DF2\u7981\u7528' : '\u6D3B\u8DC3';

      var item = document.createElement('div');
      item.className = 'activity-item';

      var dot = document.createElement('span');
      dot.className = 'activity-dot';
      dot.style.background = dotColor;

      var info = document.createElement('span');
      info.style.flex = '1';
      info.textContent = (f.email || f.name) + ' \u2014 ' + statusText;

      var timeEl = document.createElement('span');
      timeEl.className = 'activity-time';
      timeEl.textContent = time;

      item.appendChild(dot);
      item.appendChild(info);
      item.appendChild(timeEl);
      container.appendChild(item);
    }
  }

  // --------------- Accounts Table ---------------
  function uniqueValues(arr, key) {
    var seen = {};
    var result = [];
    for (var i = 0; i < arr.length; i++) {
      var v = arr[i][key];
      if (v && !seen[v]) {
        seen[v] = true;
        result.push(v);
      }
    }
    return result.sort();
  }

  function applyFilters() {
    var arr = state.files;
    var s = state.search.toLowerCase();

    if (s) {
      arr = arr.filter(function (f) {
        return (f.name || '').toLowerCase().indexOf(s) !== -1 ||
          (f.email || '').toLowerCase().indexOf(s) !== -1 ||
          (f.account || '').toLowerCase().indexOf(s) !== -1;
      });
    }
    if (state.filterType) {
      arr = arr.filter(function (f) { return f.type === state.filterType; });
    }
    if (state.filterProvider) {
      arr = arr.filter(function (f) { return f.provider === state.filterProvider; });
    }
    if (state.filterStatus) {
      if (state.filterStatus === 'active') {
        arr = arr.filter(function (f) { return !f.disabled && !f.unavailable; });
      } else if (state.filterStatus === 'disabled') {
        arr = arr.filter(function (f) { return f.disabled; });
      } else if (state.filterStatus === 'unavailable') {
        arr = arr.filter(function (f) { return f.unavailable; });
      }
    }

    arr.sort(function (a, b) {
      var va = a[state.sortKey] || '';
      var vb = b[state.sortKey] || '';
      var cmp = typeof va === 'string' ? va.localeCompare(vb) : va - vb;
      return state.sortDir === 'asc' ? cmp : -cmp;
    });

    state.filtered = arr;
    state.page = 1;
    state.selected.clear();
    populateFilterDropdowns();
    renderTable();
  }

  function populateFilterDropdowns() {
    populateSelect('#filter-type', uniqueValues(state.files, 'type'), '\u5168\u90E8\u7C7B\u578B');
    populateSelect('#filter-provider', uniqueValues(state.files, 'provider'), '\u5168\u90E8\u63D0\u4F9B\u5546');
  }

  function populateSelect(sel, values, placeholder) {
    var el = $(sel);
    var current = el.value;
    el.innerHTML = '<option value="">' + placeholder + '</option>';
    for (var i = 0; i < values.length; i++) {
      var opt = document.createElement('option');
      opt.value = values[i];
      opt.textContent = values[i] || 'unknown';
      el.appendChild(opt);
    }
    el.value = current;
  }

  function renderTable() {
    var filtered = state.filtered;
    var page = state.page;
    var pageSize = state.pageSize;
    var start = (page - 1) * pageSize;
    var pageItems = filtered.slice(start, start + pageSize);
    var tbody = $('#accounts-body');
    var countEl = $('#account-count');
    countEl.textContent = filtered.length + ' \u4E2A\u8D26\u53F7';

    tbody.innerHTML = '';

    if (pageItems.length === 0) {
      var emptyRow = document.createElement('tr');
      var emptyCell = document.createElement('td');
      emptyCell.setAttribute('colspan', '8');
      emptyCell.className = 'placeholder';
      emptyCell.textContent = '\u672A\u627E\u5230\u8D26\u53F7';
      emptyRow.appendChild(emptyCell);
      tbody.appendChild(emptyRow);
      renderPagination();
      updateBulkButtons();
      return;
    }

    for (var i = 0; i < pageItems.length; i++) {
      var f = pageItems[i];
      var tr = document.createElement('tr');

      // Checkbox cell
      var tdCheck = document.createElement('td');
      tdCheck.className = 'td-check';
      var checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'row-check';
      checkbox.setAttribute('data-name', f.name);
      checkbox.checked = state.selected.has(f.name);
      tdCheck.appendChild(checkbox);
      tr.appendChild(tdCheck);

      // Name
      var tdName = document.createElement('td');
      tdName.className = 'cell-name';
      tdName.title = f.name;
      tdName.textContent = f.name;
      tr.appendChild(tdName);

      // Email
      var tdEmail = document.createElement('td');
      tdEmail.className = 'cell-email';
      tdEmail.title = f.email || '';
      tdEmail.textContent = f.email || '-';
      tr.appendChild(tdEmail);

      // Type
      var tdType = document.createElement('td');
      var badgeType = document.createElement('span');
      badgeType.className = 'badge badge-type';
      badgeType.textContent = f.type || '-';
      tdType.appendChild(badgeType);
      tr.appendChild(tdType);

      // Provider
      var tdProvider = document.createElement('td');
      tdProvider.textContent = f.provider || '-';
      tr.appendChild(tdProvider);

      // Status
      var tdStatus = document.createElement('td');
      var statusClass = f.unavailable ? 'unavailable' : f.disabled ? 'disabled' : 'active';
      var statusText = f.unavailable ? '\u4E0D\u53EF\u7528' : f.disabled ? '\u5DF2\u7981\u7528' : '\u6D3B\u8DC3';
      var badgeStatus = document.createElement('span');
      badgeStatus.className = 'badge badge-' + statusClass;
      badgeStatus.textContent = statusText;
      tdStatus.appendChild(badgeStatus);
      tr.appendChild(tdStatus);

      // Updated
      var tdUpdated = document.createElement('td');
      tdUpdated.textContent = formatTime(f.updated_at || f.created_at);
      tr.appendChild(tdUpdated);

      // Actions
      var tdActions = document.createElement('td');
      tdActions.className = 'actions-cell';

      var viewBtn = document.createElement('button');
      viewBtn.className = 'btn btn-ghost btn-sm';
      viewBtn.textContent = '\u67E5\u770B';
      viewBtn.setAttribute('data-action', 'view-account');
      viewBtn.setAttribute('data-name', f.name);

      var dlBtn = document.createElement('button');
      dlBtn.className = 'btn btn-ghost btn-sm';
      dlBtn.textContent = '\u4E0B\u8F7D';
      dlBtn.setAttribute('data-action', 'download-account');
      dlBtn.setAttribute('data-name', f.name);

      var toggleBtn = document.createElement('button');
      if (f.disabled) {
        toggleBtn.className = 'btn btn-success btn-sm';
        toggleBtn.textContent = '\u542F\u7528';
        toggleBtn.setAttribute('data-action', 'enable-account');
      } else {
        toggleBtn.className = 'btn btn-warning btn-sm';
        toggleBtn.textContent = '\u7981\u7528';
        toggleBtn.setAttribute('data-action', 'disable-account');
      }
      toggleBtn.setAttribute('data-name', f.name);

      var delBtn = document.createElement('button');
      delBtn.className = 'btn btn-danger btn-sm';
      delBtn.textContent = '\u5220\u9664';
      delBtn.setAttribute('data-action', 'delete-account');
      delBtn.setAttribute('data-name', f.name);

      tdActions.appendChild(viewBtn);
      tdActions.appendChild(dlBtn);
      tdActions.appendChild(toggleBtn);
      tdActions.appendChild(delBtn);
      tr.appendChild(tdActions);

      tbody.appendChild(tr);
    }

    renderPagination();
    updateBulkButtons();
  }

  function renderPagination() {
    var totalPages = Math.ceil(state.filtered.length / state.pageSize) || 1;
    var container = $('#pagination');
    if (totalPages <= 1) {
      container.innerHTML = '';
      return;
    }

    container.innerHTML = '';
    var prevBtn = document.createElement('button');
    prevBtn.setAttribute('data-page', String(state.page - 1));
    prevBtn.innerHTML = '&laquo;';
    prevBtn.disabled = state.page <= 1;
    container.appendChild(prevBtn);

    var range = pagRange(state.page, totalPages);
    for (var i = 0; i < range.length; i++) {
      var p = range[i];
      var btn = document.createElement('button');
      if (p === '...') {
        btn.textContent = '...';
        btn.disabled = true;
      } else {
        btn.setAttribute('data-page', String(p));
        btn.textContent = String(p);
        if (p === state.page) btn.className = 'active';
      }
      container.appendChild(btn);
    }

    var nextBtn = document.createElement('button');
    nextBtn.setAttribute('data-page', String(state.page + 1));
    nextBtn.innerHTML = '&raquo;';
    nextBtn.disabled = state.page >= totalPages;
    container.appendChild(nextBtn);
  }

  function pagRange(current, total) {
    if (total <= 7) {
      var arr = [];
      for (var i = 1; i <= total; i++) arr.push(i);
      return arr;
    }
    var pages = [1];
    var start = Math.max(2, current - 1);
    var end = Math.min(total - 1, current + 1);
    if (start > 2) pages.push('...');
    for (var j = start; j <= end; j++) pages.push(j);
    if (end < total - 1) pages.push('...');
    pages.push(total);
    return pages;
  }

  function updateBulkButtons() {
    var n = state.selected.size;
    $('[data-action="bulk-delete"]').style.display = n ? '' : 'none';
    $('[data-action="bulk-disable"]').style.display = n ? '' : 'none';
    $('[data-action="bulk-enable"]').style.display = n ? '' : 'none';
    $('#check-all').checked = n > 0 && n === state.filtered.length;
  }

  // --------------- Account Actions ---------------
  function viewAccount(name) {
    var f = state.files.find(function (x) { return x.name === name; });
    if (!f) return;
    var safeData = Object.assign({}, f);
    delete safeData.id_token;
    var pre = document.createElement('pre');
    pre.style.cssText = 'font-size:0.8rem;overflow:auto;max-height:400px;background:var(--bg);padding:12px;border-radius:var(--radius)';
    pre.textContent = JSON.stringify(safeData, null, 2);
    showModal('\u8D26\u53F7\u8BE6\u60C5', '');
    $('#modal-body').innerHTML = '';
    $('#modal-body').appendChild(pre);
  }

  function downloadAccount(name) {
    api('GET', '/v0/management/auth-files/download?name=' + encodeURIComponent(name), null, true)
      .then(function (res) {
        if (!res.ok) throw new Error(String(res.status));
        return res.blob();
      })
      .then(function (blob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name;
        a.click();
        URL.revokeObjectURL(a.href);
        toast('\u5DF2\u4E0B\u8F7D ' + name, 'success');
      })
      .catch(function (err) {
        toast('\u4E0B\u8F7D\u5931\u8D25: ' + err.message, 'error');
      });
  }

  function toggleAccountStatus(name, disable) {
    api('PATCH', '/v0/management/auth-files/status', { name: name, disabled: disable })
      .then(function () {
        var f = state.files.find(function (x) { return x.name === name; });
        if (f) f.disabled = disable;
        applyFilters();
        renderOverview();
        renderMaintainSummary();
        toast(disable ? '\u5DF2\u7981\u7528 ' + name : '\u5DF2\u542F\u7528 ' + name, 'success');
      })
      .catch(function (err) {
        toast('\u64CD\u4F5C\u5931\u8D25: ' + err.message, 'error');
      });
  }

  function deleteAccount(name) {
    var frag = document.createDocumentFragment();
    var cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = '\u53D6\u6D88';
    cancel.setAttribute('data-action', 'close-modal');

    var confirm = document.createElement('button');
    confirm.className = 'btn btn-danger';
    confirm.textContent = '\u786E\u8BA4\u5220\u9664';
    confirm.setAttribute('data-action', 'confirm-delete');
    confirm.setAttribute('data-name', name);

    frag.appendChild(cancel);
    frag.appendChild(confirm);

    showModal(
      '\u786E\u8BA4\u5220\u9664',
      '<p>\u786E\u5B9A\u8981\u5220\u9664 <strong>' + escapeHtml(name) + '</strong> \u5417\uFF1F</p><p class="text-muted">\u6B64\u64CD\u4F5C\u4E0D\u53EF\u64A4\u9500\u3002</p>',
      frag
    );
  }

  function executeDeleteAccount(name) {
    hideModal();
    api('DELETE', '/v0/management/auth-files?name=' + encodeURIComponent(name))
      .then(function () {
        state.files = state.files.filter(function (f) { return f.name !== name; });
        applyFilters();
        renderOverview();
        renderMaintainSummary();
        toast('\u5DF2\u5220\u9664 ' + name, 'success');
      })
      .catch(function (err) {
        toast('\u5220\u9664\u5931\u8D25: ' + err.message, 'error');
      });
  }

  // --------------- Bulk Actions ---------------
  function bulkAction(action) {
    var names = Array.from(state.selected);
    if (names.length === 0) return;

    var verbMap = { delete: '\u5220\u9664', disable: '\u7981\u7528', enable: '\u542F\u7528' };
    var verb = verbMap[action];

    var frag = document.createDocumentFragment();
    var cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = '\u53D6\u6D88';
    cancel.setAttribute('data-action', 'close-modal');

    var btnClass = action === 'delete' ? 'btn-danger' : action === 'disable' ? 'btn-warning' : 'btn-success';
    var confirm = document.createElement('button');
    confirm.className = 'btn ' + btnClass;
    confirm.textContent = verb + ' ' + names.length + ' \u4E2A\u8D26\u53F7';
    confirm.setAttribute('data-action', 'confirm-bulk');
    confirm.setAttribute('data-bulk-type', action);

    frag.appendChild(cancel);
    frag.appendChild(confirm);

    showModal(
      '\u786E\u8BA4\u6279\u91CF' + verb,
      '<p>\u786E\u5B9A\u8981' + verb + ' <strong>' + names.length + '</strong> \u4E2A\u9009\u4E2D\u7684\u8D26\u53F7\u5417\uFF1F</p>',
      frag
    );
  }

  function executeBulkAction(action) {
    hideModal();
    var names = Array.from(state.selected);
    var verbMap = { delete: '\u5220\u9664', disable: '\u7981\u7528', enable: '\u542F\u7528' };
    var verb = verbMap[action];

    var tasks = names.map(function (name) {
      return function () {
        if (action === 'delete') {
          return api('DELETE', '/v0/management/auth-files?name=' + encodeURIComponent(name)).then(function () {
            state.files = state.files.filter(function (f) { return f.name !== name; });
          });
        } else {
          var disable = action === 'disable';
          return api('PATCH', '/v0/management/auth-files/status', { name: name, disabled: disable }).then(function () {
            var f = state.files.find(function (x) { return x.name === name; });
            if (f) f.disabled = disable;
          });
        }
      };
    });

    runWithConcurrency(tasks, state.concurrency).then(function (results) {
      var ok = results.filter(function (r) { return r.status === 'fulfilled'; }).length;
      var fail = results.filter(function (r) { return r.status === 'rejected'; }).length;
      state.selected.clear();
      applyFilters();
      renderOverview();
      renderMaintainSummary();
      toast(verb + ': ' + ok + ' \u6210\u529F, ' + fail + ' \u5931\u8D25', ok > 0 ? 'success' : 'error');
    });
  }

  // --------------- Promise Pool (Concurrency Control) ---------------
  function runWithConcurrency(taskFns, concurrency) {
    var results = [];
    var index = 0;

    function next() {
      if (index >= taskFns.length) {
        return Promise.resolve(null);
      }
      var currentIndex = index++;
      var taskFn = taskFns[currentIndex];
      return taskFn().then(
        function (value) {
          results[currentIndex] = { status: 'fulfilled', value: value };
        },
        function (reason) {
          results[currentIndex] = { status: 'rejected', reason: reason };
        }
      ).then(next);
    }

    var workers = [];
    var limit = Math.min(concurrency, taskFns.length);
    for (var i = 0; i < limit; i++) {
      workers.push(next());
    }

    return Promise.all(workers).then(function () {
      return results;
    });
  }

  // --------------- Maintain Tab ---------------
  function renderMaintainSummary() {
    var files = state.files;
    var total = files.length;
    var active = files.filter(function (f) { return !f.disabled && !f.unavailable; }).length;
    var disabled = files.filter(function (f) { return f.disabled; }).length;
    var unavailable = files.filter(function (f) { return f.unavailable; }).length;

    $('#maintain-total').textContent = total.toLocaleString();
    $('#maintain-active').textContent = active.toLocaleString();
    $('#maintain-disabled').textContent = disabled.toLocaleString();
    $('#maintain-unavailable').textContent = unavailable.toLocaleString();
  }

  function showMaintainProgress(title) {
    var progressEl = $('#maintain-progress');
    progressEl.style.display = 'block';
    $('#maintain-progress-title').textContent = title;
    $('#maintain-progress-fill').style.width = '0%';
    $('#maintain-progress-text').textContent = '0%';
    $('#maintain-progress-log').innerHTML = '';
    state.maintainRunning = true;
    setMaintainButtonsDisabled(true);
  }

  function updateMaintainProgress(done, total) {
    var pct = total > 0 ? Math.round((done / total) * 100) : 0;
    $('#maintain-progress-fill').style.width = pct + '%';
    $('#maintain-progress-text').textContent = pct + '%';
  }

  function appendMaintainLog(text, isError) {
    var log = $('#maintain-progress-log');
    var item = document.createElement('div');
    item.className = 'progress-log-item ' + (isError ? 'error' : 'success');
    item.textContent = text;
    log.appendChild(item);
    log.scrollTop = log.scrollHeight;
  }

  function finishMaintainProgress(summary) {
    state.maintainRunning = false;
    setMaintainButtonsDisabled(false);
    $('#maintain-progress-title').textContent = summary;
    updateMaintainProgress(1, 1);
  }

  function setMaintainButtonsDisabled(disabled) {
    var btns = $$('.maintain-actions .btn');
    for (var i = 0; i < btns.length; i++) {
      btns[i].disabled = disabled;
    }
  }

  function cleanInvalid() {
    var targets = state.files.filter(function (f) { return f.unavailable; });
    if (targets.length === 0) {
      toast('\u6CA1\u6709\u4E0D\u53EF\u7528\u7684\u8D26\u53F7\u9700\u8981\u6E05\u7406', 'info');
      return;
    }

    var frag = document.createDocumentFragment();
    var cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = '\u53D6\u6D88';
    cancel.setAttribute('data-action', 'close-modal');

    var confirm = document.createElement('button');
    confirm.className = 'btn btn-danger';
    confirm.textContent = '\u786E\u8BA4\u6E05\u7406 ' + targets.length + ' \u4E2A';
    confirm.setAttribute('data-action', 'confirm-clean-invalid');

    frag.appendChild(cancel);
    frag.appendChild(confirm);

    showModal(
      '\u6E05\u7406\u5931\u6548\u8D26\u53F7',
      '<p>\u5C06\u5220\u9664 <strong>' + targets.length + '</strong> \u4E2A\u4E0D\u53EF\u7528\u7684\u8D26\u53F7\u3002\u6B64\u64CD\u4F5C\u4E0D\u53EF\u64A4\u9500\u3002</p>',
      frag
    );
  }

  function executeCleanInvalid() {
    hideModal();
    var targets = state.files.filter(function (f) { return f.unavailable; });
    showMaintainProgress('\u6B63\u5728\u6E05\u7406\u5931\u6548\u8D26\u53F7...');

    var done = 0;
    var ok = 0;
    var fail = 0;
    var total = targets.length;

    var tasks = targets.map(function (f) {
      return function () {
        return api('DELETE', '/v0/management/auth-files?name=' + encodeURIComponent(f.name))
          .then(function () {
            state.files = state.files.filter(function (x) { return x.name !== f.name; });
            ok++;
            appendMaintainLog('\u2713 \u5DF2\u5220\u9664: ' + f.name, false);
          })
          .catch(function (err) {
            fail++;
            appendMaintainLog('\u2717 \u5220\u9664\u5931\u8D25: ' + f.name + ' - ' + err.message, true);
          })
          .then(function () {
            done++;
            updateMaintainProgress(done, total);
          });
      };
    });

    runWithConcurrency(tasks, state.concurrency).then(function () {
      finishMaintainProgress('\u6E05\u7406\u5B8C\u6210: ' + ok + ' \u6210\u529F, ' + fail + ' \u5931\u8D25');
      applyFilters();
      renderOverview();
      renderMaintainSummary();
    });
  }

  function cleanDisabled() {
    var targets = state.files.filter(function (f) { return f.disabled; });
    if (targets.length === 0) {
      toast('\u6CA1\u6709\u5DF2\u7981\u7528\u7684\u8D26\u53F7\u9700\u8981\u6E05\u7406', 'info');
      return;
    }

    var frag = document.createDocumentFragment();
    var cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = '\u53D6\u6D88';
    cancel.setAttribute('data-action', 'close-modal');

    var confirm = document.createElement('button');
    confirm.className = 'btn btn-warning';
    confirm.textContent = '\u786E\u8BA4\u6E05\u7406 ' + targets.length + ' \u4E2A';
    confirm.setAttribute('data-action', 'confirm-clean-disabled');

    frag.appendChild(cancel);
    frag.appendChild(confirm);

    showModal(
      '\u6E05\u7406\u5DF2\u7981\u7528\u8D26\u53F7',
      '<p>\u5C06\u5220\u9664 <strong>' + targets.length + '</strong> \u4E2A\u5DF2\u7981\u7528\u7684\u8D26\u53F7\u3002\u6B64\u64CD\u4F5C\u4E0D\u53EF\u64A4\u9500\u3002</p>',
      frag
    );
  }

  function executeCleanDisabled() {
    hideModal();
    var targets = state.files.filter(function (f) { return f.disabled; });
    showMaintainProgress('\u6B63\u5728\u6E05\u7406\u5DF2\u7981\u7528\u8D26\u53F7...');

    var done = 0;
    var ok = 0;
    var fail = 0;
    var total = targets.length;

    var tasks = targets.map(function (f) {
      return function () {
        return api('DELETE', '/v0/management/auth-files?name=' + encodeURIComponent(f.name))
          .then(function () {
            state.files = state.files.filter(function (x) { return x.name !== f.name; });
            ok++;
            appendMaintainLog('\u2713 \u5DF2\u5220\u9664: ' + f.name, false);
          })
          .catch(function (err) {
            fail++;
            appendMaintainLog('\u2717 \u5220\u9664\u5931\u8D25: ' + f.name + ' - ' + err.message, true);
          })
          .then(function () {
            done++;
            updateMaintainProgress(done, total);
          });
      };
    });

    runWithConcurrency(tasks, state.concurrency).then(function () {
      finishMaintainProgress('\u6E05\u7406\u5B8C\u6210: ' + ok + ' \u6210\u529F, ' + fail + ' \u5931\u8D25');
      applyFilters();
      renderOverview();
      renderMaintainSummary();
    });
  }

  function disableUnavailable() {
    var targets = state.files.filter(function (f) { return f.unavailable && !f.disabled; });
    if (targets.length === 0) {
      toast('\u6CA1\u6709\u9700\u8981\u7981\u7528\u7684\u4E0D\u53EF\u7528\u8D26\u53F7', 'info');
      return;
    }

    var frag = document.createDocumentFragment();
    var cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = '\u53D6\u6D88';
    cancel.setAttribute('data-action', 'close-modal');

    var confirm = document.createElement('button');
    confirm.className = 'btn btn-primary';
    confirm.textContent = '\u786E\u8BA4\u7981\u7528 ' + targets.length + ' \u4E2A';
    confirm.setAttribute('data-action', 'confirm-disable-unavailable');

    frag.appendChild(cancel);
    frag.appendChild(confirm);

    showModal(
      '\u7981\u7528\u4E0D\u53EF\u7528\u8D26\u53F7',
      '<p>\u5C06\u7981\u7528 <strong>' + targets.length + '</strong> \u4E2A\u4E0D\u53EF\u7528\u7684\u8D26\u53F7\u3002</p>',
      frag
    );
  }

  function executeDisableUnavailable() {
    hideModal();
    var targets = state.files.filter(function (f) { return f.unavailable && !f.disabled; });
    showMaintainProgress('\u6B63\u5728\u7981\u7528\u4E0D\u53EF\u7528\u8D26\u53F7...');

    var done = 0;
    var ok = 0;
    var fail = 0;
    var total = targets.length;

    var tasks = targets.map(function (f) {
      return function () {
        return api('PATCH', '/v0/management/auth-files/status', { name: f.name, disabled: true })
          .then(function () {
            f.disabled = true;
            ok++;
            appendMaintainLog('\u2713 \u5DF2\u7981\u7528: ' + f.name, false);
          })
          .catch(function (err) {
            fail++;
            appendMaintainLog('\u2717 \u7981\u7528\u5931\u8D25: ' + f.name + ' - ' + err.message, true);
          })
          .then(function () {
            done++;
            updateMaintainProgress(done, total);
          });
      };
    });

    runWithConcurrency(tasks, state.concurrency).then(function () {
      finishMaintainProgress('\u7981\u7528\u5B8C\u6210: ' + ok + ' \u6210\u529F, ' + fail + ' \u5931\u8D25');
      applyFilters();
      renderOverview();
      renderMaintainSummary();
    });
  }

  function enableAll() {
    var targets = state.files.filter(function (f) { return f.disabled; });
    if (targets.length === 0) {
      toast('\u6CA1\u6709\u5DF2\u7981\u7528\u7684\u8D26\u53F7\u9700\u8981\u542F\u7528', 'info');
      return;
    }

    var frag = document.createDocumentFragment();
    var cancel = document.createElement('button');
    cancel.className = 'btn btn-ghost';
    cancel.textContent = '\u53D6\u6D88';
    cancel.setAttribute('data-action', 'close-modal');

    var confirm = document.createElement('button');
    confirm.className = 'btn btn-success';
    confirm.textContent = '\u786E\u8BA4\u542F\u7528 ' + targets.length + ' \u4E2A';
    confirm.setAttribute('data-action', 'confirm-enable-all');

    frag.appendChild(cancel);
    frag.appendChild(confirm);

    showModal(
      '\u542F\u7528\u5168\u90E8\u8D26\u53F7',
      '<p>\u5C06\u542F\u7528 <strong>' + targets.length + '</strong> \u4E2A\u5DF2\u7981\u7528\u7684\u8D26\u53F7\u3002</p>',
      frag
    );
  }

  function executeEnableAll() {
    hideModal();
    var targets = state.files.filter(function (f) { return f.disabled; });
    showMaintainProgress('\u6B63\u5728\u542F\u7528\u5168\u90E8\u8D26\u53F7...');

    var done = 0;
    var ok = 0;
    var fail = 0;
    var total = targets.length;

    var tasks = targets.map(function (f) {
      return function () {
        return api('PATCH', '/v0/management/auth-files/status', { name: f.name, disabled: false })
          .then(function () {
            f.disabled = false;
            ok++;
            appendMaintainLog('\u2713 \u5DF2\u542F\u7528: ' + f.name, false);
          })
          .catch(function (err) {
            fail++;
            appendMaintainLog('\u2717 \u542F\u7528\u5931\u8D25: ' + f.name + ' - ' + err.message, true);
          })
          .then(function () {
            done++;
            updateMaintainProgress(done, total);
          });
      };
    });

    runWithConcurrency(tasks, state.concurrency).then(function () {
      finishMaintainProgress('\u542F\u7528\u5B8C\u6210: ' + ok + ' \u6210\u529F, ' + fail + ' \u5931\u8D25');
      applyFilters();
      renderOverview();
      renderMaintainSummary();
    });
  }

  // --------------- Upload ---------------
  function initUpload() {
    var dropzone = $('#dropzone');
    var fileInput = $('#file-input');

    dropzone.addEventListener('dragover', function (e) {
      e.preventDefault();
      dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', function () {
      dropzone.classList.remove('dragover');
    });
    dropzone.addEventListener('drop', function (e) {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      handleFiles(e.dataTransfer.files);
    });
    dropzone.addEventListener('click', function (e) {
      if (e.target.tagName !== 'LABEL') {
        fileInput.click();
      }
    });
    fileInput.addEventListener('change', function () {
      handleFiles(fileInput.files);
      fileInput.value = '';
    });
  }

  function handleFiles(fileList) {
    var queue = $('#upload-queue');
    var uploadPromises = [];

    for (var i = 0; i < fileList.length; i++) {
      var file = fileList[i];
      if (!file.name.endsWith('.json')) {
        toast('\u53EA\u63A5\u53D7 .json \u6587\u4EF6', 'error');
        continue;
      }
      uploadPromises.push(uploadSingleFile(file, queue));
    }

    Promise.all(uploadPromises).then(function () {
      refreshData();
    });
  }

  function uploadSingleFile(file, queue) {
    var item = document.createElement('div');
    item.className = 'upload-item';

    var filenameSpan = document.createElement('span');
    filenameSpan.className = 'filename';
    filenameSpan.textContent = file.name;

    var statusSpan = document.createElement('span');
    statusSpan.className = 'upload-status pending';
    statusSpan.textContent = '\u4E0A\u4F20\u4E2D...';

    item.appendChild(filenameSpan);
    item.appendChild(statusSpan);
    queue.prepend(item);

    var fd = new FormData();
    fd.append('file', file);

    return api('POST', '/v0/management/auth-files', fd)
      .then(function () {
        statusSpan.textContent = '\u6210\u529F';
        statusSpan.className = 'upload-status success';
        toast('\u5DF2\u4E0A\u4F20 ' + file.name, 'success');
      })
      .catch(function (err) {
        statusSpan.textContent = '\u5931\u8D25';
        statusSpan.className = 'upload-status error';
        toast('\u4E0A\u4F20\u5931\u8D25: ' + err.message, 'error');
      });
  }

  // --------------- Settings Tab ---------------
  function renderSettingsInstances() {
    var container = $('#settings-instances');
    var instances = loadInstances();
    container.innerHTML = '';

    if (instances.length === 0) {
      var empty = document.createElement('p');
      empty.className = 'text-muted';
      empty.textContent = '\u6682\u65E0\u5DF2\u4FDD\u5B58\u7684\u5B9E\u4F8B';
      container.appendChild(empty);
      return;
    }

    for (var i = 0; i < instances.length; i++) {
      var inst = instances[i];
      var item = document.createElement('div');
      item.className = 'settings-instance-item';

      var info = document.createElement('div');
      info.className = 'si-info';

      var nameEl = document.createElement('div');
      nameEl.className = 'si-name';
      nameEl.textContent = inst.name;

      var urlEl = document.createElement('div');
      urlEl.className = 'si-url';
      urlEl.textContent = inst.url;

      info.appendChild(nameEl);
      info.appendChild(urlEl);
      item.appendChild(info);

      if (inst.id === state.currentInstanceId) {
        var activeTag = document.createElement('span');
        activeTag.className = 'si-active';
        activeTag.textContent = '\u5F53\u524D\u8FDE\u63A5';
        item.appendChild(activeTag);
      }

      var removeBtn = document.createElement('button');
      removeBtn.className = 'btn btn-danger btn-sm';
      removeBtn.textContent = '\u5220\u9664';
      removeBtn.setAttribute('data-action', 'settings-remove-instance');
      removeBtn.setAttribute('data-instance-id', inst.id);
      item.appendChild(removeBtn);

      container.appendChild(item);
    }
  }

  // --------------- Refresh ---------------
  function refreshData() {
    return api('GET', '/v0/management/auth-files')
      .then(function (data) {
        state.files = data.files || [];
        applyFilters();
        renderOverview();
        renderMaintainSummary();
        toast('\u6570\u636E\u5DF2\u5237\u65B0', 'success');
      })
      .catch(function (err) {
        toast('\u5237\u65B0\u5931\u8D25: ' + err.message, 'error');
      });
  }

  // --------------- Utilities ---------------
  function formatTime(str) {
    if (!str) return '-';
    try {
      var d = new Date(str);
      var now = new Date();
      var diff = now - d;
      if (diff < 60000) return '\u521A\u521A';
      if (diff < 3600000) return Math.floor(diff / 60000) + ' \u5206\u949F\u524D';
      if (diff < 86400000) return Math.floor(diff / 3600000) + ' \u5C0F\u65F6\u524D';
      if (diff < 604800000) return Math.floor(diff / 86400000) + ' \u5929\u524D';
      return d.toLocaleDateString('zh-CN');
    } catch (e) {
      return str;
    }
  }

  // --------------- Event Delegation ---------------
  function bindEvents() {
    // Global click delegation
    document.addEventListener('click', function (e) {
      var target = e.target.closest('[data-action]');
      if (!target) return;

      var action = target.getAttribute('data-action');

      switch (action) {
        case 'show-add-instance':
          $('#connect-form').style.display = 'block';
          break;

        case 'cancel-add-instance':
          $('#connect-form').style.display = loadInstances().length === 0 ? 'block' : 'none';
          break;

        case 'toggle-password': {
          var input = $('#token');
          var eyeOn = $('.icon-eye');
          var eyeOff = $('.icon-eye-off');
          if (input.type === 'password') {
            input.type = 'text';
            eyeOn.style.display = 'none';
            eyeOff.style.display = 'block';
          } else {
            input.type = 'password';
            eyeOn.style.display = 'block';
            eyeOff.style.display = 'none';
          }
          break;
        }

        case 'remove-instance': {
          var instId = target.getAttribute('data-instance-id');
          e.stopPropagation();
          removeInstance(instId);
          renderInstanceList();
          break;
        }

        case 'toggle-theme':
          toggleTheme();
          break;

        case 'refresh':
          refreshData();
          break;

        case 'disconnect':
          state.baseUrl = '';
          state.token = '';
          state.files = [];
          state.filtered = [];
          state.selected.clear();
          state.currentInstanceId = null;
          showScreen('connect-screen');
          renderInstanceList();
          break;

        case 'select-all':
          if (state.selected.size === state.filtered.length) {
            state.selected.clear();
          } else {
            state.filtered.forEach(function (f) { state.selected.add(f.name); });
          }
          renderTable();
          break;

        case 'bulk-delete':
          bulkAction('delete');
          break;

        case 'bulk-disable':
          bulkAction('disable');
          break;

        case 'bulk-enable':
          bulkAction('enable');
          break;

        case 'view-account':
          viewAccount(target.getAttribute('data-name'));
          break;

        case 'download-account':
          downloadAccount(target.getAttribute('data-name'));
          break;

        case 'enable-account':
          toggleAccountStatus(target.getAttribute('data-name'), false);
          break;

        case 'disable-account':
          toggleAccountStatus(target.getAttribute('data-name'), true);
          break;

        case 'delete-account':
          deleteAccount(target.getAttribute('data-name'));
          break;

        case 'confirm-delete':
          executeDeleteAccount(target.getAttribute('data-name'));
          break;

        case 'confirm-bulk':
          executeBulkAction(target.getAttribute('data-bulk-type'));
          break;

        case 'close-modal':
          hideModal();
          break;

        case 'clean-invalid':
          cleanInvalid();
          break;

        case 'clean-disabled':
          cleanDisabled();
          break;

        case 'disable-unavailable':
          disableUnavailable();
          break;

        case 'enable-all':
          enableAll();
          break;

        case 'confirm-clean-invalid':
          executeCleanInvalid();
          break;

        case 'confirm-clean-disabled':
          executeCleanDisabled();
          break;

        case 'confirm-disable-unavailable':
          executeDisableUnavailable();
          break;

        case 'confirm-enable-all':
          executeEnableAll();
          break;

        case 'settings-remove-instance': {
          var sid = target.getAttribute('data-instance-id');
          removeInstance(sid);
          renderSettingsInstances();
          renderInstanceSwitcher();
          toast('\u5B9E\u4F8B\u5DF2\u5220\u9664', 'success');
          break;
        }
      }
    });

    // Instance list click (connect)
    $('#instance-list').addEventListener('click', function (e) {
      var item = e.target.closest('.instance-item');
      if (!item) return;
      // Don't connect if clicking remove button
      if (e.target.closest('[data-action="remove-instance"]')) return;

      var id = item.getAttribute('data-instance-id');
      connectToInstance(id).catch(function (err) {
        toast('\u8FDE\u63A5\u5931\u8D25: ' + err.message, 'error');
      });
    });

    // Connect form submit
    $('#connect-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var nameVal = $('#instance-name').value.trim();
      var urlVal = $('#base-url').value.trim();
      var tokenVal = $('#token').value.trim();

      if (!nameVal || !urlVal || !tokenVal) return;

      var btn = $('#save-instance-btn');
      var text = btn.querySelector('.btn-text');
      var loading = btn.querySelector('.btn-loading');
      text.style.display = 'none';
      loading.style.display = '';
      btn.disabled = true;

      var id = addInstance(nameVal, urlVal, tokenVal);

      connectToInstance(id)
        .catch(function (err) {
          removeInstance(id);
          toast('\u8FDE\u63A5\u5931\u8D25: ' + err.message, 'error');
        })
        .then(function () {
          text.style.display = '';
          loading.style.display = 'none';
          btn.disabled = false;
          // Reset form
          $('#instance-name').value = '';
          $('#base-url').value = '';
          $('#token').value = '';
        });
    });

    // Tab switching
    var tabsContainer = $('.tabs');
    tabsContainer.addEventListener('click', function (e) {
      var tab = e.target.closest('.tab');
      if (!tab) return;
      var tabs = $$('.tab');
      for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
      tab.classList.add('active');
      var panels = $$('.tab-panel');
      for (var j = 0; j < panels.length; j++) panels[j].classList.remove('active');
      var targetPanel = $('#tab-' + tab.getAttribute('data-tab'));
      if (targetPanel) targetPanel.classList.add('active');
    });

    // Search
    var searchTimeout;
    $('#search-input').addEventListener('input', function (e) {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(function () {
        state.search = e.target.value;
        applyFilters();
      }, 200);
    });

    // Filters
    $('#filter-type').addEventListener('change', function (e) {
      state.filterType = e.target.value;
      applyFilters();
    });

    $('#filter-provider').addEventListener('change', function (e) {
      state.filterProvider = e.target.value;
      applyFilters();
    });

    $('#filter-status').addEventListener('change', function (e) {
      state.filterStatus = e.target.value;
      applyFilters();
    });

    // Sort headers
    var thead = $('#accounts-table thead');
    thead.addEventListener('click', function (e) {
      var th = e.target.closest('.sortable');
      if (!th) return;
      var key = th.getAttribute('data-sort');
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = key;
        state.sortDir = 'asc';
      }
      var sortables = $$('.sortable');
      for (var i = 0; i < sortables.length; i++) {
        sortables[i].classList.remove('asc', 'desc');
      }
      th.classList.add(state.sortDir);
      applyFilters();
    });

    // Check all
    $('#check-all').addEventListener('change', function (e) {
      if (e.target.checked) {
        state.filtered.forEach(function (f) { state.selected.add(f.name); });
      } else {
        state.selected.clear();
      }
      renderTable();
    });

    // Row checks (delegated)
    $('#accounts-body').addEventListener('change', function (e) {
      if (e.target.classList.contains('row-check')) {
        var name = e.target.getAttribute('data-name');
        if (e.target.checked) {
          state.selected.add(name);
        } else {
          state.selected.delete(name);
        }
        updateBulkButtons();
      }
    });

    // Pagination (delegated)
    $('#pagination').addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-page]');
      if (!btn || btn.disabled) return;
      state.page = parseInt(btn.getAttribute('data-page'), 10);
      renderTable();
      var wrapper = $('.table-wrapper');
      if (wrapper) wrapper.scrollTop = 0;
    });

    // Modal overlay close
    $('#modal-overlay').addEventListener('click', function (e) {
      if (e.target === e.currentTarget) hideModal();
    });

    // Escape key
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') hideModal();
    });

    // Instance switcher
    $('#instance-switcher').addEventListener('change', function (e) {
      var id = e.target.value;
      if (id && id !== state.currentInstanceId) {
        connectToInstance(id).catch(function (err) {
          toast('\u5207\u6362\u5931\u8D25: ' + err.message, 'error');
          // Revert selection
          e.target.value = state.currentInstanceId;
        });
      }
    });

    // Settings: page size
    $('#page-size-select').addEventListener('change', function (e) {
      state.pageSize = parseInt(e.target.value, 10);
      var settings = loadSettings();
      settings.pageSize = e.target.value;
      saveSettings(settings);
      applyFilters();
    });

    // Settings: concurrency
    $('#concurrency-select').addEventListener('change', function (e) {
      state.concurrency = parseInt(e.target.value, 10);
      var settings = loadSettings();
      settings.concurrency = e.target.value;
      saveSettings(settings);
    });
  }

  // --------------- Init ---------------
  function init() {
    initTheme();
    applySettings();
    renderInstanceList();
    bindEvents();
    initUpload();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
