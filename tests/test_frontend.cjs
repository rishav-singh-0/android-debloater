const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = selector => {
  if (!elements.has(selector)) elements.set(selector, {value: '', insertAdjacentHTML() {}, addEventListener() {}, setAttribute() {}, classList: {toggle() {}}, click() { this.clicked = true; }, showModal() { this.open = true; }, close() { this.open = false; }});
  return elements.get(selector);
};
const context = vm.createContext({
  document: {querySelector: selector => selector === 'dialog[open]' ? null : element(selector), querySelectorAll: () => []},
  fetch: () => new Promise(() => {}), setInterval() {}
});
vm.runInContext(fs.readFileSync('src/android_debloater/static/app.js', 'utf8'), context);
vm.runInContext("notice('Failed to fetch');", context);
assert.equal(element('#notice').hidden, false);
assert.equal(typeof element('#dismiss-notice').onclick, 'function', 'Errors need a dismiss control');
element('#dismiss-notice').onclick();
assert.equal(element('#notice').hidden, true);
vm.runInContext("notice('Failed to fetch', false, true);", context);
assert.equal(element('#notice').hidden, true, 'Background retries must respect dismissal');
vm.runInContext("notice('A different error', false, true);", context);
assert.equal(element('#notice').hidden, false, 'A new error must remain visible');
element('#dismiss-notice').onclick();
vm.runInContext("notice('A different error');", context);
assert.equal(element('#notice').hidden, false, 'A manual retry must report its error');
assert.equal(vm.runInContext('Object.keys(categories).length', context), 6);
context.detail = {
  connected:true, removed:[], device:{model:'Model name', nickname:'', blockers:[], history:[]},
  packages:['money','tools','system',''].map((tag, index) => ({
    id:`com.example.app${index}`, name:`Example ${index}`, purposes:tag ? [tag] : [], installed:true,
    installation:'system', state:'installed/enabled', notes:'', purpose:'', usefulness:'optional'
  }))
};
vm.runInContext("state.detail = detail; state.current = 'one'; state.tab = 'installed';", context);
element('#sort').value = 'name';
element('#filter-purpose').value = 'money';
assert.equal(vm.runInContext('filteredRows().length', context), 1);
element('#filter-purpose').value = 'other';
assert.equal(vm.runInContext('filteredRows()[0].id', context), 'com.example.app3');
element('#filter-purpose').value = '';
element('#search').value = 'daily use';
assert.equal(vm.runInContext('filteredRows()[0].id', context), 'com.example.app1');
element('#search').value = 'com.example.app0';
assert.equal(vm.runInContext('filteredRows().length', context), 1, 'Package IDs stay searchable');
element('#clear-filters').onclick();
assert.equal(element('#filter-purpose').value, '');
assert.equal(element('#search').value, '');
vm.runInContext("state.inspecting = 'com.example.app1'; openEditor();", context);
assert.equal((element('#edit-purposes').innerHTML.match(/type="checkbox"/g) || []).length, 6);
assert.match(element('#edit-purposes').innerHTML, /value="tools" checked/);
function render(statuses, overrides = {}) {
  const batch = {id: 'test', device_id: 'one', device_name: 'Demo phone', action: 'uninstall', running: false, error: '', entries: statuses.map((status, i) => ({status, package: `com.example.app${i}`, verification: 'Verified result'})), ...overrides};
  vm.runInContext(`state.current = 'one'; state.activeBatch = ${JSON.stringify(batch)}; renderBatch();`, context);
  return element('#batch').innerHTML;
}
let html = render(['succeeded']);
assert.match(html, /Removed 1 app/);
assert.doesNotMatch(html, /<progress|<ul|Demo phone|Verified result/);
assert.match(html, /View history/);
html = render(Array(200).fill('succeeded'));
assert.match(html, /Removed 200 apps/);
assert.ok(html.length < 600, 'Completed results must stay compact for a large batch');
html = render(['succeeded'], {action: 'restore'});
assert.match(html, /Restored 1 app/);
for (const status of ['failed', 'unverified', 'partial', 'cancelled', 'unattempted']) {
  html = render(['succeeded', status]);
  assert.match(html, /1 of 2 confirmed/);
  assert.match(html, /incomplete/);
  assert.doesNotMatch(html, /Removed 2 apps/);
}
html = render(['succeeded'], {error: '<Storage unavailable>'});
assert.match(html, /needs attention/);
assert.match(html, /&lt;Storage unavailable&gt;/);
html = render(['attempting', 'pending'], {running: true});
assert.match(html, /Uninstalling apps/);
assert.match(html, /stop-batch/);
assert.match(html, /<progress/);
render(['succeeded']);
(async () => {
  context.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(vm.runInContext("api('/api/settings')", context), /Can't reach Android Debloater/);
  vm.runInContext('api = async () => detail;', context);
  await vm.runInContext('loadDevice()', context);
  assert.equal(element('#device-title').textContent, 'Model name');
  context.detail.device.nickname = 'My phone';
  await vm.runInContext('loadDevice()', context);
  assert.equal(element('#device-title').textContent, 'My phone');
  assert.match(element('#app-rows').innerHTML, /Money &amp; shopping/);
  assert.doesNotMatch(element('#app-rows').innerHTML, /table-id/);

  context.discovery = {devices:[{id:'one', model:'My phone', connection_state:'Disconnected'}], pending:[], storage_errors:{}, error:''};
  context.requestPaths = [];
  vm.runInContext(`
    state.busy = false; selectPhone(null);
    api = async path => { requestPaths.push(path); return path.startsWith('/api/refresh') ? discovery : detail; };
  `, context);
  await vm.runInContext('refresh()', context);
  assert.equal(vm.runInContext('state.current', context), null, 'Saved phones must not open when disconnected');
  assert.equal(element('#device-workspace').hidden, true);
  assert.equal(element('#empty-device').hidden, false);
  assert.deepEqual(context.requestPaths, ['/api/refresh?force=false']);

  context.discovery.devices[0].connection_state = 'Connected';
  await element('#empty-refresh').onclick();
  assert.equal(vm.runInContext('state.current', context), 'one');
  assert.equal(element('#device-workspace').hidden, false);
  assert.equal(element('#empty-device').hidden, true);
  assert.ok(context.requestPaths.includes('/api/refresh?force=true'), 'Search must scan for a connected phone');

  let resolveDetail;
  context.pendingDetail = new Promise(resolve => { resolveDetail = resolve; });
  vm.runInContext('api = () => pendingDetail;', context);
  const staleLoad = vm.runInContext('loadDevice()', context);
  vm.runInContext(`
    state.selected.add('com.example.app1');
    $('#inspector').showModal(); $('#edit-dialog').showModal(); $('#review-dialog').showModal();
    api = async path => path.startsWith('/api/refresh') ? discovery : detail;
  `, context);
  context.discovery.devices[0].connection_state = 'Disconnected';
  await vm.runInContext('refresh()', context);
  assert.equal(element('#device-workspace').hidden, true, 'Unplugging must hide the previous phone');
  assert.equal(element('#empty-device').hidden, false);
  assert.equal(vm.runInContext('state.detail', context), null);
  assert.equal(vm.runInContext('state.selected.size', context), 0);
  for (const dialog of ['#inspector', '#edit-dialog', '#review-dialog']) assert.equal(element(dialog).open, false);
  resolveDetail(context.detail);
  await staleLoad;
  assert.equal(element('#device-workspace').hidden, true, 'An old app-info response must not reopen a disconnected phone');

  context.discovery.devices[0].connection_state = 'Connected';
  await vm.runInContext('refresh()', context);
  assert.equal(element('#device-workspace').hidden, false, 'Reconnecting must reopen the phone');
  context.detail.connected = false;
  await vm.runInContext('loadDevice()', context);
  assert.equal(element('#device-workspace').hidden, true, 'A detail response can also detect a disconnect');
  assert.equal(element('#empty-device').hidden, false);
  context.detail.connected = true;

  let resolveSearch;
  context.pendingSearch = new Promise(resolve => { resolveSearch = resolve; });
  vm.runInContext('api = () => pendingSearch;', context);
  const searching = element('#empty-refresh').onclick();
  assert.equal(element('#empty-refresh').disabled, true);
  assert.equal(element('#empty-refresh').textContent, 'Searching…');
  context.discovery.devices = [];
  resolveSearch(context.discovery);
  await searching;
  assert.equal(element('#empty-refresh').disabled, false);
  assert.equal(element('#empty-refresh').textContent, 'Search for device');
  assert.equal(element('#empty-device').hidden, false);

  vm.runInContext('loadDevice = async () => {};', context);
  await element('#batch-history').onclick();
  assert.equal(element('[data-tab="history"]').clicked, true);
  element('#dismiss-batch').onclick();
  assert.equal(element('#batch').hidden, true);
  let resolveStart;
  context.pendingStart = new Promise(resolve => { resolveStart = resolve; });
  context.requestPaths = [];
  vm.runInContext(`state.detail = {packages: [{}]}; state.selected.add('com.example.keep'); api = path => {requestPaths.push(path); return pendingStart;};`, context);
  const starting = vm.runInContext('updateAppInfo()', context);
  assert.equal(element('#app-info-button').disabled, true);
  assert.ok(!element('#scan-button').disabled, 'Internet requests must not disable Refresh');
  context.infoJob = {id:'info', device_id:'one', device_name:'Demo phone', running:true, stop_requested:false, total:200, checked:0, updated:0, not_found:0, failed:0, warnings:[], error:''};
  resolveStart(context.infoJob);
  await starting;
  await vm.runInContext('updateAppInfo()', context);
  assert.equal(context.requestPaths.length, 1, 'Repeated clicks must not start duplicate jobs');
  assert.equal(vm.runInContext("state.selected.has('com.example.keep')", context), true);
  assert.match(element('#info-update-status').textContent, /Updating app info.*0 \/ 200/);
  context.loadCount = 0;
  context.infoJob = {...context.infoJob, updated:4, checked:4};
  vm.runInContext('api = async () => infoJob; loadDevice = async () => {loadCount++;};', context);
  await vm.runInContext('pollAppInfo()', context);
  await vm.runInContext('pollAppInfo()', context);
  assert.equal(context.loadCount, 1, 'Only new saved results should reload the apps');
  context.infoJob = {...context.infoJob, running:false, checked:200, updated:195, not_found:3, failed:2, warnings:['Some sources were unavailable.']};
  await vm.runInContext('pollAppInfo()', context);
  assert.equal(element('#app-info-button').disabled, false);
  assert.match(element('#info-update-status').textContent, /195 apps.*3 not found.*2 couldn't be checked/);
  assert.equal(element('#info-update-details').hidden, false);
  await element('#info-update-action').onclick();
  await vm.runInContext('pollAppInfo()', context);
  assert.equal(element('#info-update').hidden, true, 'Polling must not undo dismissal');
  context.savedEdits = [];
  context.checkedPurposes = ['other'];
  context.document.querySelectorAll = selector => selector === '#edit-purposes input:checked'
    ? context.checkedPurposes.map(value => ({value})) : [];
  vm.runInContext(`
    state.current = 'one'; state.inspecting = 'com.example.app';
    state.detail = {device:{overrides:{}}, packages:[{id:'com.example.app', name:'Online name', purposes:['other'], usefulness:'unknown', notes:''}]};
    api = async (path, body) => { savedEdits.push(JSON.parse(JSON.stringify({path, body}))); };
    loadDevice = async () => {};
    openEditor();
    state.detail.packages[0].name = 'Updated during editing';
  `, context);
  element('#edit-notes').value = 'My note';
  await element('#edit-form').onsubmit({preventDefault() {}});
  const savedBody = () => JSON.parse(JSON.stringify(context.savedEdits.at(-1).body));
  assert.deepEqual(savedBody(), {scope:'device', notes:'My note'}, 'A note edit must not pin downloaded fields, even during a refresh');
  vm.runInContext("state.detail.device.overrides['com.example.app'] = {name:'Personal name', purposes:null, usefulness:null, notes:'My note'}; openEditor();", context);
  const savedCount = context.savedEdits.length;
  await element('#edit-form').onsubmit({preventDefault() {}});
  assert.equal(context.savedEdits.length, savedCount, 'Saving without edits must not reset existing overrides');
  vm.runInContext('openEditor();', context);
  element('#edit-global').checked = true;
  await element('#edit-form').onsubmit({preventDefault() {}});
  assert.deepEqual(savedBody(), {scope:'global', name:'Personal name', notes:'My note'}, 'Sharing must copy personal overrides without pinning downloaded fields');
  vm.runInContext('openEditor();', context);
  element('#edit-name').value = '';
  context.checkedPurposes = [];
  await element('#edit-form').onsubmit({preventDefault() {}});
  assert.deepEqual(savedBody(), {scope:'device', name:null, purposes:[]}, 'Explicit field clearing must still work');
  await element('#reset-override').onclick();
  assert.deepEqual(savedBody(), {scope:'device'}, 'Reset must still clear the whole override');
  console.log('Operation results and nonblocking app-information UI checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
