/* 知新 · 文献日报 前端 */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const DIM_KEYS = ['relevance', 'expertise', 'reliability', 'novelty', 'inspiration'];
  const SCORE_WORDS = ['', '较弱', '偏弱', '合格', '不错', '很好'];
  const TYPE_LABELS = { article: '期刊论文', review: '综述', preprint: '预印本', conference: '会议论文' };
  const DAILY_CHOICES = [
    { count: 1, title: '1 篇', desc: '精读，宁缺毋滥' },
    { count: 2, title: '2 篇', desc: '主读 + 旁参' },
    { count: 3, title: '3 篇', desc: '一个小专题' },
    { count: 4, title: '4 篇', desc: '广度扫描' },
    { count: 5, title: '5 篇', desc: '集中扫读' },
  ];
  const PRESETS = [
    { label: '均衡', focus: [] },
    { label: '前沿探索', focus: ['novelty', 'inspiration'] },
    { label: '方法扎实', focus: ['reliability', 'expertise'] },
    { label: '紧贴课题', focus: ['relevance', 'expertise'] },
    { label: '换个视角', focus: ['inspiration', 'relevance'] },
  ];
  const LOADING_STEPS = [
    '连接 OpenAlex 全学科索引…',
    '核对 Crossref 的 DOI 与期刊信息…',
    '翻阅 arXiv 最新预印本…',
    '检索 Europe PMC 生命医学库…',
    '合并重复记录，剔除已读文献…',
    '按你的研究领域与偏好重排候选…',
    '写下今天的入选理由…',
  ];

  const S = {
    data: null,
    history: null,
    route: 'today',
    drafts: {},
    prefDraft: null,
    prefDay: null,
    expanded: new Set(),
    noteOpen: new Set(),
    archiveLoaded: false,
    archiveFilter: '',
    genError: null,
    generating: false,
    loadingTimer: null,
    loadingStep: 0,
    settingsDirty: false,
    clearKey: false,
    confirmResolve: null,
    booted: false,
  };

  // ---------------------------------------------------------------------
  // 基础工具
  // ---------------------------------------------------------------------
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function safeUrl(value) {
    if (!value) return '';
    try {
      const url = new URL(String(value), window.location.origin);
      if (!['http:', 'https:'].includes(url.protocol)) return '';
      return esc(url.href);
    } catch (e) {
      return '';
    }
  }

  function safeColor(value) {
    return /^#[0-9a-f]{6}$/i.test(String(value || '')) ? value : '#2F5D50';
  }

  function icon(name, cls = '') {
    return `<svg${cls ? ` class="${esc(cls)}"` : ''} aria-hidden="true"><use href="#i-${esc(name)}"></use></svg>`;
  }

  function parseLocalDate(iso) {
    if (!iso) return null;
    const parts = String(iso).slice(0, 10).split('-').map(Number);
    if (parts.length !== 3 || parts.some(Number.isNaN)) return null;
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function formatDateCN(iso, withYear = true) {
    const d = parseLocalDate(iso);
    if (!d) return iso || '';
    return `${withYear ? d.getFullYear() + ' 年 ' : ''}${d.getMonth() + 1} 月 ${d.getDate()} 日`;
  }

  function shortDate(iso) {
    const d = parseLocalDate(iso);
    return d ? `${d.getMonth() + 1}.${String(d.getDate()).padStart(2, '0')}` : iso;
  }

  function issueNumber(n) {
    return String(n || 1).padStart(2, '0');
  }

  function fieldColor(field) {
    return safeColor(field && field.color);
  }

  function dimMeta(key) {
    return (S.data.dimensions || []).find((d) => d.key === key) || { key, label: key, color: '#2F5D50' };
  }

  function dimLabel(key) {
    return dimMeta(key).label;
  }

  function focusLabels(focus) {
    return (focus || []).map(dimLabel);
  }

  function average(values) {
    const nums = values.filter((v) => typeof v === 'number' && v > 0);
    if (!nums.length) return null;
    return nums.reduce((a, b) => a + b, 0) / nums.length;
  }

  function sourceName(key) {
    const src = (S.data.sources || []).find((s) => s.key === key);
    return src ? src.name : key;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    const headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    let res;
    try {
      res = await fetch(path, { ...options, headers, signal: controller.signal });
    } catch (err) {
      clearTimeout(timer);
      if (err.name === 'AbortError') {
        const e = new Error('检索超时了，请稍后重试');
        e.hint = '网络较慢时，可只保留 OpenAlex + Crossref 两个数据源。';
        throw e;
      }
      const e = new Error('无法连接本地服务，请确认终端窗口仍在运行');
      e.detail = err.message;
      throw e;
    }
    clearTimeout(timer);
    const text = await res.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
    if (!res.ok) {
      const err = new Error((body && body.error) || `请求失败（HTTP ${res.status}）`);
      err.status = res.status;
      err.detail = (body && body.detail) || (body ? '' : text.slice(0, 300));
      err.hint = (body && body.hint) || '';
      throw err;
    }
    return body;
  }

  function toast(message, type = '', duration = 3600) {
    const region = $('#toast-region');
    if (!region) return;
    const node = document.createElement('div');
    node.className = `toast ${type}`;
    node.innerHTML = `${icon(type === 'error' ? 'x' : 'check')}<span>${esc(message)}</span>`;
    region.appendChild(node);
    while (region.children.length > 3) region.removeChild(region.firstElementChild);
    setTimeout(() => {
      node.style.opacity = '0';
      node.style.transform = 'translateY(6px)';
      setTimeout(() => node.remove(), 240);
    }, duration);
  }

  function formatError(err) {
    let text = err.message || '出了点问题';
    if (err.hint) text += ` ${err.hint}`;
    return text;
  }

  function obsidianStatusLine(status, enabled) {
    status = status || {};
    if (!enabled) return '尚未启用。开启后，已评分的文献会自动生成 Obsidian 笔记。';
    if (status.last_error) return `上次失败：${status.last_error}`;
    if (status.last_sync) {
      const result = status.last_result || {};
      return `上次同步：${String(status.last_sync).replace('T', ' ').slice(0, 16)} · 文献 ${result.papers || 0} 篇 · 日报 ${result.daily || 0} 篇`;
    }
    return '已启用，尚未执行同步。';
  }

  function zoteroStatusLine(status, enabled) {
    status = status || {};
    const counts = status.counts || {};
    if (!enabled) return '未启用。启用后会读取 Zotero 的阅读状态，避免重复推荐。';
    if (status.error) return `上次读取失败：${status.error}`;
    if (status.total) {
      return `已读取 ${status.total} 条 · 已读 ${counts.read || 0} · 在读 ${counts.reading || 0} · 未读 ${counts.unread || 0}`;
    }
    return '已启用，尚未同步。';
  }

  function notifyZotero(res) {
    if (!res || !res.zotero) return;
    const z = res.zotero;
    if (z.error) {
      toast(`Zotero：${z.error}`, 'error', 6000);
    } else if (z.total) {
      toast(`Zotero 已读取 ${z.total} 条文献`, 'success');
    }
  }

  function notifyObsidian(res) {
    if (!res || !res.obsidian) return;
    const result = res.obsidian;
    if (result.ok) {
      S.data.obsidian_status = {
        ...(S.data.obsidian_status || {}),
        last_sync: result.synced_at,
        last_result: result,
        last_error: '',
      };
      const bits = [];
      if (result.papers) bits.push(`文献 ${result.papers} 篇`);
      if (result.daily) bits.push(`日报 ${result.daily} 篇`);
      toast(bits.length ? `已同步到 Obsidian · ${bits.join(' · ')}` : 'Obsidian 已是最新', 'success');
    } else {
      S.data.obsidian_status = {
        ...(S.data.obsidian_status || {}),
        last_error: result.error || '同步失败',
      };
      toast(`Obsidian 同步失败：${result.error || '未知错误'}`, 'error', 6500);
    }
  }

  function setBusy(button, busy, label = '处理中…') {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.innerHTML;
      button.disabled = true;
      button.innerHTML = `${icon('refresh')} ${esc(label)}`;
    } else {
      button.disabled = false;
      if (button.dataset.originalLabel) button.innerHTML = button.dataset.originalLabel;
    }
  }

  function copyText(text) {
    if (!text) return Promise.reject(new Error('没有可复制的内容'));
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise((resolve, reject) => {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand('copy');
        resolve();
      } catch (e) {
        reject(e);
      } finally {
        area.remove();
      }
    });
  }

  function openConfirm(title, text, okLabel = '确认') {
    return new Promise((resolve) => {
      if (S.confirmResolve) S.confirmResolve(false);
      S.confirmResolve = resolve;
      $('#confirm-title').textContent = title;
      $('#confirm-text').textContent = text;
      $('#confirm-ok').textContent = okLabel;
      $('#confirm-dialog').hidden = false;
      document.body.classList.add('modal-open');
      setTimeout(() => $('#confirm-ok').focus(), 30);
    });
  }

  function closeConfirm(value) {
    $('#confirm-dialog').hidden = true;
    syncModalLock();
    if (S.confirmResolve) {
      const resolve = S.confirmResolve;
      S.confirmResolve = null;
      resolve(value);
    }
  }

  function syncModalLock() {
    const open = !$('#confirm-dialog').hidden || !$('#settings-drawer').hidden || !$('#loading-overlay').hidden;
    document.body.classList.toggle('modal-open', open);
  }

  // ---------------------------------------------------------------------
  // 启动与路由
  // ---------------------------------------------------------------------
  async function boot() {
    try {
      S.data = await api('/api/bootstrap');
      S.history = S.data.history || [];
      $('#boot-screen').hidden = true;
      S.booted = true;
      if (!S.data.configured) {
        renderOnboarding();
        return;
      }
      showShell();
      const route = (window.location.hash || '#today').replace('#', '');
      S.route = ['today', 'archive', 'insights'].includes(route) ? route : 'today';
      renderRoute();
      if (!S.data.day) generateToday(false);
    } catch (err) {
      $('#boot-screen').hidden = false;
      $('#boot-screen').innerHTML = `
        <div class="boot-inner">
          <div class="brand-lockup"><div class="brand-seal">知</div><div class="brand-name">知新<span>ZHIXIN</span></div></div>
          <p style="max-width:360px;line-height:1.9;margin:22px auto 16px">${esc(formatError(err))}</p>
          <button class="btn btn-ink" onclick="window.location.reload()">${icon('refresh')} 重新连接</button>
        </div>`;
    }
  }

  function showShell() {
    $('#app-shell').hidden = false;
    $('#onboarding').hidden = true;
    renderHeaderDate();
  }

  function renderHeaderDate() {
    const iso = S.data.today;
    const d = parseLocalDate(iso);
    if (!d) return;
    $('#header-date').innerHTML = `<b>${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}</b>${WEEKDAYS[d.getDay()]}`;
  }

  function go(route) {
    if (!['today', 'archive', 'insights'].includes(route)) route = 'today';
    if (S.route === route) {
      renderRoute();
      return;
    }
    window.location.hash = route;
  }

  function renderRoute(opts = {}) {
    if (!S.booted) return;
    const y = window.scrollY;
    S.route = ['today', 'archive', 'insights'].includes(S.route) ? S.route : 'today';
    $$('.nav-btn').forEach((btn) => btn.classList.toggle('is-active', btn.dataset.route === S.route));
    if (S.route === 'archive') renderArchive();
    else if (S.route === 'insights') renderInsights();
    else renderToday();
    if (opts.preserveScroll) requestAnimationFrame(() => window.scrollTo(0, y));
    else if (opts.resetScroll) window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // ---------------------------------------------------------------------
  // 今日
  // ---------------------------------------------------------------------
  function ensureDraft(pick) {
    if (!S.drafts[pick.id]) {
      S.drafts[pick.id] = {
        scores: { ...(pick.scores || {}) },
        note: pick.score_note || '',
        read_status: pick.read_status || 'unread',
      };
    }
    return S.drafts[pick.id];
  }

  function draftDirty(pick, draft) {
    const saved = pick.scores || {};
    for (const key of DIM_KEYS) {
      if ((saved[key] || null) !== (draft.scores[key] || null)) return true;
    }
    if ((pick.score_note || '') !== (draft.note || '')) return true;
    if ((pick.read_status || 'unread') !== (draft.read_status || 'unread')) return true;
    return false;
  }

  function draftTotal(draft) {
    return average(Object.values(draft.scores || {}));
  }

  function pickType(pick) {
    if (pick.type === 'preprint' || (pick.journal || '').startsWith('arXiv')) return '预印本';
    return TYPE_LABELS[pick.type] || '论文';
  }

  function renderToday() {
    if (!S.data) return;
    if (!S.data.day) {
      renderEmptyToday();
      return;
    }
    const day = S.data.day;
    ensurePrefDraft(day);
    const stats = S.data.stats || {};
    const meta = day.meta || {};
    const picks = day.picks || [];
    const scored = picks.filter((p) => p.scores && Object.keys(p.scores).length).length;
    const heroFields = (meta.fields && meta.fields.length)
      ? meta.fields
      : (day.field_snapshot && day.field_snapshot.length ? day.field_snapshot : (S.data.profile.fields || []));
    const prefText = preferenceSourceText(meta);
    const gateThreshold = Number(meta.min_venue_citedness || 0);
    const gateText = gateThreshold > 0
      ? ` 已按期刊 2 年均被引 ≥ ${gateThreshold} 过滤 ${meta.venue_filtered || 0} 篇（指标缺失：${meta.venue_missing_policy === 'include' ? '保留' : '排除'}）。`
      : '';
    const zmeta = meta.zotero || {};
    const zoteroText = zmeta.enabled
      ? (zmeta.error
        ? ' Zotero 本次未读取成功，已继续使用现有推荐规则。'
        : ` 已参考 Zotero 已读库，排除 ${zmeta.excluded || 0} 条候选。`)
      : '';
    const sourceNames = (meta.sources || []).filter((s) => s.ok).map((s) => s.name).join(' · ') || '公开学术源';
    const canRegenerate = scored === 0;
    const d = parseLocalDate(day.date);

    const hero = `
      <div class="issue-line">
        <div class="issue-left">
          <span class="issue-no">第 ${issueNumber(day.issue)} 期</span>
          <span class="issue-date">${esc(formatDateCN(day.date))} · ${d ? WEEKDAYS[d.getDay()] : ''}</span>
        </div>
        <div class="issue-right">已评 ${scored} / ${picks.length}
          <span class="read-progress-inline">${picks.map((p) => `<i class="${p.scores ? 'on' : ''}"></i>`).join('')}</span>
        </div>
      </div>
      <div class="page-hero">
        <h1>今日选读<span class="dot">。</span></h1>
        <p class="standfirst">从 <b>${meta.candidate_count || 0}</b> 篇候选中，为你留出 <b>${picks.length}</b> 篇。${prefText}${gateText}${zoteroText}${meta.warnings && meta.warnings.length ? ' 部分数据源本次未能返回，已在右侧标出。' : ''}</p>
        <div class="hero-meta">
          ${heroFields.slice(0, 4).map((f) => `<span class="field-chip" style="--c:${fieldColor(f)}">${esc(f.name)}</span>`).join('')}
          <span class="field-chip plain">${esc(sourceNames)}</span>
          <div class="hero-actions">
            ${canRegenerate ? `<button class="text-action js-regen">${icon('refresh')} 换一批</button>` : ''}
            <button class="text-action js-report">${icon('print')} 打印日报</button>
          </div>
        </div>
      </div>`;

    const errorBox = S.genError ? `
      <div class="error-box"><b>今天的检索没有完成</b>${esc(formatError(S.genError))}
        <div style="margin-top:9px"><button class="btn btn-ghost btn-small js-retry-gen">重试</button>
        <button class="btn btn-ghost btn-small js-open-settings" style="margin-left:6px">检查设置</button></div>
      </div>` : '';

    const papers = `<div class="paper-list">${picks.map((pick, i) => renderPaper(pick, i)).join('')}</div>`;
    const tomorrow = renderTomorrow(day, scored);

    $('#view').innerHTML = `
      <div class="today-layout">
        <div class="today-main">
          ${hero}
          ${errorBox}
          ${papers}
          ${tomorrow}
        </div>
        <aside class="sidebar" aria-label="今日概览">
          ${renderProgressCard(day, scored)}
          ${renderSourcesCard(day)}
          ${renderStatsCard(stats)}
          <p class="side-quote">「苟日新，日日新，又日新。」<br><em>——《礼记 · 大学》</em></p>
        </aside>
      </div>`;
    document.title = `知新 · ${day.date} 文献日报`;
  }

  function preferenceSourceText(meta) {
    if (!meta || !meta.preference_source) return '首期采用均衡权重，之后会根据你的评分与选择慢慢靠拢。';
    const labels = focusLabels(meta.preference_focus);
    if (!labels.length) return `排序参考了 ${meta.preference_source} 的均衡偏好。`;
    return `排序参考了 ${meta.preference_source} 定下的偏好：${labels.join('、')}。`;
  }

  function renderEmptyToday() {
    const isErr = !!S.genError;
    $('#view').innerHTML = `
      <div class="issue-line">
        <div class="issue-left"><span class="issue-no">TODAY</span><span class="issue-date">${esc(formatDateCN(S.data.today))}</span></div>
        <div class="issue-right">还没有选读</div>
      </div>
      <div class="page-hero"><h1>今日选读<span class="dot">。</span></h1>
        <p class="standfirst">一份只属于你的文献日报，正等着被翻开。</p>
      </div>
      <div class="empty-today">
        <div class="eyebrow" style="margin-bottom:10px">READY WHEN YOU ARE</div>
        <h2>${isErr ? '今天的检索没有完成' : '今天还没有选读'}</h2>
        <p>${isErr ? esc(formatError(S.genError)) : '从公开学术数据源中，为你挑 1–5 篇值得读的文献。首次检索大约需要 10–20 秒。'}</p>
        <div style="display:flex;gap:9px;flex-wrap:wrap;margin-top:20px">
          <button class="btn btn-ink js-retry-gen">${icon('refresh')} ${isErr ? '重新检索' : '生成今日选读'}</button>
          <button class="btn btn-ghost js-open-settings">${icon('settings')} 检查领域与数据源</button>
        </div>
      </div>`;
  }

  function renderPaper(pick, index) {
    const draft = ensureDraft(pick);
    const total = draftTotal(draft);
    const count = Object.keys(draft.scores || {}).length;
    const complete = count === DIM_KEYS.length;
    const dirty = draftDirty(pick, draft);
    const saved = pick.scores && Object.keys(pick.scores).length;
    const statusText = dirty ? '未保存' : saved ? `已评 ${average(Object.values(pick.scores)).toFixed(1)}` : '待评分';
    const statusClass = saved && !dirty ? 'scored' : '';
    const fieldBadges = (pick.fields || []).slice(0, 3).map((f) => `<span class="field-chip" style="--c:${fieldColor(f)}">${esc(f.name)}</span>`).join('');
    const titleUrl = safeUrl(pick.doi_url || pick.landing_url || pick.oa_url);
    const doi = pick.doi || '';
    const doiHref = safeUrl(pick.doi_url);
    const scholarUrl = 'https://scholar.google.com/scholar?q=' + encodeURIComponent(pick.title || '');
    const accessLinks = [
      pick.oa_url ? `<a class="text-action" href="${safeUrl(pick.oa_url)}" target="_blank" rel="noopener">${icon('arrow')} 开放全文</a>` : '',
      pick.access_url ? `<a class="text-action" href="${safeUrl(pick.access_url)}" target="_blank" rel="noopener">${icon('key')} 机构入口</a>` : '',
      pick.landing_url && pick.landing_url !== pick.doi_url ? `<a class="text-action" href="${safeUrl(pick.landing_url)}" target="_blank" rel="noopener">${icon('arrow')} 页面</a>` : '',
      `<a class="text-action" href="${safeUrl(scholarUrl)}" target="_blank" rel="noopener">${icon('search')} Google Scholar</a>`,
    ].filter(Boolean).join('');
    const abstract = pick.abstract || '';
    const isLong = abstract.length > 280;
    const abstractOpen = S.expanded.has(pick.id);
    const abstractHtml = abstract
      ? `<p class="paper-abstract ${abstractOpen ? 'is-open' : ''}" id="abs-${esc(pick.id)}">${esc(abstract)}</p>`
      : '<p class="paper-abstract is-empty">这条记录暂未提供摘要，建议通过 DOI 或机构入口查看原文。</p>';
    const abstractToggle = isLong
      ? `<button class="text-action js-toggle-abstract" data-id="${esc(pick.id)}">${abstractOpen ? '收起摘要' : '展开摘要'} ${icon('chevron')}</button>`
      : '';
    const why = (pick.why || []).map((w) => `<span>${esc(w)}</span>`).join('');
    const noteOpen = S.noteOpen.has(pick.id) || !!draft.note;
    const noteShown = pick.score_note && !noteOpen
      ? `<div class="score-note-shown"><b>札记</b>${esc(pick.score_note)}</div>` : '';

    const dims = S.data.dimensions.map((dim) => {
      const value = draft.scores[dim.key] || 0;
      return `
        <div class="dim-item" data-dim="${dim.key}">
          <div class="dim-label" title="${esc(dim.hint)}">${esc(dim.label)}</div>
          <div class="dim-hint">${esc(dim.hint)}</div>
          <div class="dot-row" role="group" aria-label="${esc(dim.label)}评分">
            ${[1, 2, 3, 4, 5].map((n) => `<button type="button" class="dot-btn ${n <= value ? 'on' : ''} js-dot" style="--c:${safeColor(dim.color)}" data-id="${esc(pick.id)}" data-key="${dim.key}" data-value="${n}" aria-label="${esc(dim.label)} ${n} 分" aria-pressed="${n <= value}"></button>`).join('')}
          </div>
          <div class="dim-value" data-dim-value="${dim.key}">${value ? `${value} · ${SCORE_WORDS[value]}` : '未评'}</div>
        </div>`;
    }).join('');

    const readOptions = Object.entries(S.data.read_status || {}).map(([value, label]) =>
      `<option value="${esc(value)}" ${draft.read_status === value ? 'selected' : ''}>${esc(label)}</option>`).join('');

    return `
      <article class="paper-card" data-paper="${esc(pick.id)}">
        <div class="paper-rail"><span class="paper-no">${String(index + 1).padStart(2, '0')}</span><span class="paper-rail-line"></span><span class="paper-rail-type">${esc(pickType(pick))}</span></div>
        <div class="paper-content">
          <div class="paper-kicker">
            ${fieldBadges}
            <span class="paper-type">${esc(pickType(pick))}</span>
            <span class="paper-status ${statusClass}">${esc(statusText)}</span>
          </div>
          <h2 class="paper-title"><a href="${titleUrl || '#'}" target="_blank" rel="noopener">${esc(pick.title)}</a></h2>
          <p class="paper-authors">${esc(pick.author_line || '作者信息暂缺')}</p>
          <div class="paper-source">
            <span class="journal">${esc(pick.journal || '未标注期刊')}</span>
            ${pick.year ? `<span class="sep">/</span><span>${esc(pick.year)}</span>` : ''}
            ${pick.published ? `<span class="sep">/</span><span>${esc(pick.published)}</span>` : ''}
            ${doi ? `<span class="sep">/</span><a class="doi-link" href="${doiHref}" target="_blank" rel="noopener">DOI ${esc(doi)}</a><button class="copy-btn js-copy-doi" data-doi="${esc(doi)}" title="复制 DOI" aria-label="复制 DOI">${icon('copy')}</button>` : ''}
          </div>
          <div class="paper-tools">
            ${doi ? `<a class="text-action" href="${doiHref}" target="_blank" rel="noopener">${icon('arrow')} DOI 原文</a>` : ''}
            ${accessLinks}
            ${abstractToggle}
          </div>
          ${abstractHtml}
          ${why ? `<div class="why-line"><span class="why-label">入选理由</span><div class="why-chips">${why}</div></div>` : ''}
          ${noteShown}
          <div class="score-panel" data-score-panel>
            <div class="score-panel-head">
              <span class="eyebrow">Reading Score · 我的评分</span>
              <span class="score-save-state ${saved && !dirty ? 'saved' : ''}">${dirty ? '有未保存的修改' : saved ? `已保存${pick.scored_at ? ' · ' + esc(pick.scored_at.slice(11, 16)) : ''}` : '五分制，凭第一判断即可'}</span>
            </div>
            <div class="score-grid">
              <div class="score-total">
                <div class="score-ring" style="--pct:${total ? (total / 5 * 100).toFixed(1) : 0}">
                  <b>${total ? total.toFixed(1) : '—'}<small>/5</small></b>
                </div>
                <div class="score-total-label">综合</div>
              </div>
              <div class="score-dims">${dims}</div>
              <div class="score-side">
                <select class="read-select" data-read-status="${esc(pick.id)}" aria-label="阅读状态">${readOptions}</select>
                <button type="button" class="note-toggle js-note-toggle" data-id="${esc(pick.id)}">${noteOpen ? '收起札记' : (draft.note ? '编辑札记' : '＋ 写一句札记')}</button>
                <button type="button" class="btn btn-ink btn-save js-save-score" data-id="${esc(pick.id)}">${saved ? '更新评分' : '保存评分'}</button>
                <div class="missing-hint" data-missing="${esc(pick.id)}">${complete ? '五维已完成' : `还差 ${DIM_KEYS.length - count} 项`}</div>
              </div>
            </div>
            <div class="note-wrap" ${noteOpen ? '' : 'hidden'}>
              <label for="note-${esc(pick.id)}">阅读札记</label>
              <textarea class="note-input" id="note-${esc(pick.id)}" data-note="${esc(pick.id)}" placeholder="一句话记下：方法、数据、可借鉴之处，或一个没想通的问题…">${esc(draft.note || '')}</textarea>
            </div>
          </div>
        </div>
      </article>`;
  }

  function renderTomorrow(day, scored) {
    const ready = scored > 0;
    const pref = day.preference;
    const savedFocus = (pref && pref.focus) || [];
    const draft = S.prefDraft || { focus: [], note: '' };
    const active = new Set(draft.focus || []);
    const focusCards = (S.data.dimensions || []).map((dim) => `
      <button type="button" class="focus-card ${active.has(dim.key) ? 'active' : ''} js-focus" data-key="${dim.key}" aria-pressed="${active.has(dim.key)}" style="--c:${safeColor(dim.color)}">
        <div class="fc-top"><span class="fc-name">${esc(dim.label)}</span><span class="fc-check">${icon('check')}</span></div>
        <span class="fc-dot" style="--c:${safeColor(dim.color)}"></span>
        <span class="fc-desc">${esc(dim.hint)}</span>
      </button>`).join('');
    const savedSummary = pref ? `
      <div class="pref-saved-stamp">已定：<b>${esc(focusLabels(pref.focus).join('、') || '均衡探索')}</b>${pref.created_at ? ` · ${esc(pref.created_at.slice(11, 16))}` : ''}</div>` : '';
    const weights = (pref && pref.effective_weights) || (day.meta && day.meta.weights) || {};
    const weightLine = pref ? `<div class="effective-weights">明日排序权重：${DIM_KEYS.map((k) => `<span>${esc(dimLabel(k))} <b>${weights[k] || 0}%</b></span>`).join('')}</div>` : '';
    return `
      <section class="tomorrow ${ready ? '' : 'locked'}" id="tomorrow">
        <div class="tomorrow-head">
          <div>
            <div class="eyebrow">Next Issue · 明日倾向</div>
            <h2>明天，想读点什么？</h2>
            <p class="pref-desc">${ready ? '选中的维度会在明日排序中提权约 1.9 倍；不选则保持均衡。也可以写一句话，让偏好更具体。' : '完成至少一篇评分后解锁。你的选择会直接改变明天的候选排序。'}</p>
          </div>
          ${ready ? savedSummary : `<span class="lock-badge">${icon('lock')} 评分后解锁</span>`}
        </div>
        <div class="presets">
          <span class="preset-label">Quick pick</span>
          ${PRESETS.map((p) => `<button type="button" class="preset-btn js-preset" data-focus="${p.focus.join(',')}">${esc(p.label)}</button>`).join('')}
        </div>
        <div class="focus-grid">${focusCards}</div>
        <div class="pref-note-row">
          <textarea class="pref-note" id="pref-note" maxlength="500" placeholder="可选：最近在准备组会，想多看方法扎实的实证研究…">${esc(draft.note || '')}</textarea>
          <button type="button" class="btn btn-accent js-save-pref" ${ready ? '' : 'disabled'}>${pref ? '更新明日方向' : '定下明日方向'} ${icon('arrow')}</button>
        </div>
        ${weightLine}
      </section>`;
  }

  function ensurePrefDraft(day) {
    if (S.prefDay !== day.date || !S.prefDraft) {
      S.prefDay = day.date;
      const pref = day.preference || {};
      S.prefDraft = { focus: [...(pref.focus || [])], note: pref.note || '' };
    }
  }

  function renderProgressCard(day, scored) {
    const picks = day.picks || [];
    const ready = scored > 0;
    const pref = day.preference;
    const prefSummary = pref
      ? `明日倾向：<b>${esc(focusLabels(pref.focus).join('、') || '均衡探索')}</b>${ready ? '<button class="mini-btn" data-scroll-tomorrow>修改</button>' : ''}`
      : ready ? '还没有定下明日倾向。<button class="mini-btn" data-scroll-tomorrow>去选择</button>' : '完成评分后解锁明日倾向。';
    return `
      <section class="side-card">
        <div class="side-card-head"><h3>Today's Progress · 今日进度</h3><span class="side-link">${ready ? '进行中' : '待开始'}</span></div>
        <div class="progress-big">${scored}<small> / ${picks.length}</small></div>
        <div class="progress-caption">${scored === 0 ? '读完一篇，就从这里开始。' : scored >= picks.length ? '今天的选读已经评完。' : `还有 ${picks.length - scored} 篇待评分。`}</div>
        <div class="progress-segments">${picks.map((p) => `<i class="${p.scores && Object.keys(p.scores).length ? 'on' : (S.drafts[p.id] && Object.keys(S.drafts[p.id].scores || {}).length ? 'half' : '')}"></i>`).join('')}</div>
        <div class="pref-mini">${prefSummary}</div>
      </section>`;
  }

  function renderSourcesCard(day) {
    const statuses = (day.meta && day.meta.sources) || [];
    const enabled = (S.data.profile.sources || []);
    const rows = statuses.length ? statuses : enabled.map((key) => ({ key, name: sourceName(key), ok: true, count: 0, error: '' }));
    const warnings = rows.filter((s) => !s.ok).map((s) => `${s.name}：${s.error || '未能返回'}`);
    return `
      <section class="side-card">
        <div class="side-card-head"><h3>Sources · 本期数据源</h3><span class="side-link">${rows.filter((s) => s.ok).length}/${rows.length}</span></div>
        <div class="source-list">
          ${rows.map((s) => `<div class="source-row" title="${esc(s.error || s.name)}"><span class="source-dot ${s.ok ? '' : 'off'}"></span><span>${esc(s.name || sourceName(s.key))}</span><small>${s.ok ? (s.count ? s.count + ' 篇' : '已连接') : '未返回'}</small></div>`).join('')}
        </div>
        ${warnings.length ? `<div class="source-warning">${esc(warnings.join('；'))}。其他数据源仍可正常推荐。</div>` : ''}
      </section>`;
  }

  function renderStatsCard(stats) {
    const last14 = stats.last14 || [];
    const bars = last14.map((d, i) => {
      const h = d.avg ? Math.max(10, d.avg / 5 * 100) : (d.count ? 16 : 4);
      const cls = i === last14.length - 1 ? 'today' : (d.scored ? 'on' : '');
      return `<div class="mini-bar" title="${esc(d.date)} · ${d.count} 篇 · ${d.avg ? '均分 ' + d.avg : '未评分'}"><i class="${cls}" style="height:${h}%"></i></div>`;
    }).join('');
    return `
      <section class="side-card">
        <div class="side-card-head"><h3>Reading Archive · 阅读档案</h3><button class="side-link" data-route="insights">洞察</button></div>
        <div class="side-stats">
          <div class="side-stat"><b>${stats.streak || 0}</b><span>连续天数</span></div>
          <div class="side-stat"><b>${stats.avg_total ? stats.avg_total.toFixed(1) : '—'}</b><span>平均分</span></div>
          <div class="side-stat"><b>${stats.scored_count || 0}</b><span>已评篇数</span></div>
        </div>
        <div class="mini-chart">${bars}</div>
        <div class="mini-chart-caption"><span>14 天前</span><span>今天</span></div>
      </section>`;
  }

  // ---------------------------------------------------------------------
  // 打分与偏好交互
  // ---------------------------------------------------------------------
  function updateScorePanel(pickId) {
    const card = document.querySelector(`[data-paper="${CSS.escape(pickId)}"]`);
    const pick = findPick(pickId);
    if (!card || !pick) return;
    const draft = ensureDraft(pick);
    const total = draftTotal(draft);
    const count = Object.keys(draft.scores || {}).length;
    const complete = count === DIM_KEYS.length;
    const dirty = draftDirty(pick, draft);
    const saved = pick.scores && Object.keys(pick.scores).length;
    const ring = $('.score-ring', card);
    if (ring) {
      ring.style.setProperty('--pct', total ? (total / 5 * 100).toFixed(1) : 0);
      const b = $('b', ring);
      if (b) b.innerHTML = `${total ? total.toFixed(1) : '—'}<small>/5</small>`;
    }
    const state = $('.score-save-state', card);
    if (state) {
      state.textContent = dirty ? '有未保存的修改' : saved ? `已保存${pick.scored_at ? ' · ' + pick.scored_at.slice(11, 16) : ''}` : '五分制，凭第一判断即可';
      state.classList.toggle('saved', Boolean(saved && !dirty));
    }
    const status = $('.paper-status', card);
    if (status) {
      status.textContent = dirty ? '未保存' : saved ? `已评 ${average(Object.values(pick.scores)).toFixed(1)}` : '待评分';
      status.classList.toggle('scored', Boolean(saved && !dirty));
    }
    const missing = $(`[data-missing="${CSS.escape(pickId)}"]`, card);
    if (missing) missing.textContent = complete ? '五维已完成' : `还差 ${DIM_KEYS.length - count} 项`;
    const saveBtn = $('.js-save-score', card);
    if (saveBtn) saveBtn.textContent = saved ? '更新评分' : '保存评分';
  }

  function setScore(pickId, key, value) {
    const pick = findPick(pickId);
    if (!pick) return;
    const draft = ensureDraft(pick);
    draft.scores[key] = value;
    const card = document.querySelector(`[data-paper="${CSS.escape(pickId)}"]`);
    if (!card) return;
    $$('.dot-btn', card).filter((btn) => btn.dataset.key === key).forEach((btn) => {
      const on = Number(btn.dataset.value) <= value;
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
    });
    const valueNode = $(`[data-dim-value="${key}"]`, card);
    if (valueNode) valueNode.textContent = `${value} · ${SCORE_WORDS[value]}`;
    updateScorePanel(pickId);
  }

  function findPick(pickId) {
    return ((S.data.day && S.data.day.picks) || []).find((p) => p.id === pickId);
  }

  async function saveScore(pickId, button) {
    const pick = findPick(pickId);
    if (!pick) return;
    const card = document.querySelector(`[data-paper="${CSS.escape(pickId)}"]`);
    const draft = ensureDraft(pick);
    const count = Object.keys(draft.scores || {}).length;
    if (count < DIM_KEYS.length) {
      toast(`还差 ${DIM_KEYS.length - count} 项评分`, 'error');
      const missingKey = DIM_KEYS.find((k) => !draft.scores[k]);
      const target = missingKey ? $(`[data-dim="${missingKey}"] .dot-btn`, card) : null;
      if (target) target.focus();
      return;
    }
    const noteInput = $(`[data-note="${CSS.escape(pickId)}"]`, card);
    if (noteInput) draft.note = noteInput.value;
    setBusy(button, true, '保存中');
    try {
      const res = await api('/api/score', {
        method: 'POST',
        body: JSON.stringify({
          date: S.data.day.date,
          id: pickId,
          scores: draft.scores,
          score_note: draft.note,
          read_status: draft.read_status,
        }),
      });
      applyDayResponse(res);
      const allScored = (S.data.day.picks || []).every((p) => p.scores && Object.keys(p.scores).length);
      renderRoute({ preserveScroll: true });
      toast('评分已保存', 'success');
      notifyObsidian(res);
      if (allScored) {
        setTimeout(() => {
          const panel = $('#tomorrow');
          if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 350);
      }
    } catch (err) {
      toast(formatError(err), 'error', 5200);
      setBusy(button, false);
    }
  }

  async function savePreference(button) {
    if (!S.data.day || S.data.day.scored_count < 1) return;
    const draft = S.prefDraft || { focus: [], note: '' };
    const noteInput = $('#pref-note');
    if (noteInput) draft.note = noteInput.value;
    setBusy(button, true, '保存中');
    try {
      const res = await api('/api/preference', {
        method: 'POST',
        body: JSON.stringify({ date: S.data.day.date, focus: draft.focus, note: draft.note }),
      });
      S.prefDay = null;
      applyDayResponse(res);
      renderRoute({ preserveScroll: true });
      toast(`已定下：${focusLabels(draft.focus).join('、') || '均衡探索'}`, 'success');
      notifyObsidian(res);
    } catch (err) {
      toast(formatError(err), 'error');
      setBusy(button, false);
    }
  }

  function applyDayResponse(res) {
    if (res.day) {
      S.data.day = res.day;
      S.data.needs_generation = false;
    }
    if (res.stats) S.data.stats = res.stats;
    if (res.history) { S.data.history = res.history; S.history = res.history; }
    if (res.preference_hint) S.data.preference_hint = res.preference_hint;
    S.genError = null;
    S.archiveLoaded = false;
  }

  // ---------------------------------------------------------------------
  // 生成与加载
  // ---------------------------------------------------------------------
  async function generateToday(force = false) {
    if (S.generating || !S.data) return;
    S.generating = true;
    S.genError = null;
    showLoading(force);
    try {
      const res = await api('/api/generate', {
        method: 'POST',
        body: JSON.stringify({ date: S.data.today, force }),
      });
      S.drafts = {};
      S.prefDay = null;
      S.prefDraft = null;
      S.expanded.clear();
      S.noteOpen.clear();
      applyDayResponse(res);
      hideLoading();
      renderRoute({ resetScroll: true });
      toast(res.cached ? '今日选读已就绪' : '今日选读已生成', 'success');
      notifyObsidian(res);
    } catch (err) {
      hideLoading();
      S.genError = err;
      renderToday();
      toast(formatError(err), 'error', 7000);
    } finally {
      S.generating = false;
    }
  }

  function showLoading(force) {
    const overlay = $('#loading-overlay');
    overlay.hidden = false;
    syncModalLock();
    $('#loading-title').textContent = force ? '正在换一批选读' : '正在翻阅今天的期刊';
    $('#loading-status').textContent = LOADING_STEPS[0];
    const selected = (S.data.profile.sources || []).map(sourceName);
    $('#loading-sources').innerHTML = selected.map((s) => `<span>${esc(s)}</span>`).join('');
    S.loadingStep = 0;
    clearInterval(S.loadingTimer);
    S.loadingTimer = setInterval(() => {
      S.loadingStep = (S.loadingStep + 1) % LOADING_STEPS.length;
      const el = $('#loading-status');
      if (el) el.textContent = LOADING_STEPS[S.loadingStep];
    }, 1900);
  }

  function hideLoading() {
    $('#loading-overlay').hidden = true;
    clearInterval(S.loadingTimer);
    S.loadingTimer = null;
    syncModalLock();
  }

  // ---------------------------------------------------------------------
  // 事件绑定
  // ---------------------------------------------------------------------
  document.addEventListener('click', async (e) => {
    const nav = e.target.closest('[data-route]');
    if (nav) {
      e.preventDefault();
      go(nav.dataset.route);
      return;
    }
    if (e.target.closest('#btn-settings') || e.target.closest('#footer-settings') || e.target.closest('.js-open-settings')) {
      e.preventDefault();
      openSettings();
      return;
    }
    if (e.target.closest('#btn-quit')) {
      e.preventDefault();
      quitApp();
      return;
    }
    if (e.target.closest('#btn-print') || e.target.closest('.js-report')) {
      e.preventDefault();
      if (!S.data || !S.data.day) { toast('还没有可打印的日报', 'error'); return; }
      window.open(`/api/report?date=${encodeURIComponent(S.data.day.date)}`, '_blank', 'noopener');
      return;
    }
    if (e.target.closest('.js-regen') || e.target.closest('.js-retry-gen')) {
      e.preventDefault();
      if (!S.data) return;
      if (S.data.day && (S.data.day.picks || []).some((p) => p.scores)) {
        toast('已评分的日报不能整批更换', 'error');
        return;
      }
      if (S.data.day) {
        const ok = await openConfirm('换一批今天的选读？', '当前推荐还没有评分，可以重新检索。已读过的文献会被排除。', '换一批');
        if (!ok) return;
      }
      generateToday(true);
      return;
    }
    if (e.target.closest('.js-toggle-abstract')) {
      const id = e.target.closest('.js-toggle-abstract').dataset.id;
      if (S.expanded.has(id)) S.expanded.delete(id); else S.expanded.add(id);
      renderRoute({ preserveScroll: true });
      return;
    }
    if (e.target.closest('.js-copy-doi')) {
      const doi = e.target.closest('.js-copy-doi').dataset.doi;
      try { await copyText(doi); toast('DOI 已复制'); } catch (err) { toast('复制失败，请手动选择', 'error'); }
      return;
    }
    const dot = e.target.closest('.js-dot');
    if (dot) {
      setScore(dot.dataset.id, dot.dataset.key, Number(dot.dataset.value));
      return;
    }
    if (e.target.closest('.js-note-toggle')) {
      const id = e.target.closest('.js-note-toggle').dataset.id;
      if (S.noteOpen.has(id)) S.noteOpen.delete(id); else S.noteOpen.add(id);
      renderRoute({ preserveScroll: true });
      return;
    }
    const saveBtn = e.target.closest('.js-save-score');
    if (saveBtn) {
      await saveScore(saveBtn.dataset.id, saveBtn);
      return;
    }
    const focusBtn = e.target.closest('.js-focus');
    if (focusBtn) {
      const key = focusBtn.dataset.key;
      if (!S.prefDraft) S.prefDraft = { focus: [], note: '' };
      const idx = S.prefDraft.focus.indexOf(key);
      if (idx >= 0) S.prefDraft.focus.splice(idx, 1);
      else S.prefDraft.focus.push(key);
      focusBtn.classList.toggle('active');
      focusBtn.setAttribute('aria-pressed', focusBtn.classList.contains('active'));
      return;
    }
    const preset = e.target.closest('.js-preset');
    if (preset) {
      const focus = preset.dataset.focus ? preset.dataset.focus.split(',') : [];
      if (!S.prefDraft) S.prefDraft = { focus: [], note: '' };
      S.prefDraft.focus = focus;
      $$('.js-focus').forEach((btn) => {
        const on = focus.includes(btn.dataset.key);
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', String(on));
      });
      return;
    }
    if (e.target.closest('.js-save-pref')) {
      await savePreference(e.target.closest('.js-save-pref'));
      return;
    }
    if (e.target.closest('[data-scroll-tomorrow]')) {
      const panel = $('#tomorrow');
      if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    if (e.target.closest('#confirm-cancel')) { closeConfirm(false); return; }
    if (e.target.closest('#confirm-ok')) { closeConfirm(true); return; }
    if (e.target.id === 'confirm-dialog') { closeConfirm(false); return; }
  });

  document.addEventListener('change', (e) => {
    const read = e.target.closest('[data-read-status]');
    if (read) {
      const pick = findPick(read.dataset.readStatus);
      if (pick) {
        ensureDraft(pick).read_status = read.value;
        updateScorePanel(pick.id);
      }
    }
  });

  document.addEventListener('input', (e) => {
    const note = e.target.closest('[data-note]');
    if (note) {
      const pick = findPick(note.dataset.note);
      if (pick) {
        ensureDraft(pick).note = note.value;
        updateScorePanel(pick.id);
      }
      return;
    }
    if (e.target.id === 'pref-note' && S.prefDraft) {
      S.prefDraft.note = e.target.value;
      return;
    }
    if (e.target.id === 'archive-search') {
      S.archiveFilter = e.target.value.trim().toLowerCase();
      applyArchiveFilter();
      return;
    }
    if (e.target.closest('#settings-drawer')) S.settingsDirty = true;
  });

  window.addEventListener('hashchange', () => {
    if (!S.booted) return;
    const route = (window.location.hash || '#today').replace('#', '');
    S.route = ['today', 'archive', 'insights'].includes(route) ? route : 'today';
    renderRoute({ resetScroll: true });
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('#confirm-dialog').hidden) { closeConfirm(false); return; }
      if (!$('#settings-drawer').hidden) { requestCloseSettings(); return; }
    }
  });

  // ---------------------------------------------------------------------
  // 往期
  // ---------------------------------------------------------------------
  function renderArchive() {
    const history = S.history || S.data.history || [];
    const rows = history.map((row) => renderArchiveEntry(row)).join('');
    $('#view').innerHTML = `
      <div class="section-hero">
        <div>
          <div class="eyebrow">THE ARCHIVE · 阅读档案</div>
          <h1>往期<span class="dot" style="color:var(--accent)">。</span></h1>
          <p class="section-desc">每一次选读、五维评分和留下的札记，都在这里按日归档。点开日报可打印成 PDF。</p>
        </div>
        <div class="section-actions">
          <button class="btn btn-ghost" id="archive-print">${icon('print')} 打印本页</button>
          <a class="btn btn-ghost" href="/api/export.csv" download>${icon('download')} 导出评分 CSV</a>
        </div>
      </div>
      <div class="archive-toolbar">
        <div class="search-box">${icon('search')}<input id="archive-search" type="search" placeholder="搜索标题、期刊或日期…" value="${esc(S.archiveFilter)}"></div>
        <span class="archive-count" id="archive-count">共 ${history.length} 期</span>
      </div>
      <div class="archive-list" id="archive-list">${rows || '<div class="archive-empty">还没有往期记录。先完成今天的第一次选读吧。</div>'}</div>
      <div class="archive-empty" id="archive-no-match" hidden>没有找到匹配的选读。</div>`;
    document.title = '往期 · 知新';
    applyArchiveFilter();
    if (!S.archiveLoaded) loadArchiveHistory();
  }

  function renderArchiveEntry(row) {
    const d = parseLocalDate(row.date);
    const pref = row.preference || {};
    const prefLabels = focusLabels(pref.focus).join('、') || '均衡探索';
    const searchText = [
      row.date,
      ...(row.picks || []).flatMap((p) => [p.title, p.journal, p.doi, ...(p.fields || [])]),
    ].join(' ').toLowerCase();
    const picks = (row.picks || []).map((p, idx) => {
      const url = safeUrl(p.doi ? `https://doi.org/${p.doi}` : '');
      return `
        <div class="archive-pick">
          <span class="ap-no">${String(idx + 1).padStart(2, '0')}</span>
          <div>
            <h3><a href="${url || '#'}" target="_blank" rel="noopener">${esc(p.title)}</a></h3>
            <div class="ap-meta">${esc(p.journal || '未标注期刊')}${p.published ? ' · ' + esc(p.published) : ''}${(p.fields || []).length ? ' · ' + esc(p.fields.join(' / ')) : ''}</div>
          </div>
          ${p.total != null ? `<div class="ap-score">${p.total.toFixed(1)}<small>/5</small></div>` : '<div class="ap-score pending">待评分</div>'}
        </div>`;
    }).join('');
    return `
      <article class="archive-entry" data-search="${esc(searchText)}">
        <div class="archive-date">
          <b>${d ? `${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}` : esc(row.date)}</b>
          <span>${d ? d.getFullYear() : ''} · 第 ${issueNumber(row.issue)} 期</span>
        </div>
        <div class="archive-picks">${picks || '<span class="muted">本期没有记录</span>'}</div>
        <div class="archive-side">
          <div><b>${row.avg != null ? row.avg.toFixed(1) + ' / 5' : '尚未评分'}</b><span>${row.scored || 0} / ${row.count || 0} 篇已评</span></div>
          <div style="margin-top:7px"><b>${esc(prefLabels)}</b><span>明日倾向</span></div>
          <button type="button" class="archive-report" data-report-date="${esc(row.date)}">打开日报 ${icon('arrow')}</button>
        </div>
      </article>`;
  }

  function applyArchiveFilter() {
    const entries = $$('#archive-list .archive-entry');
    if (!entries.length) return;
    let visible = 0;
    const q = (S.archiveFilter || '').trim().toLowerCase();
    entries.forEach((entry) => {
      const show = !q || (entry.dataset.search || '').includes(q);
      entry.hidden = !show;
      if (show) visible += 1;
    });
    const count = $('#archive-count');
    if (count) count.textContent = q ? `匹配 ${visible} 期` : `共 ${entries.length} 期`;
    const none = $('#archive-no-match');
    if (none) none.hidden = visible !== 0;
  }

  async function loadArchiveHistory() {
    if (S.archiveLoading) return;
    S.archiveLoading = true;
    try {
      const res = await api('/api/history?limit=200');
      S.history = res.history || [];
      S.data.history = S.history;
      if (res.stats) S.data.stats = res.stats;
      S.archiveLoaded = true;
      if (S.route === 'archive') renderArchive();
    } catch (err) {
      toast('往期加载失败：' + formatError(err), 'error');
    } finally {
      S.archiveLoading = false;
    }
  }

  // ---------------------------------------------------------------------
  // 洞察
  // ---------------------------------------------------------------------
  function renderInsights() {
    const stats = S.data.stats || {};
    const dims = S.data.dimensions || [];
    const hasScores = (stats.scored_count || 0) > 0;
    const statCells = [
      { value: stats.total_recommended || 0, label: '累计推荐篇数', suffix: '篇' },
      { value: stats.scored_count || 0, label: '已评篇数', suffix: '篇' },
      { value: stats.avg_total != null ? stats.avg_total.toFixed(1) : '—', label: '平均综合分', suffix: '/5' },
      { value: stats.streak || 0, label: '连续阅读天数', suffix: '天' },
    ].map((s) => `<div class="stat-cell"><b>${esc(s.value)}${s.value !== '—' ? `<small>${esc(s.suffix)}</small>` : ''}</b><span>${esc(s.label)}</span></div>`).join('');

    const dimBars = dims.map((dim) => {
      const value = stats.dimension_avg && stats.dimension_avg[dim.key] != null ? stats.dimension_avg[dim.key] : 0;
      return `<div class="dim-bar-row">
        <span class="db-label">${esc(dim.label)}</span>
        <div class="db-track"><div class="db-fill" style="width:${value ? value / 5 * 100 : 0}%;--c:${safeColor(dim.color)}"></div></div>
        <span class="db-val">${value ? value.toFixed(1) : '—'}<small> /5</small></span>
      </div>`;
    }).join('');

    const activity = (stats.last14 || []).map((d, i, arr) => {
      const height = d.avg ? Math.max(12, d.avg / 5 * 88) : (d.count ? 8 : 0);
      const isToday = i === arr.length - 1;
      return `<div class="activity-day ${isToday ? 'today' : ''}" title="${esc(d.date)} · 推荐 ${d.count} 篇 · ${d.avg ? '均分 ' + d.avg : '未评分'}">
        <span class="ad-val">${d.avg ? d.avg.toFixed(1) : (d.count ? '·' : '')}</span>
        <div class="activity-bar-track"><span class="activity-bar ${d.scored ? '' : 'unscored'}" style="height:${height}%"></span></div>
        <span class="ad-label">${esc(d.label)}</span>
      </div>`;
    }).join('');

    const fieldDists = distributionRows(stats.fields || [], '还没有领域数据');
    const journalDists = distributionRows(stats.journals || [], '还没有期刊数据');
    const emptyNote = hasScores ? '' : `<div class="insight-empty">完成第一篇评分后，这里会长出你的阅读偏好画像。</div>`;

    $('#view').innerHTML = `
      <div class="section-hero">
        <div>
          <div class="eyebrow">READING INSIGHTS · 阅读洞察</div>
          <h1>你的阅读<span class="dot" style="color:var(--accent)">。</span></h1>
          <p class="section-desc">评分会诚实地留下你的取向：偏爱什么、在哪里苛刻、最近又被什么吸引。这里不做评判，只做记录。</p>
        </div>
        <div class="section-actions">
          <button class="btn btn-ghost" id="insights-print">${icon('print')} 打印本页</button>
          <a class="btn btn-ghost" href="/api/export.csv" download>${icon('download')} 导出数据</a>
        </div>
      </div>
      <div class="stat-strip">${statCells}</div>
      ${emptyNote}
      <div class="insight-grid">
        <div class="insight-panel">
          <div class="panel-head"><h2>评分画像</h2><span>Five dimensions</span></div>
          <div class="radar-wrap">${radarSvg(stats)}</div>
        </div>
        <div class="insight-panel">
          <div class="panel-head"><h2>你的尺子</h2><span>Average score</span></div>
          <div class="dim-bars">${dimBars}</div>
          <p style="font-size:8.5px;color:var(--muted-2);line-height:1.8;margin:22px 0 0;border-top:1px dotted var(--line);padding-top:10px">评分没有标准答案。长期看，它更像你个人的“什么问题值得读”的刻度。</p>
        </div>
        <div class="insight-panel wide">
          <div class="panel-head"><h2>最近十四天</h2><span>Reading rhythm</span></div>
          <div class="activity-chart">${activity || '<div class="insight-empty">还没有阅读记录</div>'}</div>
        </div>
        <div class="insight-panel">
          <div class="panel-head"><h2>领域分布</h2><span>Research fields</span></div>
          <div class="dist-list">${fieldDists}</div>
        </div>
        <div class="insight-panel">
          <div class="panel-head"><h2>常读期刊</h2><span>Journals</span></div>
          <div class="dist-list">${journalDists}</div>
        </div>
        <div class="insight-note">推荐排序由关键词命中、发表时间、被引、发表渠道、跨领域程度与你的次日偏好共同构成，属于可解释的启发式排序，不代表对论文质量的最终判断。真正的判断，永远在阅读之后。</div>
      </div>`;
    document.title = '阅读洞察 · 知新';
  }

  function distributionRows(items, emptyText) {
    if (!items.length) return `<div class="insight-empty" style="padding:8px 0">${esc(emptyText)}</div>`;
    const max = Math.max(...items.map((x) => x.count || 0), 1);
    return items.slice(0, 6).map((item) => `
      <div class="dist-row">
        <span class="dist-name" title="${esc(item.name)}">${esc(item.name)}</span>
        <div class="dist-track"><div class="dist-fill" style="width:${(item.count || 0) / max * 100}%"></div></div>
        <span class="dist-count">${item.count}</span>
      </div>`).join('');
  }

  function radarSvg(stats) {
    const dims = S.data.dimensions || [];
    const cx = 180, cy = 166, radius = 104;
    const angles = dims.map((_, i) => -Math.PI / 2 + i * 2 * Math.PI / dims.length);
    const point = (angle, r) => [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r];
    const grid = [1, 2, 3, 4, 5].map((level) => {
      const pts = angles.map((angle) => point(angle, radius * level / 5).map((n) => n.toFixed(1)).join(',')).join(' ');
      return `<polygon class="radar-grid" points="${pts}"></polygon>`;
    }).join('');
    const axes = angles.map((angle) => {
      const [x, y] = point(angle, radius);
      return `<line class="radar-axis" x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"></line>`;
    }).join('');
    const values = dims.map((dim) => (stats.dimension_avg && stats.dimension_avg[dim.key]) || 0);
    const poly = dims.map((dim, i) => {
      const value = values[i] || 0;
      const [x, y] = point(angles[i], radius * Math.max(value, 0.04) / 5);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const labels = dims.map((dim, i) => {
      const angle = angles[i];
      const [x, y] = point(angle, radius + 26);
      const anchor = Math.cos(angle) > 0.25 ? 'start' : Math.cos(angle) < -0.25 ? 'end' : 'middle';
      return `<text class="radar-label" x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${anchor}" dominant-baseline="middle">${esc(dim.label)}</text>`;
    }).join('');
    const dots = dims.map((dim, i) => {
      const value = values[i] || 0;
      if (!value) return '';
      const [x, y] = point(angles[i], radius * value / 5);
      return `<circle class="radar-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5"></circle>`;
    }).join('');
    const empty = values.every((v) => !v)
      ? `<text x="${cx}" y="${cy + 4}" text-anchor="middle" style="font-family:var(--sans);font-size:9px;fill:var(--muted-2)">暂无评分</text>` : '';
    return `<svg class="radar-svg" viewBox="0 0 360 330" role="img" aria-label="五维评分雷达图">
      ${grid}${axes}
      ${values.some((v) => v) ? `<polygon class="radar-shape" points="${poly}"></polygon>` : ''}
      ${dots}${labels}${empty}
    </svg>`;
  }

  // ---------------------------------------------------------------------
  // 表单字段编辑器
  // ---------------------------------------------------------------------
  function fieldEditorHTML(index, field = {}, scope = '') {
    const keywords = Array.isArray(field.keywords) ? field.keywords.join(', ') : (field.keywords || '');
    return `
      <div class="field-editor" data-field-id="${esc(field.id || '')}" data-field-color="${esc(field.color || '')}">
        <div class="field-editor-head">
          <span class="fe-no">${String(index + 1).padStart(2, '0')}</span>
          <input class="text-input" data-field-name maxlength="60" autocomplete="off" placeholder="研究方向，如：人工智能教育" value="${esc(field.name || '')}" aria-label="研究方向名称">
          <button type="button" class="remove-field" title="删除这个领域" aria-label="删除这个领域">${icon('trash')}</button>
        </div>
        <div class="field-keywords">
          <label>英文检索词 · 用逗号分隔</label>
          <textarea class="text-input field-keywords-input" data-field-keywords maxlength="400" rows="2" autocomplete="off" placeholder="large language model in education, AI literacy, learning analytics" aria-label="英文检索词">${esc(keywords)}</textarea>
        </div>
        <div class="field-tip">名称可用中文展示；检索词建议英文，2–6 个具体词效果更好。</div>
      </div>`;
  }

  function updateFieldNumbers(container) {
    $$('.field-editor', container).forEach((editor, i) => {
      const no = $('.fe-no', editor);
      if (no) no.textContent = String(i + 1).padStart(2, '0');
    });
  }

  function addFieldEditor(containerSelector, scope) {
    const container = $(containerSelector);
    if (!container) return;
    if ($$(':scope > .field-editor', container).length >= 6) {
      toast('最多同时跟踪 6 个研究领域', 'error');
      return;
    }
    container.insertAdjacentHTML('beforeend', fieldEditorHTML($$(':scope > .field-editor', container).length, {}, scope));
    updateFieldNumbers(container);
    const inputs = $$('[data-field-name]', container);
    if (inputs.length) inputs[inputs.length - 1].focus();
    if (containerSelector === '#settings-fields') S.settingsDirty = true;
  }

  function removeFieldEditor(button, scope) {
    const editor = button.closest('.field-editor');
    const container = editor ? editor.parentElement : null;
    if (!editor || !container) return;
    if ($$(':scope > .field-editor', container).length <= 1) {
      toast('至少保留一个研究领域', 'error');
      return;
    }
    editor.remove();
    updateFieldNumbers(container);
    if (scope === 'settings') S.settingsDirty = true;
  }

  function readFields(container) {
    const fields = [];
    $$(':scope > .field-editor', container).forEach((editor, index) => {
      const name = ($('[data-field-name]', editor) || {}).value;
      const keywords = ($('[data-field-keywords]', editor) || {}).value;
      const cleanName = (name || '').trim();
      const cleanKeywords = (keywords || '').trim();
      if (!cleanName && !cleanKeywords) return;
      if (!cleanName) throw new Error(`第 ${index + 1} 个领域还缺名称`);
      if (!cleanKeywords) throw new Error(`「${cleanName}」还缺英文检索词`);
      const field = { name: cleanName, keywords: cleanKeywords };
      if (editor.dataset.fieldId) field.id = editor.dataset.fieldId;
      if (editor.dataset.fieldColor) field.color = editor.dataset.fieldColor;
      fields.push(field);
    });
    if (!fields.length) throw new Error('请至少填写一个研究领域和它的英文检索词');
    return fields.slice(0, 6);
  }

  function selectedSources(form) {
    return $$('.source-choice.active', form).map((el) => el.dataset.source);
  }

  function selectedDailyCount(form) {
    const active = $('.choice-row .choice-card.active', form);
    return active ? Number(active.dataset.count) : 2;
  }

  function showFormError(node, message) {
    if (!node) return;
    node.textContent = message || '';
    node.hidden = !message;
    if (message) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---------------------------------------------------------------------
  // 首次引导
  // ---------------------------------------------------------------------
  function renderOnboarding() {
    $('#boot-screen').hidden = true;
    $('#app-shell').hidden = true;
    $('#onboarding').hidden = false;
    document.body.classList.remove('modal-open');
    document.title = '开始使用 · 知新';
    const sourceChoices = (S.data.sources || []).map((s) => `
      <button type="button" class="source-choice ${s.default ? 'active' : ''}" data-source="${esc(s.key)}">
        <span class="sc-box">${icon('check')}</span>
        <span><b>${esc(s.name)}</b><small>${esc(s.scope)} · ${esc(s.note)}</small></span>
      </button>`).join('');
    $('#onboarding').innerHTML = `
      <div class="onboard-grid">
        <aside class="onboard-left">
          <div class="brand-lockup"><div class="brand-seal">知</div><div class="brand-name">知新<span>ZHIXIN</span></div></div>
          <div class="onboard-copy">
            <div class="eyebrow">A DAILY READING RITUAL</div>
            <h1>每天几篇，<br><em>读有所得。</em></h1>
            <p class="lead">不用再在数据库里翻找。告诉我你在研究什么，剩下的交给一份安静的日报。</p>
            <div class="onboard-features">
              <div class="feature-row"><span class="feature-no">01</span><div><b>少而准</b><span>每天只留 1–5 篇，按你的节奏来</span></div></div>
              <div class="feature-row"><span class="feature-no">02</span><div><b>五维评分</b><span>相关性 / 专业性 / 可靠性 / 创新性 / 启发性</span></div></div>
              <div class="feature-row"><span class="feature-no">03</span><div><b>越读越懂你</b><span>用你的评分和明日偏好，调整下一次排序</span></div></div>
            </div>
          </div>
          <div class="onboard-quote">「苟日新，日日新，又日新。」<br><b>——《礼记 · 大学》</b></div>
        </aside>
        <section class="onboard-right">
          <form class="onboard-form" id="onboard-form" novalidate autocomplete="off">
            <div class="onboard-mobile-brand">
              <div class="brand-lockup"><div class="brand-seal">知</div><div class="brand-name">知新<span>ZHIXIN</span></div></div>
              <span>每天几篇，读有所得。</span>
            </div>
            <div class="form-kicker">SET UP YOUR DAILY · 约 1 分钟</div>
            <h2>先告诉我，你在研究什么。</h2>
            <p class="form-intro">研究领域可以随时增减。英文检索词用来查文献，中文名称只用于日报展示；越具体，推荐越贴近你。</p>
            <div class="form-error" id="onboard-error" hidden></div>

            <div class="form-section">
              <label class="form-label" for="onboard-name">怎么称呼你 <small>可选，会出现在日报上</small></label>
              <input class="text-input" id="onboard-name" maxlength="40" placeholder="例如：林老师 / Lin / 一个正在写论文的人" autocomplete="nickname">
            </div>

            <div class="form-section">
              <div class="form-label"><span>研究领域</span><button type="button" class="example-fill" id="fill-example">填入一个示例</button></div>
              <div class="field-list" id="onboard-fields">${fieldEditorHTML(0)}</div>
              <button type="button" class="add-field-btn" id="onboard-add-field">${icon('plus')} 再加一个研究领域</button>
            </div>

            <div class="form-section">
              <div class="form-label"><span>每天读几篇</span><small>建议从 2 篇开始</small></div>
              <div class="choice-row counts">
                ${DAILY_CHOICES.map((c) => `<button type="button" class="choice-card ${c.count === 2 ? 'active' : ''}" data-count="${c.count}"><span class="cc-radio"></span><span><b>${c.title}</b><span>${c.desc}</span></span></button>`).join('')}
              </div>
            </div>

            <div class="form-section">
              <div class="form-label"><span>文献数据源</span><small>可随时调整</small></div>
              <div class="source-grid">${sourceChoices}</div>
              <p class="inline-note" style="margin-top:8px">已有 Zotero？保存后可在设置里连接本机 Zotero，自动避开已读文献。</p>
            </div>

            <details class="advanced-block">
              <summary>高级设置 · 邮箱、API Keys、机构访问入口</summary>
              <div class="advanced-body">
                <div class="form-section">
                  <label class="form-label" for="onboard-email">联系邮箱 <small>可选，用于学术 API 礼貌池</small></label>
                  <input class="text-input" id="onboard-email" type="email" placeholder="you@university.edu" autocomplete="email">
                  <p class="advanced-note">仅随请求发送给 Crossref / OpenAlex，帮助获得更稳定的公开接口额度。</p>
                </div>
                <div class="form-section">
                  <label class="form-label" for="onboard-key">OpenAlex API Key <small>可选，免费</small></label>
                  <input class="text-input" id="onboard-key" type="password" placeholder="无 Key 也可使用，额度共享" autocomplete="off">
                  <p class="advanced-note">在 <a href="https://openalex.org/settings/api-key" target="_blank" rel="noopener" style="color:var(--accent)">openalex.org</a> 免费申请。无 Key 时 OpenAlex 的公开额度较低，其他源不受影响。</p>
                </div>
                <div class="form-section">
                  <label class="form-label" for="onboard-institution">机构访问入口模板 <small>可选</small></label>
                  <input class="text-input" id="onboard-institution" placeholder="https://login.ezproxy.your.edu/login?url={url}">
                  <p class="advanced-note">填写学校图书馆的 EZproxy / 代理入口。链接里的 {url} 会替换为 DOI 地址，由学校页面完成登录授权；本应用不保存账号密码。</p>
                </div>
              </div>
            </details>

            <div class="form-cta">
              <button type="submit" class="btn btn-ink btn-large" id="onboard-submit">开始今日选读 ${icon('arrow')}</button>
              <span class="cta-note">接下来会检索公开学术源，首次约需 10–20 秒。</span>
            </div>
            <div class="privacy-line">${icon('lock')} 数据只保存在本机 data 文件夹，不会上传到任何第三方服务器。</div>
          </form>
        </section>
      </div>`;
    setTimeout(() => $('#onboard-name')?.focus(), 80);
  }

  function fillOnboardingExample() {
    const container = $('#onboard-fields');
    if (!container) return;
    container.innerHTML = fieldEditorHTML(0, {
      name: '人工智能教育',
      keywords: ['artificial intelligence in education', 'AI literacy', 'learning analytics'],
      color: '#2F5D50',
    }) + fieldEditorHTML(1, {
      name: '科学学与科研评价',
      keywords: ['research evaluation', 'open science', 'citation analysis'],
      color: '#B24E36',
    });
    updateFieldNumbers(container);
  }

  async function submitOnboarding(form) {
    const errorNode = $('#onboard-error');
    showFormError(errorNode, '');
    let payload;
    try {
      const fields = readFields($('#onboard-fields'));
      const sources = selectedSources(form);
      if (!sources.length) throw new Error('请至少启用一个文献数据源');
      if (!fields.some((f) => /[a-z]/i.test(f.keywords))) {
        throw new Error('检索词建议至少包含一个英文词；学术数据库对中文短语的覆盖有限');
      }
      payload = {
        display_name: $('#onboard-name').value.trim(),
        fields,
        daily_count: selectedDailyCount(form),
        sources,
        contact_email: $('#onboard-email').value.trim(),
        openalex_api_key: $('#onboard-key').value.trim(),
        institution_template: $('#onboard-institution').value.trim(),
        lookback_years: 3,
      };
    } catch (err) {
      showFormError(errorNode, err.message);
      return;
    }
    const button = $('#onboard-submit');
    setBusy(button, true, '正在建立日报');
    try {
      const res = await api('/api/profile', { method: 'POST', body: JSON.stringify(payload) });
      S.data = res;
      S.history = res.history || [];
      S.booted = true;
      S.drafts = {};
      S.prefDay = null;
      S.archiveLoaded = false;
      $('#onboarding').hidden = true;
      showShell();
      S.route = 'today';
      window.location.hash = 'today';
      renderRoute({ resetScroll: true });
      toast('欢迎，日报准备好了', 'success');
      generateToday(false);
    } catch (err) {
      showFormError(errorNode, formatError(err));
      toast(formatError(err), 'error', 5000);
      setBusy(button, false);
    }
  }

  // ---------------------------------------------------------------------
  // 设置
  // ---------------------------------------------------------------------
  function openSettings() {
    if (!S.data) return;
    S.settingsDirty = false;
    S.clearKey = false;
    $('#settings-drawer').hidden = false;
    renderSettings();
    loadBackups();
    syncModalLock();
    setTimeout(() => $('#settings-name')?.focus(), 80);
  }

  function renderSettings() {
    const profile = S.data.profile || {};
    const fields = profile.fields || [];
    const fieldHTML = (fields.length ? fields : [{}]).map((f, i) => fieldEditorHTML(i, f, 'settings')).join('');
    const daily = Math.max(1, Math.min(5, Number(profile.daily_count) || 2));
    const sourceChoices = (S.data.sources || []).map((s) => `
      <button type="button" class="source-choice ${(profile.sources || []).includes(s.key) ? 'active' : ''}" data-source="${esc(s.key)}">
        <span class="sc-box">${icon('check')}</span>
        <span><b>${esc(s.name)}</b><small>${esc(s.scope)} · ${esc(s.note)}</small></span>
      </button>`).join('');
    const hasKey = !!profile.has_openalex_key;
    const keyStatus = S.clearKey ? '<span class="key-status" style="color:var(--accent)">保存后清除</span>' : hasKey ? '<span class="key-status">已保存</span>' : '';
    const obsidianStatus = S.data.obsidian_status || {};
    const obsidianEnabled = !!profile.obsidian_enabled;
    const obsidianStatusText = obsidianStatusLine(obsidianStatus, obsidianEnabled);
    const zoteroStatus = S.data.zotero_status || {};
    const zoteroEnabled = !!profile.zotero_enabled;
    const zoteroStatusText = zoteroStatusLine(zoteroStatus, zoteroEnabled);
    const day = S.data.day;
    const scored = day ? (day.picks || []).some((p) => p.scores) : false;
    const regen = day && !scored
      ? `<button type="button" class="btn btn-ghost btn-small" id="settings-regenerate">${icon('refresh')} 按当前设置重选今天</button>`
      : day ? '<span class="inline-note">今天已有评分，选读保持不变。</span>' : '';
    $('#settings-drawer').innerHTML = `
      <div class="drawer-backdrop" id="settings-backdrop"></div>
      <div class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <div class="drawer-head">
          <div><div class="eyebrow">PREFERENCES</div><h2 id="settings-title">设置</h2></div>
          <button type="button" class="icon-btn" id="close-settings" aria-label="关闭设置">${icon('x')}</button>
        </div>
        <form id="settings-form" novalidate autocomplete="off">
          <div class="drawer-body">
            <div class="form-error" id="settings-error" hidden></div>
            <div class="settings-section-title">Researcher · 关于你</div>
            <div class="form-section">
              <label class="form-label" for="settings-name">署名 <small>出现在日报上</small></label>
              <input class="text-input" id="settings-name" maxlength="40" value="${esc(profile.display_name || '')}" placeholder="你的名字或昵称">
            </div>

            <div class="settings-section-title">Fields · 研究领域</div>
            <div class="field-list" id="settings-fields">${fieldHTML}</div>
            <button type="button" class="add-field-btn" id="settings-add-field">${icon('plus')} 新增研究领域</button>
            <p class="inline-note">领域可随时增减关键词；修改后下一次生成生效，今天已生成的选读保持不变。未评分的今天可在下方按新设置重选。</p>

            <div class="settings-section-title">Daily · 每日节奏</div>
            <div class="choice-row counts">
              ${DAILY_CHOICES.map((c) => `<button type="button" class="choice-card ${daily === c.count ? 'active' : ''}" data-count="${c.count}"><span class="cc-radio"></span><span><b>${c.title}</b><span>${c.desc}</span></span></button>`).join('')}
            </div>

            <div class="settings-section-title">Filter · 期刊门槛</div>
            <div class="form-section">
              <label class="form-label" for="settings-min-citedness">最低期刊引用指标 <small>0 = 不启用</small></label>
              <input class="text-input" id="settings-min-citedness" type="number" min="0" max="50" step="0.1" value="${esc(profile.min_venue_citedness || 0)}" style="max-width:180px">
              <p class="inline-note">使用 OpenAlex 的期刊 <b>2 年平均被引</b> 作为影响因子的开放代理（JCR 官方 IF 有版权，无法免费批量获取）。低于此值的候选不会进入推荐。示例：填 2 表示期刊 2 年均被引需 ≥ 2.0。</p>
              <label class="form-label" for="settings-venue-missing" style="margin-top:12px">指标缺失时 <small>Crossref 等源有时不给期刊指标</small></label>
              <select class="read-select" id="settings-venue-missing" style="max-width:260px">
                <option value="exclude" ${profile.venue_missing_policy !== 'include' ? 'selected' : ''}>排除（严格）</option>
                <option value="include" ${profile.venue_missing_policy === 'include' ? 'selected' : ''}>保留（宽松）</option>
              </select>
            </div>

            <div class="settings-section-title">Sources · 数据源</div>
            <div class="source-grid">${sourceChoices}</div>
            <p class="inline-note">OpenAlex / Crossref 覆盖全学科（包括 Elsevier 期刊的 DOI 元数据）；arXiv 偏数理与计算机；Europe PMC 覆盖生命医学。Google Scholar 没有官方 API，不做抓取，每篇提供 Scholar 复核链接。</p>

            <div class="settings-section-title">Access · 授权与额度</div>
            <div class="form-section">
              <label class="form-label" for="settings-lookback">检索时间范围</label>
              <select class="read-select" id="settings-lookback" style="max-width:220px">
                ${[1, 2, 3, 4, 5].map((y) => `<option value="${y}" ${Number(profile.lookback_years || 3) === y ? 'selected' : ''}>近 ${y} 年</option>`).join('')}
              </select>
              <p class="inline-note">默认近 3 年。范围越短越前沿，越长越容易遇到综述和经典工作。</p>
            </div>
            <div class="form-section">
              <label class="form-label" for="settings-email">联系邮箱 <small>可选</small></label>
              <input class="text-input" id="settings-email" type="email" value="${esc(profile.contact_email || '')}" placeholder="you@university.edu">
              <p class="inline-note">只用于 Crossref / OpenAlex 的礼貌池，不用于登录。</p>
            </div>
            <div class="form-section">
              <label class="form-label" for="settings-key">OpenAlex API Key ${keyStatus}</label>
              <input class="text-input" id="settings-key" type="password" placeholder="${hasKey ? '已保存，留空则不修改' : '可选，免费申请'}" autocomplete="off">
              <p class="inline-note">免费 Key 能显著提高每日额度。<a href="https://openalex.org/settings/api-key" target="_blank" rel="noopener" style="color:var(--accent)">去申请</a>${hasKey ? '<button type="button" class="key-clear" id="clear-key">清除已保存的 Key</button>' : ''}</p>
            </div>
            <div class="form-section">
              <label class="form-label" for="settings-institution">机构访问入口模板</label>
              <input class="text-input" id="settings-institution" value="${esc(profile.institution_template || '')}" placeholder="https://login.ezproxy.your.edu/login?url={url}">
              <p class="inline-note">用于跳转学校已订阅的全文。推荐格式：<span class="mono" style="font-size:8px">https://login.ezproxy.学校域名/login?url={url}</span>。{url} 会被替换成 DOI 地址并编码；{doi} 可替换原始 DOI。登录在学校页面完成，本应用不保存账号密码。</p>
            </div>

            <div class="settings-section-title">Zotero · 已读库</div>
            <label class="settings-check" for="settings-zotero-enabled">
              <input type="checkbox" id="settings-zotero-enabled" ${zoteroEnabled ? 'checked' : ''}>
              <span><b>联动 Zotero 阅读状态</b><small>读取本机 Zotero 的已读/在读/未读标签，避免重复推荐，并用已读文献的主题和期刊改进排序。</small></span>
            </label>
            <div class="form-section" style="margin-top:13px">
              <label class="form-label" for="settings-zotero-api">Zotero 本地 API <small>默认即可</small></label>
              <input class="text-input" id="settings-zotero-api" value="${esc(profile.zotero_api || 'http://127.0.0.1:23119/api/users/0')}" placeholder="http://127.0.0.1:23119/api/users/0">
              <p class="inline-note">需要 Zotero 7 正在运行，并在 Zotero 设置 → 高级中允许本机其他应用访问。只读，不需要账号密码。</p>
            </div>
            <div class="obsidian-grid">
              <div class="form-section">
                <label class="form-label" for="settings-zotero-exclude">排除范围</label>
                <select class="read-select" id="settings-zotero-exclude">
                  <option value="read" ${profile.zotero_exclude_mode !== 'all' && profile.zotero_exclude_mode !== 'off' ? 'selected' : ''}>已读 + 在读（推荐）</option>
                  <option value="all" ${profile.zotero_exclude_mode === 'all' ? 'selected' : ''}>Zotero 全部条目</option>
                  <option value="off" ${profile.zotero_exclude_mode === 'off' ? 'selected' : ''}>不排除，只做兴趣画像</option>
                </select>
              </div>
              <label class="settings-check compact" for="settings-zotero-prefs">
                <input type="checkbox" id="settings-zotero-prefs" ${profile.zotero_use_preferences !== false ? 'checked' : ''}>
                <span><b>用已读期刊 / 标签改进推荐</b><small>轻微加权，不刷屏</small></span>
              </label>
            </div>
            <details class="advanced-block" style="margin-top:4px">
              <summary>阅读状态标签 · 已识别 /done、/reading、/unread</summary>
              <div class="advanced-body">
                <div class="form-section">
                  <label class="form-label" for="settings-zotero-read-tags">已读标签</label>
                  <input class="text-input" id="settings-zotero-read-tags" value="${esc(profile.zotero_read_tags || '/done, done, read, 已读')}" placeholder="/done, done, read, 已读">
                </div>
                <div class="form-section">
                  <label class="form-label" for="settings-zotero-reading-tags">在读标签</label>
                  <input class="text-input" id="settings-zotero-reading-tags" value="${esc(profile.zotero_reading_tags || '/reading, reading, 在读')}" placeholder="/reading, reading, 在读">
                </div>
                <div class="form-section">
                  <label class="form-label" for="settings-zotero-unread-tags">未读标签</label>
                  <input class="text-input" id="settings-zotero-unread-tags" value="${esc(profile.zotero_unread_tags || '/unread, unread, 未读')}" placeholder="/unread, unread, 未读">
                </div>
              </div>
            </details>
            <div class="obsidian-status" id="zotero-status">${esc(zoteroStatusText)}</div>
            <div class="obsidian-actions">
              <button type="button" class="btn btn-ghost btn-small" id="settings-zotero-sync" ${zoteroEnabled ? '' : 'disabled'}>${icon('refresh')} 立即读取 Zotero</button>
              <span class="inline-note">首次使用建议先保存设置，再点读取。</span>
            </div>
            <p class="inline-note">默认只排除已读/在读，未读条目仍可能进入候选；如果你希望完全不碰 Zotero 里已有的文献，选“全部条目”。</p>

            <div class="settings-section-title">Obsidian · 回顾</div>
            <label class="settings-check" for="settings-obsidian-enabled">
              <input type="checkbox" id="settings-obsidian-enabled" ${obsidianEnabled ? 'checked' : ''}>
              <span><b>把阅读记录同步到 Obsidian</b><small>每篇已评文献一篇笔记，每天一篇日报，外加一个总索引。只写入你指定的子文件夹。</small></span>
            </label>
            <div class="form-section" style="margin-top:13px">
              <label class="form-label" for="settings-obsidian-vault">Obsidian vault 文件夹 <small>绝对路径</small></label>
              <input class="text-input" id="settings-obsidian-vault" value="${esc(profile.obsidian_vault || '')}" placeholder="D:\Documents\MyVault">
              <p class="inline-note">填 Obsidian vault 的根目录（含 .obsidian 的那一层），不是某个笔记文件。本应用不会碰这个目录之外的文件。</p>
            </div>
            <div class="obsidian-grid">
              <div class="form-section">
                <label class="form-label" for="settings-obsidian-folder">子文件夹 <small>默认 知新</small></label>
                <input class="text-input" id="settings-obsidian-folder" value="${esc(profile.obsidian_folder || '知新')}" placeholder="知新">
              </div>
              <label class="settings-check compact" for="settings-obsidian-auto">
                <input type="checkbox" id="settings-obsidian-auto" ${profile.obsidian_auto_sync !== false ? 'checked' : ''}>
                <span><b>评分后自动同步</b><small>也可以只手动同步</small></span>
              </label>
            </div>
            <div class="obsidian-status" id="obsidian-status">${esc(obsidianStatusText)}</div>
            <div class="obsidian-actions">
              <button type="button" class="btn btn-ghost btn-small" id="settings-obsidian-sync" ${obsidianEnabled ? '' : 'disabled'}>${icon('refresh')} 立即同步全部记录</button>
              <span class="inline-note">首次使用建议先保存设置，再点同步。</span>
            </div>
            <p class="inline-note">自动区会被后续同步更新；每篇文献笔记末尾有「我的补充」区块，重新同步时会保留。不要手动修改自动生成的 YAML 属性。</p>

            <div class="settings-section-title">Data · 数据与分享</div>
            <div class="data-actions">
              <a class="btn btn-ghost btn-small" href="/api/export.csv" download>${icon('download')} 导出评分 CSV</a>
              <a class="btn btn-ghost btn-small" href="/api/export.json" download>${icon('download')} 备份全部数据</a>
              <button type="button" class="btn btn-ghost btn-small" id="settings-backup-now">${icon('refresh')} 立即快照</button>
            </div>
            <p class="inline-note">下载的 JSON 不含 OpenAlex Key。分享给他人时，复制整个文件夹并删除 data/state.json 即可得到一个干净的新实例。</p>
            <div class="backup-box">
              <div class="backup-title"><b>自动版本备份</b><span>每次保存前保留上一版，最多 100 份，位于 data/backups/。</span></div>
              <div class="backup-actions">
                <select id="backup-select" class="read-select"><option value="">加载备份…</option></select>
                <button type="button" class="btn btn-ghost btn-small" id="settings-backup-restore">${icon('refresh')} 恢复所选</button>
              </div>
              <p class="inline-note" id="backup-status">加载中…</p>
            </div>
            ${regen ? `<div style="margin-top:12px">${regen}</div>` : ''}

            <div class="settings-section-title">About · 关于</div>
            <div class="about-box">
              <b>知新 ${esc(S.data.app.version)}</b> · 本地优先的每日文献日报。<br>
              推荐基于公开学术数据与可解释的启发式排序，不替代你的专业判断。评分、札记与偏好只保存在 <span class="mono">data/state.json</span>。
            </div>
            <div class="danger-zone"><span class="inline-note">分享给他人：复制整个程序文件夹，删除 data/state.json 即可；无需安装数据库或额外依赖。</span></div>
          </div>
          <div class="drawer-footer">
            <span class="footer-hint">修改领域或数据源后，明日推荐生效；今日已生成的选读保持不变。</span>
            <button type="button" class="btn btn-ghost" id="cancel-settings">取消</button>
            <button type="submit" class="btn btn-ink" id="settings-save">保存设置</button>
          </div>
        </form>
      </div>`;
  }

  function closeSettings() {
    $('#settings-drawer').hidden = true;
    S.settingsDirty = false;
    syncModalLock();
  }

  async function requestCloseSettings() {
    if ($('#settings-drawer').hidden) return;
    if (S.settingsDirty) {
      const ok = await openConfirm('放弃未保存的修改？', '研究领域、数据源或授权设置的改动还没有保存。', '放弃修改');
      if (!ok) return;
    }
    closeSettings();
  }

  async function saveSettings(form) {
    showFormError($('#settings-error'), '');
    let payload;
    try {
      const fields = readFields($('#settings-fields'));
      const sources = selectedSources(form);
      if (!sources.length) throw new Error('请至少启用一个文献数据源');
      if (!fields.some((f) => /[a-z]/i.test(f.keywords))) {
        throw new Error('检索词建议至少包含一个英文词；学术数据库对中文短语的覆盖有限');
      }
      payload = {
        display_name: $('#settings-name').value.trim(),
        fields,
        daily_count: selectedDailyCount(form),
        sources,
        contact_email: $('#settings-email').value.trim(),
        openalex_api_key: $('#settings-key').value.trim(),
        institution_template: $('#settings-institution').value.trim(),
        lookback_years: Number(($('#settings-lookback') || {}).value || 3),
        min_venue_citedness: Number($('#settings-min-citedness').value || 0),
        venue_missing_policy: $('#settings-venue-missing').value,
        zotero_enabled: $('#settings-zotero-enabled').checked,
        zotero_api: $('#settings-zotero-api').value.trim(),
        zotero_exclude_mode: $('#settings-zotero-exclude').value,
        zotero_use_preferences: $('#settings-zotero-prefs').checked,
        zotero_read_tags: $('#settings-zotero-read-tags').value.trim(),
        zotero_reading_tags: $('#settings-zotero-reading-tags').value.trim(),
        zotero_unread_tags: $('#settings-zotero-unread-tags').value.trim(),
        clear_openalex_key: S.clearKey,
        obsidian_enabled: $('#settings-obsidian-enabled').checked,
        obsidian_vault: $('#settings-obsidian-vault').value.trim(),
        obsidian_folder: $('#settings-obsidian-folder').value.trim(),
        obsidian_auto_sync: $('#settings-obsidian-auto').checked,
      };
    } catch (err) {
      showFormError($('#settings-error'), err.message);
      return;
    }
    const button = $('#settings-save');
    setBusy(button, true, '保存中');
    try {
      const res = await api('/api/profile', { method: 'POST', body: JSON.stringify(payload) });
      S.data = res;
      S.history = res.history || S.history;
      S.archiveLoaded = false;
      S.settingsDirty = false;
      S.drafts = {};
      S.prefDay = null;
      closeSettings();
      renderHeaderDate();
      renderRoute({ preserveScroll: false });
      toast('设置已保存，明日推荐会用上新设置', 'success');
      notifyZotero(res);
      notifyObsidian(res);
      if (!res.day && res.configured) generateToday(false);
    } catch (err) {
      showFormError($('#settings-error'), formatError(err));
      toast(formatError(err), 'error');
      setBusy(button, false);
    }
  }

  function showExitScreen() {
    document.body.innerHTML = `
      <div class="exit-screen">
        <div class="exit-seal">知</div>
        <div class="eyebrow">ZHIXIN · SESSION CLOSED</div>
        <h1>已退出知新</h1>
        <p>数据已经保存，可以关闭这个页面。<br>下次双击 <b>start.bat</b> 会重新打开；如果只想暂离，直接关掉浏览器也可以，服务会在后台等你。</p>
      </div>`;
  }

  async function quitApp() {
    const ok = await openConfirm(
      '退出知新服务？',
      '已保存的评分、札记、设置和 Obsidian 笔记都会保留。退出后这个页面将无法继续使用，下次双击 start.bat 再打开。',
      '退出知新',
    );
    if (!ok) return;
    try {
      await api('/api/shutdown', { method: 'POST', body: '{}' });
      showExitScreen();
    } catch (err) {
      toast(`退出失败：${formatError(err)}。也可以双击 stop.bat。`, 'error', 6500);
    }
  }

  async function loadBackups() {
    const select = $('#backup-select');
    const status = $('#backup-status');
    if (!select) return;
    try {
      const res = await api('/api/backups');
      const backups = res.backups || [];
      if (!backups.length) {
        select.innerHTML = '<option value="">暂无版本备份</option>';
        select.disabled = true;
        if (status) status.textContent = '自动备份已开启；第一次保存后就会出现历史版本。';
        return;
      }
      select.disabled = false;
      select.innerHTML = backups.map((b) => {
        const label = String(b.created_at || '').replace('T', ' ').slice(0, 19);
        const kb = Math.max(1, Math.round((b.size || 0) / 1024));
        return `<option value="${esc(b.name)}">${esc(label)} · ${kb} KB</option>`;
      }).join('');
      if (status) status.textContent = `共 ${backups.length} 份可恢复版本；恢复前会先备份当前数据。`;
    } catch (err) {
      if (status) status.textContent = `备份列表加载失败：${formatError(err)}`;
    }
  }

  async function createBackup(button) {
    setBusy(button, true, '备份中');
    try {
      const res = await api('/api/backups/create', { method: 'POST', body: '{}' });
      await loadBackups();
      toast('已创建版本快照', 'success');
    } catch (err) {
      toast(`备份失败：${formatError(err)}`, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function restoreBackup() {
    const select = $('#backup-select');
    const name = select?.value;
    if (!name) {
      toast('还没有可恢复的备份', 'error');
      return;
    }
    const ok = await openConfirm(
      '恢复到这个版本？',
      '当前数据会被覆盖，但覆盖前会自动把当前版本再备份一份。恢复后页面会刷新数据。',
      '恢复所选备份',
    );
    if (!ok) return;
    const button = $('#settings-backup-restore');
    setBusy(button, true, '恢复中');
    try {
      const res = await api('/api/backups/restore', { method: 'POST', body: JSON.stringify({ name }) });
      S.data = res;
      S.history = res.history || [];
      S.booted = true;
      S.drafts = {};
      S.prefDay = null;
      S.archiveLoaded = false;
      S.settingsDirty = false;
      closeSettings();
      renderHeaderDate();
      renderRoute({ preserveScroll: false });
      toast(`已恢复备份 ${String(name).replace(/^state-|\.json$/g, '')}`, 'success');
      notifyObsidian(res);
    } catch (err) {
      toast(`恢复失败：${formatError(err)}`, 'error', 6500);
      setBusy(button, false);
    }
  }

  async function syncZotero(button) {
    if (!S.data.profile.zotero_enabled) {
      toast('请先启用 Zotero 联动并保存设置', 'error');
      return;
    }
    if (S.settingsDirty) {
      const ok = await openConfirm('读取会使用已保存的设置', '你刚修改了 Zotero 地址或标签但还没保存；未保存的修改不会生效。仍然读取吗？', '使用已保存设置读取');
      if (!ok) return;
    }
    setBusy(button, true, '读取中');
    try {
      const res = await api('/api/zotero/sync', { method: 'POST', body: '{}' });
      S.data.zotero_status = res.zotero_status || {
        total: res.zotero?.total || 0,
        counts: res.zotero?.counts || {},
        top_tags: res.zotero?.top_tags || [],
        top_journals: res.zotero?.top_journals || [],
        synced_at: res.zotero?.fetched_at,
        error: res.zotero?.error || '',
      };
      const statusNode = $('#zotero-status');
      if (statusNode) statusNode.textContent = zoteroStatusLine(S.data.zotero_status, true);
      toast(`Zotero 已读取 ${res.zotero?.total || 0} 条文献`, 'success');
    } catch (err) {
      toast(`读取 Zotero 失败：${formatError(err)}`, 'error', 6500);
    } finally {
      setBusy(button, false);
    }
  }

  async function syncObsidian(button) {
    if (!S.data.profile.obsidian_enabled) {
      toast('请先启用 Obsidian 同步并保存设置', 'error');
      return;
    }
    if (S.settingsDirty) {
      const ok = await openConfirm('同步会使用已保存的设置', '你刚修改了设置但还没保存；未保存的路径或开关不会生效。仍然同步吗？', '使用已保存设置同步');
      if (!ok) return;
    }
    setBusy(button, true, '同步中');
    try {
      const res = await api('/api/obsidian/sync', { method: 'POST', body: '{}' });
      S.data.obsidian_status = res.obsidian_status || {
        last_sync: res.result?.synced_at,
        last_result: res.result,
        last_error: '',
      };
      const statusNode = $('#obsidian-status');
      if (statusNode) statusNode.textContent = obsidianStatusLine(S.data.obsidian_status, true);
      const bits = [];
      if (res.result?.papers) bits.push(`文献 ${res.result.papers} 篇`);
      if (res.result?.daily) bits.push(`日报 ${res.result.daily} 篇`);
      toast(bits.length ? `已同步到 Obsidian · ${bits.join(' · ')}` : 'Obsidian 已是最新', 'success');
    } catch (err) {
      S.data.obsidian_status = { ...(S.data.obsidian_status || {}), last_error: formatError(err) };
      const statusNode = $('#obsidian-status');
      if (statusNode) statusNode.textContent = obsidianStatusLine(S.data.obsidian_status, true);
      toast(`Obsidian 同步失败：${formatError(err)}`, 'error', 6500);
    } finally {
      setBusy(button, false);
    }
  }

  // ---------------------------------------------------------------------
  // 第二组事件：编辑器、设置、导出与打印
  // ---------------------------------------------------------------------
  document.addEventListener('click', async (e) => {
    if (e.target.closest('#onboard-add-field')) {
      addFieldEditor('#onboard-fields', 'onboard');
      return;
    }
    if (e.target.closest('#settings-add-field')) {
      addFieldEditor('#settings-fields', 'settings');
      return;
    }
    if (e.target.closest('.remove-field')) {
      const inSettings = !!e.target.closest('#settings-drawer');
      removeFieldEditor(e.target.closest('.remove-field'), inSettings ? 'settings' : 'onboard');
      return;
    }
    if (e.target.closest('#fill-example')) {
      fillOnboardingExample();
      return;
    }
    const choice = e.target.closest('.choice-card');
    if (choice) {
      const row = choice.parentElement;
      $$('.choice-card', row).forEach((btn) => btn.classList.remove('active'));
      choice.classList.add('active');
      if (choice.closest('#settings-drawer')) S.settingsDirty = true;
      return;
    }
    const source = e.target.closest('.source-choice');
    if (source) {
      const form = source.closest('form');
      const active = $$('.source-choice.active', form);
      if (source.classList.contains('active') && active.length === 1) {
        toast('至少保留一个数据源', 'error');
        return;
      }
      source.classList.toggle('active');
      if (source.closest('#settings-drawer')) S.settingsDirty = true;
      return;
    }
    if (e.target.closest('#close-settings') || e.target.closest('#cancel-settings') || e.target.id === 'settings-backdrop') {
      requestCloseSettings();
      return;
    }
    if (e.target.closest('#settings-zotero-sync')) {
      await syncZotero(e.target.closest('#settings-zotero-sync'));
      return;
    }
    if (e.target.closest('#settings-obsidian-sync')) {
      await syncObsidian(e.target.closest('#settings-obsidian-sync'));
      return;
    }
    if (e.target.closest('#settings-backup-now')) {
      await createBackup(e.target.closest('#settings-backup-now'));
      return;
    }
    if (e.target.closest('#settings-backup-restore')) {
      await restoreBackup();
      return;
    }
    if (e.target.closest('#clear-key')) {
      S.clearKey = true;
      S.settingsDirty = true;
      const keyInput = $('#settings-key');
      if (keyInput) { keyInput.value = ''; keyInput.placeholder = '保存后清除已存 Key'; }
      const status = $('.key-status');
      if (status) { status.textContent = '保存后清除'; status.style.color = 'var(--accent)'; }
      toast('保存设置后将清除 Key');
      return;
    }
    if (e.target.closest('#settings-regenerate')) {
      const ok = await openConfirm('按当前设置重选今天？', '今天的选读还没有评分，可以重新检索。', '重选今天');
      if (!ok) return;
      closeSettings();
      S.route = 'today';
      window.location.hash = 'today';
      renderRoute();
      generateToday(true);
      return;
    }
    if (e.target.closest('#archive-print') || e.target.closest('#insights-print')) {
      window.print();
      return;
    }
    const report = e.target.closest('[data-report-date]');
    if (report) {
      window.open(`/api/report?date=${encodeURIComponent(report.dataset.reportDate)}`, '_blank', 'noopener');
      return;
    }
  });

  document.addEventListener('submit', async (e) => {
    if (e.target.id === 'onboard-form') {
      e.preventDefault();
      if (e.target.dataset.busy === '1') return;
      e.target.dataset.busy = '1';
      await submitOnboarding(e.target);
      e.target.dataset.busy = '0';
      return;
    }
    if (e.target.id === 'settings-form') {
      e.preventDefault();
      await saveSettings(e.target);
    }
  });

  document.addEventListener('input', (e) => {
    if (e.target.id === 'settings-key' && e.target.value.trim()) {
      S.clearKey = false;
      const status = $('.key-status');
      if (status && status.textContent.includes('保存后清除')) status.textContent = '保存新 Key';
    }
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
