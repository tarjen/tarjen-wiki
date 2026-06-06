// tests/js/assets.test.js
// 验证 HTML 里 inline 引用 hash / CSP / 资源路径对得上磁盘上的文件
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const REPO = path.join(__dirname, '..', '..');

function readUtf8(p) { return fs.readFileSync(p, 'utf8'); }

function sha384B64(buf) {
  return 'sha384-' + crypto.createHash('sha384').update(buf).digest('base64');
}

// 1) 合并后 editor 里的 marked.min.js SRI hash 必须和磁盘文件一致
test('editor SRI hash for marked.min.js matches the file', () => {
  const html = readUtf8(path.join(REPO, 'docs/editor/index.html'));
  const m = /src="\.\.\/assets\/marked\.min\.js"[^>]*integrity="([^"]+)"/.exec(html);
  assert.ok(m, 'should declare integrity attribute on marked.min.js <script>');

  const filePath = path.join(REPO, 'docs/assets/marked.min.js');
  const fileBuf = fs.readFileSync(filePath);
  const expected = sha384B64(fileBuf);
  assert.equal(m[1], expected, 'SRI hash must match the actual file content');
});

// 2) 合并后的 editor 有 CSP
test('editor declares a Content-Security-Policy meta tag', () => {
  const html = readUtf8(path.join(REPO, 'docs/editor/index.html'));
  assert.match(html, /http-equiv="Content-Security-Policy"/, 'editor missing CSP meta');
});

// 3) editor 的 CSP 必须同时允许 api.github.com 和 raw.githubusercontent.com
test('CSP allows api.github.com and raw.githubusercontent.com', () => {
  const html = readUtf8(path.join(REPO, 'docs/editor/index.html'));
  const m = /Content-Security-Policy" content="([^"]+)"/.exec(html);
  assert.ok(m, 'editor missing CSP');
  const csp = m[1];
  assert.match(csp, /https:\/\/api\.github\.com/, 'editor CSP should allow api.github.com');
  assert.match(csp, /https:\/\/raw\.githubusercontent\.com/,
    'editor CSP should allow raw.githubusercontent.com (needed by view=md loadFile fallback)');
});

// 4) editor 不再引用 cdn.jsdelivr.net（防止 CDN 投毒）
test('editor does not reference jsdelivr CDN', () => {
  const html = readUtf8(path.join(REPO, 'docs/editor/index.html'));
  assert.doesNotMatch(html, /cdn\.jsdelivr\.net/,
    'editor should not reference jsdelivr CDN — marked is now self-hosted');
});

// 5) docs/edit-md/ 已被合并到 editor/，目录应该消失
test('docs/edit-md/ directory has been removed (merged into editor/)', () => {
  assert.equal(fs.existsSync(path.join(REPO, 'docs/edit-md/index.html')), false,
    'docs/edit-md/index.html should be deleted; the md view lives at editor/?view=md');
});

// 5) marked.min.js 文件本身存在 + 大小合理
test('docs/assets/marked.min.js exists and is non-trivial', () => {
  const p = path.join(REPO, 'docs/assets/marked.min.js');
  assert.ok(fs.existsSync(p), 'marked.min.js should exist in docs/assets/');
  const sz = fs.statSync(p).size;
  assert.ok(sz > 10000, 'marked.min.js too small (' + sz + ' bytes), maybe truncated');
  assert.ok(sz < 200000, 'marked.min.js too large (' + sz + ' bytes)');
});

// 6) marked.min.js 必须能 parse 成合法 JS
test('marked.min.js parses as valid JavaScript', () => {
  const code = readUtf8(path.join(REPO, 'docs/assets/marked.min.js'));
  assert.doesNotThrow(() => new Function(code), 'marked.min.js is not valid JS');
});

// 7) wiki.css / wiki.js 必须存在
test('docs/assets/wiki.css + wiki.js exist', () => {
  assert.ok(fs.existsSync(path.join(REPO, 'docs/assets/wiki.css')));
  assert.ok(fs.existsSync(path.join(REPO, 'docs/assets/wiki.js')));
});
