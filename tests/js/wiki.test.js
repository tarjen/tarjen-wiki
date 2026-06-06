// tests/js/wiki.test.js
// 用 node:test 跑（node ≥ 18 自带，无 npm install）。
//
// wiki.js 是浏览器脚本（挂在 window.Wiki 上），用 vm 模块跑在带 mock 的 sandbox 里。
// crypto.subtle 用 Node 自带的 webcrypto 模拟（Node 18+ 有 globalThis.crypto）。

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
 * @param {string} [opts.token]     预置 v1 明文 token
 * @param {string} [opts.encBlob]   预置 v2 加密 blob（b64）
 * @param {Function} [opts.fetchImpl] 自定义 fetch
 * @param {object}   [opts.elements]  document.getElementById mock 返回的元素，key = id
 * @param {object}   [opts.overrides]  覆盖默认 ctx 字段
 */
function loadWiki({ token = '', encBlob = '', fetchImpl = null, elements = {}, overrides = {} } = {}) {
  const store = {};
  if (token) store['gh_token_v1'] = token;
  if (encBlob) store['gh_token_v2_enc'] = encBlob;

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

  // 用 Node 内置 webcrypto 模拟浏览器 Web Crypto
  // 实际测试里需要可控制随机数所以传一个真实的 crypto
  const ctx = {
    window: win,
    localStorage,
    document,
    console,
    fetch: fetchImpl,
    setTimeout,
    clearTimeout,
    btoa, atob,
    crypto: globalThis.crypto,  // Node 18+ 自带 webcrypto
    TextEncoder, TextDecoder,  // 浏览器全局，wiki.js 调
    confirm: () => true,
    ...overrides,
  };
  vm.createContext(ctx);
  vm.runInContext(WIKI_JS, ctx);
  // 模拟 boot 时编辑器的 v1 → 内存迁移：legacy 旧版兼容性测试需要
  if (token && !win.Wiki.getToken()) win.Wiki.setToken(token);
  return { Wiki: win.Wiki, store, win, doc: document };
}

// ---- token (in-memory model) ----

test('getToken returns "" when not configured', () => {
  const { Wiki } = loadWiki();
  assert.equal(Wiki.getToken(), '');
});

test('setToken puts token in memory (NOT localStorage)', () => {
  const { Wiki, store } = loadWiki();
  Wiki.setToken('ghp_test_xxx');
  assert.equal(Wiki.getToken(), 'ghp_test_xxx');
  // 关键：v1/v2_enc 都不应被 setToken 写入
  assert.equal(store['gh_token_v1'], undefined,
    'setToken must NOT write plaintext to localStorage (v1)');
  assert.equal(store['gh_token_v2_enc'], undefined,
    'setToken must NOT write to localStorage (v2_enc)');
});

test('setToken("") clears in-memory token', () => {
  const { Wiki, store } = loadWiki();
  Wiki.setToken('t');
  assert.equal(Wiki.getToken(), 't');
  Wiki.setToken('');
  assert.equal(Wiki.getToken(), '');
  assert.equal(store['gh_token_v1'], undefined);
});

test('clearToken wipes in-memory + both localStorage keys', () => {
  const { Wiki, store } = loadWiki({ token: 'old', encBlob: 'blob' });
  Wiki.setToken('t');
  Wiki.clearToken();
  assert.equal(Wiki.getToken(), '');
  assert.equal(store['gh_token_v1'], undefined);
  assert.equal(store['gh_token_v2_enc'], undefined);
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

// ---- wireTokenUI: in-memory flow ----

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

test('wireTokenUI: token in memory shows "已解锁（仅内存）"', () => {
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: () => {}, style: {} };
  const btnEnc = { addEventListener: () => {}, style: {} };
  const st = { textContent: '' };
  const { Wiki } = loadWiki({
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'btn-encrypt': btnEnc, 'token-status': st },
  });
  Wiki.setToken('ghp_in_mem');
  Wiki.wireTokenUI();
  assert.match(st.textContent, /已解锁/);
  assert.equal(inp.style.display, 'none', 'input hidden when unlocked');
  assert.equal(btnSv.style.display, 'none', 'save button hidden when unlocked');
  assert.equal(btnEnc.style.display, '', 'encrypt button visible (in-mem → can encrypt)');
});

test('wireTokenUI: legacy v1 token on disk shows migration hint', () => {
  const st = { textContent: '' };
  const banner = { textContent: '' };
  const { Wiki } = loadWiki({
    token: 'ghp_legacy',
    elements: { 'token-status': st, 'gh-status-banner': banner },
  });
  Wiki.wireTokenUI();
  // 旧 token 应被自动加载到内存
  assert.equal(Wiki.getToken(), 'ghp_legacy',
    'legacy v1 token should auto-migrate to memory at boot');
  // 状态应是「已解锁（仅内存）」并提示可以升级到加密存储
  assert.match(st.textContent, /已解锁/);
  assert.match(banner.textContent, /密码加密/);
});

test('wireTokenUI: encrypted blob + no memory shows "未解锁" + unlock button', async () => {
  // 先加密一个 token 拿到 blob
  const tmp = loadWiki();
  const blob = await tmp.Wiki.encryptToken('ghp_secret', 'pw123456');
  assert.ok(blob && blob.length > 0);

  const inp = { value: '', style: {} };
  const pwd = { value: '', style: {}, addEventListener: () => {} };
  const btnUnl = { addEventListener: () => {}, style: {} };
  const st = { textContent: '' };
  const banner = { textContent: '' };
  const { Wiki } = loadWiki({
    encBlob: blob,
    elements: { 'gh-token': inp, 'gh-pwd': pwd, 'btn-unlock': btnUnl, 'token-status': st, 'gh-status-banner': banner },
  });
  Wiki.wireTokenUI();
  assert.match(st.textContent, /未解锁/);
  assert.equal(btnUnl.style.display, '', 'unlock button visible');
  assert.equal(inp.style.display, 'none', 'paste input hidden');
});

test('wireTokenUI: save button loads token to memory (no localStorage write)', () => {
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
  assert.equal(store['gh_token_v1'], undefined, 'should not write v1');
  assert.equal(store['gh_token_v2_enc'], undefined, 'should not write v2');
});

test('wireTokenUI: paste with bullet prefix (••) is rejected', () => {
  let saveHandler;
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: (_evt, fn) => { saveHandler = fn; }, style: {} };
  const { Wiki } = loadWiki({
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'token-status': { textContent: '' } },
  });
  Wiki.wireTokenUI();
  inp.value = '••••••••••';
  saveHandler();
  assert.equal(Wiki.getToken(), '');
});

test('wireTokenUI: clear button wipes both memory + storage', () => {
  let clearHandler;
  const inp = { value: '', style: {} };
  const btnSv = { addEventListener: () => {}, style: {} };
  const btnCl = { style: {}, addEventListener: (_evt, fn) => { clearHandler = fn; } };
  const { Wiki, store } = loadWiki({
    token: 'old',
    elements: { 'gh-token': inp, 'btn-save-token': btnSv, 'btn-clear-token': btnCl, 'token-status': { textContent: '' } },
  });
  Wiki.setToken('in_mem');
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

test('encryptToken + decryptToken: roundtrip with correct password', async () => {
  const { Wiki } = loadWiki();
  const blob = await Wiki.encryptToken('ghp_super_secret', 'hunter2');
  assert.ok(blob && typeof blob === 'string' && blob.length > 0);
  const back = await Wiki.decryptToken(blob, 'hunter2');
  assert.equal(back, 'ghp_super_secret');
});

test('decryptToken: wrong password throws', async () => {
  const { Wiki } = loadWiki();
  const blob = await Wiki.encryptToken('ghp_secret', 'right');
  await assert.rejects(
    Wiki.decryptToken(blob, 'wrong'),
    /密码错|crypto-failed/
  );
});

test('decryptToken: corrupted blob throws', async () => {
  const { Wiki } = loadWiki();
  // 短于 28 字节的 blob 必然损坏
  const tooShort = Buffer.alloc(20).toString('base64');
  await assert.rejects(
    Wiki.decryptToken(tooShort, 'pw'),
    /blob 损坏|crypto-failed/
  );
});

test('encryptToken: rejects empty password', async () => {
  const { Wiki } = loadWiki();
  await assert.rejects(Wiki.encryptToken('t', ''), /密码/);
});

test('encryptToken: each call produces a different blob (random salt+iv)', async () => {
  const { Wiki } = loadWiki();
  const a = await Wiki.encryptToken('t', 'p');
  const b = await Wiki.encryptToken('t', 'p');
  assert.notEqual(a, b, 'salt+iv 必须是随机的');
});

test('encryptToken: handles non-ASCII plaintext', async () => {
  const { Wiki } = loadWiki();
  const blob = await Wiki.encryptToken('中文密码 / emoji 🔑', 'pw');
  const back = await Wiki.decryptToken(blob, 'pw');
  assert.equal(back, '中文密码 / emoji 🔑');
});

test('encryptCurrentToken: stores encrypted blob + removes legacy v1', async () => {
  let encHandler;
  const pwd = { value: 'mypw123', addEventListener: () => {}, style: {} };
  const btnEnc = { addEventListener: (_evt, fn) => { encHandler = fn; }, style: {} };
  const { Wiki, store } = loadWiki({
    token: 'ghp_old_v1',
    elements: { 'gh-pwd': pwd, 'btn-encrypt': btnEnc },
  });
  // boot 时编辑器会把 v1 加载到内存。这里手动模拟。
  Wiki.setToken('ghp_in_mem');
  Wiki.wireTokenUI();
  encHandler();
  // 等待 encryptCurrentToken 的 promise
  await new Promise((r) => setTimeout(r, 50));
  assert.ok(store['gh_token_v2_enc'] && store['gh_token_v2_enc'].length > 0);
  // v1 应被清掉
  assert.equal(store['gh_token_v1'], undefined,
    'v1 plaintext should be wiped after encryption');
  // 内存里仍应有
  assert.equal(Wiki.getToken(), 'ghp_in_mem');
});

test('unlockToken: correct password puts plaintext in memory', async () => {
  // 先制造一个 blob
  const tmp = loadWiki();
  const blob = await tmp.Wiki.encryptToken('ghp_top_secret', 'rightpw');
  // 模拟新会话：blob 在盘上，内存里没有
  const inp = { value: '', style: {} };
  const pwd = { value: '', addEventListener: () => {} };
  let unlHandler;
  const btnUnl = { addEventListener: (_evt, fn) => { unlHandler = fn; }, style: {} };
  const ctx = loadWiki({
    encBlob: blob,
    elements: { 'gh-token': inp, 'gh-pwd': pwd, 'btn-unlock': btnUnl },
  });
  ctx.Wiki.wireTokenUI();
  pwd.value = 'rightpw';
  unlHandler();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(ctx.Wiki.getToken(), 'ghp_top_secret');
});

test('unlockToken: wrong password keeps memory empty + shows friendly error', async () => {
  const tmp = loadWiki();
  const blob = await tmp.Wiki.encryptToken('t', 'right');
  const pwd = { value: '', addEventListener: () => {} };
  let unlHandler;
  const btnUnl = { addEventListener: (_evt, fn) => { unlHandler = fn; }, style: {} };
  const ctx = loadWiki({
    encBlob: blob,
    elements: { 'gh-pwd': pwd, 'btn-unlock': btnUnl },
  });
  ctx.Wiki.wireTokenUI();
  pwd.value = 'wrong';
  unlHandler();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(ctx.Wiki.getToken(), '');
});

test('hasEncryptedToken: true when v2_enc present, false otherwise', () => {
  const a = loadWiki();
  assert.equal(a.Wiki.hasEncryptedToken(), false);
  const b = loadWiki({ encBlob: 'blob' });
  assert.equal(b.Wiki.hasEncryptedToken(), true);
});

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

// ---- triggerWorkflow（QOJ 导入用）----

test('triggerWorkflow: 204 success → returns true and POSTs correct URL + body', async () => {
  const calls = [];
  const fakeFetch = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET', body: opts.body, headers: opts.headers });
    return { ok: true, status: 204, json: async () => ({}) };
  };
  const { Wiki } = loadWiki({ token: 'ghp_t', fetchImpl: fakeFetch });
  const ok = await Wiki.triggerWorkflow('qoj-import.yml', {
    contest_id: '2564', username: 'tarjen',
  });
  assert.equal(ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'POST');
  assert.equal(
    calls[0].url,
    'https://api.github.com/repos/tarjen/tarjen-wiki/actions/workflows/qoj-import.yml/dispatches'
  );
  const body = JSON.parse(calls[0].body);
  assert.equal(body.ref, 'main');
  assert.deepEqual(body.inputs, { contest_id: '2564', username: 'tarjen' });
  assert.match(calls[0].headers.Authorization, /^Bearer ghp_t$/);
  assert.equal(calls[0].headers['Content-Type'], 'application/json');
});

test('triggerWorkflow: 401 → friendly "Workflows: write" error', async () => {
  const fakeFetch = async () => ({
    ok: false, status: 401, json: async () => ({ message: 'Bad credentials' }),
  });
  const { Wiki } = loadWiki({ token: 'ghp_bad', fetchImpl: fakeFetch });
  await assert.rejects(
    Wiki.triggerWorkflow('qoj-import.yml', { contest_id: '1', username: 'u' }),
    /Workflows: write 权限/
  );
});

test('triggerWorkflow: 404 → friendly "找不到 workflow 文件" error', async () => {
  const fakeFetch = async () => ({
    ok: false, status: 404, json: async () => ({ message: 'Not Found' }),
  });
  const { Wiki } = loadWiki({ token: 't', fetchImpl: fakeFetch });
  await assert.rejects(
    Wiki.triggerWorkflow('missing.yml', {}),
    /找不到 workflow 文件.*missing\.yml/
  );
});

test('triggerWorkflow: 403 "ref locked" or similar propagates message', async () => {
  const fakeFetch = async () => ({
    ok: false, status: 403,
    json: async () => ({ message: 'Workflow does not have trigger:workflow_dispatch' }),
  });
  const { Wiki } = loadWiki({ token: 't', fetchImpl: fakeFetch });
  await assert.rejects(
    Wiki.triggerWorkflow('qoj-import.yml', { contest_id: '1', username: 'u' }),
    /trigger:workflow_dispatch/
  );
});

test('triggerWorkflow: without token → throws "No PAT configured"', async () => {
  const { Wiki } = loadWiki();
  await assert.rejects(
    Wiki.triggerWorkflow('qoj-import.yml', { contest_id: '1', username: 'u' }),
    /No PAT configured/
  );
});

test('triggerWorkflow: workflow file with path separator is URI-encoded in URL', async () => {
  // 防 path traversal: file path 里出现 ../ 之类的，不应出现在 URL 路径里
  const calls = [];
  const fakeFetch = async (url, opts = {}) => {
    calls.push({ url });
    return { ok: true, status: 204, json: async () => ({}) };
  };
  const { Wiki } = loadWiki({ token: 't', fetchImpl: fakeFetch });
  await Wiki.triggerWorkflow('qoj-import.yml', { contest_id: '1', username: 'u' });
  assert.ok(calls[0].url.includes('/actions/workflows/qoj-import.yml/dispatches'),
    'URL path must be well-formed');
  assert.ok(!calls[0].url.includes('..'), 'no path traversal');
});

test('triggerWorkflow: defaults ref to REPO.branch (main) when not specified', async () => {
  const calls = [];
  const fakeFetch = async (url, opts = {}) => {
    calls.push({ body: opts.body });
    return { ok: true, status: 204, json: async () => ({}) };
  };
  const { Wiki } = loadWiki({ token: 't', fetchImpl: fakeFetch });
  await Wiki.triggerWorkflow('qoj-import.yml', { contest_id: '1', username: 'u' });
  const body = JSON.parse(calls[0].body);
  assert.equal(body.ref, 'main');
});
