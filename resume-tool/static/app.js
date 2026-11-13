// 简历筛选工具 Web UI 交互（无外部依赖，离线可用）

// ---- 通用：弹窗开关 ----
function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.hidden = false;
}
function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.hidden = true;
}
document.addEventListener('click', e => {
  const closer = e.target.closest('[data-close]');
  if (closer) closeModal(closer.dataset.close);
  // 点击遮罩空白处关闭
  if (e.target.classList.contains('modal-mask')) e.target.hidden = true;
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-mask:not([hidden])').forEach(m => { m.hidden = true; });
  }
});

// ---- 文件拖拽区（简历上传 / 模板导入共用） ----
document.querySelectorAll('.dropzone').forEach(zone => {
  const input = zone.querySelector('input[type="file"]');
  const label = zone.querySelector('.dropzone-label');
  if (!input || !label) return;
  const base = label.dataset.base || label.textContent;

  function refresh() {
    const n = input.files.length;
    label.textContent = n > 0 ? `已选择 ${n} 个文件，点击右侧按钮提交` : base;
  }
  input.addEventListener('change', refresh);

  ['dragenter', 'dragover'].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach(evt =>
    zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.remove('dragover'); }));
  zone.addEventListener('drop', e => {
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      refresh();
    }
  });
});

// ---- 筛选提交：防重复点击 ----
(function () {
  const form = document.getElementById('screen-form');
  const btn = document.getElementById('screen-submit');
  if (!form || !btn) return;
  form.addEventListener('submit', () => {
    btn.disabled = true;
    btn.textContent = '筛选运行中，请稍候…（AI 模式按份调用，耗时较长）';
  });
})();

// ---- 进入页面：更新公告弹窗（同一会话只弹一次；勾选则当日不再弹） ----
(function () {
  const modal = document.getElementById('announce-modal');
  if (!modal || !window.RT) return;
  const key = window.RT.announceKey, day = window.RT.today;
  let dismissedDay = null, seenSession = null;
  try {
    dismissedDay = localStorage.getItem(key);
    seenSession = sessionStorage.getItem(key);
  } catch (e) { /* 隐私模式忽略 */ }
  if (dismissedDay !== day && seenSession !== day) openModal('announce-modal');

  const ok = document.getElementById('announce-ok');
  const skip = document.getElementById('announce-skip');
  function done() {
    try {
      sessionStorage.setItem(key, day);           // 本次会话内不再重复打扰
      if (skip && skip.checked) localStorage.setItem(key, day);  // 勾选则当日不再弹
    } catch (e) { /* 忽略 */ }
    closeModal('announce-modal');
  }
  if (ok) ok.addEventListener('click', done);
  modal.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', done));
})();

// ---- 简历库：多选批量删除 ----
(function () {
  const toggle = document.getElementById('multi-toggle');
  const bar = document.getElementById('batch-bar');
  const form = document.getElementById('batch-form');
  if (!toggle || !bar || !form) return;

  const countEl = document.getElementById('batch-count');
  const checkAll = document.getElementById('check-all');
  const rows = () => [...form.querySelectorAll('.row-check')];
  const checked = () => rows().filter(c => c.checked);

  function sync() {
    const n = checked().length;
    countEl.textContent = n;
    bar.hidden = !document.body.classList.contains('multi-mode');
    if (checkAll) {
      checkAll.checked = n > 0 && n === rows().length;
      checkAll.indeterminate = n > 0 && n < rows().length;
    }
  }
  function enter() {
    document.body.classList.add('multi-mode');
    toggle.classList.add('active');
    bar.hidden = false;
    sync();
  }
  function exit() {
    document.body.classList.remove('multi-mode');
    toggle.classList.remove('active');
    rows().forEach(c => { c.checked = false; });
    if (checkAll) { checkAll.checked = false; checkAll.indeterminate = false; }
    sync();
    bar.hidden = true;
  }

  toggle.addEventListener('click', () =>
    document.body.classList.contains('multi-mode') ? exit() : enter());
  document.getElementById('batch-cancel').addEventListener('click', exit);
  form.addEventListener('change', e => {
    if (e.target.classList.contains('row-check')) sync();
  });
  if (checkAll) {
    checkAll.addEventListener('change', () => {
      rows().forEach(c => { c.checked = checkAll.checked; });
      sync();
    });
  }

  // 提交拦截：确认弹窗 + 今日不再提示
  form.addEventListener('submit', e => {
    if (e.submitter && e.submitter.id !== 'batch-delete') return; // 单条删除放行
    const n = checked().length;
    if (!n) {
      e.preventDefault();
      bar.querySelector('span').innerHTML = '<b class="text-fail">请先勾选要删除的简历</b>';
      return;
    }
    const skipKey = 'rt_batch_skip';
    let skipped = null;
    try { skipped = localStorage.getItem(skipKey); } catch (err) { /* 忽略 */ }
    if (skipped !== window.RT.today) {
      e.preventDefault();
      document.getElementById('modal-count').textContent = n;
      openModal('batch-modal');
    }
  });

  document.getElementById('batch-confirm').addEventListener('click', () => {
    const skip = document.getElementById('batch-skip');
    try {
      if (skip && skip.checked) localStorage.setItem('rt_batch_skip', window.RT.today);
    } catch (e) { /* 忽略 */ }
    form.submit();
  });

  sync();
})();

// ---- 侧边栏：更新日志未读小红点 ----
(function () {
  const dot = document.getElementById('nav-changelog-dot');
  if (!dot || !window.RT || !window.RT.version) return;
  const KEY = 'rt_seen_version';
  if (location.pathname === '/changelog') {
    try { localStorage.setItem(KEY, window.RT.version); } catch (e) { /* 忽略 */ }
    return;
  }
  let seen = null;
  try { seen = localStorage.getItem(KEY); } catch (e) { /* 忽略 */ }
  if (seen !== window.RT.version) dot.hidden = false;
})();

// ---- 模板管理页：收藏切换 ----
document.querySelectorAll('.tpl-card [data-fav]').forEach(btn => {
  btn.addEventListener('click', async e => {
    e.preventDefault();
    try {
      const res = await fetch(`/templates/${btn.dataset.fav}/favorite`, { method: 'POST' });
      const data = await res.json();
      if (data.ok) btn.classList.toggle('on', data.favorite);
    } catch (err) { /* 忽略网络错误 */ }
  });
});
