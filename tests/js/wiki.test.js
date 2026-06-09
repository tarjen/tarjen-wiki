// tests/js/wiki.test.js
// 用 node:test 跑（node ≥ 18 自带，无 npm install）。
//
// wiki.js 是浏览器脚本（挂在 window.Wiki 上），用 vm 模块跑在带 mock 的 sandbox 里。
// PAT 存 localStorage（明文），QOJ cookie 同款。

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WIKI_JS = fs.readFileSync(
  path.join(__dirname, '..', '..', 'docs', 'assets', 'wiki.js'),
  'utf8'
);

// ---- sandbox loader ----

/**
 * 跑一次 wiki.js 在带 mock 的 vm context 里，返回 window.Wiki。
 *
 * @param {object} opts
 * @param {string} [opts.token]     预置 gh_token_v1
 * @param {Function} [opts.fetchImpl] 自定义 fetch
 * @param {object}   [opts.elements]  document.getElementById mock 返回的元素，key = id
 * @param {object}   [opts.overrides]  覆盖默认 ctx 字段
 */
function loadWiki({ token = '', fetchImpl = null, elements = {}, overrides = {} } = {}) {
  const store = {};
  if (token) store['gh_token_v1'] = token;

  const localStorage = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
  };

  const document = {
    getElementById(id) { return elements[id] || null; },
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  // window 需要 addEventListener/removeEventListener，因为 wiki.js 直接调
  const win = {
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  // Node 18+ 移除了 global btoa/atob，但 wiki.js 用了
  const btoa = (s) => Buffer.from(s, 'binary').toString('base64');
  const atob = (s) => Buffer.from(s, 'base64').toString('binary');

  const ctx = {
    window: win,
    localStorage,
    document,
    console,
    fetch: fetchImpl,
    setTimeout,
    clearTimeout,
    btoa, atob,
    crypto: globalThis.crypto,  // Node 18+ 自带 webcrypto（虽然现在不再用，留着以防万一）
    TextEncoder, TextDecoder,  // 浏览器全局，wiki.js 调
    confirm: () => true,
    ...overrides,
  };
  vm.createContext(ctx);
  vm.runInContext(WIKI_JS, ctx);
  return { Wiki: win.Wiki, store, win, doc: document };
}

// ---- token (localStorage model) ----

test('getToken returns "" when not configured', () => {
  const { Wiki } = loadWiki();
  assert.equal(Wiki.getToken(), '');
});

test('getToken reads from localStorage (single source of truth)', () => {
  const { Wiki } = loadWiki({ token: 'ghp_persisted' });
  assert.equal(Wiki.getToken(), 'ghp_persisted');
});

test('setToken writes to localStorage', () => {
  const { Wiki, store } = loadWiki();
  Wiki.setToken('ghp_test_xxx');
  assert.equal(Wiki.getToken(), 'ghp_test_xxx');
  assert.equal(store['gh_token_v1'], 'ghp_test_xxx');
});

test('setToken("") clears the stored token', () => {
  const { Wiki, store } = loadWiki({ token: 'old' });
  Wiki.setToken('');
  assert.equal(Wiki.getToken(), '');
  assert.equal(store['gh_token_v1'], undefined);
});

test('clearToken removes from localStorage', () => {
  const { Wiki, store } = loadWiki({ token: 'old' });
  Wiki.clearToken();
  assert.equal(Wiki.getToken(), '');
  assert.equal(store['gh_token_v1'], undefined);
});

test('setToken/clearToken do not touch qoj_cookie_v1 (independent keys)', () => {
  const { Wiki, store } = loadWiki();
  store['qoj_cookie_v1'] = 'uoj=1';
  Wiki.setToken('ghp_x');
  assert.equal(store['qoj_cookie_v1'], 'uoj=1');
  Wiki.clearToken();
  assert.equal(store['qoj_cookie_v1'], 'uoj=1', 'clearToken must not wipe QOJ cookie');
});

// ---- URLs ----

test('apiUrl constructs correct Contents API URL', () => {
  const { Wiki } = loadWiki();
  assert.equal(
    Wiki.apiUrl('contests.csv'),
    'https://api.github.com/repos/tarjen/tarjen-wiki/contents/contests.csv'
  );
});

test('rawUrl constructs correct raw URL', () => {
  const { Wiki } = loadWiki();
  assert.equal(
    Wiki.rawUrl('docs/contests/foo.md'),
    'https://raw.githubusercontent.com/tarjen/tarjen-wiki/main/docs/contests/foo.md'
  );
});

// ---- esc ----

test('esc escapes all dangerous chars', () => {
  const { Wiki } = loadWiki();
  assert.equal(Wiki.esc('<script>'), '&lt;script&gt;');
  assert.equal(Wiki.esc('a&b'), 'a&amp;b');
  assert.equal(Wiki.esc(`"foo" 'bar'`), '&quot;foo&quot; &#39;bar&#39;');
  assert.equal(Wiki.esc(''), '');
  assert.equal(Wiki.esc(null), '');
  assert.equal(Wiki.esc(undefined), '');
});

// ---- commitFile ----

test('commitFile: GET SHA → PUT with same SHA + b64 content', async () => {
  const calls = [];
  const fakeFetch = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET', body: opts.body, headers: opts.headers });
    if (calls.length === 1) return { ok: true, status: 200, json: async () => ({ sha: 'abc123' }) };
    return { ok: true, status: 200, json: async () => ({ commit: { sha: 'new' } }) };
  };
  const { Wiki } = loadWiki({ token: 'ghp_t', fetchImpl: fakeFetch });

  const result = await Wiki.commitFile('contests.csv', 'hello,world\n', 'test commit');

  assert.equal(calls.length, 2);
  // 1) GET
  assert.equal(calls[0].method, 'GET');
  assert.ok(calls[0].url.endsWith('?ref=main'), 'should pass ?ref=main');
  // 2) PUT
  assert.equal(calls[1].method, 'PUT');
  const body = JSON.parse(calls[1].body);
  assert.equal(body.sha, 'abc123', 'should pass SHA from GET');
  assert.equal(body.branch, 'main');
  assert.equal(body.message, 'test commit');
  assert.equal(body.content, Buffer.from('hello,world\n').toString('base64'),
    'content should be base64 of UTF-8 string');
  // 3) Authorization header should be set
  assert.match(calls[0].headers.Authorization, /^Bearer ghp_t$/);
  // 4) Should return the PUT response
  assert.equal(result.commit.sha, 'new');
});

test('commitFile: 401 → friendly Chinese error', async () => {
  const fakeFetch = async () => ({
    ok: false, status: 401,
    json: async () => ({ message: 'Bad credentials' }),
  });
  const { Wiki } = loadWiki({ token: 'ghp_bad', fetchImpl: fakeFetch });
  await assert.rejects(
    Wiki.commitFile('contests.csv', 'x', 'm'),
    /Token 无效或已过期.*Bad credentials/
  );
});

test('commitFile: 404 → friendly "找不到文件" error', async () => {
  const fakeFetch = async () => ({
    ok: false, status: 404,
    json: async () => ({ message: 'Not Found' }),
  });
  const { Wiki } = loadWiki({ token: 'ghp_t', fetchImpl: fakeFetch });
  await assert.rejects(
    Wiki.commitFile('wrong.csv', 'x', 'm'),
    /找不到文件.*Not Found/
  );
});

test('commitFile: 500 GET → raw error message', async () => {
  const fakeFetch = async () => ({
    ok: false, status: 500,
    json: async () => ({ message: 'Internal server error' }),
  });
  const { Wiki } = loadWiki({ token: 'ghp_t', fetchImpl: fakeFetch });
  await assert.rejects(
    Wiki.commitFile('contests.csv', 'x', 'm'),
    /Internal server error/
  );
});

test('commitFile: PUT 422 (e.g. SHA mismatch) propagates message', async () => {
  let n = 0;
  const fakeFetch = async (url, opts = {}) => {
    n++;
    if (n === 1) return { ok: true, status: 200, json: async () => ({ sha: 'old' }) };
    return {
      ok: false, status: 422,
      json: async () => ({ message: 'does not match' }),
    };
  };
  const { Wiki } = loadWiki({ token: 'ghp_t', fetchImpl: fakeFetch });
  await assert.rejects(
    Wiki.commitFile('contests.csv', 'x', 'm'),
    /does not match/
  );
});

test('commitFile: without token → throws "No PAT configured"', async () => {
  const { Wiki } = loadWiki();
  await assert.rejects(
    Wiki.commitFile('contests.csv', 'x', 'm'),
    /No PAT configured/
  );
});

test('commitFile: b64 handles non-ASCII (中文) correctly', async () => {
  const calls = [];
  const fakeFetch = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET', body: opts.body });
    if (calls.length === 1) return { ok: true, status: 200, json: async () => ({ sha: 's' }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const { Wiki } = loadWiki({ token: 't', fetchImpl: fakeFetch });
  await Wiki.commitFile('c.csv', '中文,2024.1.1\n', 'msg');
  const body = JSON.parse(calls[1].body);
  // decode and compare
  const decoded = Buffer.from(body.content, 'base64').toString('utf8');
  assert.equal(decoded, '中文,2024.1.1\n');
});

// ---- wireTokenUI: localStorage flow ----

test('wireTokenUI: empty store shows "未配置" + save button', () => {
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: () => {}, style: {} };
  const st = { textContent: '' };
  const { Wiki } = loadWiki({
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'token-status': st },
  });
  Wiki.wireTokenUI();
  assert.equal(st.textContent, '未配置');
  assert.equal(inp.style.display, '', 'input visible');
  assert.equal(btnSv.style.display, '', 'save button visible');
});

test('wireTokenUI: token in localStorage shows "已配置（N 字符）" + clear button', () => {
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: () => {}, style: {} };
  const btnCl = { style: {}, addEventListener: () => {} };
  const st = { textContent: '' };
  const { Wiki } = loadWiki({
    token: 'ghp_in_store',
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'btn-clear-token': btnCl, 'token-status': st },
  });
  Wiki.wireTokenUI();
  assert.match(st.textContent, /已配置.*字符/);
  assert.equal(inp.style.display, 'none', 'input hidden when configured');
  assert.equal(btnSv.style.display, 'none', 'save button hidden when configured');
  assert.equal(btnCl.style.display, '', 'clear button visible');
});

test('wireTokenUI: save button writes token to localStorage', () => {
  let saveHandler;
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: (_evt, fn) => { saveHandler = fn; }, style: {} };
  const { Wiki, store } = loadWiki({
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'token-status': { textContent: '' } },
  });
  Wiki.wireTokenUI();
  inp.value = 'ghp_new_token';
  saveHandler();
  assert.equal(Wiki.getToken(), 'ghp_new_token');
  assert.equal(store['gh_token_v1'], 'ghp_new_token');
});

test('wireTokenUI: save with empty input is rejected (no localStorage write)', () => {
  let saveHandler;
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: (_evt, fn) => { saveHandler = fn; }, style: {} };
  const { Wiki, store } = loadWiki({
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'token-status': { textContent: '' } },
  });
  Wiki.wireTokenUI();
  inp.value = '   ';
  saveHandler();
  assert.equal(Wiki.getToken(), '');
  assert.equal(store['gh_token_v1'], undefined);
});

test('wireTokenUI: clear button removes from localStorage', () => {
  let clearHandler;
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: () => {}, style: {} };
  const btnCl = { style: {}, addEventListener: (_evt, fn) => { clearHandler = fn; } };
  const { Wiki, store } = loadWiki({
    token: 'old',
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'btn-clear-token': btnCl, 'token-status': { textContent: '' } },
  });
  Wiki.wireTokenUI();
  clearHandler();
  assert.equal(Wiki.getToken(), '');
  assert.equal(store['gh_token_v1'], undefined);
});

test('wireTokenUI: missing DOM elements → silent no-op (graceful degrade)', () => {
  const { Wiki } = loadWiki();  // 没有任何 elements
  // 不应抛
  assert.doesNotThrow(() => Wiki.wireTokenUI());
});

// ---- encryption ----
// (removed 2026-06-09: PAT 改成明文存 localStorage，威胁模型太弱 + UX 太重)
// 之前 10 个 encryptToken/decryptToken/unlockToken/hasEncryptedToken 测试
// 整体删除。如有需要 git history 可查。

// ---- wireBeforeUnload ----

test('wireBeforeUnload: dirty → preventDefault on beforeunload', () => {
  const winListeners = {};
  const win = {
    addEventListener: (evt, fn) => { winListeners[evt] = fn; },
    removeEventListener: () => {},
  };
  const docListeners = {};
  const document = {
    addEventListener: (evt, fn) => { docListeners[evt] = fn; },
    getElementById: () => null,
  };
  const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  const btoa = (s) => Buffer.from(s, "binary").toString("base64");
  const atob = (s) => Buffer.from(s, "base64").toString("binary");
  const ctx = { window: win, document, localStorage, console, setTimeout, clearTimeout, confirm: () => true, btoa, atob, crypto: globalThis.crypto };
  vm.createContext(ctx);
  vm.runInContext(WIKI_JS, ctx);
  ctx.window.Wiki.wireBeforeUnload(function () { return true; }, null);
  const e = { preventDefault: () => { e._prevented = true; }, returnValue: null };
  winListeners.beforeunload(e);
  assert.equal(e._prevented, true);
  assert.equal(e.returnValue, '');
});

test('wireBeforeUnload: clean → no preventDefault', () => {
  const winListeners = {};
  const win = {
    addEventListener: (evt, fn) => { winListeners[evt] = fn; },
    removeEventListener: () => {},
  };
  const document = { addEventListener: () => {}, getElementById: () => null };
  const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  const btoa = (s) => Buffer.from(s, "binary").toString("base64");
  const atob = (s) => Buffer.from(s, "base64").toString("binary");
  const ctx = { window: win, document, localStorage, console, setTimeout, clearTimeout, confirm: () => true, btoa, atob, crypto: globalThis.crypto };
  vm.createContext(ctx);
  vm.runInContext(WIKI_JS, ctx);
  ctx.window.Wiki.wireBeforeUnload(function () { return false; }, null);
  const e = { preventDefault: () => { e._prevented = true; }, returnValue: null };
  winListeners.beforeunload(e);
  assert.equal(e._prevented, undefined);
});

test('wireBeforeUnload: Ctrl+S calls onSave', () => {
  const docListeners = {};
  const document = { addEventListener: (evt, fn) => { docListeners[evt] = fn; }, getElementById: () => null };
  const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  const win = { addEventListener: () => {}, removeEventListener: () => {} };
  const btoa = (s) => Buffer.from(s, "binary").toString("base64");
  const atob = (s) => Buffer.from(s, "base64").toString("binary");
  const ctx = { window: win, document, localStorage, console, setTimeout, clearTimeout, confirm: () => true, btoa, atob, crypto: globalThis.crypto };
  vm.createContext(ctx);
  vm.runInContext(WIKI_JS, ctx);

  let saveCalled = 0;
  ctx.window.Wiki.wireBeforeUnload(function () { return false; }, function () { saveCalled++; });
  const e = { metaKey: false, ctrlKey: true, key: 's', preventDefault: () => {} };
  docListeners.keydown(e);
  assert.equal(saveCalled, 1);
});

test('wireBeforeUnload: Cmd+S (mac) also calls onSave', () => {
  const docListeners = {};
  const document = { addEventListener: (evt, fn) => { docListeners[evt] = fn; }, getElementById: () => null };
  const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  const win = { addEventListener: () => {}, removeEventListener: () => {} };
  const btoa = (s) => Buffer.from(s, "binary").toString("base64");
  const atob = (s) => Buffer.from(s, "base64").toString("binary");
  const ctx = { window: win, document, localStorage, console, setTimeout, clearTimeout, confirm: () => true, btoa, atob, crypto: globalThis.crypto };
  vm.createContext(ctx);
  vm.runInContext(WIKI_JS, ctx);

  let saveCalled = 0;
  ctx.window.Wiki.wireBeforeUnload(function () { return false; }, function () { saveCalled++; });
  const e = { metaKey: true, ctrlKey: false, key: 's', preventDefault: () => {} };
  docListeners.keydown(e);
  assert.equal(saveCalled, 1);
});

test('wireBeforeUnload: plain "s" key does NOT trigger save', () => {
  const docListeners = {};
  const document = { addEventListener: (evt, fn) => { docListeners[evt] = fn; }, getElementById: () => null };
  const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  const win = { addEventListener: () => {}, removeEventListener: () => {} };
  const btoa = (s) => Buffer.from(s, "binary").toString("base64");
  const atob = (s) => Buffer.from(s, "base64").toString("binary");
  const ctx = { window: win, document, localStorage, console, setTimeout, clearTimeout, confirm: () => true, btoa, atob, crypto: globalThis.crypto };
  vm.createContext(ctx);
  vm.runInContext(WIKI_JS, ctx);

  let saveCalled = 0;
  ctx.window.Wiki.wireBeforeUnload(function () { return false; }, function () { saveCalled++; });
  const e = { metaKey: false, ctrlKey: false, key: 's', preventDefault: () => {} };
  docListeners.keydown(e);
  assert.equal(saveCalled, 0);
});
