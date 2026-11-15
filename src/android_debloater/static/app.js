'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const e = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const categories = {
  communication: 'Communication', money: 'Money & shopping', entertainment: 'Entertainment',
  tools: 'Tools & daily use', system: 'System', other: 'Other'
};
const state = {
  devices: [], current: null, detail: null, tab: 'installed', selected: new Set(), inspecting: null,
  detailSignature: null, deviceSignature: null, settings: {}, busy: false, activeBatch: null,
  batchBusy: false, reviewBusy: false, loadVersion: 0, rows: [],
  dismissedNotice: null, editOriginal: null
};
const date = value => value ? new Date(value).toLocaleString([], {dateStyle:'medium', timeStyle:'short'}) : 'Never';
const title = value => value ? value[0].toUpperCase() + value.slice(1) : '';
const status = value => ({'installed/enabled':'Enabled', 'installed/disabled':'Disabled', 'unverified':'Needs checking'}[value] || title(value));
const appType = value => value === 'third-party' ? 'Downloaded' : title(value);
const token = $('meta[name="session-token"]').content;

async function api(path, body) {
  const options = body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json','X-Session-Token':token}, body:JSON.stringify(body)
  };
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error("Can't reach Android Debloater. Make sure it's running, then refresh."); }
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the values and try again.');
  return data;
}
function notice(message, success = false, background = false) {
  if (background && message && message === state.dismissedNotice) return;
  state.dismissedNotice = null;
  $('#notice-message').textContent = message;
  $('#notice').classList.toggle('success', success);
  $('#notice').hidden = !message;
}
function handle(callback, background = false) {
  return async event => {
    try { await callback(event); } catch (error) { notice(error.message, false, background); }
  };
}
function selectPhone(id) {
  if (state.current === id) return;
  state.current = id;
  state.loadVersion++;
  state.detail = null;
  state.rows = [];
  state.selected.clear();
  state.detailSignature = null;
  closeInspector();
  $('#edit-dialog').close();
  $('#review-dialog').close();
  $('#device-workspace').hidden = true;
  $('#empty-device').hidden = !!id;
}
async function refresh(force = false) {
  if (state.busy || state.reviewBusy) return;
  state.busy = true;
  $('#scan-button').disabled = true;
  $('#scan-button').textContent = 'Refreshing…';
  $('#empty-refresh').disabled = true;
  $('#empty-refresh').textContent = 'Searching…';
  try {
    const result = state.activeBatch?.running
      ? await api('/api/devices')
      : await api('/api/refresh?force=' + force, {});
    state.devices = result.devices.sort((a,b) => Number(b.connection_state === 'Connected') - Number(a.connection_state === 'Connected') || (b.last_seen || '').localeCompare(a.last_seen || ''));
    const connected = state.devices.filter(d => d.connection_state === 'Connected');
    if (!connected.some(d => d.id === state.current)) {
      selectPhone(connected[0]?.id || null);
    }
    renderConnection(result.pending, connected);
    if (result.error) notice(result.error, false, !force);
    else if (Object.keys(result.storage_errors).length) notice(Object.values(result.storage_errors).join(' '), false, !force);
    else if (force) notice('');
    await loadDevice();
  } finally {
    state.busy = false;
    $('#scan-button').disabled = false;
    $('#scan-button').textContent = 'Refresh';
    $('#empty-refresh').disabled = false;
    $('#empty-refresh').textContent = 'Search for device';
  }
}
function renderConnection(pending, connected) {
  const signature = JSON.stringify([state.current, connected.map(d => [d.id,d.nickname,d.model]), pending]);
  if (signature === state.deviceSignature) return;
  state.deviceSignature = signature;
  $('#phone-choice').hidden = connected.length < 2;
  $('#phone-select').innerHTML = connected.map(d => `<option value="${e(d.id)}" ${d.id === state.current ? 'selected' : ''}>${e(d.nickname || d.model)}</option>`).join('');
  $('#connection-help').hidden = !pending.length;
  $('#connection-help').innerHTML = pending.map((d,i) => `<div class="warning pending-device"><strong>${e(d.model)}</strong><p>${e(d.reason || 'Unlock your phone and accept the connection prompt, then refresh.')}</p>${d.match_required ? `<label>Match saved history<select id="match-${i}"><option value="new">This is a new phone</option>${state.devices.map(saved => `<option value="${e(saved.id)}">${e(saved.nickname || saved.model)}</option>`).join('')}</select></label><button data-match="${i}">Continue</button>` : ''}</div>`).join('');
  $$('[data-match]').forEach(button => button.onclick = handle(async () => {
    const i = Number(button.dataset.match);
    const result = await api('/api/match/' + $('#match-' + i).value, {transport:pending[i].transport});
    selectPhone(result.device_id);
    await refresh(true);
  }));
}
async function loadDevice() {
  const version = ++state.loadVersion;
  if (!state.current) return;
  const detail = await api('/api/devices/' + state.current);
  if (version !== state.loadVersion) return;
  if (!detail.connected) { selectPhone(null); return; }
  const signature = JSON.stringify({...detail, device:{...detail.device, last_seen:null}});
  state.detail = detail;
  if (signature === state.detailSignature) return;
  state.detailSignature = signature;
  $('#empty-device').hidden = true;
  $('#device-workspace').hidden = false;
  const d = detail.device;
  $('#device-title').textContent = d.nickname || d.model;
  const warnings = [...d.blockers];
  if (d.reconciliation_required) warnings.push('This phone was reset or its profile changed. Refresh it before continuing.');
  $('#device-warning').hidden = !warnings.length;
  $('#device-warning').innerHTML = warnings.map(w => `<p>${e(w)}</p>`).join('') + (d.reconciliation_required ? '<button id="reconcile-button">Confirm this phone</button>' : '');
  if ($('#reconcile-button')) $('#reconcile-button').onclick = handle(async () => {
    await api(`/api/devices/${state.current}/reconcile`, {});
    await refresh(true);
  });
  $('#installed-count').textContent = detail.packages.filter(p => p.installed).length;
  $('#removed-count').textContent = detail.removed.length;
  renderRows();
  if (state.tab === 'history') renderHistory();
  if (state.inspecting && $('#inspector').open && !$('#edit-dialog').open) inspect(state.inspecting);
}
function filteredRows() {
  if (!state.detail) return [];
  const removed = new Map(state.detail.removed.map(row => [row.package, row]));
  const term = $('#search').value.toLowerCase().trim();
  return state.detail.packages.filter(p => state.tab === 'removed' ? removed.has(p.id) : p.installed)
    .map(p => ({...p, removal:removed.get(p.id)}))
    .filter(p => !term || [p.name,p.id,p.notes,p.purpose,...p.purposes.map(tag => categories[tag])].join(' ').toLowerCase().includes(term))
    .filter(p => !$('#filter-installation').value || p.installation === $('#filter-installation').value)
    .filter(p => !$('#filter-purpose').value || (p.purposes.length ? p.purposes : ['other']).includes($('#filter-purpose').value))
    .filter(p => !$('#filter-usefulness').value || p.usefulness === $('#filter-usefulness').value)
    .filter(p => !$('#filter-state').value || p.state === $('#filter-state').value)
    .sort((a,b) => String(a[$('#sort').value]).localeCompare(String(b[$('#sort').value])) || a.id.localeCompare(b.id));
}
function canSelect(p) {
  return state.tab === 'removed' ? p.removal?.eligible : p.installed && !p.protection && p.state !== 'unverified';
}
function renderRows() {
  const focused = document.activeElement?.dataset;
  state.rows = filteredRows();
  const allIds = new Set(state.tab === 'removed'
    ? state.detail.removed.map(r => r.package)
    : state.detail.packages.filter(p => p.installed).map(p => p.id));
  state.selected.forEach(id => { if (!allIds.has(id)) state.selected.delete(id); });
  $('#app-rows').innerHTML = state.rows.map(p => {
    const rowStatus = state.tab === 'removed' ? p.removal.recovery : p.protection ? 'Protected' : status(p.state);
    return `<tr class="${state.selected.has(p.id) ? 'selected' : ''}">
      <td class="check-cell"><input type="checkbox" data-select="${e(p.id)}" aria-label="Select ${e(p.name)}" ${state.selected.has(p.id) ? 'checked' : ''} ${canSelect(p) ? '' : 'disabled'}></td>
      <td><button class="app-name" data-inspect="${e(p.id)}">${e(p.name)}</button><span class="app-category">${e(p.purposes.map(tag => categories[tag]).join(', ') || 'Other')}</span>${state.tab === 'removed' ? `<span class="removed-date">Removed ${e(date(p.removal.removed_at))}</span>` : ''}<span class="mobile-state">${e(rowStatus)}</span></td>
      <td class="type-col">${e(appType(p.installation))}</td>
      <td class="state-col"><span class="${p.protection && state.tab === 'installed' ? 'protected-label' : 'state-text'}">${e(rowStatus)}</span>${state.tab === 'removed' ? `<span class="removed-date">${e(status(p.state))}</span>` : ''}</td>
    </tr>`;
  }).join('');
  $('#result-count').textContent = `${state.rows.length} ${state.rows.length === 1 ? 'app' : 'apps'}`;
  $('#no-results').hidden = state.rows.length > 0;
  $('#no-results').textContent = state.tab === 'removed' && !state.detail.removed.length
    ? 'Apps you remove will appear here.' : 'No matching apps. Try another search or reset the filters.';
  renderSelection();
  if (focused?.select) $(`[data-select="${focused.select}"]`)?.focus({preventScroll:true});
  else if (focused?.inspect) $(`[data-inspect="${focused.inspect}"]`)?.focus({preventScroll:true});
}
function renderSelection() {
  const eligible = state.rows.filter(canSelect);
  $('#select-all').checked = eligible.length > 0 && eligible.every(p => state.selected.has(p.id));
  $('#select-all').indeterminate = eligible.some(p => state.selected.has(p.id)) && !$('#select-all').checked;
  $('#select-all').disabled = !eligible.length;
  $$('[data-select]').forEach(box => {
    box.checked = state.selected.has(box.dataset.select);
    box.closest('tr').classList.toggle('selected', box.checked);
  });
  $('#selection-count').textContent = state.selected.size ? `${state.selected.size} selected` : `Select apps to ${state.tab === 'removed' ? 'restore' : 'uninstall'}`;
  $('#clear-selection').hidden = !state.selected.size;
  $('#review-button').textContent = state.tab === 'removed' ? 'Review restore' : 'Review uninstall';
  $('#review-button').disabled = state.reviewBusy || !state.selected.size || !state.detail?.connected || state.detail.device.blockers.length > 0 || state.detail.device.reconciliation_required || !!state.activeBatch?.running;
}
function inspect(id) {
  const p = state.detail.packages.find(p => p.id === id);
  if (!p) return;
  state.inspecting = id;
  const removed = state.detail.removed.find(r => r.package === id);
  const recovery = removed?.recovery || (p.installation === 'system'
    ? 'You can restore this app while its system package remains on your phone.'
    : 'You will need to reinstall this app yourself.');
  const tags = [appType(p.installation), status(p.state), ...p.purposes.map(tag => categories[tag])];
  $('#app-details').innerHTML = `<h2 id="app-title">${e(p.name)}</h2><p class="package-id">${e(p.id)}</p><button class="quiet copy" id="copy-package">Copy package ID</button>
    <div class="detail-tags">${tags.map(t => `<span class="tag">${e(t)}</span>`).join('')}</div>
    <p class="app-description">${e(p.purpose)}</p>${p.protection ? `<p class="warning">${e(p.protection)}</p>` : ''}
    <section><h3>If you uninstall</h3><p>${e(p.impact)}</p></section>
    <section><h3>Getting it back</h3><p>${e(recovery)}</p>${removed ? `<p>Removed ${e(date(removed.removed_at))}</p>` : ''}${removed && p.installer === 'com.android.vending' ? `<a href="https://play.google.com/store/apps/details?id=${encodeURIComponent(p.id)}" target="_blank" rel="noopener noreferrer">Find on Google Play</a>` : ''}</section>
    ${p.notes ? `<section><h3>Your notes</h3><p>${e(p.notes)}</p></section>` : ''}
    <details><summary>Technical details</summary><p>Version ${e(p.version ?? 'unknown')}<br>Usefulness: ${e(title(p.usefulness))}<br>Source: ${e(p.source)}<br>Confidence: ${e(p.confidence)}<br>Name source: ${e(p.name_source)}</p></details>
    <button id="edit-button" class="edit-button">Edit details</button>`;
  $('#copy-package').onclick = handle(async () => { await navigator.clipboard.writeText(id); $('#copy-package').textContent = 'Copied'; });
  $('#edit-button').onclick = openEditor;
  if (!$('#inspector').open) { $('#inspector').showModal(); $('#inspector').scrollTop = 0; }
}
function closeInspector() {
  $('#inspector').close();
  state.inspecting = null;
}
function editorValues() {
  return {
    name:$('#edit-name').value || null,
    purposes:$$('#edit-purposes input:checked').map(input => input.value),
    usefulness:$('#edit-usefulness').value, notes:$('#edit-notes').value
  };
}
function openEditor() {
  const p = state.detail.packages.find(p => p.id === state.inspecting);
  $('#edit-package').textContent = p.id;
  $('#edit-name').value = p.name === p.id ? '' : p.name;
  $('#edit-usefulness').value = p.usefulness;
  $('#edit-notes').value = p.notes;
  $('#edit-global').checked = false;
  $('#edit-purposes').innerHTML = Object.entries(categories).map(([tag, label]) => `<label><input type="checkbox" value="${e(tag)}" ${p.purposes.includes(tag) ? 'checked' : ''}>${e(label)}</label>`).join('');
  state.editOriginal = editorValues();
  $('#edit-dialog').showModal();
}
function renderHistory() {
  const groups = new Map();
  for (const entry of [...state.detail.device.history].reverse()) {
    if (!groups.has(entry.batch_id)) groups.set(entry.batch_id, []);
    groups.get(entry.batch_id).push(entry);
  }
  $('#history-view').innerHTML = !groups.size ? '<div class="empty-results">Your uninstalls and restores will appear here.</div>' : [...groups.values()].map(entries => `<article class="history-batch"><h3>${entries[0].action === 'uninstall' ? 'Uninstall' : 'Restore'}</h3><p class="muted">${e(date(entries[0].timestamp))}</p>${entries.reverse().map(entry => `<div class="history-entry"><strong>${e(state.detail.packages.find(p => p.id === entry.package)?.name || entry.package)}</strong> <span class="tag outcome-${e(entry.status)}">${e(title(entry.status))}</span><p>${e(entry.verification || entry.outcome || 'Not attempted.')}</p><details><summary>Details</summary><p>${e(entry.package)}<br>Recovery: ${e(entry.recovery)}<br>Before: ${e(status(entry.before.state))}<br>Version: ${e(entry.before.version)}<br>User: ${e(entry.user_id)}</p><code>${e(entry.command.join(' '))}</code><p>${e(entry.outcome)}</p>${entry.observed_at ? `<p>Last seen ${e(status(entry.observed_state))} on ${e(date(entry.observed_at))}. The cause of an external change is unknown.</p>` : ''}</details></div>`).join('')}</article>`).join('');
}
async function review() {
  if (state.reviewBusy) return;
  state.reviewBusy = true;
  const action = state.tab === 'removed' ? 'restore' : 'uninstall';
  $('#review-button').disabled = true;
  $('#review-content').innerHTML = `<h2 id="review-title">Review ${action}</h2><p role="status">Preparing your selection…</p>`;
  $('#review-dialog').showModal();
  try {
    const plan = await api(`/api/devices/${state.current}/review`, {action, packages:[...state.selected]});
    if (!$('#review-dialog').open) return;
    const count = plan.packages.length;
    $('#review-content').innerHTML = `<h2 id="review-title">${title(plan.action)} ${count} ${count === 1 ? 'app' : 'apps'}?</h2>
      <p class="muted">${e(plan.device_name)}</p>
      <p>${plan.action === 'uninstall' ? 'App data may be kept, but this is not a backup or a guarantee of recovery.' : 'This brings back the system app, including its previous enabled or disabled setting. It does not restore backed-up data or an exact app version.'}</p>
      ${plan.packages.map(p => {
        const described = state.detail.packages.find(item => item.id === p.id);
        const recovery = plan.action === 'uninstall'
          ? (p.installation === 'system' ? 'Can be restored while available on this phone.' : 'Manual reinstall required.')
          : (plan.previous_enabled[p.id] >= 2 ? 'Will be restored disabled, as before.' : 'Will be restored enabled.');
        return `<div class="review-row"><strong>${e(described?.name || p.id)}</strong>${described?.name !== p.id ? `<p class="package-id">${e(p.id)}</p>` : ''}<p>${e(described?.impact || '')}</p><p>${e(recovery)}</p>${plan.action === 'uninstall' && p.state === 'installed/disabled' ? '<p>Currently disabled</p>' : ''}</div>`;
      }).join('')}
      ${Object.keys(plan.excluded).length ? `<section class="warning"><h3>These apps will stay</h3>${Object.entries(plan.excluded).map(([pkg, reason]) => `<p>${e(pkg)}: ${e(reason)}</p>`).join('')}</section>` : ''}
      <p class="muted">Your phone is checked again before changes begin.</p>
      ${state.settings.read_only ? '<p class="warning">Read-only mode. Changes are turned off.</p>' : ''}
      <div class="dialog-actions"><button id="cancel-review">Back</button><button id="confirm-plan" class="${plan.action === 'uninstall' ? 'danger' : 'primary'}" ${!count || state.settings.read_only ? 'disabled' : ''}>${title(plan.action)} ${count} ${count === 1 ? 'app' : 'apps'}</button></div>`;
    $('#cancel-review').onclick = () => $('#review-dialog').close();
    $('#confirm-plan').onclick = handle(async () => {
      $('#confirm-plan').disabled = true;
      $('#confirm-plan').textContent = 'Checking phone…';
      $('#cancel-review').disabled = true;
      try {
        state.activeBatch = await api(`/api/plans/${plan.id}/confirm`, {});
        state.selected.clear();
        $('#review-dialog').close();
        renderBatch();
        renderSelection();
        await pollBatch();
      } catch (error) {
        $('#review-dialog').close();
        throw error;
      }
    });
  } catch (error) {
    if ($('#review-dialog').open) {
      $('#review-content').innerHTML = `<h2 id="review-title">Couldn't prepare review</h2><p role="alert">${e(error.message)}</p><div class="dialog-actions"><button id="cancel-review">Back to apps</button></div>`;
      $('#cancel-review').onclick = () => $('#review-dialog').close();
    } else notice(error.message);
  } finally {
    state.reviewBusy = false;
    renderSelection();
  }
}
function renderBatch() {
  const batch = state.activeBatch;
  $('#batch').hidden = !batch;
  if (!batch) return;
  $('#batch').classList.toggle('finished', !batch.running);
  if (!batch.running) {
    const succeeded = batch.entries.filter(entry => entry.status === 'succeeded').length;
    const total = batch.entries.length;
    const complete = succeeded === total && !batch.error;
    const action = batch.action === 'uninstall' ? 'Removal' : 'Restore';
    const summary = complete
      ? `${batch.action === 'uninstall' ? 'Removed' : 'Restored'} ${succeeded} ${succeeded === 1 ? 'app' : 'apps'}`
      : `${action} ${succeeded === total ? 'needs attention' : `incomplete. ${succeeded} of ${total} confirmed`}`;
    $('#batch').innerHTML = `<div class="batch-top"><strong class="${complete ? 'outcome-succeeded' : 'outcome-unverified'}">${e(summary)}</strong><div class="batch-actions"><button id="batch-history" class="quiet">View history</button><button id="dismiss-batch" class="quiet" aria-label="Dismiss result">Dismiss</button></div></div>${batch.error ? `<p>${e(batch.error)}</p>` : ''}`;
    $('#batch-history').onclick = handle(async () => {
      selectPhone(batch.device_id);
      await loadDevice();
      $('[data-tab="history"]').click();
    });
    $('#dismiss-batch').onclick = () => { state.activeBatch = null; renderBatch(); };
    return;
  }
  const done = batch.entries.filter(entry => !['pending','attempting'].includes(entry.status)).length;
  $('#batch').innerHTML = `<div class="batch-top"><div><strong>${batch.action === 'uninstall' ? 'Uninstalling apps' : 'Restoring apps'}</strong><p>${e(batch.device_name)}</p></div><button id="stop-batch" ${batch.stop_requested ? 'disabled' : ''}>${batch.stop_requested ? 'Stopping after this app' : 'Stop after this app'}</button></div><progress value="${done}" max="${batch.entries.length || 1}" aria-label="Apps processed"></progress><p>${done} of ${batch.entries.length} apps${batch.error ? '. ' + e(batch.error) : ''}</p><ul>${batch.entries.map(entry => `<li class="outcome-${e(entry.status)}">${e(state.detail?.packages.find(p => p.id === entry.package)?.name || entry.package)}: ${e(title(entry.status))}${entry.verification ? '. ' + e(entry.verification) : ''}</li>`).join('')}</ul>`;
  $('#stop-batch').onclick = handle(async () => {
    state.activeBatch = await api(`/api/batches/${batch.id}/stop`, {});
    renderBatch();
  });
}
async function pollBatch() {
  if (!state.activeBatch?.running || state.batchBusy) return;
  state.batchBusy = true;
  try {
    state.activeBatch = await api('/api/batches/' + state.activeBatch.id);
    renderBatch();
    if (!state.activeBatch.running) { await loadDevice(); await refresh(true); }
  } finally { state.batchBusy = false; }
}
async function openSettings() {
  state.settings = await api('/api/settings');
  const s = state.settings, d = state.detail?.device;
  $('#settings-content').innerHTML = `${d ? `<form id="nickname-form"><label>Phone name<input id="nickname" maxlength="120" value="${e(d.nickname)}" placeholder="${e(d.model)}"></label><button type="submit">Save name</button></form>` : ''}
    <details><summary>Advanced settings</summary><h3>Connection</h3>
      <form id="adb-form"><label>ADB path<input id="adb-path" value="${e(s.adb_path || '')}" placeholder="/path/to/adb"></label><p class="muted">Use the adb executable from Android SDK Platform Tools.</p><button type="submit">Save path</button></form>
      <h3>Saved data</h3><p class="settings-data">${e(s.data_dir)}</p><p class="muted">Your app details and history stay on this computer.</p>
      ${d ? `<h3>Phone details</h3><p class="settings-data">${e(d.manufacturer)} ${e(d.model)}<br>Android ${e(d.android)}<br>Last refreshed: ${e(date(d.scanned_at))}<br>Serial: ${e(d.serial || 'Unavailable')}<br>Firmware: ${e(d.fingerprint)}</p>${d.profiles.map(p => `<p>User ${e(p.id)}: ${e(p.name)}${p.id === d.managed_user ? ' (managed)' : ''}</p>`).join('')}` : ''}
      <p class="muted">Catalog ${e(s.catalog_version)}</p>
    </details>
    ${Object.keys(s.global_overrides).length ? `<details><summary>Shared app details</summary><p class="muted">Changes saved for all phones. Details saved for this phone take precedence.</p>${Object.keys(s.global_overrides).map(pkg => `<div class="global-row"><span>${e(pkg)}</span><button data-reset-global="${e(pkg)}">Reset</button></div>`).join('')}</details>` : ''}
    ${Object.values(s.errors).map(error => `<p class="warning">${e(error)}</p>`).join('')}`;
  $('#adb-form').onsubmit = handle(async event => {
    event.preventDefault();
    await api('/api/settings', {adb_path:$('#adb-path').value || null});
    $('#settings-dialog').close();
    await refresh(true);
  });
  if ($('#nickname-form')) $('#nickname-form').onsubmit = handle(async event => {
    event.preventDefault();
    await api(`/api/devices/${state.current}/nickname`, {nickname:$('#nickname').value});
    $('#settings-dialog').close();
    await loadDevice();
  });
  $$('[data-reset-global]').forEach(button => button.onclick = handle(async () => {
    await api('/api/global-overrides/' + encodeURIComponent(button.dataset.resetGlobal) + '/reset', {});
    await openSettings();
    await loadDevice();
  }));
  if (!$('#settings-dialog').open) $('#settings-dialog').showModal();
}
$('#filter-purpose').insertAdjacentHTML('beforeend', Object.entries(categories).map(([tag, label]) => `<option value="${e(tag)}">${e(label)}</option>`).join(''));
for (const input of ['#search','#filter-installation','#filter-purpose','#filter-usefulness','#filter-state','#sort']) {
  $(input).addEventListener('input', () => {
    const active = $$('.filters select').filter(input => input.id !== 'sort' && input.value).length;
    $('#filters-button').textContent = active ? `Filters (${active})` : 'Filters';
    if (state.detail) renderRows();
  });
}
$('#filters-button').onclick = () => {
  $('#filters').hidden = !$('#filters').hidden;
  $('#filters-button').setAttribute('aria-expanded', String(!$('#filters').hidden));
};
$('#clear-filters').onclick = () => {
  $$('.filters select').forEach(input => input.value = input.id === 'sort' ? 'name' : '');
  $('#filter-purpose').value = '';
  $('#search').value = '';
  $('#filters-button').textContent = 'Filters';
  if (state.detail) renderRows();
};
$('#clear-selection').onclick = () => { state.selected.clear(); renderSelection(); };
$('#select-all').onchange = event => {
  state.rows.filter(canSelect).forEach(p => event.target.checked ? state.selected.add(p.id) : state.selected.delete(p.id));
  renderSelection();
};
$('#app-rows').onchange = event => {
  const box = event.target.closest('[data-select]');
  if (!box) return;
  box.checked ? state.selected.add(box.dataset.select) : state.selected.delete(box.dataset.select);
  renderSelection();
};
$('#app-rows').onclick = event => {
  const button = event.target.closest('[data-inspect]');
  if (button) inspect(button.dataset.inspect);
};
$$('[data-tab]').forEach(button => button.onclick = () => {
  state.tab = button.dataset.tab;
  state.selected.clear();
  $$('[data-tab]').forEach(b => {
    b.classList.toggle('active', b === button);
    b.setAttribute('aria-current', b === button ? 'page' : 'false');
  });
  $('#inventory-view').hidden = state.tab === 'history';
  $('#history-view').hidden = state.tab !== 'history';
  if (state.detail) { if (state.tab === 'history') renderHistory(); else renderRows(); }
});
$('#phone-select').onchange = handle(async event => { selectPhone(event.target.value); await loadDevice(); });
$('#empty-refresh').onclick = handle(() => refresh(true));
$('#scan-button').onclick = handle(() => refresh(true));
$('#settings-button').onclick = handle(openSettings);
$('#dismiss-notice').onclick = () => {
  state.dismissedNotice = $('#notice-message').textContent;
  $('#notice').hidden = true;
};
$('#review-button').onclick = handle(review);
$('#inspector').onclose = () => { state.inspecting = null; };
$('#edit-form').onsubmit = handle(async event => {
  event.preventDefault();
  const scope = $('#edit-global').checked ? 'global' : 'device';
  const changes = scope === 'global'
    ? Object.fromEntries(Object.entries(state.detail.device.overrides[state.inspecting] || {}).filter(([, value]) => value !== null))
    : {};
  for (const [field, value] of Object.entries(editorValues())) {
    if (JSON.stringify(value) !== JSON.stringify(state.editOriginal[field])) changes[field] = value;
  }
  if (Object.keys(changes).length) {
    await api(`/api/devices/${state.current}/overrides/${encodeURIComponent(state.inspecting)}`, {...changes, scope});
  }
  $('#edit-dialog').close();
  await loadDevice();
});
$('#reset-override').onclick = handle(async () => {
  await api(`/api/devices/${state.current}/overrides/${encodeURIComponent(state.inspecting)}`, {scope:$('#edit-global').checked ? 'global' : 'device'});
  $('#edit-dialog').close();
  await loadDevice();
});
async function start() {
  state.settings = await api('/api/settings');
  $('#mode').hidden = !state.settings.read_only;
  const current = await api('/api/batches/current');
  if (current?.id) { state.activeBatch = current; renderBatch(); }
  await refresh(true);
}
handle(start)();
setInterval(handle(() => { if (!document.hidden && !$('dialog[open]')) return refresh(); }, true), 15000);
setInterval(handle(pollBatch, true), 1000);
