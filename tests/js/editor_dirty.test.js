// tests/js/editor_dirty.test.js
// 验证 editor 的 snapshot/isDirty 逻辑——refactor 修复后别再退化
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

// 把 snapshot/isDirty 提到外面来测
// 复制一份核心逻辑过来测，避免要拉整个 editor HTML
function makeDirtyChecker(getForm, getCells) {
  let snap = null;
  return {
    snapshot() {
      snap = {
        name: getForm().name, slug: getForm().slug, date: getForm().date,
        total: getForm().total, link: getForm().link, tags: getForm().tags,
        cells: getCells().join(''),
      };
    },
    isDirty() {
      if (!snap) return false;
      const cur = {
        name: getForm().name, slug: getForm().slug, date: getForm().date,
        total: getForm().total, link: getForm().link, tags: getForm().tags,
        cells: getCells().join(''),
      };
      for (const k in cur) if (cur[k] !== snap[k]) return true;
      return false;
    },
  };
}

test('initial state (no snapshot) is not dirty', () => {
  const ck = makeDirtyChecker(() => ({ name: 'A', slug: 'a', date: '2024.5.1', total: '5', link: '', tags: '' }), () => ['.', '.', 'O', '.', '.']);
  assert.equal(ck.isDirty(), false);
});

test('just after snapshot with empty form is not dirty', () => {
  const ck = makeDirtyChecker(() => ({ name: '', slug: '', date: '', total: '13', link: '', tags: '' }), () => []);
  ck.snapshot();
  assert.equal(ck.isDirty(), false);
});

test('changing name after snapshot becomes dirty', () => {
  let form = { name: '', slug: '', date: '', total: '13', link: '', tags: '' };
  const ck = makeDirtyChecker(() => form, () => []);
  ck.snapshot();
  form = Object.assign({}, form, { name: 'New Contest' });
  assert.equal(ck.isDirty(), true);
});

test('changing a cell after snapshot becomes dirty', () => {
  let cells = ['.', '.', '.', '.', '.'];
  const ck = makeDirtyChecker(() => ({ name: '', slug: '', date: '', total: '5', link: '', tags: '' }), () => cells);
  ck.snapshot();
  cells = ['O', '.', '.', '.', '.'];
  assert.equal(ck.isDirty(), true);
});

test('re-snapshot resets the dirty baseline', () => {
  let form = { name: '', slug: '', date: '', total: '13', link: '', tags: '' };
  const ck = makeDirtyChecker(() => form, () => []);
  ck.snapshot();
  form = Object.assign({}, form, { name: 'X' });
  assert.equal(ck.isDirty(), true);
  form = Object.assign({}, form, { name: '' });
  ck.snapshot();  // 用户主动清空后重置基准
  assert.equal(ck.isDirty(), false);
});

test('cells.join( ) matches "OOOO" vs "OO.O" correctly', () => {
  let cells = ['O', 'O', 'O', 'O'];
  const ck = makeDirtyChecker(() => ({ name: '', slug: '', date: '', total: '4', link: '', tags: '' }), () => cells);
  ck.snapshot();
  assert.equal(ck.isDirty(), false);
  cells = ['O', 'O', '.', 'O'];
  assert.equal(ck.isDirty(), true);
});
