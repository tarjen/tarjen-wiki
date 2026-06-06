// docs/assets/wiki.js
// 共享前端模块：被 /editor/ 和 /edit-md/ 两个页面 <script src="../assets/wiki.js"> 加载。
// 没有构建步骤、没有模块系统——直接挂到 window.Wiki。
// 要加第三个编辑器：引入这个文件 + 准备好约定的 DOM 元素 ID（见 wireTokenUI / wireBeforeUnload 注释）。
(function () {
  'use strict';

  // ---- 仓库元信息（要改 owner / repo / branch 只改这一处） ----
  var REPO = { owner: 'tarjen', repo: 'tarjen-wiki', branch: 'main' };
  var TOKEN_KEY = 'gh_token_v1';
  var API_BASE = 'https://api.github.com';
  var RAW_BASE = 'https://raw.githubusercontent.com';

  // ---- Token ----
  function getToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
  function setToken(t) {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  }

  // ---- URL 拼接 ----
  function apiUrl(path) { return API_BASE + '/repos/' + REPO.owner + '/' + REPO.repo + '/contents/' + path; }
  function rawUrl(path) { return RAW_BASE + '/' + REPO.owner + '/' + REPO.repo + '/' + REPO.branch + '/' + path; }

  // ---- API headers ----
  function apiHeaders() {
    return {
      'Authorization': 'Bearer ' + getToken(),
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'tarjen-wiki-editor',
    };
  }

  // ---- 提交一个文件：先 GET 拿 SHA，再 PUT 新内容 ----
  // 任何错误都 throw。错误信息对常见状态码 (401/404) 做了友好映射。
  async function commitFile(path, content, message) {
    if (!getToken()) throw new Error('No PAT configured (fill token in ⚙ Token config)');

    var h = apiHeaders();
    var getRes = await fetch(apiUrl(path) + '?ref=' + REPO.branch, { headers: h });
    if (!getRes.ok) {
      var getErr = await getRes.json().catch(function () { return {}; });
      var msg = getErr.message || ('GET ' + path + ' HTTP ' + getRes.status);
      if (getRes.status === 401) throw new Error('Token 无效或已过期：' + msg);
      if (getRes.status === 404) throw new Error('找不到文件（检查仓库/分支/路径）：' + msg);
      throw new Error(msg);
    }
    var sha = (await getRes.json()).sha;

    var b64 = btoa(unescape(encodeURIComponent(content)));
    var putRes = await fetch(apiUrl(path), {
      method: 'PUT',
      headers: Object.assign({}, h, { 'Content-Type': 'application/json' }),
      body: JSON.stringify({ message: message, content: b64, sha: sha, branch: REPO.branch }),
    });
    if (!putRes.ok) {
      var putErr = await putRes.json().catch(function () { return {}; });
      throw new Error(putErr.message || ('PUT ' + path + ' HTTP ' + putRes.status));
    }
    return await putRes.json();
  }

  // ---- UI helpers ----

  // HTML 转义。任何把用户输入塞进 innerHTML 之前必须走这个。
  function esc(s) {
    return (s || '').replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // 轻量 toast：需要页面里有 <div id="toast" class="toast"></div>。
  // 没有就 fallback 到 console。
  var _toastTimer = 0;
  function toast(msg, isError) {
    var t = document.getElementById('toast');
    if (!t) { (isError ? console.error : console.log)(msg); return; }
    t.textContent = msg;
    t.className = 'toast show' + (isError ? ' error' : '');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { t.className = 'toast'; }, 1800);
  }

  // 状态药丸：需要页面里有 <span id="status-pill" class="status-pill">。
  // state 候选：undefined（idle）/ 'dirty' / 'saved'
  function setStatus(text, state) {
    var el = document.getElementById('status-pill');
    if (!el) return;
    el.textContent = text;
    el.className = 'status-pill' + (state ? ' ' + state : '');
  }

  // 绑定 <details class="token-config" id="token-config"> 块。
  // 块内必须有以下 ID：gh-token (input)、btn-save-token、btn-clear-token、token-status (span)。
  // 任一缺失就静默跳过（不报错，方便部分页面只用子集）。
  function wireTokenUI() {
    var inp = document.getElementById('gh-token');
    var btnSv = document.getElementById('btn-save-token');
    var btnCl = document.getElementById('btn-clear-token');
    var st = document.getElementById('token-status');

    function refresh() {
      var has = !!getToken();
      if (inp) inp.value = has ? '••••••••••' : '';
      if (btnCl) btnCl.style.display = has ? '' : 'none';
      if (st) st.textContent = has ? '✓ 已配置' : '未配置';
    }

    if (btnSv) btnSv.addEventListener('click', function () {
      var v = (inp && inp.value || '').trim();
      if (!v || v.indexOf('••') === 0) { toast('先在输入框里粘 token', true); return; }
      setToken(v); refresh(); toast('✓ Token 已保存到 localStorage');
    });
    if (btnCl) btnCl.addEventListener('click', function () {
      if (!confirm('清除本地存储的 GitHub Token？')) return;
      setToken(''); refresh(); toast('Token 已清除');
    });

    refresh();
  }

  // 离开确认 + Cmd/Ctrl+S 快捷键。
  // 用法：Wiki.wireBeforeUnload(() => isDirty, () => saveFn());
  // 当 isDirty() 返回 true 时刷新/关 tab 会弹系统确认。
  // Cmd/Ctrl+S 会 preventDefault + 调 onSave()。
  function wireBeforeUnload(isDirty, onSave) {
    window.addEventListener('beforeunload', function (e) {
      if (isDirty()) { e.preventDefault(); e.returnValue = ''; }
    });
    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        if (onSave) onSave();
      }
    });
  }

  // ---- 暴露 ----
  window.Wiki = {
    REPO: REPO, TOKEN_KEY: TOKEN_KEY,
    getToken: getToken, setToken: setToken,
    apiUrl: apiUrl, rawUrl: rawUrl, apiHeaders: apiHeaders,
    commitFile: commitFile,
    esc: esc, toast: toast, setStatus: setStatus,
    wireTokenUI: wireTokenUI, wireBeforeUnload: wireBeforeUnload,
  };
})();
