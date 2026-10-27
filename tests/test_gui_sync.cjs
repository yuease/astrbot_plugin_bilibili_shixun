const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');

function page() {
  const listeners = {}, messages = {}, fills = [];
  const context = {
    window: {BilibiliShixunTransport: {drafts: {}}},
    document: {
      addEventListener: (name, fn) => (listeners[name] ??= []).push(fn),
      querySelector: selector => messages[selector] ??= {textContent: '', children: [], append(x) {this.children.push(x);}},
      createElement: () => ({dataset: {}}),
    },
    fillSources: value => fills.push(['sources', value]),
    fillFilters: value => fills.push(['filters', value]),
    fillSettings: () => fills.push(['settings']),
    toast: () => {throw new Error('A visible form should receive its own conflict notice');},
  };
  vm.createContext(context);
  const app = fs.readFileSync(path.join(__dirname, '../views/bilibili-shixun/app.js'), 'utf8');
  vm.runInContext(app.slice(0, app.indexOf('const hasSources')), context);
  const run = source => vm.runInContext(source, context);
  run("state={profile_version:'new'};currentView='sources';");
  return {context, run, listeners, fills, messages};
}

test('a newer shared version reloads an untouched visible form', () => {
  const p = page();
  p.run("syncRemoteSettings('old')");
  assert.deepEqual(p.fills, [['sources', false]]);
});

test('input preserves an unsaved form and offers an explicit reload', () => {
  const p = page();
  for (const fn of p.listeners.input) fn({target: {closest: () => ({id:'sources-form'})}});
  p.run("syncRemoteSettings('old')");
  assert.equal(p.fills.length, 0);
  const message = p.messages['#sources-message'];
  assert.match(message.textContent, /草稿已保留/);
  assert.equal(message.children[0].dataset.reloadSettings, 'sources');
});

test('adding or removing a source row also counts as an unsaved edit', () => {
  const p = page();
  for (const fn of p.listeners.click) fn({target: {closest: selector => selector === 'form' ? {id:'sources-form'} : {}}});
  p.run("syncRemoteSettings('old')");
  assert.equal(p.fills.length, 0);
});

test('polling cannot reload the form while its own save is in progress', () => {
  const p = page();
  p.run("pending=true;syncRemoteSettings('old')");
  assert.equal(p.fills.length, 0);
  p.run("pending=false;syncRemoteSettings('new')");
  assert.equal(p.fills.length, 0);
});
