// docs/assets/wiki.js
// 共享前端模块：被 /editor/?view=table 和 /editor/?view=md 同一页面 <script src="../assets/wiki.js"> 加载。
// 没有构建步骤、没有模块系统——直接挂到 window.Wiki。
// 要加第三个编辑器：引入这个文件 + 准备好约定的 DOM 元素 ID（见 wireTokenUI / wireBeforeUnload 注释）。
(function () {
  'use strict';

  // ---- 仓库元信息（要改 owner / repo / branch 只改这一处） ----
  var REPO = { owner: 'tarjen', repo: 'tarjen-wiki', branch: 'main' };
  // v1 = 明文 PAT（遗留，向后兼容）；v2_enc = AES-GCM 加密后的 blob（base64 字符串）
  var TOKEN_KEY = 'gh_token_v1';
  var TOKEN_KEY_ENC = 'gh_token_v2_enc';
  var API_BASE = 'https://api.github.com';
  var RAW_BASE = 'https://raw.githubusercontent.com';

  // ---- Token 状态 ----
  // 明文 PAT 永远不写 localStorage（遗留的 v1 例外）；运行时只放在 _plain 内存里，刷新就丢。
  var _plain = '';
  function getToken() { return _plain; }
  function setToken(t) { _plain = t || ''; }
  // 兼容旧用法：编辑器的「保存 Token」按钮会调 setToken；新流程下 setToken 只放内存
  function clearToken() { _plain = ''; localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(TOKEN_KEY_ENC); }

  // ---- Web Crypto：密码派生 + AES-GCM 加密 ----
  // 失败抛 Error('crypto-unavailable') 或 Error('crypto-failed: ...')。
  // 存储格式：base64(salt[16] || iv[12] || ciphertext+N)，解密时再切。
  function b64encode(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  function b64decode(s) {
    var bin = atob(s);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }
  function _crypto() {
    if (typeof crypto === 'undefined' || !crypto.subtle) throw new Error('crypto-unavailable');
    return crypto.subtle;
  }
  // PBKDF2(password) → AES-GCM key. salt 至少 16 字节随机。
  async function deriveKey(password, salt) {
    var c = _crypto();
    var baseKey = await c.importKey(
      'raw', new TextEncoder().encode(password), { name: 'PBKDF2' }, false, ['deriveKey']
    );
    return c.deriveKey(
      { name: 'PBKDF2', salt: salt, iterations: 250000, hash: 'SHA-256' },
      baseKey,
      { name: 'AES-GCM', length: 256 },
      false, ['encrypt', 'decrypt']
    );
  }
  async function encryptToken(plaintext, password) {
    if (!password) throw new Error('密码不能为空');
    var salt = crypto.getRandomValues(new Uint8Array(16));
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var key = await deriveKey(password, salt);
    var ct = await _crypto().encrypt(
      { name: 'AES-GCM', iv: iv },
      key, new TextEncoder().encode(plaintext)
    );
    var out = new Uint8Array(salt.length + iv.length + ct.byteLength);
    out.set(salt, 0); out.set(iv, salt.length); out.set(new Uint8Array(ct), salt.length + iv.length);
    return b64encode(out);
  }
  async function decryptToken(blob, password) {
    if (!password) throw new Error('密码不能为空');
    var raw = b64decode(blob);
    if (raw.length < 16 + 12 + 1) throw new Error('crypto-failed: blob 损坏');
    var salt = raw.slice(0, 16);
    var iv = raw.slice(16, 28);
    var ct = raw.slice(28);
    var key = await deriveKey(password, salt);
    var pt;
    try {
      pt = await _crypto().decrypt({ name: 'AES-GCM', iv: iv }, key, ct);
    } catch (e) {
      throw new Error('crypto-failed: 密码错或 blob 损坏');
    }
    return new TextDecoder().decode(pt);
  }
  // 是否有加密的 token（localStorage 里）
  function hasEncryptedToken() {
    try { return !!localStorage.getItem(TOKEN_KEY_ENC); } catch (e) { return false; }
  }
  // 用密码解锁：成功 → 把明文放进 _plain；失败抛
  async function unlockToken(password) {
    var blob = localStorage.getItem(TOKEN_KEY_ENC);
    if (!blob) throw new Error('没有加密的 token');
    var pt = await decryptToken(blob, password);
    _plain = pt;
    return pt;
  }
  // 把当前明文（_plain）用密码加密后存到 localStorage；存完 _plain 不变（仍在内存里）
  async function encryptCurrentToken(password) {
    if (!_plain) throw new Error('没有可加密的 token');
    var blob = await encryptToken(_plain, password);
    localStorage.setItem(TOKEN_KEY_ENC, blob);
    // 既然已加密，把明文 v1 也清掉（如果有的话），单一来源
    localStorage.removeItem(TOKEN_KEY);
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
  // gh-pwd (input, 可选)、btn-unlock、btn-encrypt、gh-status-banner (span, 可选)。
  // 任一缺失就静默跳过（不报错，方便部分页面只用子集）。
  // 状态机：
  //   - 没有 token (v1/v2_enc 都没有) → 显示保存表单（粘 token → 保存明文到内存）
  //   - 有 v2_enc + 内存里没有明文 → 显示解锁表单（粘密码 → 解锁到内存）
  //   - 内存里有明文 → 显示「已解锁」+ 可选「加密到磁盘」+ 清除按钮
  function wireTokenUI() {
    var inp = document.getElementById('gh-token');
    var btnSv = document.getElementById('btn-save-token');
    var btnCl = document.getElementById('btn-clear-token');
    var st = document.getElementById('token-status');
    var pwd = document.getElementById('gh-pwd');
    var btnUnl = document.getElementById('btn-unlock');
    var btnEnc = document.getElementById('btn-encrypt');
    var banner = document.getElementById('gh-status-banner');

    function setVisible(el, on) { if (el) el.style.display = on ? '' : 'none'; }

    function refresh() {
      var inMem = !!_plain;
      var onDisk = hasEncryptedToken();
      var legacyDisk = false;
      try { legacyDisk = !!localStorage.getItem(TOKEN_KEY); } catch (e) {}

      if (inMem) {
        // 已解锁
        if (inp) { setVisible(inp, false); }
        if (btnSv) { setVisible(btnSv, false); }
        if (st) st.textContent = onDisk ? '🔓 已解锁（加密存储）' : '🔓 已解锁（仅内存）';
        if (btnEnc) { setVisible(btnEnc, !onDisk); }   // 已加密就不显示「加密」按钮
        if (banner) banner.textContent = onDisk
          ? 'Token 已用密码加密保存到 localStorage；关闭浏览器后需重新输入密码解锁。'
          : 'Token 当前只在内存里。点「🔒 用密码加密」可以存到 localStorage（下次启动要密码解锁）。';
      } else if (onDisk) {
        // 有加密 blob，需要解锁
        if (inp) { setVisible(inp, false); }
        if (btnSv) { setVisible(btnSv, false); }
        if (btnUnl) setVisible(btnUnl, true);
        if (st) st.textContent = '🔒 已加密，未解锁';
        if (banner) banner.textContent = 'localStorage 里有一个加密的 GitHub Token；输入密码解锁后即可保存。';
      } else {
        // 全新
        if (inp) { setVisible(inp, true); }
        if (btnSv) { setVisible(btnSv, true); }
        if (st) st.textContent = legacyDisk ? '✓ 已配置（v1 明文）' : '未配置';
        if (banner) banner.textContent = legacyDisk
          ? '检测到旧版（v1）明文 token。点「保存」会迁移到内存模式；点「🔒 用密码加密」会升级到加密存储。'
          : '粘一个 fine-grained PAT（仓库勾 tarjen/tarjen-wiki，权限只勾 Contents: Read and write），点保存。';
      }
      if (btnCl) setVisible(btnCl, inMem || onDisk || legacyDisk);
    }

    if (btnSv) btnSv.addEventListener('click', function () {
      var v = (inp && inp.value || '').trim();
      if (!v || v.indexOf('••') === 0) { toast('先在输入框里粘 token', true); return; }
      setToken(v);
      // 清理旧 v1 明文（如果有的话），新模式只在内存里
      try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
      refresh();
      toast('✓ Token 已加载到内存（关闭浏览器即清空）');
    });

    if (btnCl) btnCl.addEventListener('click', function () {
      if (!confirm('清除 GitHub Token？（同时清掉 localStorage 里的加密 blob）')) return;
      clearToken();
      if (inp) inp.value = '';
      if (pwd) pwd.value = '';
      refresh();
      toast('Token 已清除');
    });

    if (btnUnl) btnUnl.addEventListener('click', function () {
      var p = (pwd && pwd.value || '').trim();
      if (!p) { toast('先输入密码', true); return; }
      if (p.length < 6) { toast('密码至少 6 位', true); return; }
      btnUnl.disabled = true;
      var orig = btnUnl.textContent;
      btnUnl.textContent = '⏳ 解锁中…';
      unlockToken(p).then(function () {
        if (pwd) pwd.value = '';
        refresh();
        toast('🔓 已解锁');
      }).catch(function (e) {
        toast('解锁失败：' + e.message, true);
      }).then(function () {
        btnUnl.disabled = false;
        btnUnl.textContent = orig;
      });
    });

    if (btnEnc) btnEnc.addEventListener('click', function () {
      var p = (pwd && pwd.value || '').trim();
      if (!_plain) { toast('先粘 token 再加密', true); return; }
      if (!p) { toast('先在密码框里输入一个密码（要记牢！丢了就解不开）', true); return; }
      if (p.length < 6) { toast('密码至少 6 位', true); return; }
      // 已经加密过的话，第二次加密会覆盖旧 blob（salt/iv 都换）——旧密码立即失效
      if (hasEncryptedToken()) {
        if (!confirm('已经有一个加密的 token，要覆盖吗？\n旧密码会立即失效，丢了就找不回来。')) return;
      }
      btnEnc.disabled = true;
      var orig = btnEnc.textContent;
      btnEnc.textContent = '⏳ 加密中…';
      encryptCurrentToken(p).then(function () {
        if (pwd) pwd.value = '';
        refresh();
        toast('🔒 已用密码加密保存到 localStorage');
      }).catch(function (e) {
        // 常见原因：Safari 隐私模式 / storage 被禁
        if (e && (e.name === 'QuotaExceededError' || e.name === 'SecurityError')) {
          toast('localStorage 不可用（Safari 隐私模式？），token 只能留在内存里', true);
        } else {
          toast('加密失败：' + e.message, true);
        }
      }).then(function () {
        btnEnc.disabled = false;
        btnEnc.textContent = orig;
      });
    });

    // Enter 键在密码框里直接触发解锁 / 加密
    if (pwd) pwd.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        // S0（什么都没）的情况下不响应——用户得先点保存到内存把 token 加载进来
        if (!_plain && !hasEncryptedToken()) return;
        if (hasEncryptedToken() && !_plain && btnUnl) btnUnl.click();
        else if (_plain && btnEnc) btnEnc.click();
      }
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

  // ---- 触发 GitHub Actions workflow（用于「📥 从 QOJ 导入」等场景）----
  // 调 POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches
  // 需要 token 有 Workflows: write 权限；fine-grained PAT 需明确勾上
  // 返回 204 即视为成功；workflow 异步跑，本函数不等待结果
  async function triggerWorkflow(workflowFile, inputs, ref) {
    if (!getToken()) throw new Error('No PAT configured');
    var h = apiHeaders();
    h['Content-Type'] = 'application/json';
    var res = await fetch(
      API_BASE + '/repos/' + REPO.owner + '/' + REPO.repo +
      '/actions/workflows/' + encodeURIComponent(workflowFile) + '/dispatches',
      {
        method: 'POST',
        headers: h,
        body: JSON.stringify({
          ref: ref || REPO.branch,
          inputs: inputs || {},
        }),
      }
    );
    if (res.status === 204) return true;
    var err = {};
    try { err = await res.json(); } catch (e) {}
    if (res.status === 401) throw new Error('Token 无权触发 workflow（需要 Workflows: write 权限）');
    if (res.status === 404) throw new Error('找不到 workflow 文件：' + workflowFile);
    throw new Error(err.message || ('HTTP ' + res.status));
  }

  // ---- 暴露 ----
  window.Wiki = {
    REPO: REPO, TOKEN_KEY: TOKEN_KEY, TOKEN_KEY_ENC: TOKEN_KEY_ENC,
    getToken: getToken, setToken: setToken, clearToken: clearToken,
    hasEncryptedToken: hasEncryptedToken,
    encryptToken: encryptToken, decryptToken: decryptToken,
    unlockToken: unlockToken, encryptCurrentToken: encryptCurrentToken,
    apiUrl: apiUrl, rawUrl: rawUrl, apiHeaders: apiHeaders,
    commitFile: commitFile, triggerWorkflow: triggerWorkflow,
    esc: esc, toast: toast, setStatus: setStatus,
    wireTokenUI: wireTokenUI, wireBeforeUnload: wireBeforeUnload,
  };
})();
