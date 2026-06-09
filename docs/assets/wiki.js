// docs/assets/wiki.js
// 共享前端模块：被 /editor/?view=table 和 /editor/?view=md 同一页面 <script src="../assets/wiki.js"> 加载。
// 没有构建步骤、没有模块系统——直接挂到 window.Wiki。
// 要加第三个编辑器：引入这个文件 + 准备好约定的 DOM 元素 ID（见 wireTokenUI / wireBeforeUnload 注释）。
(function () {
  'use strict';

  // ---- 仓库元信息（要改 owner / repo / branch 只改这一处） ----
  var REPO = { owner: 'tarjen', repo: 'tarjen-wiki', branch: 'main' };
  // GH PAT：明文存 localStorage（和 QOJ cookie 一个待遇）。
  // 之前的 v2 加密方案已删：威胁模型（有人能读 localStorage 同时又不能读内存 token）
  // 太弱，且每次开 tab 输密码太烦。不存 repo、不打印到 log、不写 commit message。
  var TOKEN_KEY = 'gh_token_v1';
  // QOJ 登录 cookie：明文存 localStorage。qoj.ac cookie 7-30 天过期，过期去页面改一次。
  // 不加密——比 GH PAT 威力小（只能在 qoj.ac 当你发题），且只是 session 级权限。
  var QOJ_COOKIE_KEY = 'qoj_cookie_v1';
  var API_BASE = 'https://api.github.com';
  var RAW_BASE = 'https://raw.githubusercontent.com';

  // ---- Token 状态 ----
  // 单一来源：localStorage。getToken 每次都读（不是热路径，没问题）。
  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function setToken(t) {
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) {}
  }
  function clearToken() {
    try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
  }

  // ---- QOJ cookie（明文 localStorage） ----
  function getQojCookie() {
    try { return localStorage.getItem(QOJ_COOKIE_KEY) || ''; } catch (e) { return ''; }
  }
  function setQojCookie(v) {
    try {
      if (v) localStorage.setItem(QOJ_COOKIE_KEY, v);
      else localStorage.removeItem(QOJ_COOKIE_KEY);
    } catch (e) {}
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
  // 块内必须有以下 ID：gh-token (input)、btn-save-token、btn-clear-token、token-status (span)、
  // gh-status-banner (span, 可选)。任一缺失就静默跳过（不报错，方便部分页面只用子集）。
  function wireTokenUI() {
    var inp = document.getElementById('gh-token');
    var btnSv = document.getElementById('btn-save-token');
    var btnCl = document.getElementById('btn-clear-token');
    var st = document.getElementById('token-status');
    var banner = document.getElementById('gh-status-banner');

    function setVisible(el, on) { if (el) el.style.display = on ? '' : 'none'; }

    function refresh() {
      var v = getToken();
      if (v) {
        if (inp) { setVisible(inp, false); }
        if (btnSv) { setVisible(btnSv, false); }
        if (st) st.textContent = '✓ 已配置（' + v.length + ' 字符，存 localStorage）';
        if (banner) banner.textContent = 'Token 存浏览器 localStorage，不上传服务器。要换就粘新的再点保存。';
        if (btnCl) setVisible(btnCl, true);
      } else {
        if (inp) { setVisible(inp, true); }
        if (btnSv) { setVisible(btnSv, true); }
        if (st) st.textContent = '未配置';
        if (banner) banner.textContent = '粘一个 fine-grained PAT（仓库勾 tarjen/tarjen-wiki，权限只勾 Contents: Read and write），点保存。';
        if (btnCl) setVisible(btnCl, false);
      }
    }

    if (btnSv) btnSv.addEventListener('click', function () {
      var v = (inp && inp.value || '').trim();
      if (!v) { toast('先在输入框里粘 token', true); return; }
      setToken(v);
      if (inp) inp.value = '';
      refresh();
      toast('✓ Token 已保存到 localStorage');
    });

    if (btnCl) btnCl.addEventListener('click', function () {
      if (!confirm('清除 GitHub Token？')) return;
      clearToken();
      if (inp) inp.value = '';
      refresh();
      toast('Token 已清除');
    });

    refresh();
  }

  // ---- QOJ cookie UI（独立 details 块，明文存 localStorage） ----
  // 块内 ID：qoj-cookie (input)、btn-save-qoj-cookie、btn-clear-qoj-cookie、qoj-cookie-status (span)。
  // 任一缺失静默跳过。
  function wireQojCookieUI() {
    var inp = document.getElementById('qoj-cookie');
    var btnSv = document.getElementById('btn-save-qoj-cookie');
    var btnCl = document.getElementById('btn-clear-qoj-cookie');
    var st = document.getElementById('qoj-cookie-status');
    if (!inp || !btnSv) return;

    function refresh() {
      var v = getQojCookie();
      if (v) {
        if (st) st.textContent = '✓ 已配置（' + v.length + ' 字符）';
        if (btnCl) btnCl.style.display = '';
        if (inp) inp.placeholder = '已存（重新粘可覆盖）';
      } else {
        if (st) st.textContent = '未配置';
        if (btnCl) btnCl.style.display = 'none';
        if (inp) inp.placeholder = 'uoj_remember_token=...;uoj_remember_token_checksum=...;UOJSESSID=...';
      }
    }
    btnSv.addEventListener('click', function () {
      var v = (inp.value || '').trim();
      if (!v) { toast('先粘 cookie', true); return; }
      setQojCookie(v);
      inp.value = '';
      refresh();
      toast('✓ QOJ cookie 已保存到 localStorage');
    });
    if (btnCl) btnCl.addEventListener('click', function () {
      if (!confirm('清除 QOJ cookie？')) return;
      setQojCookie('');
      refresh();
      toast('已清除');
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
    REPO: REPO, TOKEN_KEY: TOKEN_KEY, QOJ_COOKIE_KEY: QOJ_COOKIE_KEY,
    getToken: getToken, setToken: setToken, clearToken: clearToken,
    getQojCookie: getQojCookie, setQojCookie: setQojCookie,
    apiUrl: apiUrl, rawUrl: rawUrl, apiHeaders: apiHeaders,
    commitFile: commitFile,
    esc: esc, toast: toast, setStatus: setStatus,
    wireTokenUI: wireTokenUI, wireQojCookieUI: wireQojCookieUI,
    wireBeforeUnload: wireBeforeUnload,
  };
})();
