#!/usr/bin/env node
// StudioBench render_html 抓帧 worker：headless Chromium 逐帧截图，经 stdin 管道交给 ffmpeg，帧不落盘。
//
//   node web_render.mjs --config -          # MCP 工具用：从 stdin 读 JSON 配置，最后一行 stdout 是 JSON 结果
//   node web_render.mjs page.html --duration 5 --size 1280x720 --out clip.mp4   # shell 里直接用（toolbox/bin/render-html）
//
// 页面的时间由 workers/web/shim.js 接管：页面定义了 window.render(t) 就逐帧调用（render(t) 模式），
// 否则 rAF / 定时器 / CSS 动画 / Web Animations 都按虚拟时钟推进（virtual-time 模式）。
// 网络：页面只能访问虚拟源 http://localhost:17771（根目录 = 工作目录，/__lib/ = 工具箱里的前端库）；
// unpkg / jsdelivr / esm.sh / cdnjs / skypack / jspm 上的已收录库重定向到本地，其余外部请求一律拦截。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TOOLBOX = process.env.STUDIO_TOOLBOX || '/taijifs_sz34/share_304169095/xenoxu/StudioBench/toolbox';
// 前端库：共享盘上是 node_modules.tar，调用方（extra/web.py、toolbox/bin/render-html）先解到节点本机缓存再传进来
const LIBDIR = process.env.STUDIO_WEB_LIBDIR || path.join(TOOLBOX, 'web', 'node_modules');
const ORIGIN = 'http://localhost:17771';
const SHIM = path.join(HERE, 'web', 'shim.js');

const MIME = {
  '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.cjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.map': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
  '.ico': 'image/x-icon', '.bmp': 'image/bmp', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
  '.otf': 'font/otf', '.wasm': 'application/wasm', '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
  '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.glb': 'model/gltf-binary',
  '.gltf': 'model/gltf+json', '.hdr': 'application/octet-stream', '.exr': 'application/octet-stream',
  '.ktx2': 'image/ktx2', '.txt': 'text/plain; charset=utf-8', '.csv': 'text/csv; charset=utf-8',
  '.xml': 'application/xml', '.obj': 'text/plain', '.mtl': 'text/plain', '.fbx': 'application/octet-stream',
};
const mimeOf = (p) => MIME[path.extname(p).toLowerCase()] || 'application/octet-stream';
const isFile = (p) => { try { return fs.statSync(p).isFile(); } catch { return false; } };
const isDir = (p) => { try { return fs.statSync(p).isDirectory(); } catch { return false; } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (msg) => process.stderr.write(msg + '\n');

// --------------------------------------------------------------------------- 前端库：import map 与 CDN 重写
const pkgCache = new Map();
function readPkg(name) {
  if (pkgCache.has(name)) return pkgCache.get(name);
  let pkg = null;
  try { pkg = JSON.parse(fs.readFileSync(path.join(LIBDIR, name, 'package.json'), 'utf8')); } catch { pkg = null; }
  pkgCache.set(name, pkg);
  return pkg;
}

function resolveCond(c) {
  if (typeof c === 'string') return c.endsWith('.cjs') ? null : c;
  if (Array.isArray(c)) { for (const x of c) { const r = resolveCond(x); if (r) return r; } return null; }
  if (c && typeof c === 'object') {
    for (const key of ['browser', 'import', 'module', 'production', 'default']) {
      if (key in c) { const r = resolveCond(c[key]); if (r) return r; }
    }
  }
  return null;
}

function esmEntry(pkg) {
  if (!pkg) return null;
  const exp = pkg.exports;
  if (exp) {
    let r = null;
    if (typeof exp === 'string' || Array.isArray(exp)) r = resolveCond(exp);
    else if (exp['.'] !== undefined) r = resolveCond(exp['.']);
    else if (!Object.keys(exp).some((k) => k.startsWith('.'))) r = resolveCond(exp);
    if (r) return r;
  }
  if (typeof pkg.module === 'string') return pkg.module;
  if (typeof pkg.main === 'string' && (pkg.type === 'module' || pkg.main.endsWith('.mjs'))) return pkg.main;
  return null;
}

function classicEntry(pkg) {
  if (!pkg) return null;
  for (const v of [pkg.unpkg, pkg.jsdelivr, typeof pkg.browser === 'string' ? pkg.browser : null, pkg.main]) {
    if (typeof v === 'string' && v) return v;
  }
  return 'index.js';
}

const clean = (p) => String(p || '').replace(/^\.\//, '').replace(/^\//, '');

function listPackages() {
  const out = [];
  let names = [];
  try { names = fs.readdirSync(LIBDIR); } catch { return out; }
  for (const n of names) {
    if (n.startsWith('.')) continue;
    if (n.startsWith('@')) {
      for (const m of fs.readdirSync(path.join(LIBDIR, n))) out.push(n + '/' + m);
    } else out.push(n);
  }
  return out;
}

const SKIP_PKGS = new Set(['playwright-core']);
function buildImportMap(useCache = true) {
  const cached = path.join(TOOLBOX, 'web', 'importmap.json');
  if (useCache && isFile(cached)) {
    try { return JSON.parse(fs.readFileSync(cached, 'utf8')); } catch { /* 重新生成 */ }
  }
  const imports = {};
  for (const name of listPackages()) {
    if (SKIP_PKGS.has(name)) continue;
    const entry = esmEntry(readPkg(name));
    if (entry) imports[name] = '/__lib/' + name + '/' + clean(entry);
    imports[name + '/'] = '/__lib/' + name + '/';
  }
  if (imports.three) imports['three/addons/'] = '/__lib/three/examples/jsm/';
  return { imports };
}

function firstExisting(base) {
  for (const cand of [base, base + '.js', base + '.mjs', path.join(base, 'index.js'), path.join(base, 'index.mjs')]) {
    if (isFile(cand)) return cand;
  }
  return null;
}

const CDNJS = {
  'three.js': 'three', 'gsap': 'gsap', 'd3': 'd3', 'katex': 'katex', 'p5.js': 'p5', 'animejs': 'animejs',
  'chart.js': 'chart.js', 'pixi.js': 'pixi.js', 'matter-js': 'matter-js', 'lottie-web': 'lottie-web',
  'bodymovin': 'lottie-web', 'tween.js': '@tweenjs/tween.js', 'simplex-noise': 'simplex-noise',
};
const findCache = new Map();
function findByBasename(pkgName, base) {
  const key = pkgName + '|' + base;
  if (findCache.has(key)) return findCache.get(key);
  const root = path.join(LIBDIR, pkgName);
  const wanted = [base, base.replace(/\.min\.js$/, '.js'), base.replace(/\.js$/, '.min.js')];
  let hit = null;
  const walk = (dir, depth) => {
    if (hit || depth > 3) return;
    let ents = [];
    try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of ents) if (e.isFile() && wanted.includes(e.name)) { hit = path.join(dir, e.name); return; }
    for (const e of ents) if (e.isDirectory() && e.name !== 'node_modules' && e.name !== 'src') walk(path.join(dir, e.name), depth + 1);
  };
  walk(root, 0);
  findCache.set(key, hit);
  return hit;
}

function splitSpec(rest) {
  // "three@0.170.0/examples/jsm/x.js" -> {name: "three", sub: "examples/jsm/x.js"}；"@scope/name@1/x" 同理
  const parts = rest.split('/').filter((x) => x !== '');
  if (!parts.length) return null;
  let name;
  let i;
  if (parts[0].startsWith('@')) {
    if (parts.length < 2) return null;
    name = parts[0] + '/' + parts[1].replace(/@[^@]*$/, '');
    i = 2;
  } else {
    name = parts[0].replace(/@[^@]*$/, '');
    i = 1;
  }
  return { name, sub: parts.slice(i).join('/') };
}

// CDN 地址 -> 本地库文件 {file, url}（找不到就返回 null，交给拦截）
function cdnRewrite(url, headers) {
  const host = url.hostname.toLowerCase();
  let p = decodeURIComponent(url.pathname);
  let spec = null;
  let esmHost = false;
  if (host === 'unpkg.com' || host === 'www.unpkg.com') spec = splitSpec(p);
  else if (host === 'cdn.jsdelivr.net' || host === 'fastly.jsdelivr.net') {
    if (!p.startsWith('/npm/')) return null;
    spec = splitSpec(p.slice(5));
  } else if (host === 'esm.sh' || host === 'cdn.esm.sh') {
    p = p.replace(/^\/(v\d+|stable)\//, '/');
    spec = splitSpec(p); esmHost = true;
  } else if (host === 'cdn.skypack.dev' || host === 'jspm.dev' || host === 'esm.run') {
    spec = splitSpec(p.replace(/^\/npm:/, '/')); esmHost = true;
  } else if (host === 'ga.jspm.io') {
    spec = splitSpec(p.replace(/^\/npm:/, '/')); esmHost = true;
  } else if (host === 'cdnjs.cloudflare.com') {
    const m = p.match(/^\/ajax\/libs\/([^/]+)\/[^/]+\/(.+)$/);
    if (!m) return null;
    const pkgName = CDNJS[m[1].toLowerCase()] || (readPkg(m[1]) ? m[1] : null);
    if (!pkgName) return null;
    const hit = findByBasename(pkgName, path.basename(m[2]));
    return hit ? { file: hit, url: ORIGIN + '/__lib/' + path.relative(LIBDIR, hit).split(path.sep).join('/') } : null;
  } else return null;
  if (!spec) return null;
  const pkg = readPkg(spec.name);
  if (!pkg) return null;
  const wantsModule = esmHost || (headers['sec-fetch-mode'] || '') === 'cors';
  let rel = null;
  if (!spec.sub || spec.sub === '+esm') {
    rel = (wantsModule || spec.sub === '+esm') ? (esmEntry(pkg) || classicEntry(pkg)) : classicEntry(pkg);
  } else {
    const sub = spec.sub.replace(/\/\+esm$/, '');
    const hit = firstExisting(path.join(LIBDIR, spec.name, sub));
    if (hit) rel = path.relative(path.join(LIBDIR, spec.name), hit);
    else if (esmHost) rel = esmEntry(pkg);           // esm.sh 内部构建路径（/es2022/x.mjs）
  }
  if (!rel) return null;
  const full = path.join(LIBDIR, spec.name, clean(rel));
  if (!isFile(full)) return null;
  return { file: full, url: ORIGIN + '/__lib/' + spec.name + '/' + clean(rel).split(path.sep).join('/') };
}

const defaultCache = new Map();
function hasDefaultExport(file) {
  if (!defaultCache.has(file)) {
    let src = '';
    try { src = fs.readFileSync(file, 'utf8'); } catch { /* ignore */ }
    defaultCache.set(file, /\bexport\s+default\b|\bas\s+default\b/.test(src));
  }
  return defaultCache.get(file);
}

function injectImportMap(html, importmap) {
  const re = /<script\b[^>]*type\s*=\s*["']?importmap["']?[^>]*>([\s\S]*?)<\/script>/i;
  const m = html.match(re);
  if (m) {
    let theirs = null;
    try { theirs = JSON.parse(m[1]); } catch { return html; }
    const missing = {};
    for (const [k, v] of Object.entries(importmap.imports)) {
      if (!(theirs.imports && k in theirs.imports)) missing[k] = v;
    }
    if (!Object.keys(missing).length) return html;
    const extra = '<script type="importmap">' + JSON.stringify({ imports: missing }) + '</script>';
    return html.slice(0, m.index + m[0].length) + extra + html.slice(m.index + m[0].length);
  }
  const tag = '<script type="importmap">' + JSON.stringify(importmap) + '</script>';
  const head = html.match(/<head\b[^>]*>/i);
  if (head) return html.slice(0, head.index + head[0].length) + tag + html.slice(head.index + head[0].length);
  const htmlTag = html.match(/<html\b[^>]*>/i);
  if (htmlTag) return html.slice(0, htmlTag.index + htmlTag[0].length) + '<head>' + tag + '</head>' + html.slice(htmlTag.index + htmlTag[0].length);
  return tag + html;
}

// --------------------------------------------------------------------------- 浏览器
async function loadPlaywright() {
  const entry = path.join(LIBDIR, 'playwright-core', 'index.mjs');
  return import(pathToFileURL(entry).href);
}

function browserArgs(cfg) {
  const args = ['--hide-scrollbars', '--mute-audio', '--font-render-hinting=none', '--disable-lcd-text',
    '--force-color-profile=srgb', '--autoplay-policy=no-user-gesture-required',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows', '--disable-features=PaintHolding',
    '--ignore-gpu-blocklist', '--enable-webgl', '--js-flags=--max-old-space-size=4096'];
  if (cfg.gpu) args.push('--use-angle=' + (process.env.STUDIO_WEB_ANGLE || 'vulkan'), '--enable-features=Vulkan', '--enable-gpu-rasterization');
  else args.push('--use-angle=swiftshader', '--enable-unsafe-swiftshader');
  return args;
}

class Session {
  constructor(cfg, importmap, shared) {
    this.cfg = cfg; this.importmap = importmap; this.shared = shared;
  }

  async open(chromium) {
    const cfg = this.cfg;
    this.browser = await chromium.launch({
      headless: true, args: browserArgs(cfg), proxy: { server: 'http://127.0.0.1:9' },
      env: { ...process.env, FONTCONFIG_FILE: process.env.FONTCONFIG_FILE || path.join(TOOLBOX, 'fonts', 'fonts.conf') },
      timeout: 120000,
    });
    this.context = await this.browser.newContext({
      viewport: { width: cfg.width, height: cfg.height }, deviceScaleFactor: 1, serviceWorkers: 'block',
      bypassCSP: true, colorScheme: 'light', locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    });
    await this.context.addInitScript({ path: SHIM });
    await this.context.route('**/*', (route) => this.route(route));
    this.page = await this.context.newPage();
    const sh = this.shared;
    this.page.on('pageerror', (e) => sh.pageErrors.push(String((e && (e.stack || e.message)) || e).slice(0, 2000)));
    this.page.on('console', (m) => { if (m.type() === 'error') sh.consoleErrors.push(m.text().slice(0, 500)); });
    this.cdp = await this.context.newCDPSession(this.page);
    if (cfg.transparent) {
      await this.cdp.send('Emulation.setDefaultBackgroundColorOverride', { color: { r: 0, g: 0, b: 0, a: 0 } });
    }
  }

  async route(route) {
    const req = route.request();
    let url;
    try { url = new URL(req.url()); } catch { return route.abort('blockedbyclient'); }
    if (url.origin === ORIGIN) {
      const p = decodeURIComponent(url.pathname);
      if (p.startsWith('/__lib/')) return this.serveFile(route, LIBDIR, p.slice('/__lib/'.length), true);
      return this.serveFile(route, this.cfg.root, p.replace(/^\//, ''), false);
    }
    if (url.protocol === 'data:' || url.protocol === 'blob:') return route.continue();
    let headers = {};
    try { headers = await req.allHeaders(); } catch { headers = req.headers(); }
    const local = cdnRewrite(url, headers);
    if (local) {
      // 不用 302：被重定向的请求不再经过 route。模块脚本回一个转发 stub（import 本地地址，模块实例和相对 import 都落在本地），
      // 其余（经典脚本、CSS、字体、JSON）直接回本地文件内容
      this.shared.rewritten.add(req.url() + ' -> ' + local.url.replace(ORIGIN, ''));
      const cors = { 'access-control-allow-origin': '*', 'cache-control': 'no-store' };
      const isModule = (headers['sec-fetch-dest'] || '') === 'script' && (headers['sec-fetch-mode'] || '') === 'cors';
      if (isModule && /\.(m?js)$/i.test(local.file)) {
        const target = JSON.stringify(local.url);
        const body = 'export * from ' + target + ';\n' + (hasDefaultExport(local.file) ? 'export { default } from ' + target + ';\n' : '');
        return route.fulfill({ status: 200, contentType: 'text/javascript; charset=utf-8', headers: cors, body });
      }
      return route.fulfill({ status: 200, contentType: mimeOf(local.file), headers: cors, body: fs.readFileSync(local.file) });
    }
    this.shared.blocked.add(req.url().slice(0, 300));
    return route.abort('blockedbyclient');
  }

  async serveFile(route, root, rel, lib) {
    const base = path.resolve(root);
    let full = path.resolve(base, rel || '.');
    if (full !== base && !full.startsWith(base + path.sep)) {
      this.shared.blocked.add('越界路径：' + rel);
      return route.fulfill({ status: 403, body: 'forbidden' });
    }
    if (isDir(full)) full = path.join(full, 'index.html');
    let hit = isFile(full) ? full : (lib ? firstExisting(full) : null);
    if (!hit) {
      if (!/favicon\.ico$/i.test(rel)) this.shared.missing.add((lib ? '/__lib/' : '') + rel);
      return route.fulfill({ status: 404, contentType: 'text/plain', body: 'not found: ' + rel });
    }
    const headers = { 'access-control-allow-origin': '*', 'cache-control': 'no-store' };
    if (!lib && /\.html?$/i.test(hit)) {
      const html = injectImportMap(fs.readFileSync(hit, 'utf8'), this.importmap);
      return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', headers, body: html });
    }
    return route.fulfill({ status: 200, contentType: mimeOf(hit), headers, body: fs.readFileSync(hit) });
  }

  async goto() {
    const cfg = this.cfg;
    const url = ORIGIN + '/' + cfg.entry.split(path.sep).map(encodeURIComponent).join('/') + (cfg.query ? (cfg.query.startsWith('?') ? cfg.query : '?' + cfg.query) : '');
    await this.page.goto(url, { waitUntil: 'load', timeout: 120000 });
    try { await this.page.waitForLoadState('networkidle', { timeout: 15000 }); } catch { /* 长连接页面：不等 */ }
    await this.page.evaluate(() => (document.fonts ? document.fonts.ready.then(() => true) : true));
    const t0 = Date.now();
    for (;;) {
      const st = await this.page.evaluate(() => ({ has: 'READY' in window, ready: window.READY === true }));
      if (!st.has || st.ready) break;
      if (this.shared.pageErrors.length && !this.cfg.allowErrors) break;   // 加载阶段已经报错：不必再等 READY
      if (Date.now() - t0 > 180000) throw new Error('页面 180 秒内没有把 window.READY 设成 true');
      await sleep(100);
    }
    return this.page.evaluate(() => ({
      hasRender: typeof window.render === 'function',
      dur: typeof window.DUR === 'number' && isFinite(window.DUR) ? window.DUR : null,
    }));
  }

  async step(t, capture) {
    const errs = await this.page.evaluate(([tt, cap]) => window.__studio.step(tt, cap), [t, capture]);
    for (const e of errs || []) this.shared.pageErrors.push('t=' + t.toFixed(3) + 's ' + String(e).slice(0, 2000));
  }

  async shot() {
    const fmt = this.cfg.transparent ? 'png' : 'jpeg';
    const opts = { format: fmt, fromSurface: true, captureBeyondViewport: false, optimizeForSpeed: true };
    if (fmt === 'jpeg') opts.quality = this.cfg.jpegQuality;
    const r = await this.cdp.send('Page.captureScreenshot', opts);
    return Buffer.from(r.data, 'base64');
  }

  async collectWarnings() {
    try {
      const ws = await this.page.evaluate(() => (window.__studio ? window.__studio.warnings.slice() : []));
      for (const x of ws) this.shared.warnings.add(String(x).slice(0, 400));
    } catch { /* 页面已关 */ }
  }

  async close() {
    if (this.page) await this.collectWarnings();
    try { if (this.browser) await this.browser.close(); } catch { /* ignore */ }
  }
}

// --------------------------------------------------------------------------- ffmpeg
function encoder(cfg, outFile) {
  const fps = String(cfg.fps);
  const input = ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'image2pipe', '-framerate', fps,
    '-c:v', cfg.transparent ? 'png' : 'mjpeg', '-i', '-'];
  // 透明：Chrome 给的 RGBA PNG 原样封进 .mov（无损、不重编码；字幕/角标这类大片透明的画面很小，ffmpeg overlay 直接认 alpha）
  const output = cfg.transparent
    ? ['-c:v', 'copy']
    : ['-c:v', 'libx264', '-preset', cfg.preset || 'veryfast', '-crf', String(cfg.crf ?? 16), '-pix_fmt', 'yuv420p',
      '-movflags', '+faststart'];
  const proc = spawn(cfg.ffmpeg, [...input, ...output, '-r', fps, outFile], { stdio: ['pipe', 'ignore', 'pipe'] });
  let err = '';
  proc.stderr.on('data', (d) => { err += d.toString(); });
  const done = new Promise((resolve, reject) => {
    proc.on('error', reject);
    proc.on('close', (code) => (code === 0 ? resolve() : reject(new Error('ffmpeg 退出码 ' + code + '：' + err.slice(-800)))));
  });
  done.catch(() => { /* 被 kill 时不算未处理的 rejection；end() 里仍会抛出 */ });
  proc.stdin.on('error', () => { /* ffmpeg 提前退出时由 done 报错 */ });
  return {
    async write(buf) {
      if (!proc.stdin.write(buf)) await new Promise((r) => proc.stdin.once('drain', r));
    },
    async end() { proc.stdin.end(); await done; },
    kill() { try { proc.kill('SIGKILL'); } catch { /* ignore */ } },
  };
}

function runFfmpeg(ffmpeg, args) {
  return new Promise((resolve, reject) => {
    const p = spawn(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-y', ...args], { stdio: ['ignore', 'ignore', 'pipe'] });
    let err = '';
    p.stderr.on('data', (d) => { err += d.toString(); });
    p.on('error', reject);
    p.on('close', (c) => (c === 0 ? resolve() : reject(new Error('ffmpeg 退出码 ' + c + '：' + err.slice(-800)))));
  });
}

// --------------------------------------------------------------------------- 主流程
function failIfErrors(cfg, shared) {
  if (!cfg.allowErrors && shared.pageErrors.length) {
    const e = new Error('页面脚本出错：' + shared.pageErrors.slice(0, 3).join(' | '));
    e.pageError = true;
    throw e;
  }
}

async function render(cfg) {
  const t0 = Date.now();
  const shared = { pageErrors: [], consoleErrors: [], blocked: new Set(), missing: new Set(), rewritten: new Set(), warnings: new Set() };
  const importmap = buildImportMap();
  const { chromium } = await loadPlaywright();
  const result = { ok: false, width: cfg.width, height: cfg.height, fps: cfg.fps };
  const sessions = [];
  const encoders = [];
  try {
    const first = new Session(cfg, importmap, shared);
    sessions.push(first);
    await first.open(chromium);
    const info = await first.goto();
    failIfErrors(cfg, shared);
    result.mode = info.hasRender ? 'render(t)' : 'virtual-time';
    const duration = cfg.duration != null ? Number(cfg.duration) : info.dur;

    if (cfg.times && cfg.times.length) {                       // ---- 预览静帧
      const want = [...new Set(cfg.times.map((t) => Math.max(0, Math.round(Number(t) * cfg.fps))))].sort((a, b) => a - b);
      fs.mkdirSync(cfg.stillsDir, { recursive: true });
      const stills = [];
      const ext = cfg.transparent ? 'png' : 'jpg';
      const wantSet = new Set(want);
      const last = want[want.length - 1];
      if (info.hasRender) {
        for (const k of want) {
          const t = k / cfg.fps;
          await first.step(t, true);
          failIfErrors(cfg, shared);
          const file = path.join(cfg.stillsDir, 't_' + t.toFixed(3) + '.' + ext);
          fs.writeFileSync(file, await first.shot());
          stills.push({ t, file });
        }
      } else {
        for (let k = 0; k <= last; k++) {
          const t = k / cfg.fps;
          await first.step(t, wantSet.has(k));
          failIfErrors(cfg, shared);
          if (wantSet.has(k)) {
            const file = path.join(cfg.stillsDir, 't_' + t.toFixed(3) + '.' + ext);
            fs.writeFileSync(file, await first.shot());
            stills.push({ t, file });
          }
        }
      }
      result.stills = stills;
      result.frames = stills.length;
      result.duration = duration;
    } else {                                                   // ---- 整段视频
      if (!(duration > 0)) throw new Error('页面没有 window.DUR，也没给 duration：不知道要录多长');
      if (duration > cfg.maxDuration) throw new Error('duration ' + duration + ' 秒超过单次上限 ' + cfg.maxDuration + ' 秒，分段渲染再 concat');
      const n = Math.max(1, Math.round(duration * cfg.fps));
      let workers = Math.max(1, Math.min(cfg.workers || 1, Math.ceil(n / 45)));
      if (!info.hasRender && !cfg.workersExplicit) workers = 1;   // 虚拟时钟页面有状态，默认单 worker 顺序录
      const sheetIdx = new Set();
      const sheetN = Math.min(cfg.sheetCount || 8, n);
      for (let i = 0; i < sheetN; i++) sheetIdx.add(Math.min(n - 1, Math.round((i + 0.5) * n / sheetN)));
      fs.mkdirSync(cfg.sheetDir, { recursive: true });
      const sheet = [];
      const ranges = [];
      for (let w = 0; w < workers; w++) ranges.push([Math.floor(w * n / workers), Math.floor((w + 1) * n / workers)]);
      const ext = path.extname(cfg.out) || (cfg.transparent ? '.mov' : '.mp4');
      const segs = workers === 1 ? [cfg.out] : ranges.map((_, w) => cfg.out + '.seg' + w + ext);
      while (sessions.length < workers) {
        const s = new Session(cfg, importmap, shared);
        sessions.push(s);
      }
      let doneFrames = 0;
      const deadline = Date.now() + cfg.timeoutSec * 1000;
      const runWorker = async (w) => {
        const s = sessions[w];
        if (w > 0) {
          await s.open(chromium);
          await s.goto();
          failIfErrors(cfg, shared);
        }
        const [a, b] = ranges[w];
        if (!info.hasRender) {                                 // 并行时快进到本段起点（只跑逻辑，不截图）
          for (let k = 0; k < a; k++) await s.step(k / cfg.fps, false);
        }
        const enc = encoder(cfg, segs[w]);
        encoders.push(enc);
        for (let k = a; k < b; k++) {
          if (Date.now() > deadline) throw new Error('渲染超时（' + Math.round(cfg.timeoutSec / 60) + ' 分钟），已完成 ' + doneFrames + '/' + n + ' 帧');
          const t = k / cfg.fps;
          await s.step(t, true);
          failIfErrors(cfg, shared);
          const buf = await s.shot();
          await enc.write(buf);
          if (sheetIdx.has(k)) {
            const file = path.join(cfg.sheetDir, 'f_' + String(k).padStart(5, '0') + (cfg.transparent ? '.png' : '.jpg'));
            fs.writeFileSync(file, buf);
            sheet.push({ t, file });
          }
          doneFrames++;
          if (doneFrames % 30 === 0) log('frame ' + doneFrames + '/' + n);
        }
        await enc.end();
      };
      await Promise.all(ranges.map((_, w) => runWorker(w)));
      if (workers > 1) {
        const list = cfg.out + '.concat.txt';
        fs.writeFileSync(list, segs.map((s) => "file '" + s.replace(/'/g, "'\\''") + "'").join('\n') + '\n');
        try {
          await runFfmpeg(cfg.ffmpeg, ['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy',
            ...(cfg.transparent ? [] : ['-movflags', '+faststart']), cfg.out]);
        } finally {
          for (const s of segs) { try { fs.unlinkSync(s); } catch { /* ignore */ } }
          try { fs.unlinkSync(list); } catch { /* ignore */ }
        }
      }
      sheet.sort((x, y) => x.t - y.t);
      result.sheet = sheet;
      result.frames = n;
      result.duration = n / cfg.fps;
      result.workers = workers;
    }
    result.ok = true;
  } catch (e) {
    result.ok = false;
    result.error = String((e && e.message) || e);
    result.page_error = !!(e && e.pageError);
    for (const enc of encoders) enc.kill();
  } finally {
    await Promise.all(sessions.map((s) => s.close()));
  }
  result.blocked_requests = [...shared.blocked].slice(0, 50);
  result.missing_files = [...shared.missing].slice(0, 50);
  result.rewritten = [...shared.rewritten].slice(0, 50);
  result.page_errors = shared.pageErrors.slice(0, 20);
  result.console_errors = shared.consoleErrors.slice(0, 20);
  result.warnings = [...shared.warnings].slice(0, 20);
  result.seconds = (Date.now() - t0) / 1000;
  return result;
}

// --------------------------------------------------------------------------- 入口
function parseCli(argv) {
  const cfg = { root: process.cwd(), fps: 30, width: 1920, height: 1080, transparent: false, times: null,
    duration: null, query: '', allowErrors: false, workers: 3, timeoutSec: 1800 };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const v = () => argv[++i];
    if (a === '--duration') cfg.duration = Number(v());
    else if (a === '--fps') cfg.fps = Number(v());
    else if (a === '--size') { const [w, h] = v().split('x').map(Number); cfg.width = w; cfg.height = h; }
    else if (a === '--transparent') cfg.transparent = true;
    else if (a === '--times') cfg.times = v().split(',').map(Number);
    else if (a === '--out') cfg.out = v();
    else if (a === '--root') cfg.root = v();
    else if (a === '--query') cfg.query = v();
    else if (a === '--workers') { cfg.workers = Number(v()); cfg.workersExplicit = true; }
    else if (a === '--allow-errors') cfg.allowErrors = true;
    else if (a === '--gpu') cfg.gpu = true;
    else if (a === '-h' || a === '--help') {
      process.stdout.write('用法：render-html <page.html> [--duration 秒] [--fps 30] [--size 1920x1080] [--transparent]\n' +
        '        [--times 0,1.5,3] [--out out.mp4|out.mov] [--root 目录] [--query k=v] [--workers N] [--allow-errors]\n' +
        '页面定义 window.render(t) 就逐帧调用；否则用虚拟时钟录 rAF/CSS 动画/GSAP。透明背景出带 alpha 的 .mov（PNG 编码）。\n');
      process.exit(0);
    } else rest.push(a);
  }
  if (!rest.length) throw new Error('缺少页面路径（render-html --help）');
  cfg.root = path.resolve(cfg.root);
  const entryAbs = path.resolve(rest[0]);
  cfg.entry = path.relative(cfg.root, entryAbs);
  if (cfg.entry.startsWith('..')) throw new Error('页面要在 --root 目录里：' + rest[0]);
  const stem = path.basename(entryAbs).replace(/\.[^.]+$/, '');
  cfg.out = path.resolve(cfg.out || path.join(path.dirname(entryAbs), stem + (cfg.transparent ? '.mov' : '.mp4')));
  cfg.stillsDir = path.join(path.dirname(cfg.out), stem + '_stills');
  cfg.sheetDir = path.join(path.dirname(cfg.out), stem + '_sheet');
  return cfg;
}

function normalize(cfg) {
  cfg.ffmpeg = cfg.ffmpeg || process.env.STUDIO_FFMPEG || 'ffmpeg';
  cfg.fps = Number(cfg.fps || 30);
  cfg.width = Math.round(Number(cfg.width || 1920) / 2) * 2;
  cfg.height = Math.round(Number(cfg.height || 1080) / 2) * 2;
  cfg.jpegQuality = cfg.jpegQuality || 92;
  cfg.maxDuration = cfg.maxDuration || 600;
  cfg.timeoutSec = cfg.timeoutSec || 1800;
  cfg.gpu = cfg.gpu || process.env.STUDIO_WEB_GPU === '1';
  if (cfg.workers == null) cfg.workers = Number(process.env.STUDIO_WEB_WORKERS || 3);
  return cfg;
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === '--print-importmap') {                // 重建 toolbox/web/importmap.json 用
    process.stdout.write(JSON.stringify(buildImportMap(false), null, 1) + '\n');
    return;
  }
  let cfg;
  let cli = false;
  if (argv[0] === '--config') {
    const src = argv[1] && argv[1] !== '-' ? fs.readFileSync(argv[1], 'utf8') : fs.readFileSync(0, 'utf8');
    cfg = JSON.parse(src);
  } else {
    cli = true;
    cfg = parseCli(argv);
  }
  normalize(cfg);
  const res = await render(cfg);
  if (cli && res.ok) {
    res.file = cfg.times ? undefined : cfg.out;
  }
  process.stdout.write(JSON.stringify(res) + '\n');
  process.exit(res.ok ? 0 : 1);
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.stack) || e) }) + '\n');
  process.exit(1);
});
