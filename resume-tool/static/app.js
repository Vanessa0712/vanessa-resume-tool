// 简历筛选工具 Web UI 交互（无外部依赖，离线可用）

// ---- 滚动位置保持 ----
// 保存/删除/恢复备份等操作走的是「POST → 302 → GET」模式，浏览器把重定向当成
// 一次全新导航，页面会跳回顶部；从详情页点「返回简历库」同理。这里按 URL 记住
// 离开时的滚动位置，回到同一 URL 时还原一次。
(function () {
  const KEY = 'rt:scroll:';
  const key = () => KEY + location.pathname + location.search;

  function save() {
    try {
      sessionStorage.setItem(key(), String(window.scrollY || 0));
    } catch (e) { /* 隐私模式禁用存储时忽略 */ }
  }

  function restore() {
    if (location.hash) return;  // 带锚点（如 #history）时交给浏览器定位
    let raw = null;
    try {
      raw = sessionStorage.getItem(key());
      if (raw === null) return;
      sessionStorage.removeItem(key());  // 只还原一次，下次主动进入该页仍从头开始
    } catch (e) {
      return;
    }
    const y = parseInt(raw, 10);
    if (!(y > 0)) return;
    window.scrollTo(0, y);
    // 闪存提示条/字体加载会把内容下推，稍后再校一次
    setTimeout(() => window.scrollTo(0, y), 90);
  }

  document.addEventListener('submit', save, true);   // 表单提交（保存、删除、筛选）
  window.addEventListener('pagehide', save);          // 点链接离开
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restore);
  } else {
    restore();
  }
})();

// ---- 「返回」按钮：来源页正好是目标页时走浏览器后退 ----
// 直接链到 / 会丢掉来源页的模板筛选与页码；后退能原样回去，滚动位置也由浏览器保留。
// 只有来源页路径与按钮指向一致时才接管，避免「返回简历库」把人送到候选人页。
document.addEventListener('click', e => {
  const link = e.target.closest('a[data-back]');
  if (!link || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  const ref = document.referrer;
  if (!ref) return;
  try {
    const from = new URL(ref);
    if (from.origin !== location.origin) return;
    if (from.pathname !== new URL(link.href, location.origin).pathname) return;
  } catch (err) {
    return;
  }
  e.preventDefault();
  history.back();
});

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

// ---- 通用确认弹窗（替代浏览器原生 confirm）----
// 原生 confirm() 一旦被浏览器拦截（用户勾过「阻止此页面创建更多对话框」），
// 会直接返回 false 且不显示对话框，删除等写操作就静默失效、毫无反馈。
(function () {
  const modal = document.getElementById('confirm-modal');
  if (!modal) return;
  const titleEl = document.getElementById('confirm-title');
  const subEl = document.getElementById('confirm-sub');
  const bodyEl = document.getElementById('confirm-body');
  const okBtn = document.getElementById('confirm-ok');
  let pending = null;

  function ask(el, onOk) {
    titleEl.textContent = el.dataset.confirmTitle || '请确认';
    subEl.textContent = el.dataset.confirmSub || '';
    subEl.hidden = !el.dataset.confirmSub;
    bodyEl.textContent = el.dataset.confirmBody || '';
    okBtn.textContent = el.dataset.confirmOk || '确认';
    pending = onOk;
    openModal('confirm-modal');
    okBtn.focus();
  }

  okBtn.addEventListener('click', () => {
    const fn = pending;
    pending = null;
    closeModal('confirm-modal');
    if (fn) fn();
  });
  modal.querySelectorAll('[data-close]').forEach(b => {
    b.addEventListener('click', () => { pending = null; });
  });

  // 1) 按钮式操作（单条删除等）：按 formaction 提交
  document.addEventListener('click', e => {
    const btn = e.target.closest('button[data-confirm-delete]');
    if (!btn) return;
    e.preventDefault();
    ask(btn, () => {
      const url = btn.formAction || (btn.form && btn.form.action);
      let form = btn.form;
      if (!form) {
        form = document.createElement('form');
        form.method = 'post';
        document.body.appendChild(form);
      }
      form.action = url;
      form.submit();
    });
  });

  // 2) 表单式操作（彻底删除、清空回收站、恢复备份等）
  document.addEventListener('submit', e => {
    const form = e.target;
    if (!form.matches || !form.matches('form[data-confirm-body]')) return;
    e.preventDefault();
    ask(form, () => form.submit());
  });
})();

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

// ---- 简历库：模板搜索面板（搜索模板名/技能/部门，回车直达首项） ----
(function () {
  const btn = document.getElementById('tpl-pick-btn');
  const panel = document.getElementById('tpl-pick-panel');
  if (!btn || !panel) return;
  const search = document.getElementById('tpl-pick-search');
  const empty = document.getElementById('tpl-pick-empty');
  const items = Array.from(panel.querySelectorAll('[data-tpl-item], [data-tpl-all]'));
  const groups = Array.from(panel.querySelectorAll('.tpl-pick-group'));

  const setOpen = open => {
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    if (open) search.focus();
  };
  btn.addEventListener('click', e => { e.stopPropagation(); setOpen(panel.hidden); });
  document.addEventListener('click', e => {
    if (!panel.hidden && !panel.contains(e.target)) setOpen(false);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !panel.hidden) setOpen(false);
  });

  const visible = () => items.filter(it => !it.hidden);

  const applyFilter = () => {
    const q = search.value.trim().toLowerCase();
    items.forEach(it => {
      const hay = (it.dataset.search || '').toLowerCase();
      it.hidden = q !== '' && !hay.includes(q);
    });
    // 分组标题跟着组内可见项显隐，避免一片空标题
    groups.forEach(g => {
      let sib = g.nextElementSibling, any = false;
      while (sib && !sib.classList.contains('tpl-pick-group')) {
        if (sib.matches('[data-tpl-item]') && !sib.hidden) { any = true; break; }
        sib = sib.nextElementSibling;
      }
      g.hidden = !any;
    });
    empty.hidden = visible().length > 0;
  };
  search.addEventListener('input', applyFilter);

  // 回车直达首个匹配模板：审查时不用鼠标点选
  search.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const first = visible()[0];
    if (first) { e.preventDefault(); window.location.href = first.href; }
  });
})();
