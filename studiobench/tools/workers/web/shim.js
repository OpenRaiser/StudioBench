// StudioBench render_html 虚拟时钟：在页面脚本之前注入（context.addInitScript），把时间交给抓帧器。
// 接管 performance.now / Date / requestAnimationFrame / setTimeout / setInterval，
// CSS 动画、CSS 过渡、Web Animations 每帧暂停后 seek 到虚拟时间，<video> 也按虚拟时间 seek。
// 抓帧器每帧调用 window.__studio.step(t)：推进到 t 秒、跑到期的定时器和 rAF 回调，
// 页面定义了 window.render(t) 就再调用它（render(t) 模式）。
(() => {
  if (window.__studio) return;
  const BASE_EPOCH = 1767225600000;            // 2026-01-01T00:00:00Z：Date 从固定时刻起走，结果可复现
  const RealDate = Date;
  let vt = 0;                                   // 虚拟时间（毫秒）
  const errors = [];
  const warnings = [];
  const report = (e) => {
    try { errors.push(String((e && (e.stack || e.message)) || e)); } catch (_) { errors.push('error'); }
  };

  // Math.random 换成固定种子的 PRNG（mulberry32）：同一页面每次、每个并行 worker 得到同一串随机数
  let seed = 0x5eed1234 >>> 0;
  Math.random = () => {
    seed = (seed + 0x6D2B79F5) >>> 0;
    let r = seed;
    r = Math.imul(r ^ (r >>> 15), r | 1);
    r ^= r + Math.imul(r ^ (r >>> 7), r | 61);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };

  const perfNow = () => vt;
  try { performance.now = perfNow; } catch (_) {
    Object.defineProperty(performance, 'now', { value: perfNow, configurable: true });
  }
  const realSetTimeout = window.setTimeout.bind(window);   // 只给 <video> seek 的兜底超时用
  const realRaf = window.requestAnimationFrame.bind(window);  // seek 后等真实合成帧，确保截图里是新画面
  function VirtualDate(...args) {
    if (!new.target) return new RealDate(BASE_EPOCH + vt).toString();
    return args.length === 0 ? new RealDate(BASE_EPOCH + vt) : new RealDate(...args);
  }
  VirtualDate.prototype = RealDate.prototype;
  VirtualDate.now = () => BASE_EPOCH + vt;
  VirtualDate.parse = RealDate.parse;
  VirtualDate.UTC = RealDate.UTC;
  window.Date = VirtualDate;
  try {
    const desc = Object.getOwnPropertyDescriptor(AnimationTimeline.prototype, 'currentTime');
    if (desc && desc.configurable) {
      Object.defineProperty(AnimationTimeline.prototype, 'currentTime', { get() { return vt; }, configurable: true });
    }
  } catch (_) { /* 只影响 JS 读到的 document.timeline.currentTime */ }

  // ---- 定时器
  let nextTimer = 1;
  const timers = new Map();                     // id -> {at, fn, args, every}
  const addTimer = (fn, ms, args, repeat) => {
    const id = nextTimer++;
    const delay = Math.max(0, Number(ms) || 0);
    timers.set(id, { at: vt + (repeat ? Math.max(1, delay) : delay), fn, args, every: repeat ? Math.max(1, delay) : 0 });
    return id;
  };
  window.setTimeout = (fn, ms, ...args) => addTimer(fn, ms, args, false);
  window.setInterval = (fn, ms, ...args) => addTimer(fn, ms, args, true);
  window.clearTimeout = (id) => { timers.delete(id); };
  window.clearInterval = (id) => { timers.delete(id); };

  // ---- rAF
  let nextRaf = 1;
  let rafQueue = new Map();
  window.requestAnimationFrame = (cb) => { const id = nextRaf++; rafQueue.set(id, cb); return id; };
  window.cancelAnimationFrame = (id) => { rafQueue.delete(id); };

  const MAX_CALLBACKS = 20000;                  // 每帧最多执行的定时器回调，防止 setTimeout(fn, 0) 死循环
  let warnedLoop = false;
  function advanceTo(target) {
    let fired = 0;
    for (;;) {
      let pick = null;
      for (const [id, t] of timers) {
        if (t.at <= target && (pick === null || t.at < pick[1].at || (t.at === pick[1].at && id < pick[0]))) pick = [id, t];
      }
      if (pick === null) break;
      const [id, t] = pick;
      if (t.at > vt) vt = t.at;
      if (t.every) t.at += t.every; else timers.delete(id);
      try {
        if (typeof t.fn === 'function') t.fn(...(t.args || []));
        else (0, eval)(String(t.fn));
      } catch (e) { report(e); }
      if (++fired >= MAX_CALLBACKS) {
        if (!warnedLoop) { warnedLoop = true; report('定时器在一帧内触发超过 ' + MAX_CALLBACKS + ' 次，已截断（setTimeout(fn, 0) 循环？）'); }
        break;
      }
    }
    if (target > vt) vt = target;
  }

  function runRaf() {
    const queue = rafQueue;
    rafQueue = new Map();
    for (const cb of queue.values()) {
      try { cb(vt); } catch (e) { report(e); }
    }
  }

  // ---- CSS 动画 / 过渡 / Web Animations：暂停后按虚拟时间 seek
  const seen = new WeakMap();                   // Animation -> {born, own}
  function seekAnimations() {
    let list = [];
    try { list = document.getAnimations(); } catch (_) { return; }
    for (const a of list) {
      let info = seen.get(a);
      if (!info) {
        info = { born: vt, own: a.playState !== 'paused' };   // 页面自己暂停的动画不接管
        seen.set(a, info);
      }
      if (!info.own) continue;
      try {
        a.pause();
        a.currentTime = Math.max(0, (vt - info.born) * (a.playbackRate || 1));
      } catch (e) { /* 已结束或被移除的动画 */ }
    }
  }

  // ---- <video>：每帧都暂停并记下出现时刻；截图帧按虚拟时间 seek，等 seeked 和新画面合成后再截
  const videoBorn = new WeakMap();
  function trackVideos() {
    for (const v of document.querySelectorAll('video')) {
      if (!videoBorn.has(v)) videoBorn.set(v, vt);
      if (!v.paused) { try { v.pause(); } catch (_) { /* ignore */ } }
    }
  }
  async function seekVideos() {
    const waits = [];
    for (const v of document.querySelectorAll('video')) {
      if (!(v.readyState >= 1) || !isFinite(v.duration) || v.duration <= 0) continue;
      let target = (vt - videoBorn.get(v)) / 1000 * (v.playbackRate || 1);
      if (v.loop) target = target % v.duration; else target = Math.min(target, Math.max(0, v.duration - 0.001));
      if (Math.abs(v.currentTime - target) < 0.0005) continue;
      if (v.__studioNoSeek) continue;
      waits.push(new Promise((resolve) => {
        let finished = false;
        const done = (ok) => {
          if (finished) return;
          finished = true;
          if (!ok && !v.__studioNoSeek) {           // seek 卡住：这个 <video> 以后不再等，免得每帧都超时
            v.__studioNoSeek = true;
            warnings.push('<video ' + (v.currentSrc || v.src || '').slice(-80) + '> 在无头浏览器里 seek 没有完成，画面不随时间更新；'
              + '要把视频和图形合成，请用 transparent=true 只渲染图形层，再用 ffmpeg 叠到视频上');
          }
          resolve();
        };
        // 暂停态 rVFC 在无头模式下不触发，不等它；seek 后时间没变（实测本机 headless shell 如此）就当作不支持
        v.addEventListener('seeked', () => done(Math.abs(v.currentTime - target) < 0.05), { once: true });
        realSetTimeout(() => done(false), 1500);
        try { v.currentTime = target; } catch (_) { done(false); }
      }));
    }
    if (waits.length) {
      await Promise.all(waits);
      await new Promise((r) => realRaf(() => realRaf(() => r())));
    }
  }

  window.__studio = {
    errors,
    now: () => vt / 1000,
    // 推进到 t 秒并画这一帧。capture=false 用于并行 worker 的快进（只跑逻辑，不截图）
    async step(t, capture) {
      advanceTo(t * 1000);
      trackVideos();
      runRaf();
      seekAnimations();
      if (typeof window.render === 'function') {
        try { await window.render(t); } catch (e) { report(e); }
      }
      if (capture) await seekVideos();
      return errors.splice(0, errors.length);    // 这一帧里被接住的异常，交给抓帧器判失败
    },
    hasRender: () => typeof window.render === 'function',
    warnings,
    takeErrors() { const out = errors.splice(0, errors.length); return out; },
  };
})();
