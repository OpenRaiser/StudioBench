/* StudioBench 制作画布 v2
 * 读 build.py 生成的 data/index.js（总览）和 data/<case>.js（单题全部 run），在一张可平移缩放的画布上摆出
 * 每个模型每一步怎么做的：总览胶片墙 / 阶段看板 / 时间线 / 成片分镜 / 一致性。
 * 画布：#world 用 transform 平移缩放；卡片是普通 DOM，点开右侧抽屉看全文、媒体、血缘和跨模型对照。
 * 和 v1 的区别：阶段格子可以折起（默认展开）；图片按视野懒加载；排版一次量完高度；血缘连线按相对位置选边。 */
'use strict';
(() => {
  const IDX = window.SB_INDEX || { cases: [], models: [], near: 10 };
  const CASES = (window.SB_CASE = window.SB_CASE || {});
  const $ = (id) => document.getElementById(id);
  const vp = $('viewport');
  const world = $('world');
  const panel = $('panel');

  const STAGES = [
    { key: 'plan', en: 'PLAN', title: '剧本 · 回复', sub: '写在回复里的剧本、分镜和说明' },
    { key: 'think', en: 'THINK', title: '思考', sub: '可见的思考过程（有的模型不返回）' },
    { key: 'research', en: 'RESEARCH', title: '调研', sub: 'search 检索 · fetch_url 下载' },
    { key: 'image', en: 'IMAGE', title: '生图', sub: 't2i：定妆、场景、关键帧' },
    { key: 'edit', en: 'EDIT', title: '改图', sub: 'image_edit 改图 · remove_bg 抠图' },
    { key: 'video', en: 'SHOTS', title: '视频镜头', sub: 't2v · i2v · ref_video · lipsync' },
    { key: 'render', en: 'RENDER', title: '程序渲染', sub: 'render_html · render_blender · render_three · ken_burns' },
    { key: 'audio', en: 'AUDIO', title: '声音', sub: 'tts 旁白 · music 配乐 · sfx 音效 · audio_edit · audio_mix' },
    { key: 'cut', en: 'CUT', title: '剪辑', sub: 'trim · concat · transition · composite · color · mix_audio' },
    { key: 'qa', en: 'QA', title: '质检', sub: 'sample_frames 看帧 · scene_detect · asr · beat_detect · inspect' },
    { key: 'deliver', en: 'DELIVER', title: '交付', sub: '成片与 export' },
  ];
  // 每一列：卡片宽、最少/最多几列；列数按该阶段最多的那家 ÷ ROWS 取整
  const COLCFG = {
    plan: { w: 420, min: 1, max: 3 }, think: { w: 320, min: 1, max: 3 }, research: { w: 380, min: 1, max: 2 },
    image: { w: 220, min: 3, max: 6 }, edit: { w: 220, min: 2, max: 5 }, video: { w: 220, min: 3, max: 6 },
    render: { w: 240, min: 2, max: 5 }, audio: { w: 260, min: 1, max: 3 }, cut: { w: 240, min: 1, max: 4 },
    qa: { w: 160, min: 2, max: 8 }, deliver: { w: 400, min: 1, max: 1 },
  };
  const ROWS = 6;
  const FOLD_W = 300;            // 折叠的格子宽
  const PEEK = 8;                // 折叠时露出几张小图
  const TOOL = {
    t2i: '生图', image_edit: '改图', remove_bg: '抠图', t2v: '文生视频', i2v: '图生视频', ref_video: '参考生视频', lipsync: '对口型',
    render_html: 'HTML 渲染', render_blender: 'Blender 渲染', render_three: 'three.js 渲染', ken_burns: '推拉摇移',
    tts: '配音', music: '配乐', sfx: '音效', audio_edit: '音频处理', audio_mix: '分轨混音',
    trim: '裁剪', concat: '拼接', transition: '转场拼接', overlay: '烧字幕', composite: '叠层', mix_audio: '混音',
    color: '调色', speed: '变速', reframe: '改画幅',
    sample_frames: '看帧', scene_detect: '切镜检测', asr: '听写', beat_detect: '节拍检测', inspect: '检查',
    export: '交卷', search: '检索', fetch_url: '下载',
  };
  const STAGE_OF = {
    search: 'research', fetch_url: 'research', t2i: 'image', image_edit: 'edit', remove_bg: 'edit',
    t2v: 'video', i2v: 'video', ref_video: 'video', lipsync: 'video',
    render_html: 'render', render_blender: 'render', render_three: 'render', ken_burns: 'render',
    tts: 'audio', music: 'audio', sfx: 'audio', audio_edit: 'audio', audio_mix: 'audio',
    trim: 'cut', concat: 'cut', transition: 'cut', overlay: 'cut', composite: 'cut', mix_audio: 'cut', color: 'cut', speed: 'cut', reframe: 'cut',
    sample_frames: 'qa', scene_detect: 'qa', asr: 'qa', beat_detect: 'qa', inspect: 'qa', export: 'deliver',
  };
  const THUMB_TOOLS = new Set(['t2i', 'image_edit', 'remove_bg', 't2v', 'i2v', 'ref_video', 'lipsync', 'render_html', 'render_blender', 'render_three', 'ken_burns',
    'trim', 'concat', 'transition', 'overlay', 'composite', 'color', 'speed', 'reframe', 'sample_frames', 'scene_detect']);
  const SEEN_TOOLS = new Set(['sample_frames', 'scene_detect']);
  const STATUS = { done: ['已交卷', 'good'], running: ['进行中', 'acc'], no_export: ['没交卷', 'crit'], incomplete: ['没跑完', 'warn'], empty: ['无记录', 'warn'], error: ['起跑报错', 'crit'] };
  const SRC = { text: '回复', think: '思考', script: '最终说明', t2i: '生图 prompt', image_edit: '改图 prompt', t2v: '视频 prompt', i2v: '视频 prompt', ref_video: '视频 prompt', tts: '旁白/对白', music: '配乐描述', sfx: '音效描述', overlay: '字幕', search: '检索词', render_html: 'HTML 页面文字' };
  const KIND = { quote: '原片字幕/角标', proper: '专名', cjk: '中文片段' };
  // 一致性矩阵：[key, 标题, 说明, 色阶上限, 格式, 是否距离（越小越像）]
  const METRICS = [
    ['image', '出图构图', '每张生图/视频封面在对方那里最接近的 pHash 距离，取平均。越小越像；互不相干约 30，≤10 才算近似重复', 32, (v) => v.toFixed(1), true],
    ['final', '成片抽帧', '成片 16 张抽帧在对方成片里最接近的 pHash 距离，取平均。越小越像', 32, (v) => v.toFixed(1), true],
    ['prompt', 'prompt 文本', '每条生图/视频 prompt 在对方那里的最高 TF-IDF 余弦，取平均', 0.6, (v) => v.toFixed(2), false],
    ['script', '剧本文本', '全部回复 + 最终说明的整体余弦', 0.6, (v) => v.toFixed(2), false],
    ['search', '检索结果重合', '两家搜回的 URL 的 Jaccard', 1, (v) => Math.round(v * 100) + '%', false],
  ];

  // ------------------------------------------------------------------ 状态
  const S = {
    mode: 'overview', caseId: null, data: null,
    k: 1, tx: 0, ty: 0, bounds: { w: 1, h: 1 },
    cards: new Map(), frames: [], sel: null, q: '', hits: [], hitIdx: -1,
    hidden: new Set(), showQA: true, showThink: true, wheelZoom: true,
    pairFilter: null, pairLimit: 24, showWeak: false, keepView: false, then: null, copy: [],
    closed: new Set(),           // 阶段看板里手动折起的格子：`${case}|${slot}|${stage}`；默认全部展开
    where: new Map(),            // 卡片 key → 所在格子（折叠时卡片不建 DOM，靠它定位、展开）
    folded: [],                  // 折叠格子里的条目 [{key, frame, text, slot}]，给检索用
    frameEls: new Map(),         // 格子 key → DOM
  };
  try {
    const p = JSON.parse(localStorage.getItem('sbcanvas:prefs') || '{}');
    if (typeof p.showQA === 'boolean') S.showQA = p.showQA;
    if (typeof p.showThink === 'boolean') S.showThink = p.showThink;
    if (typeof p.wheelZoom === 'boolean') S.wheelZoom = p.wheelZoom;
    if (Array.isArray(p.hidden)) S.hidden = new Set(p.hidden);
    if (typeof p.fDom === 'string') S.fDom = p.fDom;
    if (typeof p.fFam === 'string') S.fFam = p.fFam;
  } catch (e) { /* 没有存储也能用 */ }
  const savePrefs = () => {
    try { localStorage.setItem('sbcanvas:prefs', JSON.stringify({ showQA: S.showQA, showThink: S.showThink, wheelZoom: S.wheelZoom, hidden: [...S.hidden], fDom: S.fDom || '', fFam: S.fFam || '' })); } catch (e) { /* 忽略 */ }
  };

  // ------------------------------------------------------------------ 小工具
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const mk = (cls, html) => { const d = document.createElement('div'); if (cls) d.className = cls; if (html != null) d.innerHTML = html; return d; };
  const mcol = (slot) => `--mc:var(--m${slot % 8})`;
  const short = (s, n) => { s = String(s ?? '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const fmtWall = (s) => { if (s == null) return '–'; s = +s; if (s >= 3600) return `${Math.floor(s / 3600)}h${String(Math.round((s % 3600) / 60)).padStart(2, '0')}m`; if (s >= 60) return `${Math.round(s / 60)}m`; return `${Math.round(s)}s`; };
  const tc = (s) => { s = Math.max(0, +s || 0); const m = Math.floor(s / 60); const r = Math.round(s - m * 60); return `${m}:${String(r).padStart(2, '0')}`; };
  const chip = (txt, cls = '', tip = '') => `<span class="chip ${cls}"${tip ? ` data-tip="${esc(tip)}" title="${esc(tip)}"` : ''}>${esc(txt)}</span>`;
  const mchip = (slot, txt) => `<span class="chip m" style="${mcol(slot)}">${esc(txt)}</span>`;
  const dot = (slot) => `<i style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--m${slot % 8});margin-left:2px"></i>`;
  const model = (slot) => IDX.models.find((m) => m.slot === slot) || { name: '#' + slot, id: '' };
  const abbr = (slot) => { const n = model(slot).name; return n.split(' ')[0]; };
  // 图片、条带都先只写 data-src / data-bg，进入视野（且不是最远景）才真正加载；见下方 lazy*
  const img = (src, cls = '') => (src ? `<img decoding="async"${cls ? ` class="${cls}"` : ''} data-src="${esc(src)}" alt="">` : '');
  const bg = (src) => (src ? ` data-bg="${esc(src)}"` : '');
  const caseNo = (id) => { const p = String(id).split('-'); return p.length > 2 ? p.slice(1).join('-') : p.pop(); };
  const isDark = () => { const t = document.documentElement.dataset.theme; return t === 'dark' || (t !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches); };
  const visibleRuns = () => (S.data ? S.data.runs.filter((r) => !S.hidden.has(r.slot)) : []);
  const place = (el, x, y, w) => { el.style.left = x + 'px'; el.style.top = y + 'px'; if (w != null) el.style.width = w + 'px'; return el; };
  const effSeed = (r) => (r.args && r.args.seed != null ? r.args.seed : (r.tool === 't2i' || r.tool === 'image_edit' ? 0 : null));
  const relT = (run, t) => (t && run._t0 ? '+' + Math.max(0, Math.round((t - run._t0) / 60)) + 'm' : '');

  // ------------------------------------------------------------------ Markdown（回复里的剧本、分镜表）
  function inline(s) {
    return esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  }
  const RE_SPECIAL = /^\s*```|^#{1,6}\s|^\s*[-*•]\s+|^\s*\d+[.)、]\s+|^\s*>/;
  function md(src) {
    const L = String(src || '').replace(/\r/g, '').split('\n');
    const out = [];
    const isRow = (s) => /^\s*\|.*\|\s*$/.test(s || '');
    const isSep = (s) => /^\s*\|?\s*:?-{2,}/.test(s || '');
    let i = 0;
    while (i < L.length) {
      const s = L[i];
      if (/^\s*```/.test(s)) {
        const buf = []; i++;
        while (i < L.length && !/^\s*```/.test(L[i])) buf.push(L[i++]);
        i++; out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`); continue;
      }
      if (isRow(s) && isSep(L[i + 1])) {
        const cells = (r) => r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => inline(c.trim()));
        const head = cells(s); i += 2;
        const rows = [];
        while (i < L.length && isRow(L[i])) rows.push(cells(L[i++]));
        out.push(`<div class="tablewrap"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
        continue;
      }
      let m = s.match(/^(#{1,6})\s+(.*)$/);
      if (m) { const lv = Math.min(6, m[1].length + 3); out.push(`<h${lv}>${inline(m[2])}</h${lv}>`); i++; continue; }
      if (/^\s*[-*•]\s+/.test(s)) {
        const it = []; while (i < L.length && /^\s*[-*•]\s+/.test(L[i])) it.push(inline(L[i++].replace(/^\s*[-*•]\s+/, '')));
        out.push(`<ul>${it.map((x) => `<li>${x}</li>`).join('')}</ul>`); continue;
      }
      if (/^\s*\d+[.)、]\s+/.test(s)) {
        const it = []; while (i < L.length && /^\s*\d+[.)、]\s+/.test(L[i])) it.push(inline(L[i++].replace(/^\s*\d+[.)、]\s+/, '')));
        out.push(`<ol>${it.map((x) => `<li>${x}</li>`).join('')}</ol>`); continue;
      }
      if (/^\s*>/.test(s)) {
        const it = []; while (i < L.length && /^\s*>/.test(L[i])) it.push(inline(L[i++].replace(/^\s*>\s?/, '')));
        out.push(`<blockquote>${it.join('<br>')}</blockquote>`); continue;
      }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(s)) { out.push('<hr>'); i++; continue; }
      if (!s.trim()) { i++; continue; }
      const buf = [inline(L[i++])];
      while (i < L.length && L[i].trim() && !RE_SPECIAL.test(L[i]) && !(isRow(L[i]) && isSep(L[i + 1]))) buf.push(inline(L[i++]));
      out.push(`<p>${buf.join('<br>')}</p>`);
    }
    return out.join('');
  }

  // ------------------------------------------------------------------ 数据
  function loadCase(id) {
    if (CASES[id]) return Promise.resolve(CASES[id]);
    return new Promise((ok, fail) => {
      const s = document.createElement('script');
      s.src = `data/${encodeURIComponent(id)}.js?v=${encodeURIComponent(IDX.built || '')}`;
      s.onload = () => (CASES[id] ? ok(CASES[id]) : fail(new Error('data/' + id + '.js 是空的')));
      s.onerror = () => fail(new Error('读不到 data/' + id + '.js'));
      document.head.appendChild(s);
    });
  }
  function prep(c) {
    if (c._prep) return c;
    c._prep = true;
    c.runBySlot = {};
    c.shared = {};
    for (const u of (c.sim && c.sim.shared_urls) || []) c.shared[u.url] = u.slots;
    for (const run of c.runs) {
      c.runBySlot[run.slot] = run;
      run._cons = {};
      let t0 = Infinity;
      for (const r of Object.values(run.calls)) {
        if (r.t) t0 = Math.min(t0, r.t);
        for (const x of r.ins || []) if (x.from != null) (run._cons[x.from] = run._cons[x.from] || []).push(r.i);
      }
      run._t0 = isFinite(t0) ? t0 : 0;
      run._ev = {};
      for (const e of run.events) run._ev[e.n] = e;
    }
    return c;
  }

  // ------------------------------------------------------------------ 画布：平移缩放
  const topInset = () => $('sub').getBoundingClientRect().bottom;
  function apply() {
    world.style.transform = `translate(${S.tx}px, ${S.ty}px) scale(${S.k})`;
    let gs = 24 * S.k; while (gs < 14) gs *= 2; while (gs > 56) gs /= 2;
    vp.style.setProperty('--gs', gs + 'px');
    vp.style.setProperty('--gx', S.tx + 'px');
    vp.style.setProperty('--gy', S.ty + 'px');
    const lod = S.k < 0.15 ? 'far' : S.k < 0.4 ? 'mid' : 'near';
    if (world.dataset.lod !== lod) { world.dataset.lod = lod; if (lod !== 'far') lazyRefresh(); }
    $('zpct').textContent = Math.round(S.k * 100) + '%';
    scheduleMinimap();
  }

  // 懒加载：画布是 transform 缩放的，原生 loading=lazy 在“适配全部”时会把所有图一起拉下来。
  // 这里用以 #viewport 为根的 IntersectionObserver（它按变换后的位置算相交），最远景不加载。
  const loadEl = (el) => {
    if (el.dataset.src) { el.src = el.dataset.src; delete el.dataset.src; }
    if (el.dataset.bg) { el.style.backgroundImage = `url("${el.dataset.bg}")`; delete el.dataset.bg; }
  };
  const lazyIO = 'IntersectionObserver' in window ? new IntersectionObserver((ents) => {
    if (world.dataset.lod === 'far') return;
    for (const en of ents) if (en.isIntersecting) { lazyIO.unobserve(en.target); loadEl(en.target); }
  }, { root: vp, rootMargin: '400px' }) : null;
  function lazyScan(root) {
    const els = root.querySelectorAll('[data-src], [data-bg]');
    if (!lazyIO) { els.forEach(loadEl); return; }
    els.forEach((el) => lazyIO.observe(el));
  }
  function lazyRefresh() {                 // 从最远景拉近：已经在视野里的不会再触发回调，重新观察一遍
    if (!lazyIO) return;
    for (const el of world.querySelectorAll('[data-src], [data-bg]')) { lazyIO.unobserve(el); lazyIO.observe(el); }
  }
  function zoomAt(cx, cy, f) {
    const k2 = clamp(S.k * f, 0.02, 4);
    const wx = (cx - S.tx) / S.k, wy = (cy - S.ty) / S.k;
    S.k = k2; S.tx = cx - wx * k2; S.ty = cy - wy * k2;
    apply();
  }
  const viewW = () => window.innerWidth - (panel.hidden ? 0 : panel.offsetWidth);
  function fitRect(b, how = 'all', pad = 36, kmax = 1.2) {
    const W = viewW(), top = topInset(), H = window.innerHeight - top;
    let k;
    if (how === 'width') k = (W - 2 * pad) / b.w;
    else if (how === 'height') k = (H - 2 * pad) / b.h;
    else k = Math.min((W - 2 * pad) / b.w, (H - 2 * pad) / b.h);
    S.k = clamp(k, 0.02, kmax);
    S.tx = how === 'height' ? pad - b.x * S.k : (W - b.w * S.k) / 2 - b.x * S.k;
    S.ty = how === 'width' ? top + pad - b.y * S.k : top + (H - b.h * S.k) / 2 - b.y * S.k;
    if (how === 'height' && b.w * S.k < W) S.tx = (W - b.w * S.k) / 2 - b.x * S.k;
    if (how === 'width' && b.h * S.k < H) S.ty = top + (H - b.h * S.k) / 2 - b.y * S.k;
    apply();
  }
  function fitAll() {
    const how = { timeline: 'height', sim: 'width', overview: 'width', stage: 'width' }[S.mode] || 'all';
    fitRect({ x: 0, y: 0, w: S.bounds.w, h: S.bounds.h }, how, 36, how === 'width' ? 1 : 1.2);
  }
  function cardRect(key) {
    const c = S.cards.get(key);
    if (!c) return null;
    let e = c.el, x = 0, y = 0;
    while (e && e !== world) { x += e.offsetLeft; y += e.offsetTop; e = e.offsetParent; }
    return { x, y, w: c.el.offsetWidth, h: c.el.offsetHeight };
  }
  function zoomToKey(key, kmin = 0.85) {
    const r = cardRect(key);
    if (!r) return;
    const W = viewW(), top = topInset(), H = window.innerHeight - top;
    const k = clamp(Math.min((W * 0.6) / r.w, (H * 0.7) / r.h, 1.4), Math.min(kmin, S.k > kmin ? S.k : kmin), 1.4);
    S.k = Math.max(k, Math.min(S.k, 1.4));
    S.tx = W / 2 - (r.x + r.w / 2) * S.k;
    S.ty = top + H / 2 - (r.y + r.h / 2) * S.k;
    apply();
  }

  // 指针：拖动平移（卡片上拖也行），没拖动就算点击；双指缩放
  const pts = new Map();
  let drag = null, pinch = null, lastTap = { key: null, t: 0 };
  vp.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.button !== 1) return;
    if (e.target.closest('a, button, input, select, textarea, video, audio, summary')) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: S.k };
      drag = null;
      return;
    }
    drag = { x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty, moved: false, target: e.target };
    vp.setPointerCapture(e.pointerId);
  });
  vp.addEventListener('pointermove', (e) => {
    if (pts.has(e.pointerId)) pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, (pinch.k * d) / pinch.d / S.k);
      return;
    }
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) > 4) { drag.moved = true; vp.classList.add('panning'); hideTip(); }
      if (drag.moved) { S.tx = drag.tx + dx; S.ty = drag.ty + dy; apply(); }
      return;
    }
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (t) showTip(t.dataset.tip, e.clientX, e.clientY); else hideTip();
  });
  const endPointer = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (drag && e.type === 'pointerup' && !drag.moved) onTap(drag.target, e);
    drag = null;
    vp.classList.remove('panning');
  };
  vp.addEventListener('pointerup', endPointer);
  vp.addEventListener('pointercancel', endPointer);
  vp.addEventListener('pointerleave', () => hideTip());
  vp.addEventListener('wheel', (e) => {
    e.preventDefault();
    const scale = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
    const zoom = e.ctrlKey || e.metaKey || (S.wheelZoom && !e.shiftKey);
    if (zoom) zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * scale * (e.ctrlKey ? 0.01 : 0.0016)));
    else { S.tx -= (e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX) * scale; S.ty -= (e.shiftKey ? 0 : e.deltaY) * scale; apply(); }
  }, { passive: false });

  function onTap(target, e) {
    const now = Date.now();
    const act = target.closest('[data-act]');
    if (act) { doAct(act.dataset.act); return; }
    const ov = target.closest('[data-open]');
    if (ov) { const [cid, slot] = ov.dataset.open.split(':'); openCase(cid, 'stage', () => focusFinal(+slot)); return; }
    const pair = target.closest('[data-pair]');
    if (pair) { const [key, a, b] = pair.dataset.pair.split(':'); if (key === 'image') { S.pairFilter = [+a, +b]; S.pairLimit = 24; rerender(); } return; }
    const go = target.closest('[data-go]');
    if (go) { goRef(go.dataset.go); return; }
    const fold = target.closest('[data-fold]');
    if (fold) { toggleFold(fold.dataset.fold); return; }
    const card = target.closest('[data-key]');
    if (card) {
      const key = card.dataset.key;
      if (!S.cards.has(key)) { goRef(key); return; }       // 泳道头等：没有对应卡片时直接开抽屉
      if (lastTap.key === key && now - lastTap.t < 350) zoomToKey(key, 1);
      else select(key);
      lastTap = { key, t: now };
      return;
    }
    clearSel();
    closePanel();
  }
  function doAct(a) {
    if (a === 'more-pairs') { S.pairLimit += 24; rerender(); }
    else if (a === 'clear-pairs') { S.pairFilter = null; S.pairLimit = 24; rerender(); }
    else if (a === 'toggle-weak') { S.showWeak = !S.showWeak; rerender(); }
  }

  // ------------------------------------------------------------------ 小地图
  let mmQueued = false;
  function scheduleMinimap() { if (!mmQueued) { mmQueued = true; requestAnimationFrame(() => { mmQueued = false; drawMinimap(); }); } }
  function drawMinimap() {
    const cv = $('minimap'), ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height, b = S.bounds;
    ctx.clearRect(0, 0, W, H);
    if (!b.w || !b.h) return;
    const s = Math.min((W - 16) / b.w, (H - 16) / b.h);
    const ox = (W - b.w * s) / 2, oy = (H - b.h * s) / 2;
    const cs = getComputedStyle(document.documentElement);
    for (const f of S.frames) {
      ctx.fillStyle = f.slot != null ? cs.getPropertyValue('--m' + (f.slot % 8)).trim() : cs.getPropertyValue('--ink-3').trim();
      ctx.globalAlpha = f.slot != null ? 0.45 : 0.25;
      ctx.fillRect(ox + f.x * s, oy + f.y * s, Math.max(1, f.w * s), Math.max(1, f.h * s));
    }
    ctx.globalAlpha = 1;
    const top = topInset();
    ctx.strokeStyle = cs.getPropertyValue('--accent').trim();
    ctx.lineWidth = 2;
    ctx.strokeRect(ox + (-S.tx / S.k) * s, oy + ((top - S.ty) / S.k) * s, (viewW() / S.k) * s, ((window.innerHeight - top) / S.k) * s);
    cv._map = { s, ox, oy };
  }
  function minimapGo(e) {
    const cv = $('minimap'), m = cv._map;
    if (!m) return;
    const r = cv.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * cv.width, py = ((e.clientY - r.top) / r.height) * cv.height;
    const wx = (px - m.ox) / m.s, wy = (py - m.oy) / m.s;
    const top = topInset();
    S.tx = viewW() / 2 - wx * S.k;
    S.ty = top + (window.innerHeight - top) / 2 - wy * S.k;
    apply();
  }
  $('minimap').addEventListener('pointerdown', (e) => {
    minimapGo(e);
    const mv = (ev) => minimapGo(ev);
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', mv);
    window.addEventListener('pointerup', up);
  });

  // ------------------------------------------------------------------ 提示
  const tip = $('tip');
  function showTip(text, x, y) {
    tip.textContent = text;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = clamp(x + 14, 8, window.innerWidth - w - 8) + 'px';
    tip.style.top = clamp(y + 16, 8, window.innerHeight - h - 8) + 'px';
  }
  function hideTip() { tip.hidden = true; }

  // ------------------------------------------------------------------ 卡片
  function reg(el, key, run, info) {
    el.dataset.key = key;
    S.cards.set(key, Object.assign({ el, run }, info));
    return el;
  }
  function callText(r) {
    const a = r.args || {};
    const parts = [r.tool, TOOL[r.tool], a.prompt, a.text, a.query, a.lyrics, a.instruct, a.negative_prompt, a.srt_text, a.speaker, a.url, a.html, a.code];
    for (const s of a.subtitles || []) parts.push(s && s.text);
    for (const o of r.outs || []) parts.push(o);
    for (const x of r.ins || []) parts.push(x.path);
    if (r.res) { if (r.res.text) parts.push(r.res.text); for (const x of r.res.results || []) parts.push(x.title, x.snippet, x.url); }
    for (const c of r.carry || []) parts.push(c.p);
    if (r.err) parts.push(r.err);
    if (a.seed != null) parts.push('seed ' + a.seed); else if (r.seed_default) parts.push('seed 0');
    return parts.filter((x) => typeof x === 'string' && x).join('\n').toLowerCase();
  }
  function flagsOf(r) {
    const f = [];
    if (!r.ok) f.push(chip('失败', 'crit', r.err || ''));
    if (r.seed_default) f.push(chip('seed 0', 'warn', '没传 seed：t2i / image_edit 默认 seed=0，各家没传的都用同一份初始噪声'));
    else if (r.args && r.args.seed != null) f.push(chip('seed ' + r.args.seed));
    if (r.dup && r.dup.length) f.push(`<span class="chip acc" data-tip="其他模型有近似重复的画面（pHash 距离 ≤ ${IDX.near}）">≈${[...new Set(r.dup.map((x) => x[0]))].map(dot).join('')}</span>`);
    if (r.over) f.push(chip('已被覆盖', '', '这个文件后来被 #' + r.over + ' 重写'));
    if (r.batch) f.push(chip('并行', 'acc', '和同一个 batch 里的其它调用一起提交、并行执行（batch ' + r.batch + '）'));
    if (r.queued) f.push(chip('排队 ' + Math.round(r.queued) + 's', r.queued > 60 ? 'warn' : '', '在工具池里等空闲实例的时间'));
    return f;
  }
  function thumbBox(run, r) {
    const src = SEEN_TOOLS.has(r.tool) ? r.seen : r.thumb;
    if (!src) {
      const why = !r.ok ? short(r.err, 90) : r.over ? `文件后来被 #${r.over} 覆盖，没留下当时的画面` : '没有缩略图';
      return `<div class="th none">${esc(why)}</div>`;
    }
    let inset = '';
    const ff = (r.ins || []).find((x) => ['first_frame', 'src', 'references'].includes(x.arg) && x.from != null);
    if (ff && !SEEN_TOOLS.has(r.tool)) {
      const p = run.calls[ff.from];
      const ps = p && (p.thumb || p.seen);
      if (ps) inset = `<div class="inset">${img(ps)}<span>#${ff.from}</span></div>`;
    }
    const dur = r.res && r.res.duration_s && !SEEN_TOOLS.has(r.tool) ? `<span class="badge">${(+r.res.duration_s).toFixed(1)}s</span>` : '';
    const from = r.thumb_from ? `<span class="badge" style="left:4px;top:4px;right:auto;bottom:auto" data-tip="磁盘上的文件后来被覆盖，这是模型在 #${r.thumb_from} 看帧时看到的画面">当时画面</span>` : '';
    return `<div class="th">${img(src)}${inset}${dur}${from}</div>${r.strip ? `<div class="strip"${bg(r.strip)}></div>` : ''}`;
  }
  function searchBody(run, r) {
    const rows = (r.res && r.res.results) || [];
    const shared = (S.data && S.data.shared) || {};
    const lis = rows.map((x) => {
      const n = (shared[(x.url || '').split('#')[0]] || []).length;
      const tipText = (x.block ? `屏蔽表启用时这条会被挡掉（命中 ${x.block}）\n` : '') + x.title + '\n' + x.snippet;
      return `<li class="${x.block ? 'blk' : ''}">${chip(x.host || '?', x.block ? 'crit' : '')}<span class="t" data-tip="${esc(tipText)}">${esc(x.title)}${n > 1 ? ' ' + chip(n + ' 家都搜到', 'acc') : ''}</span></li>`;
    }).join('');
    const carry = (r.carry || []).slice(0, 14).map((c) => chip(c.p, '', `后来写进了${SRC[c.src] || c.src || ''}${c.ref && c.ref.i ? ' #' + c.ref.i : ''}：${c.ctx || ''}`)).join('');
    const low = r.res && r.res.quality === 'low' ? chip('结果不相关', 'warn', r.res.note || '') : '';
    return `<div class="q">${esc(r.args.query)} ${low}</div>${!r.ok ? `<div class="meta">${esc(r.err || '')}</div>` : ''}<ul class="res">${lis}</ul>${carry ? `<div class="carry" data-tip="检索结果里这些片段，后来被这个模型写进了自己的 prompt / 旁白 / 字幕 / 回复">${carry}</div>` : ''}`;
  }
  function callBody(run, r) {
    const a = r.args || {}, res = r.res || {};
    const d = (x) => (x ? (+x).toFixed(1) + 's' : '');
    switch (r.tool) {
      case 't2i': case 'image_edit': case 't2v': case 'i2v': case 'ref_video':
        return `<div class="pr">${esc(a.prompt)}</div><div class="meta">${esc([(r.outs || [])[0], a.aspect, a.duration ? a.duration + 's' : ''].filter(Boolean).join(' · '))}</div>`;
      case 'lipsync': return `<div class="pr l2">${esc(a.video)} ← ${esc(a.audio)}</div>`;
      case 'tts': return `<div class="pr">${esc(a.text)}</div><div class="meta">${esc([a.speaker || (a.voice_ref ? '克隆 ' + a.voice_ref : '默认音色'), d(res.duration_s), a.speed ? '×' + a.speed : ''].filter(Boolean).join(' · '))}</div>${a.instruct ? `<div class="meta" data-tip="${esc(a.instruct)}">${esc(a.instruct)}</div>` : ''}`;
      case 'music': return `<div class="pr">${esc(a.prompt)}</div><div class="meta">${esc([d(res.duration_s) || (a.duration ? a.duration + 's' : ''), a.lyrics ? '有歌词' : '纯音乐'].filter(Boolean).join(' · '))}</div>`;
      case 'sfx': return `<div class="pr">${esc(a.prompt)}</div><div class="meta">按 ${esc(a.video || '')} ${esc(d(res.duration_s))}</div>`;
      case 'search': return searchBody(run, r);
      case 'trim': return `<div class="pr l2">${esc(a.video)} 截 ${(+a.start || 0).toFixed(2)}–${(+a.end || 0).toFixed(2)}s</div><div class="meta">→ ${esc((r.outs || [])[0] || '')}</div>`;
      case 'concat': return `<div class="pr l2">拼接 ${(a.videos || []).length} 段${res.duration_s ? '，共 ' + d(res.duration_s) : ''}</div><div class="meta">→ ${esc((r.outs || [])[0] || '')}</div>`;
      case 'overlay': {
        const subs = a.subtitles || [];
        return `<div class="pr l2">烧字幕 ${subs.length ? subs.length + ' 条' : (a.srt_text ? 'SRT' : esc(a.srt || ''))}：${esc(((subs[0] || {}).text || '').replace(/\{[^}]*\}/g, ''))}</div><div class="meta">${esc(a.video)} → ${esc((r.outs || [])[0] || '')}</div>`;
      }
      case 'mix_audio': {
        const src = (r.ins || []).find((x) => x.arg === 'audio');
        const p = src && src.from != null ? run.calls[src.from] : null;
        const what = p ? (TOOL[p.tool] || p.tool) : '音频';
        return `<div class="pr l2">混入${esc(what)}${p && p.tool === 'tts' ? '：' + esc(short(p.args.text, 40)) : ''}</div><div class="meta">${esc([a.audio, '@' + (+a.start || 0).toFixed(1) + 's', a.gain != null ? 'gain ' + a.gain : '', a.replace ? '替换原声' : ''].filter(Boolean).join(' · '))}</div>`;
      }
      case 'sample_frames': return `<div class="meta">看 ${esc(a.video)}${a.fps ? ' @' + a.fps + 'fps' : ''}${a.start != null ? ' ' + a.start + '–' + (a.end ?? '') + 's' : ''}</div>`;
      case 'asr': return `<div class="pr l2">${esc(res.text || '')}</div><div class="meta">听 ${esc(a.audio)}</div>`;
      case 'inspect': return `<div class="meta">${esc(a.path)} ${esc(d(res.duration_s))} ${res.width ? res.width + '×' + res.height : ''}</div>`;
      case 'export': return `<div class="pr l2">交卷 ${esc(a.src)}</div><div class="meta">${esc(d(res.duration_s))} ${res.width ? res.width + '×' + res.height : ''}</div>`;
      case 'fetch_url': return `<div class="pr l2">${esc(a.url)}</div><div class="meta">→ ${esc(a.out || (r.outs || [])[0] || '')}</div>`;
      case 'remove_bg': return `<div class="meta">${esc(a.image)} → ${esc((r.outs || [])[0] || '')}</div>`;
      case 'render_html': case 'render_blender': case 'render_three': {
        const code = a.html || a.code || a.script || '';
        const title = (String(code).match(/<title>([^<]*)<\/title>/i) || [])[1];
        // HTML：去掉 style / script / 标签，取页面上真正显示的字；代码：取第一行注释；都没有就写规模
        const text = a.html ? String(a.html).replace(/<(style|script)[\s\S]*?<\/\1>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() : '';
        const cmt = !a.html ? (String(code).match(/^\s*(?:#|\/\/)\s*(.+)$/m) || [])[1] : '';
        const what = a.entry || title || text.slice(0, 90) || cmt || (a.html ? `HTML 页面 · ${String(code).length} 字` : `代码 ${String(code).split('\n').length} 行`);
        const sz = [a.width && a.height ? a.width + '×' + a.height : '', a.duration ? a.duration + 's' : '', a.fps ? a.fps + 'fps' : '',
          Array.isArray(a.times) ? '预览 ' + a.times.length + ' 帧' : '', Array.isArray(a.frames) ? '预览 ' + a.frames.length + ' 帧' : '', a.transparent ? '透明' : ''].filter(Boolean).join(' · ');
        return `<div class="pr l2">${esc(what)}</div><div class="meta">${esc([sz, (r.outs || [])[0] || ''].filter(Boolean).join(' · '))}</div>`;
      }
      case 'ken_burns': return `<div class="pr l2">${esc(a.image)} 缩放 ${a.zoom_from ?? 1}→${a.zoom_to ?? 1}${a.pan ? ' · ' + esc(a.pan) : ''}</div><div class="meta">${esc([a.duration ? (+a.duration).toFixed(1) + 's' : '', (r.outs || [])[0] || ''].filter(Boolean).join(' · '))}</div>`;
      case 'audio_edit': return `<div class="pr l2">${esc(a.audio)}${a.start != null || a.end != null ? ` 截 ${(+a.start || 0).toFixed(1)}–${a.end != null ? (+a.end).toFixed(1) : '尾'}s` : ''}</div><div class="meta">${esc([a.fade_in ? '淡入 ' + a.fade_in : '', a.fade_out ? '淡出 ' + a.fade_out : '', a.normalize_lufs != null ? a.normalize_lufs + ' LUFS' : '', a.speed ? '×' + a.speed : '', '→ ' + ((r.outs || [])[0] || '')].filter(Boolean).join(' · '))}</div>`;
      case 'audio_mix': {
        const tr = a.tracks || [];
        return `<div class="pr l2">混 ${tr.length} 轨：${esc(tr.slice(0, 4).map((x) => String((x && x.file) || '').split('/').pop()).join('、'))}${tr.length > 4 ? '…' : ''}</div><div class="meta">${esc([a.duration ? (+a.duration).toFixed(1) + 's' : '', a.duck ? 'ducking' : '', a.normalize_lufs != null ? a.normalize_lufs + ' LUFS' : '', '→ ' + ((r.outs || [])[0] || '')].filter(Boolean).join(' · '))}</div>`;
      }
      case 'transition': {
        const k = Array.isArray(a.kind) ? [...new Set(a.kind)].join('/') : (a.kind || '');
        return `<div class="pr l2">转场拼接 ${(a.videos || []).length} 段 · ${esc(k)} ${a.duration ? a.duration + 's' : ''}${res.duration_s ? '，共 ' + d(res.duration_s) : ''}</div><div class="meta">→ ${esc((r.outs || [])[0] || '')}</div>`;
      }
      case 'composite': return `<div class="pr l2">${esc(a.layer)} 叠到 ${esc(a.video)}</div><div class="meta">${esc([a.position || (a.x != null ? a.x + ',' + a.y : ''), a.start != null ? (+a.start).toFixed(1) + '–' + (a.end != null ? (+a.end).toFixed(1) : '尾') + 's' : '', '→ ' + ((r.outs || [])[0] || '')].filter(Boolean).join(' · '))}</div>`;
      case 'color': return `<div class="pr l2">调色 ${esc(a.video)}</div><div class="meta">${esc(['contrast', 'saturation', 'gamma', 'temperature', 'brightness'].filter((k) => a[k] != null).map((k) => k + ' ' + a[k]).concat(a.vignette ? ['暗角'] : []).join(' · '))}</div>`;
      case 'speed': return `<div class="pr l2">${esc(a.video)} ×${esc(a.factor)}</div><div class="meta">→ ${esc((r.outs || [])[0] || '')}</div>`;
      case 'reframe': return `<div class="pr l2">${esc(a.video)} → ${a.width}×${a.height} ${esc(a.mode || '')}</div><div class="meta">→ ${esc((r.outs || [])[0] || '')}</div>`;
      case 'scene_detect': return `<div class="meta">切镜 ${esc(a.video)}${res.shots ? ' · ' + (res.shots.length || res.shots) + ' 镜' : ''}</div>`;
      case 'beat_detect': return `<div class="pr l2">${esc(a.audio)}</div><div class="meta">${esc([res.bpm || res.tempo ? 'BPM ' + Math.round(res.bpm || res.tempo) : '', Array.isArray(res.beats) ? res.beats.length + ' 拍' : ''].filter(Boolean).join(' · '))}</div>`;
      default: return `<div class="pr l2">${esc(JSON.stringify(a))}</div>`;
    }
  }
  function callCard(run, r, mini) {
    const d = mk(`card st-${r.stage} ${r.stage}${r.ok ? '' : ' fail'}${mini ? ' mini' : ''}`);
    d.style.cssText = mcol(run.slot);
    const flags = flagsOf(r);
    let html = `<div class="hd"><span class="ix">#${r.i}</span><span class="tl">${esc(TOOL[r.tool] || r.tool)}</span><span class="sp"></span>${mini ? `<span>${relT(run, r.t)}</span>` : flags.join('')}</div>`;
    if (THUMB_TOOLS.has(r.tool)) html += thumbBox(run, r);
    html += `<div class="bd">${callBody(run, r)}${mini && flags.length ? `<div class="meta">${flags.join(' ')}</div>` : ''}</div>`;
    d.innerHTML = html;
    return reg(d, `${run.slot}:c${r.i}`, run, { kind: 'call', i: r.i, text: callText(r) });
  }
  function textCard(run, e, mini) {
    const d = mk(`card st-plan text${mini ? ' mini note' : ''}`);
    d.style.cssText = mcol(run.slot);
    const body = mini ? `<div class="pr">${esc(short(e.text, 150))}</div>` : `<div class="md">${md(e.text.slice(0, 3500))}</div>`;
    d.innerHTML = `<div class="hd"><span class="ix">回复</span><span class="tl">${e.text.length} 字</span><span class="sp"></span>${mini ? `<span>${relT(run, e.t)}</span>` : ''}</div><div class="bd">${body}</div>`;
    return reg(d, `${run.slot}:e${e.n}`, run, { kind: 'event', n: e.n, text: e.text.toLowerCase() });
  }
  function thinkCard(run, e, mini) {
    const d = mk(`card st-think think${mini ? ' mini' : ''}`);
    d.style.cssText = mcol(run.slot);
    d.innerHTML = `<div class="hd"><span class="ix">思考</span><span class="tl">${e.text.length} 字</span><span class="sp"></span>${mini ? `<span>${relT(run, e.t)}</span>` : ''}</div><div class="bd">${mini ? `<div class="pr">${esc(short(e.text, 150))}</div>` : esc(e.text.slice(0, 1200))}</div>`;
    return reg(d, `${run.slot}:e${e.n}`, run, { kind: 'event', n: e.n, text: e.text.toLowerCase() });
  }
  function lostCard(run, e, mini) {
    const d = mk(`card lost${mini ? ' mini' : ''}`);
    d.style.cssText = mcol(run.slot);
    d.innerHTML = `<div class="hd"><span class="ix">未执行</span><span class="tl">${esc(TOOL[e.tool] || e.tool)}</span></div><div class="bd"><div class="pr l2">${esc(e.err || '没有对应的调用记录（被拒绝或超时）')}</div></div>`;
    return reg(d, `${run.slot}:e${e.n}`, run, { kind: 'event', n: e.n, text: (e.tool + ' ' + (e.err || '')).toLowerCase() });
  }
  function finalCard(run) {
    const f = run.final || {}, s = run.summary || {};
    const st = STATUS[s.status] || [s.status || '?', ''];
    const dur = f.dur || (s.video && s.video.duration_s);
    const d = mk('card st-deliver final');
    d.style.cssText = mcol(run.slot);
    d.innerHTML = `<div class="hd"><span class="ix">成片</span><span class="sp"></span>${chip(st[0], st[1])}</div>` +
      (f.poster ? `<div class="th">${img(f.poster)}<span class="badge">${dur ? (+dur).toFixed(1) + 's' : ''}</span></div><div class="strip"${bg(f.strip)}></div>` : `<div class="th none">${s.status === 'running' ? '还在跑，没有成片' : s.status === 'error' ? esc(short(s.error || '起跑就报错', 120)) : '没有成片'}</div>`) +
      `<div class="bd"><div class="md">${md((run.script || '').slice(0, 1500)) || '<p class="muted">没有最终说明</p>'}</div></div>`;
    return reg(d, `${run.slot}:final`, run, { kind: 'final', text: (run.script || '').toLowerCase() });
  }
  function cardFor(run, it, mini) {
    if (it.kind === 'call') return callCard(run, it.r, mini);
    if (it.kind === 'text') return textCard(run, it.e, mini);
    if (it.kind === 'think') return thinkCard(run, it.e, mini);
    if (it.kind === 'lost') return lostCard(run, it.e, mini);
    return finalCard(run);
  }

  // 泳道头：一个模型在这题上的整体情况
  function laneHead(run) {
    const cs = S.data, s = run.summary || {}, st = STATUS[s.status] || [s.status || '?', ''];
    const v = s.video || {}, target = cs.duration || [];
    const inRange = v.duration_s && target.length === 2 && v.duration_s >= target[0] && v.duration_s <= target[1];
    const calls = Object.values(run.calls);
    const cnt = {};
    for (const r of calls) cnt[r.stage] = (cnt[r.stage] || 0) + 1;
    const t2i = calls.filter((r) => r.tool === 't2i'), seed0 = t2i.filter((r) => r.seed_default).length;
    const searches = calls.filter((r) => r.tool === 'search');
    const blocked = searches.reduce((n, r) => n + ((r.res && r.res.results) || []).filter((x) => x.block).length, 0);
    const think = run.events.some((e) => e.k === 'think');
    const d = mk('lanehead');
    d.style.cssText = mcol(run.slot);
    const total = calls.length || 1;
    const segs = STAGES.filter((x) => cnt[x.key]);
    d.innerHTML = `<div class="eb">RUN · <b class="${st[1]}">${esc(st[0])}</b>${s.valid === false ? ` · <b class="warn" data-tip="${esc((s.invalid_reasons || []).join('；'))}">bench 无效</b>` : ''}</div>
      <h3>${esc(run.name)}</h3><div class="mid">${esc(run.model)}</div>
      <div class="kpi">
        <div><span>FINAL<i>成片</i></span><b class="${v.duration_s ? (inRange ? 'good' : 'warn') : ''}" data-tip="目标 ${esc(target.join('–'))} 秒">${v.duration_s ? v.duration_s.toFixed(1) : '–'}<small>s</small></b></div>
        <div><span>CALLS<i>调用</i></span><b>${calls.length}${s.tool_errors ? `<small class="crit">×${s.tool_errors}</small>` : ''}</b></div>
        <div><span>WALL<i>用时</i></span><b>${fmtWall(s.seconds)}</b></div>
        <div><span>COST<i>花费</i></span><b>${s.cost != null ? '<small>$</small>' + (+s.cost).toFixed(2) : '–'}</b></div>
      </div>
      <div class="stbar">${segs.map((x) => `<i class="st-${x.key}" style="width:${(cnt[x.key] / total) * 100}%" data-tip="${esc(x.title)} ${cnt[x.key]}"></i>`).join('')}</div>
      <div class="stagecount">${segs.map((x) => `<span class="st-${x.key}"><i></i>${esc(x.title.split(' ')[0])} ${cnt[x.key]}</span>`).join('')}</div>
      <dl class="kv"><dt>检索</dt><dd>${searches.length ? searches.length + ' 次' : '没用'}${blocked ? ' ' + chip('该挡 ' + blocked + ' 条', 'crit', '屏蔽表启用时会被挡掉的结果') : ''}</dd>
      <dt>生图 seed</dt><dd>${t2i.length ? (seed0 ? chip('默认 0：' + seed0 + '/' + t2i.length, 'warn') : '都显式传了') : '没用 t2i'}</dd>
      <dt>思考</dt><dd>${think ? '可见' : '不可见'}</dd></dl>`;
    d.dataset.key = `${run.slot}:final`;
    return d;
  }

  // ------------------------------------------------------------------ 视图：阶段看板
  function stageItems(run) {
    const by = {};
    for (const s of STAGES) by[s.key] = [];
    by.deliver.push({ kind: 'final' });
    for (const e of run.events) {
      if (e.k === 'text') by.plan.push({ kind: 'text', e });
      else if (e.k === 'think') by.think.push({ kind: 'think', e });
      else if (e.k === 'lost') (by[STAGE_OF[e.tool]] || by.cut).push({ kind: 'lost', e });
      else if (e.k === 'call') { const r = run.calls[e.i]; if (r) (by[r.stage] || by.cut).push({ kind: 'call', r }); }
    }
    return by;
  }
  function emptyNote(key, run) {
    if (key === 'research') return '没有检索';
    if (key === 'think') return run.events.some((e) => e.k === 'think') ? '没有' : '这个模型不返回思考内容';
    if (key === 'plan') return '回复里没写东西';
    return '没用到';
  }
  function band(run, x, y, w, h) {
    const b = mk('laneband');
    b.style.cssText = mcol(run.slot);
    place(b, x, y, w);
    b.style.height = h + 'px';
    world.insertBefore(b, world.firstChild);
    S.frames.push({ x, y, w, h, slot: run.slot });
  }
  function colHeads(cols, colX, y, extra, colW) {
    for (const s of cols) {
      const h = mk('colhead st-' + s.key, `<em>${esc(s.en || '')}${extra && extra[s.key] != null ? `<b>${extra[s.key]}</b>` : ''}</em>${esc(s.title)}<small>${esc(s.sub)}</small>`);
      place(h, colX[s.key] + 4, y, colW ? colW[s.key] - 8 : null);     // 折叠后列变窄，标题不许压到下一列
      if (colW) h.title = s.title + ' · ' + s.sub;
      world.appendChild(h);
    }
  }
  // 折叠：每个“模型 × 阶段”格子默认只放计数和几张小图，点开才建卡片（v1 一次建几百张卡片 + 逐格量高度，很卡）
  const itemKey = (run, it) => (it.kind === 'call' ? `${run.slot}:c${it.r.i}` : it.kind === 'final' ? `${run.slot}:final` : `${run.slot}:e${it.e.n}`);
  function itemText(it) {
    if (it.kind === 'call') return it.r._text || (it.r._text = callText(it.r));
    if (it.kind === 'final') return '';
    return it.kind === 'lost' ? (it.e.tool + ' ' + (it.e.err || '')).toLowerCase() : it.e.text.toLowerCase();
  }
  const frameKey = (run, stage) => `${S.caseId}|${run.slot}|${stage}`;
  const NO_FOLD = new Set(['deliver']);    // 交付格就是成片 + export，不折
  const isOpen = (fk, list) => list.length <= 2 || NO_FOLD.has(fk.split('|')[2]) || !S.closed.has(fk);   // 默认展开；一两张的格子不给折
  function peekHtml(run, key, list) {
    const calls = list.filter((it) => it.kind === 'call').map((it) => it.r);
    const fails = calls.filter((r) => !r.ok).length + list.filter((it) => it.kind === 'lost').length;
    const thumbs = calls.filter((r) => r.thumb || r.seen);
    let body = '';
    if (thumbs.length) {
      const pick = thumbs.length <= PEEK ? thumbs : Array.from({ length: PEEK }, (_, k) => thumbs[Math.floor((k * (thumbs.length - 1)) / (PEEK - 1))]);
      body = `<div class="peekgrid">${pick.map((r) => `<div class="pk${r.ok ? '' : ' fail'}" data-go="${run.slot}:${r.i}" data-tip="#${r.i} ${esc(TOOL[r.tool] || r.tool)}  ${esc(short(r.args.prompt || r.args.text || (r.outs || [])[0] || '', 120))}">${img(SEEN_TOOLS.has(r.tool) ? r.seen : r.thumb)}</div>`).join('')}</div>`;
    } else {
      const lines = list.filter((it) => it.kind !== 'final').slice(0, 3).map((it) => {
        if (it.kind === 'call') { const a = it.r.args || {}; return `<li data-go="${run.slot}:${it.r.i}"><b>#${it.r.i}</b> ${esc(short(a.query || a.text || a.prompt || a.url || a.audio || a.video || (it.r.outs || [])[0] || TOOL[it.r.tool] || it.r.tool, 70))}</li>`; }
        if (it.kind === 'lost') return `<li>未执行 ${esc(it.e.tool)}</li>`;
        return `<li data-go="${run.slot}:e${it.e.n}">${esc(short(it.e.text, 70))}</li>`;
      }).join('');
      body = `<ul class="peeklist">${lines}</ul>`;
    }
    const tools = {};
    for (const r of calls) tools[r.tool] = (tools[r.tool] || 0) + 1;
    const mix = Object.entries(tools).sort((a, b) => b[1] - a[1]).map(([t, n]) => `${TOOL[t] || t} ${n}`).join(' · ');
    return `${body}<div class="unfold">${mix ? `<span class="mix">${esc(mix)}</span>` : ''}${fails ? chip('失败 ' + fails, 'crit') : ''}<span class="sp"></span><b>展开 ${list.length} 张 ▾</b></div>`;
  }
  function renderStage() {
    const runs = visibleRuns();
    const cols = STAGES.filter((s) => (s.key !== 'think' || S.showThink) && (s.key !== 'qa' || S.showQA));
    const items = new Map(runs.map((r) => [r.slot, stageItems(r)]));
    const X0 = 330, GAP = 64, TOP = 130;
    const colX = {}, colN = {}, colW = {}, totals = {};
    let x = X0;
    for (const s of cols) {
      const cfg = COLCFG[s.key];
      const lists = runs.map((r) => [r, items.get(r.slot)[s.key]]);
      totals[s.key] = lists.reduce((a, [, l]) => a + l.length, 0) - (s.key === 'deliver' ? runs.length : 0);
      const open = lists.filter(([r, l]) => isOpen(frameKey(r, s.key), l)).map(([, l]) => l.length);
      colN[s.key] = clamp(Math.ceil(Math.max(1, ...open) / ROWS), cfg.min, cfg.max);
      const wOpen = open.length ? colN[s.key] * cfg.w + (colN[s.key] - 1) * 10 + 26 : 0;
      colW[s.key] = Math.max(wOpen, open.length < lists.length ? FOLD_W : 0);
      colX[s.key] = x;
      x += colW[s.key] + GAP;
    }
    colHeads(cols, colX, 50, totals, colW);
    // 先把所有格子建好一次性挂上去，再统一量高度、定位：只触发一次排版
    const lanes = [];
    const frag = document.createDocumentFragment();
    for (const run of runs) {
      const frames = [];
      for (const s of cols) {
        const list = items.get(run.slot)[s.key];
        const cfg = COLCFG[s.key], n = colN[s.key], fk = frameKey(run, s.key);
        const open = isOpen(fk, list);
        const f = mk('frame st-' + s.key + (list.length ? '' : ' empty') + (open ? '' : ' folded'));
        const count = s.key === 'deliver' ? list.length - 1 : list.length;
        const foldBtn = list.length > 2 && open && !NO_FOLD.has(s.key) ? `<span class="foldbtn" data-fold="${esc(fk)}">折叠 ▴</span>` : '';
        f.innerHTML = `<div class="fl"><b>${esc(run.name)}</b> · ${esc(s.title)} · ${count}${foldBtn}</div>`;
        for (const it of list) S.where.set(itemKey(run, it), fk);
        if (!list.length) { f.style.width = colW[s.key] + 'px'; f.appendChild(mk('none', esc(emptyNote(s.key, run)))); }
        else if (!open) {
          f.style.width = FOLD_W + 'px';
          f.dataset.fold = fk;
          f.insertAdjacentHTML('beforeend', peekHtml(run, s.key, list));
          for (const it of list) S.folded.push({ key: itemKey(run, it), frame: fk, text: itemText(it), slot: run.slot });
        } else {
          f.style.width = n * cfg.w + (n - 1) * 10 + 26 + 'px';
          const g = mk('grid');
          g.style.gridTemplateColumns = `repeat(${n}, ${cfg.w}px)`;
          for (const it of list) g.appendChild(cardFor(run, it, false));
          f.appendChild(g);
        }
        f.style.left = colX[s.key] + 'px';
        S.frameEls.set(fk, f);
        frag.appendChild(f);
        frames.push(f);
      }
      const head = laneHead(run);
      head.style.left = '24px';
      frag.appendChild(head);
      lanes.push({ run, frames, head });
    }
    world.appendChild(frag);
    const hs = lanes.map((L) => [L.head.offsetHeight, ...L.frames.map((f) => [f.offsetWidth, f.offsetHeight])]);   // 一次读完
    let y = TOP;
    lanes.forEach((L, k) => {
      const [hh, ...fs] = hs[k];
      const h = Math.max(hh, ...fs.map((z) => z[1]));
      L.head.style.top = y + 'px';
      L.frames.forEach((f, j) => {
        f.style.top = y + 'px';
        S.frames.push({ x: parseFloat(f.style.left), y, w: fs[j][0], h: fs[j][1], slot: L.run.slot });
      });
      band(L.run, 8, y - 16, x - GAP + 24, h + 32);
      y += h + 72;
    });
    for (const s of cols) {                           // 每列一条竖向导光带，代替硬边框把同阶段串起来
      const g = mk('colguide st-' + s.key);
      place(g, colX[s.key] - 18, 104, colW[s.key] + 36);
      g.style.height = y - 104 + 'px';
      world.insertBefore(g, world.firstChild);
    }
    S.bounds = { w: x, h: y };
  }
  function toggleFold(fk, open) {
    if (open ?? S.closed.has(fk)) S.closed.delete(fk); else S.closed.add(fk);
    rerender();
    updateFoldBtn();
  }
  function caseFrameKeys() {
    if (!S.data) return [];
    const out = [];
    for (const run of visibleRuns()) {
      const by = stageItems(run);
      for (const s of STAGES) if (by[s.key].length > 2 && !NO_FOLD.has(s.key)) out.push(frameKey(run, s.key));
    }
    return out;
  }
  function updateFoldBtn() {
    const b = $('foldall');
    if (!b) return;
    b.hidden = S.mode !== 'stage';
    const keys = caseFrameKeys();
    const allOpen = keys.length && keys.every((k) => !S.closed.has(k));
    b.textContent = allOpen ? '全部折叠' : '全部展开';
    b.dataset.all = allOpen ? '1' : '';
  }

  // ------------------------------------------------------------------ 视图：时间线（按事件顺序）
  function renderTimeline() {
    const runs = visibleRuns();
    const X0 = 300, W = 176, TOP = 120;
    const maxLen = Math.max(1, ...runs.map((r) => r.events.length));
    const ru = mk('ruler');
    place(ru, X0, TOP - 44, maxLen * W);
    let marks = '';
    for (let k = 0; k <= maxLen; k += 10) marks += `<i style="left:${k * W}px;height:9px"></i><span style="left:${k * W + 84}px">第 ${k + 1} 步</span>`;
    ru.innerHTML = marks;
    world.appendChild(ru);
    // 每条泳道的卡片先全部建好（只定 left），一次挂上去、一次量高度，再定 top；v1 每张卡片量一次，几百张就卡住
    const lanes = [];
    const frag = document.createDocumentFragment();
    for (const run of runs) {
      const head = laneHead(run);
      head.style.left = '24px';
      frag.appendChild(head);
      const cards = [];
      let x = X0;
      for (const e of run.events) {
        let it;
        if (e.k === 'text') it = { kind: 'text', e };
        else if (e.k === 'think') { if (!S.showThink) continue; it = { kind: 'think', e }; }
        else if (e.k === 'lost') it = { kind: 'lost', e };
        else { const r = run.calls[e.i]; if (!r || (r.stage === 'qa' && !S.showQA)) continue; it = { kind: 'call', r }; }
        const c = cardFor(run, it, true);
        c.style.position = 'absolute';
        c.style.left = x + 'px';
        frag.appendChild(c);
        cards.push(c);
        x += W;
      }
      const fc = finalCard(run);
      fc.style.position = 'absolute';
      place(fc, x + 20, 0, 300);
      frag.appendChild(fc);
      cards.push(fc);
      x += 340;
      lanes.push({ run, head, cards, x });
    }
    world.appendChild(frag);
    const hs = lanes.map((L) => Math.max(L.head.offsetHeight, ...L.cards.map((c) => c.offsetHeight)));
    let y = TOP, maxX = X0;
    lanes.forEach((L, k) => {
      L.head.style.top = y + 'px';
      for (const c of L.cards) c.style.top = y + 'px';
      band(L.run, 8, y - 16, L.x - 8 + 16, hs[k] + 32);
      maxX = Math.max(maxX, L.x);
      y += hs[k] + 64;
    });
    S.bounds = { w: maxX + 40, h: y };
  }

  // ------------------------------------------------------------------ 视图：成片分镜（成片时间轴）
  function stackRows(items, n) {
    const ends = new Array(n).fill(-1e9);
    return items.map((it) => {
      let r = ends.findIndex((e) => it.s >= e - 0.05);
      if (r < 0) r = ends.indexOf(Math.min(...ends));
      ends[r] = it.e;
      return r;
    });
  }
  function renderBoard() {
    const runs = visibleRuns(), cs = S.data;
    const PX = 26, X0 = 320, TOP = 130;
    const maxDur = Math.max(10, ...runs.map((r) => (r.board && r.board.dur) || (r.final && r.final.dur) || 0)) + 4;
    const ru = mk('ruler');
    place(ru, X0, TOP - 48, maxDur * PX);
    let marks = '';
    for (let t = 0; t <= maxDur; t += 5) marks += `<i style="left:${t * PX}px;height:${t % 10 ? 5 : 9}px"></i>${t % 10 ? '' : `<span style="left:${t * PX}px">${tc(t)}</span>`}`;
    const tg = cs.duration || [];
    if (tg.length === 2) marks += `<div style="position:absolute;left:${tg[0] * PX}px;width:${(tg[1] - tg[0]) * PX}px;top:0;height:8px;border-radius:3px;background:var(--good-bg)" data-tip="目标时长 ${tg[0]}–${tg[1]} 秒"></div>`;
    ru.innerHTML = marks;
    world.appendChild(ru);
    let y = TOP;
    const ROW = { shot: 0, sub: 160, vo: 160 + 84, mu: 160 + 84 + 84 };
    for (const run of runs) {
      const head = laneHead(run);
      place(head, 24, y);
      world.appendChild(head);
      for (const [k, label] of [['shot', '镜头'], ['sub', '字幕'], ['vo', '旁白'], ['mu', '配乐 · 音效']]) {
        const l = mk('trklabel', label);
        place(l, X0 - 76, y + ROW[k] + 6);
        world.appendChild(l);
      }
      const b = run.board;
      if (!b) {
        const n = mk('trk mu', run.summary.status === 'running' ? '还在跑，没有 export' : '没有 export，追不出成片分镜');
        place(n, X0, y + 20, 360);
        world.appendChild(n);
      } else {
        b.shots.forEach((s, k) => {
          const r = s.i != null ? run.calls[s.i] : null;
          const w = Math.max(6, (s.e - s.s) * PX - 3);
          const d = mk('shot');
          d.style.height = '146px';
          const p = r ? (r.args.prompt || '') : s.src;
          d.innerHTML = `${img(s.thumb || (r && r.thumb))}<div class="lb">${s.i != null ? '#' + s.i + ' ' + esc(TOOL[r.tool] || r.tool) : esc(s.src)} · ${(s.e - s.s).toFixed(1)}s${s.lip ? ' · 口型' : ''}${s.trim ? ' · 裁' : ''}</div><div class="pt">${esc(short(p, 140))}</div>`;
          place(d, X0 + s.s * PX, y + ROW.shot, w);
          world.appendChild(d);
          if (r) reg(d, `${run.slot}:s${k}`, run, { kind: 'call', i: s.i, text: callText(r) });
        });
        const subs = b.subs || [], sr = stackRows(subs, 2);
        subs.forEach((s, k) => {
          const d = mk('trk sub');
          d.style.height = '38px';
          d.style.whiteSpace = 'pre-line';
          d.textContent = s.t;
          d.dataset.tip = `${tc(s.s)}–${tc(s.e)}  ${s.t}`;
          place(d, X0 + s.s * PX, y + ROW.sub + sr[k] * 41, Math.max(8, (s.e - s.s) * PX - 2));
          world.appendChild(d);
          reg(d, `${run.slot}:u${k}`, run, { kind: 'call', i: s.i, text: (s.t || '').toLowerCase() });
        });
        const vo = (b.mix || []).filter((m) => m.role === 'tts').map((m) => ({ s: m.at, e: m.at + (m.dur || 3), m }));
        const vr = stackRows(vo, 2);
        vo.forEach((v, k) => {
          const d = mk('trk vo');
          d.style.cssText += ';' + mcol(run.slot) + ';height:38px';
          d.textContent = v.m.text || v.m.audio;
          d.dataset.tip = `${tc(v.s)} 起 ${(v.e - v.s).toFixed(1)}s · gain ${v.m.gain ?? 1}\n${v.m.text || ''}`;
          place(d, X0 + v.s * PX, y + ROW.vo + vr[k] * 41, Math.max(8, (v.e - v.s) * PX - 2));
          world.appendChild(d);
          if (v.m.src_i != null) reg(d, `${run.slot}:v${k}`, run, { kind: 'call', i: v.m.src_i, text: (v.m.text || '').toLowerCase() });
        });
        const mu = (b.mix || []).filter((m) => m.role === 'music' || m.role === 'sfx' || m.role === 'orig').map((m) => ({ s: m.at, e: m.at + (m.dur || 4), m }));
        const mr = stackRows(mu, 3);
        mu.forEach((v, k) => {
          const d = mk('trk mu');
          d.style.height = '24px';
          const p = v.m.src_i != null ? run.calls[v.m.src_i] : null;
          d.textContent = ({ music: '配乐 ', sfx: '音效 ', orig: '原声 ' }[v.m.role] || '') + (p && v.m.role !== 'orig' ? short(p.args.prompt, 60) : v.m.audio);
          d.dataset.tip = `${tc(v.s)} 起 · gain ${v.m.gain ?? 1}${v.m.replace ? ' · 替换原声' : ''}\n${(p && p.args.prompt) || v.m.audio}`;
          place(d, X0 + v.s * PX, y + ROW.mu + mr[k] * 27, Math.max(8, (Math.min(v.e, b.dur || v.e) - v.s) * PX - 2));
          world.appendChild(d);
          if (v.m.src_i != null) reg(d, `${run.slot}:m${k}`, run, { kind: 'call', i: v.m.src_i, text: (p ? p.args.prompt || '' : '').toLowerCase() });
        });
      }
      const h = Math.max(head.offsetHeight, ROW.mu + 3 * 27 + 10);
      band(run, 8, y - 16, X0 + maxDur * PX + 24, h + 32);
      y += h + 64;
    }
    S.bounds = { w: X0 + maxDur * PX + 60, h: y };
  }

  // ------------------------------------------------------------------ 视图：一致性
  function maxPair(mat, slots, dist) {
    let best = null;
    for (let a = 0; a < slots.length; a++) for (let b = a + 1; b < slots.length; b++) {
      const v = mat[a][b];
      if (v == null || S.hidden.has(slots[a]) || S.hidden.has(slots[b])) continue;
      if (!best || (dist ? v < best.v : v > best.v)) best = { a: slots[a], b: slots[b], v };
    }
    return best;
  }
  const pairName = (p) => `${model(p.a).name} × ${model(p.b).name}`;
  function factsBoard(cs) {
    const runs = cs.runs, M = runs.length, slots = cs.sim.slots, mats = cs.sim.matrices;
    const facts = [];
    const off = runs.filter((r) => r.summary.block_file === false).length;
    const unk = runs.filter((r) => r.summary.block_file == null).length;
    if (off) facts.push(['crit', '!', `<b>检索屏蔽表没有生效</b>：本题 ${off}/${M} 个 run 的配置里都没有 STUDIO_SEARCH_BLOCK_FILE，原片页面、视频站域名、原片标题都不会被过滤。${unk ? `另有 ${unk} 个 run 还没写 result.json。` : ''}`]);
    let total = 0, blocked = 0;
    const hosts = {}, users = [];
    for (const r of runs) {
      let n = 0;
      for (const c of Object.values(r.calls)) if (c.tool === 'search') {
        n++; total++;
        for (const x of (c.res && c.res.results) || []) if (x.block) { blocked++; hosts[x.host] = (hosts[x.host] || 0) + 1; }
      }
      if (n) users.push(r.name + ' ' + n + ' 次');
    }
    facts.push([blocked ? 'warn' : 'info', 'S', users.length
      ? `检索：${users.join('、')}，共 ${total} 次；搜回的结果里本该被挡掉的 ${blocked} 条${blocked ? '（' + Object.entries(hosts).map(([h, n]) => h + ' ' + n).join('，') + '）' : ''}。${M - users.length ? `其余 ${M - users.length} 家完全没检索，它们之间的雷同和检索无关。` : ''}`
      : '<b>这一题没有任何模型用检索</b>：各家之间的雷同不可能来自检索，只能来自 brief、模型的共同先验和同一套生成模型。']);
    const carry = runs.map((r) => [r, Object.values(r.calls).reduce((n, c) => n + (c.carry || []).length, 0)]).filter(([, n]) => n);
    if (carry.length) facts.push(['info', '→', `检索结果被写进后续产出：${carry.map(([r, n]) => r.name + ' ' + n + ' 处').join('，')}。下方检索看板里每张卡片底部的紫色标签就是被带进去的片段。`]);
    const seedUse = {};
    for (const r of runs) for (const c of Object.values(r.calls)) if (c.tool === 't2i' || c.tool === 'image_edit') {
      const s = effSeed(c);
      seedUse[s] = seedUse[s] || {};
      seedUse[s][r.slot] = (seedUse[s][r.slot] || 0) + 1;
    }
    const shared = Object.entries(seedUse).filter(([, m]) => Object.keys(m).length >= 2).sort((a, b) => Object.keys(b[1]).length - Object.keys(a[1]).length);
    const def = runs.map((r) => { const t = Object.values(r.calls).filter((c) => c.tool === 't2i'); return [r, t.filter((c) => c.seed_default).length, t.length]; }).filter(([, n]) => n);
    if (def.length || shared.length) facts.push(['warn', '0', `生图 seed 撞车：${def.length ? '没传 seed（默认 0）的 t2i —— ' + def.map(([r, n, t]) => `${r.name} ${n}/${t}`).join('，') + '。' : ''}${shared.length ? '不同模型用了同一个 seed —— ' + shared.slice(0, 6).map(([s, m]) => `seed ${s} × ${Object.keys(m).length} 家`).join('，') + '。' : ''}但同 seed 不等于同图：prompt 一变，构图就分开了，看下面的构图距离。`]);
    const bi = maxPair(mats.image, slots, true);
    if (bi) facts.push([bi.v <= IDX.near ? 'warn' : 'ok', '≈', `出图构图最接近的一对：${pairName(bi)}，平均最近 pHash 距离 ${bi.v.toFixed(1)}（≤${IDX.near} 才算近似重复，互不相干约 30）。${(cs.sim.dup_pairs || []).some((p) => p[4] <= IDX.near) ? '有近似重复的画面，见下方画廊。' : '<b>没有任何一对跨模型的图达到近似重复</b>，雷同不是像素级的。'}`]);
    const bf = maxPair(mats.final, slots, true);
    if (bf) facts.push(['info', '▶', `成片抽帧最接近：${pairName(bf)}，平均最近距离 ${bf.v.toFixed(1)}。`]);
    const bp = maxPair(mats.prompt, slots, false);
    if (bp) facts.push(['info', 'P', `prompt 文本最像：${pairName(bp)}（平均最近邻余弦 ${bp.v.toFixed(2)}）。`]);
    const strong = (cs.leaks || []).filter((l) => l.tier === 'strong');
    if (strong.length) {
      const via = strong.filter((l) => Object.values(l.by).some((b) => b.via_search));
      facts.push([via.length ? 'crit' : 'warn', '原', `原片转写里的字幕、角标、专名被写出来 ${strong.length} 条${via.length ? `，其中 ${via.length} 条在该模型自己的检索结果里出现过` : '，都不在检索结果里'}：${strong.slice(0, 8).map((l) => '“' + esc(l.p) + '”').join('、')}。逐条看下方表格。`]);
    } else facts.push(['ok', '原', '原片转写里的字幕、角标、专名没有被任何模型写出来。']);
    const d = mk('board');
    d.innerHTML = `<h3>结论速览 · ${esc(cs.name)}</h3><p class="lead">由 build.py 从 transcript、调用日志和缩略图算出来；点任意格子、圆点、卡片可以打开对应的那一步。</p><ul class="facts">${facts.map(([c, ic, t]) => `<li class="${c}"><span class="ic">${ic}</span><span>${t}</span></li>`).join('')}</ul>`;
    return d;
  }
  function heatmap(cs, [key, title, lead, vmax, fmt, dist]) {
    const slots = cs.sim.slots, mat = cs.sim.matrices[key];
    const vis = slots.map((_, k) => k).filter((k) => !S.hidden.has(slots[k]));
    const dark = isDark();
    let html = `<div style="min-width:0"><h4 style="margin:0 0 2px;font:600 17px/1.2 var(--f-display)">${esc(title)}</h4><p class="help" style="margin:0 0 8px;max-width:${96 + vis.length * 52}px">${esc(lead)}</p><div class="hm" style="grid-template-columns:76px repeat(${vis.length},50px)"><div></div>`;
    for (const k of vis) html += `<div class="cl" style="${mcol(slots[k])}" title="${esc(model(slots[k]).name)}"><i></i>${esc(abbr(slots[k]).slice(0, 6))}</div>`;
    for (const a of vis) {
      html += `<div class="rl" style="${mcol(slots[a])}"><i></i>${esc(abbr(slots[a]))}</div>`;
      for (const b of vis) {
        if (a === b) { html += '<div class="c diag"></div>'; continue; }
        const v = mat[a][b];
        const nm = model(slots[a]).name + ' × ' + model(slots[b]).name;
        if (v == null) { html += `<div class="c na" data-tip="${esc(nm + '：没有可比的数据')}">–</div>`; continue; }
        const x = dist ? 1 - clamp((v - 10) / (vmax - 10), 0, 1) : clamp(v / vmax, 0, 1);
        const step = clamp(Math.round(x * 7), 0, 7);
        const inv = dark ? step >= 5 : step >= 4;
        const sel = S.pairFilter && key === 'image' && ((S.pairFilter[0] === slots[a] && S.pairFilter[1] === slots[b]) || (S.pairFilter[1] === slots[a] && S.pairFilter[0] === slots[b]));
        html += `<div class="c${sel ? ' on' : ''}" data-pair="${key}:${slots[a]}:${slots[b]}" data-tip="${esc(nm + '：' + fmt(v) + (key === 'image' ? '（点击只看这一对的画面）' : ''))}" style="background:var(--seq-${step});color:${inv ? 'var(--surface)' : 'var(--ink)'}">${fmt(v)}</div>`;
      }
    }
    const lo = dist ? `≥${vmax}` : '0', hi = dist ? '≤10' : fmt(vmax);
    html += `</div><div class="scale">${lo}${[0, 1, 2, 3, 4, 5, 6, 7].map((s) => `<span style="background:var(--seq-${s})"></span>`).join('')}${hi}<span style="width:auto;background:none">${dist ? '越深越像' : '越深越像'}</span></div></div>`;
    return html;
  }
  function pairCard(p) {
    const [sa, ia, sb, ib, d] = p;
    const ra = S.data.runBySlot[sa], rb = S.data.runBySlot[sb];
    if (!ra || !rb) return '';
    const A = ra.calls[ia], B = rb.calls[ib];
    if (!A || !B) return '';
    const s1 = effSeed(A), s2 = effSeed(B);
    const nn = (A.nn || []).find((x) => x[0] === sb && x[1] === ib);
    const side = (run, r, s) => `<div data-go="${run.slot}:${r.i}"><img loading="lazy" src="${esc(r.thumb)}" alt=""><div class="cap" style="${mcol(run.slot)}"><i></i>${esc(abbr(run.slot))} #${r.i} ${esc(TOOL[r.tool] || r.tool)}${s != null ? ' · seed ' + s : ''}</div></div>`;
    return `<div class="pair"><div class="two">${side(ra, A, s1)}${side(rb, B, s2)}</div><div class="ft">${chip('距离 ' + d, d <= IDX.near ? 'crit' : '')}${s1 != null && s1 === s2 ? chip('同 seed ' + s1, 'warn') : ''}${nn ? chip('prompt 余弦 ' + nn[2].toFixed(2), 'acc') : ''}</div></div>`;
  }
  function pairsBoard(cs) {
    let pairs = (cs.sim.dup_pairs || []).filter((p) => !S.hidden.has(p[0]) && !S.hidden.has(p[2]));
    const f = S.pairFilter;
    if (f) pairs = pairs.filter((p) => (p[0] === f[0] && p[2] === f[1]) || (p[0] === f[1] && p[2] === f[0]));
    const shown = pairs.slice(0, S.pairLimit);
    const d = mk('board');
    d.innerHTML = `<h3>跨模型最接近的画面</h3><p class="lead">每对模型取 pHash 距离最小的几对生图 / 视频封面。距离 ≤ ${IDX.near} 才算近似重复（红色）；互不相干的图约 30。${f ? `当前只看 ${esc(model(f[0]).name)} × ${esc(model(f[1]).name)}。` : '点上方“出图构图”矩阵的格子可以只看某一对。'}</p>
      ${f ? '<div class="sq" data-act="clear-pairs" style="display:inline-block;margin-bottom:10px">看全部模型对</div>' : ''}
      <div class="pairs">${shown.map(pairCard).join('') || '<p class="muted">没有可比的图</p>'}</div>
      ${pairs.length > shown.length ? `<div class="sq" data-act="more-pairs" style="text-align:center;margin-top:10px">再显示 ${Math.min(24, pairs.length - shown.length)} 对（还有 ${pairs.length - shown.length}）</div>` : ''}`;
    return d;
  }
  function refOf(slot, ref) {
    if (!ref) return `${slot}:final`;
    if (ref.i != null) return `${slot}:${ref.i}`;
    if (ref.script) return `${slot}:final`;
    return `${slot}:e${ref.n}`;
  }
  function leaksBoard(cs) {
    const runs = cs.runs.filter((r) => !S.hidden.has(r.slot));
    const L = cs.leaks || [];
    const strong = L.filter((x) => x.tier === 'strong'), weak = L.filter((x) => x.tier !== 'strong');
    const row = (l) => `<tr><td class="p">${esc(l.p)}</td><td>${chip(KIND[l.kind] || l.kind)}</td><td class="ctx">${esc(l.orig_ctx)}</td>${runs.map((r) => {
      const b = l.by[r.slot];
      return `<td>${b ? `<span class="dotm${b.via_search ? ' via' : ''}" style="${mcol(r.slot)}" data-go="${refOf(r.slot, b.ref)}" data-tip="${esc(r.name + ' · ' + (SRC[b.src] || b.src) + (b.via_search ? '（自己的检索结果里出现过）' : '') + '\n' + (b.ctx || ''))}"></span>` : ''}</td>`;
    }).join('')}</tr>`;
    const head = `<tr><th>片段</th><th>类型</th><th>原片转写里的上下文</th>${runs.map((r) => `<th style="${mcol(r.slot)}" title="${esc(r.name)}"><span class="dotm" style="width:12px;height:12px;cursor:default"></span></th>`).join('')}</tr>`;
    const orig = cs.orig || {};
    const d = mk('board');
    d.innerHTML = `<h3>原片独有细节</h3><p class="lead">原片转写（full_prompt）里有、而 brief、系统提示和其他题目都没有的片段，哪些模型写了出来。圆点带红圈 = 这个片段在该模型<b>自己的检索结果</b>里出现过。“强”是原片引号里的字幕、角标原文和专名；“弱”是普通中文片段，噪声多，只作参考。</p>
      <p class="help">原片：${esc(orig.title || '（没有转写）')}${orig.url ? ` · <a href="${esc(orig.url)}" target="_blank" rel="noopener">${esc(orig.url)}</a>` : ''}</p>
      <div class="tablewrap"><table class="ltab"><thead>${head}</thead><tbody>${strong.map(row).join('') || `<tr><td colspan="${3 + runs.length}" class="muted">没有强线索</td></tr>`}</tbody></table></div>
      <div class="sq" data-act="toggle-weak" style="display:inline-block;margin-top:10px">${S.showWeak ? '收起' : '展开'}弱线索（${weak.length} 条）</div>
      ${S.showWeak ? `<div class="tablewrap" style="margin-top:8px"><table class="ltab"><thead>${head}</thead><tbody>${weak.map(row).join('')}</tbody></table></div>` : ''}`;
    return d;
  }
  function searchBoard(cs) {
    const runs = cs.runs.filter((r) => !S.hidden.has(r.slot));
    const d = mk('board');
    const cols = runs.map((run) => {
      const ss = Object.values(run.calls).filter((r) => r.tool === 'search').sort((a, b) => a.i - b.i);
      return `<div class="scol" style="${mcol(run.slot)}"><h4><i></i>${esc(run.name)} · ${ss.length} 次</h4>${ss.map((r) => `<div class="sq" data-go="${run.slot}:${r.i}"><div class="hd" style="padding:0 0 4px;display:flex;gap:6px;font-size:11px;color:var(--ink-3)"><span class="ix" style="font-family:var(--f-mono)">#${r.i}</span>${esc(r.backend || '')}</div>${searchBody(run, r)}</div>`).join('') || '<p class="muted">没有检索</p>'}</div>`;
    });
    d.innerHTML = `<h3>检索看板</h3><p class="lead">每家查了什么、搜回什么。红色域名 = 屏蔽表启用时会被挡掉；“n 家都搜到”= 同一条 URL 返回给了多个模型；紫色标签 = 这条结果里的片段后来被写进了该模型的产出。</p><div style="display:grid;grid-template-columns:repeat(${runs.length},320px);gap:14px;align-items:start">${cols.join('')}</div>`;
    return d;
  }
  function renderSim() {
    const cs = S.data;
    const X0 = 40;
    let y = 110;
    const add = (d, x, w) => { place(d, x, y, w); world.appendChild(d); S.frames.push({ x, y, w: d.offsetWidth, h: d.offsetHeight, slot: null }); return d; };
    const facts = add(factsBoard(cs), X0, 900);
    const hm = mk('board');
    hm.innerHTML = `<h3>相似度矩阵</h3><p class="lead">深色 = 更像。出图和成片两项是距离，另外三项是相似度。</p><div style="display:flex;flex-wrap:wrap;gap:28px">${METRICS.map((m) => heatmap(cs, m)).join('')}</div>`;
    place(hm, X0 + 940, y, 1100);
    world.appendChild(hm);
    S.frames.push({ x: X0 + 940, y, w: hm.offsetWidth, h: hm.offsetHeight, slot: null });
    y += Math.max(facts.offsetHeight, hm.offsetHeight) + 40;
    const sw = Math.max(2040, visibleRuns().length * 334 + 40);
    const sb = add(searchBoard(cs), X0, sw);                 // 先看检索，再看原片细节，最后是画面对照
    y += sb.offsetHeight + 40;
    const lb = add(leaksBoard(cs), X0, 2040);
    y += lb.offsetHeight + 40;
    const pb = add(pairsBoard(cs), X0, 2040);
    y += pb.offsetHeight + 60;
    S.bounds = { w: X0 + sw + 40, h: y };
  }

  // ------------------------------------------------------------------ 视图：总览胶片墙
  function renderOverview() {
    const X0 = 40;
    let y = 16;
    const W = 200 + 300 + 1536 + 28 + 42;
    // 顶部指标条：这一批交卷的整体情况
    const shown = shownCases();
    const rows = shown.flatMap((cs) => cs.rows.filter((r) => !S.hidden.has(r.slot)).map((r) => [cs, r]));
    const sum = (f) => rows.reduce((a, [cs, r]) => a + (+f(cs, r) || 0), 0);
    const durs = rows.map(([, r]) => (r.summary.video || {}).duration_s).filter(Boolean);
    const inRange = rows.filter(([cs, r]) => { const t = cs.duration || [], v = (r.summary.video || {}).duration_s; return v && t.length === 2 && v >= t[0] && v <= t[1]; }).length;
    const calls = sum((cs, r) => r.summary.tool_calls), errs = sum((cs, r) => r.summary.tool_errors);
    const kpis = [
      ['DELIVERED', '交卷', `${rows.length}`, `${shown.length} 道题 · ${IDX.models.length} 个模型`],
      ['ON TARGET', '时长达标', `${inRange}<small>/ ${rows.length}</small>`, '成片时长落在题目要求区间'],
      ['AVG FINAL', '平均成片', `${durs.length ? (durs.reduce((a, b) => a + b, 0) / durs.length).toFixed(1) : '–'}<small>s</small>`, durs.length ? `最短 ${Math.min(...durs).toFixed(1)}s · 最长 ${Math.max(...durs).toFixed(1)}s` : ''],
      ['TOOL CALLS', '工具调用', `${calls}`, `失败 ${errs} · 平均 ${rows.length ? Math.round(calls / rows.length) : 0} / run`],
      ['WALL', '总用时', fmtWall(sum((cs, r) => r.summary.seconds)), `平均 ${fmtWall(rows.length ? sum((cs, r) => r.summary.seconds) / rows.length : null)} / run`],
      ['COST', '总花费', `<small>$</small>${sum((cs, r) => r.summary.cost).toFixed(2)}`, `平均 $${rows.length ? (sum((cs, r) => r.summary.cost) / rows.length).toFixed(2) : '–'} / run`],
    ];
    const strip = mk('kpistrip', kpis.map(([en, zh, v, sub], k) => `<div class="${k ? '' : 'big'}"><span>${en}<i>${zh}</i></span><b>${v}</b><em>${esc(sub)}</em></div>`).join(''));
    place(strip, X0, y, W);
    world.appendChild(strip);
    y += 128;
    const els = [];
    if (!shown.length) {
      const d = mk('board', `<h3>这个筛选下还没有交卷的题</h3><p class="lead">换一个领域或题型，或点「全部」。跑批里新的有效交卷会自动同步进来。</p>`);
      place(d, X0, y, 600);
      world.appendChild(d);
      S.bounds = { w: X0 + 640, h: y + 200 };
      return;
    }
    for (const cs of shown) {
      const t = cs.duration || [];
      let html = `<div class="eb">CASE · <b>${esc(caseNo(cs.id))}</b> · ${esc(String(cs.domain || '').toUpperCase())} · ${esc(String(cs.family || '').toUpperCase())} · 目标 ${esc(t.join('–'))} 秒${cs.leaks ? ` · <b class="warn">原片细节 ${cs.leaks}</b>` : ''}</div>
        <h2>${esc(cs.name)}</h2><p class="brief">${esc(cs.brief)}</p>`;
      for (const row of cs.rows) {
        if (S.hidden.has(row.slot)) continue;
        const s = row.summary || {}, v = s.video || {}, st = STATUS[s.status] || [s.status || '?', ''];
        const ok = v.duration_s && t.length === 2 && v.duration_s >= t[0] && v.duration_s <= t[1];
        const flags = [row.searches ? chip('检索 ' + row.searches + (row.blocked ? ' · 该挡 ' + row.blocked : ''), row.blocked ? 'crit' : 'acc') : chip('没检索'),
          row.seed0 ? chip('seed0 ' + row.seed0 + '/' + row.t2i, 'warn', '没传 seed 的 t2i') : '', s.tool_errors ? chip('失败 ' + s.tool_errors, 'crit') : ''].join('');
        const kp = `<div class="mk"><div><span>FINAL</span><b class="${ok ? 'good' : 'warn'}" data-tip="目标 ${esc(t.join('–'))} 秒">${v.duration_s ? v.duration_s.toFixed(1) + '<small>s</small>' : '–'}</b></div><div><span>CALLS</span><b>${s.tool_calls ?? '–'}</b></div><div><span>WALL</span><b>${fmtWall(s.seconds)}</b></div><div><span>COST</span><b>${s.cost != null ? '<small>$</small>' + (+s.cost).toFixed(2) : '–'}</b></div></div>`;
        const film = row.final && row.final.strip ? `<div class="film"${bg(row.final.strip)}></div>` : `<div class="film none">${s.status === 'running' ? '还在跑' : s.status === 'error' ? esc(short(s.error || '起跑就报错', 90)) : '没有成片'}</div>`;
        html += `<div class="ovrow" data-open="${esc(cs.id)}:${row.slot}" style="${mcol(row.slot)}"><div class="nm"><i></i><span>${esc(model(row.slot).name)}<em>${chip(st[0], st[1])}</em></span></div><div class="st">${kp}<div class="fl">${flags}</div></div>${film}</div>`;
      }
      const d = mk('ovcase', html);
      d.style.left = X0 + 'px';
      d.style.width = W + 'px';
      world.appendChild(d);
      els.push(d);
    }
    const hs = els.map((d) => d.offsetHeight);           // 一次量完
    els.forEach((d, k) => {
      d.style.top = y + 'px';
      S.frames.push({ x: X0, y, w: W, h: hs[k], slot: null });
      y += hs[k] + 28;
    });
    S.bounds = { w: X0 + W + 40, h: y };
  }

  // ------------------------------------------------------------------ 血缘连线（选中卡片时画）
  // v1 一律从上游卡片右边连到下游卡片左边；同一列里的（约 38%）和往左回流的就会绕错。这里按相对位置选边：
  // 前向：右 → 左；回流：左出、进右边（虚线）；同一列：从右侧绕出去再回来。下游卡片在折叠格子里时连到格子。
  function endpoint(key) {
    if (S.cards.has(key)) return { r: cardRect(key), card: true };
    const fk = S.where.get(key), f = fk && S.frameEls.get(fk);
    return f ? { r: frameRect(fk), card: false } : null;
  }
  function edgeRoute(a, b) {
    const A = a.r, B = b.r;
    const ya = A.y + (a.card ? Math.min(A.h / 2, 70) : 18), yb = B.y + (b.card ? Math.min(B.h / 2, 70) : 18);
    if (B.x >= A.x + A.w + 6) {
      const x1 = A.x + A.w, x2 = B.x, dx = Math.max(50, (x2 - x1) / 2);
      return { d: `M${x1},${ya} C${x1 + dx},${ya} ${x2 - dx},${yb} ${x2},${yb}`, back: false };
    }
    if (A.x >= B.x + B.w + 6) {
      const x1 = A.x, x2 = B.x + B.w, dx = Math.max(50, (x1 - x2) / 2);
      return { d: `M${x1},${ya} C${x1 - dx},${ya} ${x2 + dx},${yb} ${x2},${yb}`, back: true };
    }
    const x1 = A.x + A.w, x2 = B.x + B.w, xm = Math.max(x1, x2) + 28 + Math.min(120, Math.abs(yb - ya) * 0.22);
    return { d: `M${x1},${ya} C${xm},${ya} ${xm},${yb} ${x2},${yb}`, back: false };
  }
  function drawEdges(run, edges) {
    let svg = world.querySelector('#edges');
    if (!svg) {
      svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.id = 'edges';
      world.appendChild(svg);
    }
    svg.setAttribute('width', S.bounds.w);
    svg.setAttribute('height', S.bounds.h);
    const seen = new Set();
    let html = '';
    for (const [a, b, dir] of edges) {
      const k = a + '>' + b;
      if (seen.has(k)) continue;
      seen.add(k);
      const ea = endpoint(`${run.slot}:c${a}`), eb = endpoint(`${run.slot}:c${b}`);
      if (!ea || !eb || (!ea.card && !eb.card && ea.r.x === eb.r.x && ea.r.y === eb.r.y)) continue;
      const g = edgeRoute(ea, eb);
      html += `<path class="${dir}${g.back ? ' back' : ''}" style="stroke:var(--m${run.slot % 8})" d="${g.d}"/>`;
    }
    svg.innerHTML = html;
  }

  // ------------------------------------------------------------------ 选择、血缘连线
  function clearSel() {
    for (const e of world.querySelectorAll('.sel, .anc, .des')) e.classList.remove('sel', 'anc', 'des');
    world.classList.remove('focus');
    const svg = world.querySelector('#edges');
    if (svg) svg.innerHTML = '';
    S.sel = null;
  }
  function lineage(run, i) {
    const anc = new Set(), des = new Set(), edges = [];
    const up = [i];
    while (up.length) {
      const j = up.pop();
      for (const x of (run.calls[j] && run.calls[j].ins) || []) if (x.from != null) {
        edges.push([x.from, j, 'up']);
        if (!anc.has(x.from)) { anc.add(x.from); up.push(x.from); }
      }
    }
    const dn = [i];
    while (dn.length && des.size < 400) {
      const j = dn.pop();
      for (const k of run._cons[j] || []) {
        edges.push([j, k, 'dn']);
        if (!des.has(k)) { des.add(k); dn.push(k); }
      }
    }
    return { anc, des, edges };
  }
  function select(key, opts = {}) {
    clearSel();
    const c = S.cards.get(key);
    if (!c) return;
    S.sel = key;
    c.el.classList.add('sel');
    if (c.kind === 'call' && (S.mode === 'stage' || S.mode === 'timeline')) {
      const L = lineage(c.run, c.i);
      const mark = (j, cls) => {
        const k = `${c.run.slot}:c${j}`, x = S.cards.get(k);
        if (x) { x.el.classList.add(cls); return; }
        const f = S.frameEls.get(S.where.get(k));          // 在折叠的格子里：给格子描边
        if (f) f.classList.add(cls);
      };
      for (const j of L.anc) mark(j, 'anc');
      for (const j of L.des) mark(j, 'des');
      if (L.anc.size || L.des.size) world.classList.add('focus');
      drawEdges(c.run, L.edges);
    }
    if (opts.open !== false) openFor(c.run, c);
    if (opts.zoom) zoomToKey(key);
  }
  function openFor(run, c) {
    if (c.kind === 'call' && c.i != null) callPanel(run, c.i);
    else if (c.kind === 'event') eventPanel(run, c.n);
    else finalPanel(run);
  }
  function goRef(ref) {
    if (!S.data) return;
    const [slot, what] = ref.split(':');
    const run = S.data.runBySlot[+slot];
    if (!run) return;
    const key = /^\d+$/.test(what) ? `${slot}:c${what}` : `${slot}:${what}`;
    if (S.cards.has(key) && (S.mode === 'stage' || S.mode === 'timeline')) { select(key, { zoom: true }); return; }
    if (/^\d+$/.test(what)) callPanel(run, +what);
    else if (what === 'final') finalPanel(run);
    else if (what[0] === 'e') eventPanel(run, +what.slice(1));
    S.panelKey = key;
  }
  function focusFinal(slot) {
    const key = `${slot}:final`;
    if (S.cards.has(key)) select(key, { zoom: true });
  }
  // 卡片在折叠的格子里时，展开那一格再返回 true（阶段看板）
  function ensureCard(key) {
    if (S.cards.has(key)) return true;
    const fk = S.mode === 'stage' && S.where.get(key);
    if (!fk) return false;
    S.closed.delete(fk);
    rerender();
    updateFoldBtn();
    return S.cards.has(key);
  }

  // ------------------------------------------------------------------ 详情抽屉
  function sec(title, body, copyText) {
    let btn = '';
    if (copyText != null) { S.copy.push(copyText); btn = ` <button type="button" class="copy" data-copy="${S.copy.length - 1}">复制</button>`; }
    return `<div class="sec"><h5>${title}${btn}</h5>${body}</div>`;
  }
  function openPanel(title, html, key) {
    $('ptitle').textContent = title;
    $('pbody').innerHTML = html;
    $('pbody').querySelectorAll('[data-src], [data-bg]').forEach(loadEl);   // 抽屉里的图直接加载
    $('pbody').scrollTop = 0;
    panel.hidden = false;
    document.body.classList.add('panel-open');
    S.panelKey = key;
    scheduleMinimap();
  }
  function closePanel() {
    if (panel.hidden) return;
    for (const m of panel.querySelectorAll('video, audio')) m.pause();
    panel.hidden = true;
    document.body.classList.remove('panel-open');
    S.copy = [];
    scheduleMinimap();
  }
  function linkItem(run, i, note) {
    const r = run.calls[i];
    if (!r) return '';
    const th = r.thumb || r.seen || '';
    const txt = r.args.prompt || r.args.text || r.args.query || r.args.url || (r.outs || [])[0] || '';
    return `<div class="lk" data-go="${run.slot}:${i}">${th ? img(th) : '<span></span>'}<div class="tx"><div>${mchip(run.slot, abbr(run.slot))} <b>#${i}</b> ${esc(TOOL[r.tool] || r.tool)} ${note || ''}</div><div class="pr">${esc(short(txt, 160))}</div></div></div>`;
  }
  const MAIN_ARGS = ['prompt', 'text', 'query', 'url', 'lyrics', 'instruct', 'negative_prompt', 'srt_text', 'subtitles', 'html', 'code', 'script'];
  const ARG_LABEL = { prompt: 'prompt', text: '台词 / 旁白', query: '检索词', url: 'URL', lyrics: '歌词', instruct: '语气说明', negative_prompt: '负向 prompt', srt_text: 'SRT' };
  const CODE_ARGS = { html: 'HTML 源码', code: '渲染代码', script: '渲染脚本' };
  function callPanel(run, i) {
    const r = run.calls[i];
    if (!r) return;
    S.copy = [];
    const a = r.args || {};
    const out = [];
    out.push(`<div class="row">${mchip(run.slot, run.name)}${chip(r.ok ? '成功' : '失败', r.ok ? 'good' : 'crit')}${r.backend ? chip(r.backend) : ''}${r.sec != null ? chip(r.sec + ' 秒') : ''}${r.t ? chip(relT(run, r.t) + ' 开始') : ''}${r.instance ? chip('实例 ' + r.instance) : ''}${r.queued ? chip('排队 ' + r.queued + ' 秒', r.queued > 60 ? 'warn' : '') : ''}${r.batch ? chip('batch 并行', 'acc') : ''}${r.seed_default ? chip('seed 0（没传，默认）', 'warn') : a.seed != null ? chip('seed ' + a.seed) : ''}</div>`);
    const rel = (r.outs || [])[0] || '';
    const ext = rel.split('.').pop().toLowerCase();
    let media = '';
    if (SEEN_TOOLS.has(r.tool)) {
      if (r.seen) media = `<div class="mediabox"><img src="${esc(r.seen)}" alt=""></div><p class="help">模型这次看到的联系表（从 transcript 里还原）</p>`;
    } else if (r.over) {
      media = (r.thumb ? `<div class="mediabox"><img src="${esc(r.thumb)}" alt=""></div><p class="help">模型在 #${r.thumb_from} 看帧时看到的画面。</p>` : '') +
        `<p class="help">这个文件后来被 #${r.over} 重写，磁盘上是新版本${r.media ? `：<a href="${esc(r.media)}" target="_blank" rel="noopener">打开磁盘上的文件</a>` : ''}。</p>`;
    } else if (r.media) {
      if (['png', 'jpg', 'jpeg', 'webp', 'bmp'].includes(ext)) media = `<div class="mediabox"><a href="${esc(r.media)}" target="_blank" rel="noopener"><img src="${esc(r.media)}" alt=""></a></div>`;
      else if (['mp4', 'mov', 'webm', 'mkv'].includes(ext)) media = `<div class="mediabox"><video controls preload="metadata" src="${esc(r.media)}"${r.thumb ? ` poster="${esc(r.thumb)}"` : ''}></video></div>`;
      else if (['wav', 'mp3', 'flac', 'm4a', 'aac', 'ogg'].includes(ext)) media = `<audio controls preload="none" src="${esc(r.media)}"></audio>`;
    }
    if (media) out.push(sec('产物', media));
    for (const k of MAIN_ARGS) {
      if (typeof a[k] !== 'string' || !a[k]) continue;
      if (CODE_ARGS[k]) { S.copy.push(a[k]); out.push(`<details class="sec"><summary>${CODE_ARGS[k]} · ${a[k].length} 字 <button type="button" class="copy" data-copy="${S.copy.length - 1}">复制</button></summary><pre class="json">${esc(a[k])}</pre></details>`); }
      else out.push(sec(ARG_LABEL[k] || k, `<div class="box">${esc(a[k])}</div>`, a[k]));
    }
    if (Array.isArray(a.tracks) && a.tracks.length) {
      const rows = a.tracks.map((t) => `<tr><td style="white-space:nowrap;font-family:var(--f-mono)">@${(+(t && t.start) || 0).toFixed(2)}s</td><td>${esc((t && t.file) || '')}</td><td style="font-family:var(--f-mono)">${esc(Object.entries(t || {}).filter(([k]) => k !== 'file' && k !== 'start').map(([k, v]) => k + ' ' + v).join(' · '))}</td></tr>`).join('');
      out.push(sec(`分轨 ${a.tracks.length} 条`, `<div class="tablewrap"><table class="ltab"><tbody>${rows}</tbody></table></div>`));
    }
    if (Array.isArray(a.subtitles) && a.subtitles.length) {
      const rows = a.subtitles.map((s) => `<tr><td style="white-space:nowrap;font-family:var(--f-mono)">${(+s.start || 0).toFixed(1)}–${(+s.end || 0).toFixed(1)}</td><td style="white-space:pre-line">${esc(String(s.text || '').replace(/\{[^}]*\}/g, ''))}</td></tr>`).join('');
      out.push(sec(`字幕 ${a.subtitles.length} 条`, `<div class="tablewrap"><table class="ltab"><tbody>${rows}</tbody></table></div>`));
    }
    const rest = Object.entries(a).filter(([k]) => !MAIN_ARGS.includes(k) && k !== 'tracks');
    if (rest.length) out.push(sec('其它参数', `<dl class="kvt">${rest.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'string' ? v : JSON.stringify(v))}</dd>`).join('')}</dl>`));
    if (r.tool === 'search') {
      const rows = ((r.res && r.res.results) || []).map((x) => `<li style="display:block;margin-bottom:8px">${chip(x.host || '?', x.block ? 'crit' : '', x.block ? '屏蔽表启用时会被挡掉（' + x.block + '）' : '')} <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a><div class="help" style="margin-top:2px">${esc(x.snippet)}</div></li>`).join('');
      out.push(sec('搜回给模型的结果', `<ul class="res" style="display:block">${rows || '<li class="muted">没有结果</li>'}</ul>`));
      if (r.carry && r.carry.length) out.push(sec('被写进后续产出的片段', `<div class="links">${r.carry.map((c) => `<div class="lk" data-go="${refOf(run.slot, c.ref)}" style="grid-template-columns:1fr"><div class="tx"><div><b>${esc(c.p)}</b> → ${esc(SRC[c.src] || c.src || '')}${c.ref && c.ref.i != null ? ' #' + c.ref.i : ''}</div><div class="pr">${esc(c.ctx || '')}</div></div></div>`).join('')}</div>`));
    }
    const ins = (r.ins || []).filter((x) => x.from != null);
    if (ins.length) out.push(sec('输入来自', `<div class="links">${ins.map((x) => linkItem(run, x.from, chip(x.arg))).join('')}</div>`));
    const raw = (r.ins || []).filter((x) => x.from == null);
    if (raw.length) out.push(sec('不是工具产出的输入', `<div class="box">${raw.map((x) => esc(x.arg + ': ' + x.path)).join('\n')}</div>`));
    const cons = run._cons[i] || [];
    if (cons.length) out.push(sec(`后续用到它的调用（${cons.length}）`, `<div class="links">${cons.slice(0, 30).map((j) => linkItem(run, j)).join('')}</div>`));
    if (r.dup && r.dup.length) out.push(sec(`其他模型里画面近似重复的（pHash ≤ ${IDX.near}）`, `<div class="links">${r.dup.map(([s, j, d]) => { const o = S.data.runBySlot[s]; return o ? linkItem(o, j, chip('距离 ' + d, 'crit')) : ''; }).join('')}</div>`));
    if (r.nn && r.nn.length) out.push(sec('其他模型里最像的 prompt（TF-IDF 余弦）', `<div class="links">${r.nn.map(([s, j, v]) => { const o = S.data.runBySlot[s]; return o ? linkItem(o, j, chip(v.toFixed(2), 'acc')) : ''; }).join('')}</div>`));
    if (r.err) out.push(sec('错误', `<div class="box">${esc(r.err)}</div>`));
    if (r.res) out.push(`<details><summary>工具返回（JSON）</summary><pre class="json">${esc(JSON.stringify(r.res, null, 2))}</pre></details>`);
    openPanel(`${run.name} · #${i} ${TOOL[r.tool] || r.tool}`, out.join(''), `${run.slot}:c${i}`);
  }
  function eventPanel(run, n) {
    const e = run._ev[n];
    if (!e) return;
    S.copy = [];
    let prev = null;
    for (const x of run.events) { if (x.n >= n) break; if (x.k === 'call') prev = x.i; }
    const where = `<div class="row">${mchip(run.slot, run.name)}${chip('第 ' + (n + 1) + ' 个事件')}${prev != null ? chip('在 #' + prev + ' 之后') : chip('第一次调用之前')}${e.t ? chip(relT(run, e.t)) : ''}</div>`;
    if (e.k === 'text') openPanel(`${run.name} · 回复`, where + sec(`回复全文 · ${e.text.length} 字`, `<div class="box md">${md(e.text)}</div>`, e.text), `${run.slot}:e${n}`);
    else if (e.k === 'think') openPanel(`${run.name} · 思考`, where + sec(`思考全文 · ${e.text.length} 字`, `<div class="box">${esc(e.text)}</div>`, e.text), `${run.slot}:e${n}`);
    else openPanel(`${run.name} · 未执行的调用`, where + sec('工具', `<div class="box">${esc(e.tool)}</div>`) + sec('参数', `<pre class="json">${esc(JSON.stringify(e.input, null, 2))}</pre>`) + sec('返回', `<div class="box">${esc(e.err || '')}</div>`), `${run.slot}:e${n}`);
  }
  function finalPanel(run) {
    S.copy = [];
    const f = run.final || {}, s = run.summary || {}, v = s.video || {}, b = run.board;
    const st = STATUS[s.status] || [s.status || '?', ''];
    const out = [`<div class="row">${mchip(run.slot, run.name)}${chip(st[0], st[1])}${v.duration_s ? chip(v.duration_s.toFixed(2) + 's') : ''}${v.width ? chip(v.width + '×' + v.height + ' ' + (v.fps || '') + 'fps') : ''}${s.cost != null ? chip('$' + (+s.cost).toFixed(2)) : ''}${s.seconds ? chip(fmtWall(s.seconds)) : ''}</div>`];
    if (f.media) out.push(sec('成片', `<div class="mediabox"><video controls preload="metadata" src="${esc(f.media)}"${f.poster ? ` poster="${esc(f.poster)}"` : ''}></video></div>${f.strip ? `<div class="strip" style="height:40px;margin-top:6px;border-radius:4px;background-size:100% 100%;background-image:url('${esc(f.strip)}')"></div>` : ''}`));
    if (b) out.push(sec('成片结构', `<div class="box">镜头 ${b.shots.length} 个 · 字幕 ${(b.subs || []).length} 条 · 混音 ${(b.mix || []).length} 次 · 交卷 #${b.export}\n${b.shots.map((x) => `${tc(x.s)}–${tc(x.e)}  #${x.i ?? '–'} ${x.src}`).join('\n')}</div>`));
    if ((s.invalid_reasons || []).length) out.push(sec('bench 判为无效的原因', `<div class="box">${esc(s.invalid_reasons.join('\n'))}</div>`));
    out.push(sec('题面（发给模型的 prompt）', `<div class="box">${esc(run.prompt)}</div>`, run.prompt));
    if (run.script) out.push(sec('模型的最终说明（script.md）', `<div class="box md">${md(run.script)}</div>`, run.script));
    openPanel(`${run.name} · 成片`, out.join(''), `${run.slot}:final`);
  }
  $('pbody').addEventListener('click', (e) => {
    const cp = e.target.closest('[data-copy]');
    if (cp) {
      e.preventDefault();                  // 复制按钮在 <summary> 里时，别顺带展开/收起
      const text = S.copy[+cp.dataset.copy] || '';
      const done = () => { cp.textContent = '已复制'; setTimeout(() => { cp.textContent = '复制'; }, 1200); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => selectText(cp));
      else selectText(cp);
      return;
    }
    const go = e.target.closest('[data-go]');
    if (go) goRef(go.dataset.go);
  });
  function selectText(btn) {
    const box = btn.closest('.sec').querySelector('.box');
    if (!box) return;
    const r = document.createRange();
    r.selectNodeContents(box);
    const s = window.getSelection();
    s.removeAllRanges();
    s.addRange(r);
    btn.textContent = '已选中，按 Ctrl+C';
  }
  $('pclose').addEventListener('click', () => { closePanel(); clearSel(); });
  $('pzoom').addEventListener('click', () => {
    const key = S.panelKey;
    if (!key) return;
    if (ensureCard(key)) { select(key, { zoom: true, open: false }); return; }
    if (S.caseId) openCase(S.caseId, 'stage', () => { if (ensureCard(key)) select(key, { zoom: true, open: false }); });
  });

  // ------------------------------------------------------------------ 检索高亮
  function applySearch() {
    for (const k of S.hits) { const c = S.cards.get(k); if (c) c.el.classList.remove('hit', 'cur'); }
    for (const f of S.frameEls.values()) f.classList.remove('hit');
    S.hits = [];
    S.hitIdx = -1;
    const q = S.q.trim().toLowerCase();
    world.classList.toggle('searching', !!q && S.mode !== 'overview');
    if (!q || S.mode === 'overview') { $('qn').textContent = q ? '先进题目' : ''; return; }
    for (const [k, c] of S.cards) if (c.text && c.text.includes(q)) { S.hits.push(k); c.el.classList.add('hit'); }
    for (const it of S.folded) if (it.text && it.text.includes(q)) {     // 折叠格子里的也算，给格子标黄，跳到时再展开
      S.hits.push(it.key);
      const f = S.frameEls.get(it.frame);
      if (f) f.classList.add('hit');
    }
    const pos = new Map(S.hits.map((k) => [k, cardRect(k) || frameRect(S.where.get(k))]));
    S.hits.sort((a, b) => pos.get(a).y - pos.get(b).y || pos.get(a).x - pos.get(b).x);
    const per = {};
    for (const k of S.hits) { const s = +k.split(':')[0]; per[s] = (per[s] || 0) + 1; }
    $('qn').textContent = S.hits.length ? `${S.hits.length} 处` : '没有';
    $('qn').title = Object.entries(per).map(([s, n]) => model(+s).name + ' ' + n).join('\n');
  }
  function frameRect(fk) {
    const f = S.frameEls.get(fk);
    return f ? { x: parseFloat(f.style.left) || 0, y: parseFloat(f.style.top) || 0, w: f.offsetWidth, h: f.offsetHeight } : { x: 0, y: 0, w: 0, h: 0 };
  }
  function nextHit(dir) {
    if (!S.hits.length) return;
    if (S.hitIdx >= 0) { const c = S.cards.get(S.hits[S.hitIdx]); if (c) c.el.classList.remove('cur'); }
    S.hitIdx = (S.hitIdx + dir + S.hits.length) % S.hits.length;
    const k = S.hits[S.hitIdx];
    if (!S.cards.has(k)) {                 // 在折叠格子里：展开（会重跑检索、重排命中），再找回同一张
      if (!ensureCard(k)) return;
      S.hitIdx = Math.max(0, S.hits.indexOf(k));
    }
    const c = S.cards.get(k);
    if (!c) return;
    c.el.classList.add('cur');
    zoomToKey(k, 0.7);
    $('qn').textContent = `${S.hitIdx + 1}/${S.hits.length}`;
  }
  let qTimer = 0;
  $('q').addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.q = $('q').value; applySearch(); }, 160); });
  $('q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); S.q = $('q').value; if (!S.hits.length) applySearch(); nextHit(e.shiftKey ? -1 : 1); }
    if (e.key === 'Escape') { $('q').value = ''; S.q = ''; applySearch(); $('q').blur(); }
  });

  // ------------------------------------------------------------------ 渲染与路由
  function render() {
    if (lazyIO) lazyIO.disconnect();
    world.innerHTML = '';
    S.cards.clear();
    S.where.clear();
    S.frameEls.clear();
    S.folded = [];
    S.frames = [];
    S.sel = null;
    world.classList.remove('focus');
    const lod = world.dataset.lod;
    world.dataset.lod = 'near';           // 一律按近景量尺寸排版：远景会藏掉正文，量出来的泳道就矮了
    if (S.mode === 'overview') renderOverview();
    else if (S.data) ({ stage: renderStage, timeline: renderTimeline, board: renderBoard, sim: renderSim }[S.mode] || renderStage)();
    world.dataset.lod = lod || 'near';
    world.style.width = S.bounds.w + 'px';
    world.style.height = S.bounds.h + 'px';
    lazyScan(world);
    applySearch();
  }
  function rerender() {
    const keep = { k: S.k, tx: S.tx, ty: S.ty }, sel = S.sel;
    render();
    Object.assign(S, keep);
    apply();
    if (sel && S.cards.has(sel)) select(sel, { open: false });
  }
  // ------------------------------------------------------------------ 按领域 / 题型筛选（总览、题目导航都只显示筛出来的题）
  const DOMAIN_ZH = { commercial: '商业广告', creator: '创作者', dialogue: '对话访谈', documentary: '纪录片', education: '教育科普',
    music: '音乐', narrative: '剧情叙事', product: '产品展示' };
  const FAMILY_ZH = { constraint: '约束题', assets: '素材题', open: '开放题', 'audio-led': '声音主导', reference: '参考视频' };
  const domainZh = (d) => DOMAIN_ZH[d] || d;
  const familyZh = (f) => FAMILY_ZH[f] || f;
  function shownCases() {
    return IDX.cases.filter((c) => (!S.fDom || c.domain === S.fDom) && (!S.fFam || c.family === S.fFam));
  }
  function renderNav() {
    $('cases').innerHTML = shownCases().map((c) => `<button type="button" data-case="${esc(c.id)}" title="${esc(c.brief)}"><span class="no">${esc(caseNo(c.id))}</span><span class="nm">${esc(c.name)}</span></button>`).join('');
  }
  function renderFilters() {
    // 各项后面的数字：在另一维筛选条件下，这一项有几道题（点了以后会剩几道）
    const cnt = (d, f) => IDX.cases.filter((c) => (!d || c.domain === d) && (!f || c.family === f)).length;
    const doms = [...new Set(IDX.cases.map((c) => c.domain).filter(Boolean))].sort((a, b) => Object.keys(DOMAIN_ZH).indexOf(a) - Object.keys(DOMAIN_ZH).indexOf(b));
    const fams = [...new Set(IDX.cases.map((c) => c.family).filter(Boolean))].sort((a, b) => Object.keys(FAMILY_ZH).indexOf(a) - Object.keys(FAMILY_ZH).indexOf(b));
    const btn = (attr, v, label, n, on) => `<button type="button" class="fchip" ${attr}="${esc(v)}" aria-pressed="${on}"${n ? '' : ' disabled'}>${esc(label)}<b>${n}</b></button>`;
    $('filters').innerHTML =
      `<span class="fk">领域</span>` + btn('data-dom', '', '全部', cnt('', S.fFam), !S.fDom) +
      doms.map((d) => btn('data-dom', d, domainZh(d), cnt(d, S.fFam), S.fDom === d)).join('') +
      `<span class="fk">题型</span>` + btn('data-fam', '', '全部', cnt(S.fDom, ''), !S.fFam) +
      fams.map((f) => btn('data-fam', f, familyZh(f), cnt(S.fDom, f), S.fFam === f)).join('');
  }
  function updateBar() {
    for (const b of $('cases').children) {
      const cur = b.dataset.case === S.caseId && S.mode !== 'overview';
      b.setAttribute('aria-current', String(cur));
      if (cur) b.scrollIntoView({ block: 'nearest', inline: 'center' });
    }
    for (const b of $('modes').children) b.setAttribute('aria-pressed', String(b.dataset.mode === S.mode));
    const cs = S.caseId ? IDX.cases.find((c) => c.id === S.caseId) : null;
    if (S.mode === 'overview' || !cs) {
      const fl = [S.fDom && domainZh(S.fDom), S.fFam && familyZh(S.fFam)].filter(Boolean).join(' · ');
      $('brief').innerHTML = `<b>${shownCases().length} 道题${fl ? '（' + esc(fl) + '）' : ''} × ${IDX.models.length} 个模型</b> · 每行是一个模型的成片，16 张均匀抽帧。点任意一行进入这道题的制作过程（点卡片看上下游连线）。数据生成于 ${esc(IDX.built || '')}`;
    } else {
      const t = cs.duration || [];
      $('brief').innerHTML = `<b>${esc(caseNo(cs.id))} ${esc(cs.name)}</b> · ${esc(cs.brief)} <span class="muted">· 目标 ${esc(t.join('–'))} 秒</span>`;
      $('brief').title = cs.brief;
    }
    const slots = S.mode === 'overview' || !S.data ? IDX.models.map((m) => m.slot) : S.data.runs.map((r) => r.slot);
    $('legend').innerHTML = slots.map((s) => `<button type="button" class="mchip" data-slot="${s}" aria-pressed="${!S.hidden.has(s)}" style="${mcol(s)}" title="显示 / 隐藏这个模型"><i></i>${esc(model(s).name)}</button>`).join('');
    renderFilters();
    const top = $('bar').offsetHeight;
    $('sub').style.top = top + 'px';
    panel.style.setProperty('--top', $('sub').getBoundingClientRect().bottom + 'px');
  }
  function openCase(id, mode, then) {
    S.then = then || null;
    const h = `${id}-${mode}`;
    if (location.hash.slice(1) === h) route();
    else location.hash = h;
  }
  function parseHash() {
    const h = decodeURIComponent(location.hash.slice(1));
    const m = h.match(/^(.+)-(stage|timeline|board|sim)$/);
    if (m && IDX.cases.some((c) => c.id === m[1])) return { caseId: m[1], mode: m[2] };
    if (IDX.cases.some((c) => c.id === h)) return { caseId: h, mode: 'stage' };
    return { caseId: S.caseId, mode: 'overview' };
  }
  async function route() {
    const { caseId, mode } = parseHash();
    closePanel();
    if (mode !== 'overview' && caseId && caseId !== (S.data && S.data.id)) {
      $('loading').hidden = false;
      try { S.data = prep(await loadCase(caseId)); } catch (err) {
        $('loading').hidden = true;
        world.innerHTML = '';
        const d = mk('board', `<h3>这道题的数据读不出来</h3><p class="lead">${esc(err.message)}。先运行 build.py 生成 data/${esc(caseId)}.js。</p>`);
        place(d, 40, 120, 600);
        world.appendChild(d);
        S.bounds = { w: 700, h: 400 };
        return;
      }
      $('loading').hidden = true;
      S.pairFilter = null;
      S.pairLimit = 24;
    }
    S.caseId = caseId || S.caseId;
    S.mode = mode;
    updateBar();
    render();
    fitAll();
    updateFoldBtn();
    if (S.then) { const f = S.then; S.then = null; f(); }
  }

  // ------------------------------------------------------------------ 顶栏
  $('batch').textContent = (IDX.runs_root || '').split('/').filter(Boolean).pop() || '';
  renderNav();
  $('filters').addEventListener('click', (e) => {
    const b = e.target.closest('[data-dom],[data-fam]');
    if (!b) return;
    if (b.dataset.dom !== undefined) S.fDom = S.fDom === b.dataset.dom ? '' : b.dataset.dom;
    else S.fFam = S.fFam === b.dataset.fam ? '' : b.dataset.fam;
    savePrefs();
    renderNav();
    updateBar();
    if (S.mode === 'overview') { render(); fitAll(); }
  });
  $('cases').addEventListener('wheel', (e) => {     // 竖向滚轮横着翻题目
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { $('cases').scrollLeft += e.deltaY; e.preventDefault(); }
  }, { passive: false });
  $('cases').addEventListener('click', (e) => {
    const b = e.target.closest('[data-case]');
    if (b) openCase(b.dataset.case, S.mode === 'overview' ? 'stage' : S.mode);
  });
  $('modes').addEventListener('click', (e) => {
    const b = e.target.closest('[data-mode]');
    if (!b) return;
    const m = b.dataset.mode;
    if (m === 'overview') { location.hash = 'overview'; return; }
    const id = S.caseId || (IDX.cases[0] && IDX.cases[0].id);
    if (id) openCase(id, m);
  });
  $('legend').addEventListener('click', (e) => {
    const b = e.target.closest('[data-slot]');
    if (!b) return;
    const s = +b.dataset.slot;
    if (S.hidden.has(s)) S.hidden.delete(s); else S.hidden.add(s);
    savePrefs();
    updateBar();
    render();
    fitAll();
  });
  for (const [id, key] of [['showqa', 'showQA'], ['showthink', 'showThink'], ['wheelzoom', 'wheelZoom']]) {
    $(id).checked = S[key];
    $(id).addEventListener('change', () => {
      S[key] = $(id).checked;
      savePrefs();
      if (key !== 'wheelZoom') { render(); fitAll(); }
    });
  }
  $('zin').addEventListener('click', () => zoomAt(viewW() / 2, (topInset() + window.innerHeight) / 2, 1.25));
  $('zout').addEventListener('click', () => zoomAt(viewW() / 2, (topInset() + window.innerHeight) / 2, 0.8));
  $('zfit').addEventListener('click', fitAll);
  function foldAll() {
    if (S.mode !== 'stage') return;
    const keys = caseFrameKeys(), close = $('foldall').dataset.all === '1';
    for (const k of keys) if (close) S.closed.add(k); else S.closed.delete(k);
    render();
    fitAll();
    updateFoldBtn();
  }
  $('foldall').addEventListener('click', foldAll);
  window.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select')) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const cx = viewW() / 2, cy = (topInset() + window.innerHeight) / 2;
    const step = 140;
    switch (e.key) {
      case 'f': case 'F': fitAll(); break;
      case '+': case '=': zoomAt(cx, cy, 1.25); break;
      case '-': case '_': zoomAt(cx, cy, 0.8); break;
      case '/': e.preventDefault(); $('q').focus(); break;
      case 'Escape': closePanel(); clearSel(); break;
      case 'e': case 'E': foldAll(); break;
      case 'n': nextHit(1); break;
      case 'N': nextHit(-1); break;
      case 'ArrowLeft': S.tx += step; apply(); break;
      case 'ArrowRight': S.tx -= step; apply(); break;
      case 'ArrowUp': S.ty += step; apply(); break;
      case 'ArrowDown': S.ty -= step; apply(); break;
      case '1': case '2': case '3': case '4': case '5': {
        const m = ['overview', 'stage', 'timeline', 'board', 'sim'][+e.key - 1];
        if (m === 'overview') location.hash = 'overview';
        else if (S.caseId || IDX.cases[0]) openCase(S.caseId || IDX.cases[0].id, m);
        break;
      }
      default: return;
    }
  });
  window.addEventListener('resize', () => { updateBar(); scheduleMinimap(); });
  window.addEventListener('hashchange', route);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (S.mode === 'sim') rerender(); scheduleMinimap(); });

  if (!IDX.cases.length) {
    const d = mk('board', '<h3>还没有数据</h3><p class="lead">先运行 build.py 生成 data/index.js。</p>');
    place(d, 40, 120, 560);
    world.appendChild(d);
  }
  window.SBCanvas = { state: S, select, zoomToKey, goRef, fitAll, toggleFold, foldAll };   // 调试 / 自动截图用
  updateBar();
  route();
})();
