# -*- coding: utf-8 -*-
"""search：联网检索，返回最多 5 条 title / url / snippet。只给资料，不给标注。

后端由 STUDIO_SEARCH_PROVIDER 选：
· web     免 key 的网页检索：360 / 百度 / DuckDuckGo / 必应按顺序换着用（见下），出网要代理
· serper  Serper 兼容的 JSON 接口，需要 key（内网网关就换 STUDIO_SEARCH_URL）
· bing    旧配置的写法，现在等同 web（单用必应，中文查询基本不可用，见下）
auto（默认）：配了 STUDIO_SEARCH_API_KEY 走 serper，否则走 web。

在只能通过代理出网的环境里，必须给 STUDIO_SEARCH_PROXY：
run.py 只把 STUDIO_/H3_/QWEN_ 等前缀透传给 MCP server，http_proxy 不会跟着过去。

2026-10-25 经代理实测（出口 IP 在国内，www.bing.com 一律跳到 cn.bing.com）：
· 必应只按查询的第一个词搜：「欧达M5 口袋云台相机 参数」→ 全是「欧」字的字典页，「helium flash red giant star」
  → Helium 浏览器。RSS / HTML、count 5 或 10、setmkt / cc、加引号、去空格、换 UA、先拿 cookie 都一样；
  ensearch=1 给的是无关的新闻、日文页。不是代码问题，是必应对这个出口降级
· DuckDuckGo html 版中英文都好（10 条固定查询每条都给满 5 条相关结果）；lite 版第一次就给 202 反爬页
· 360（www.so.com）中文结果好，但同一出口连查十来次就要验证码；百度要先访问首页拿 cookie，连查两次就跳安全验证
· 搜狗一律跳 antispider，不用
默认顺序：DuckDuckGo、360、百度、必应（STUDIO_SEARCH_ENGINES 可改）。
每个引擎的结果先还原成真实链接、过屏蔽表，再按查询词覆盖率打分，够好就用，不够就换下一个（条数不够时用已经查到的
其它引擎补齐，不多发请求）；最好的一组也不沾边就带 quality=low 返回，让模型知道这批资料不可信。
遇到验证码/反爬页的引擎在本进程内冷却 10 分钟；同一个站两次请求之间至少隔 STUDIO_SEARCH_INTERVAL 秒。

STUDIO_SEARCH_PROVIDER   web | bing | serper | auto（默认 auto）
STUDIO_SEARCH_ENGINES    web 模式的引擎顺序，逗号分隔，取值 360 / baidu / ddg / bing；不设就用上面的默认顺序
STUDIO_SEARCH_INTERVAL   同一个站两次请求的最小间隔秒数（默认 1）
STUDIO_SEARCH_PROXY      出网代理，如 http://<internal-domain>:3128；不填则回落到 https_proxy / http_proxy
STUDIO_SEARCH_MKT        必应市场（默认不传；传 zh-CN 实测会把「岳阳楼」带偏成「岳阳市」）
STUDIO_SEARCH_LANG       必应界面语言（默认空）
STUDIO_SEARCH_HL         serper 语言（默认 zh-cn）
STUDIO_SEARCH_URL        serper 地址（默认 https://google.serper.dev/search）
STUDIO_SEARCH_API_KEY    serper key，只 export
STUDIO_SEARCH_KEY_HEADER serper key 放哪个请求头（默认 X-API-KEY）
STUDIO_SEARCH_TIMEOUT    单次超时秒数（默认 30）
STUDIO_SEARCH_BLOCK_FILE 屏蔽表（build_csv_tasks.py 写的 search_block.json）：题目由真实成片转写时，
                         检索结果里不能出现原片页面。命中原片链接、视频站域名、原片标题或指定短语的结果直接丢掉，
                         不告诉模型；丢了哪些记到运行目录的 search_blocked.jsonl（工作目录外，模型读不到）。
                         对所有引擎都生效；引擎跳转链接还原不出真实地址的结果一律丢掉（没法按域名判断）。
                         设了但读不了就报不可用（宁可不给检索，也不漏）
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .common import Unavailable, env, env_int

NAME = "web"                                             # 默认后端：不配 key 也能用
SERPER_DEFAULT = "https://google.serper.dev/search"
BING_HOME = "https://www.bing.com/"
BING_URL = "https://www.bing.com/search"
SO_URL = "https://www.so.com/s"
BAIDU_HOME = "https://www.baidu.com/"
BAIDU_URL = "https://www.baidu.com/s"
DDG_URL = "https://html.duckduckgo.com/html/"
ENGINES = ("ddg", "360", "baidu", "bing")              # 也是默认顺序
LABEL = {"360": "360", "baidu": "baidu", "ddg": "duckduckgo", "bing": "bing"}
COOLDOWN_S = 600                                         # 给了验证码的引擎，本进程内多久不再找它
GOOD = 0.5                                               # 一组结果的覆盖率到这个数就不再换引擎
LOW = 0.3                                                # 最好的一组也低于这个数：quality=low
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NOISE = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", re.S | re.I)
_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_UNIT = re.compile(r"[㐀-鿿豈-﫿]+|[0-9a-z]+")
_STOP = {"the", "an", "of", "in", "on", "and", "or", "for", "to", "is", "are", "was", "what", "how", "why",
         "when", "with", "by", "at", "from", "vs", "does", "do"}
_CHALLENGE = re.compile(r"antispider|captcha|wappass|安全验证|异常访问|anomaly\.js|challenge-form|unusual traffic", re.I)
_ALGO = re.compile(r'<li class="b_algo".*?</li>', re.S)                   # 必应 HTML 结果块
_H2A = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIP = re.compile(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>|<p[^>]*>(.*?)</p>', re.S)

_JAR: Optional[http.cookiejar.CookieJar] = None
_WARMED: set = set()
_LAST: Dict[str, float] = {}                             # 站点 → 上次请求时间
_COOL: Dict[str, float] = {}                             # 引擎 → 冷却到什么时候


class _Challenge(RuntimeError):
    """引擎给了验证码/反爬页。"""


# --------------------------------------------------------------------------- 配置
def provider() -> str:
    want = env("STUDIO_SEARCH_PROVIDER", "auto").lower()
    if want == "serper":
        return "serper"
    if want in ("web", "bing"):                          # bing 是旧写法：单用必应中文不可靠，一律走 web
        return "web"
    return "serper" if env("STUDIO_SEARCH_API_KEY") else "web"


def proxy() -> str:
    return (env("STUDIO_SEARCH_PROXY") or env("HTTPS_PROXY") or env("https_proxy")
            or env("HTTP_PROXY") or env("http_proxy"))


def backend_name() -> str:
    return "serper" if provider() == "serper" else "web"


def engines(query: str) -> List[str]:
    """web 模式按顺序要试的引擎（现在和查询内容无关，留着 query 参数方便以后按语种分流）。"""
    want = [e.strip().lower() for e in env("STUDIO_SEARCH_ENGINES").split(",") if e.strip()]
    return list(dict.fromkeys([e for e in want if e in ENGINES] or ENGINES))


def available() -> Tuple[bool, str]:
    if env("STUDIO_SEARCH_BLOCK_FILE"):
        try:
            _block()
        except Exception as exc:  # noqa: BLE001
            return False, "屏蔽表读不了（%s）：%s" % (env("STUDIO_SEARCH_BLOCK_FILE"), exc)
    p, px = provider(), proxy()
    if p == "serper":
        if not env("STUDIO_SEARCH_API_KEY"):
            return False, "未配置 STUDIO_SEARCH_API_KEY（或设 STUDIO_SEARCH_PROVIDER=web 走免 key 的网页检索）"
        return True, "%s 代理=%s" % (env("STUDIO_SEARCH_URL", SERPER_DEFAULT), px or "直连")
    return True, "web（%s）代理=%s" % ("/".join(engines("")), px or "直连")


# --------------------------------------------------------------------------- HTTP
def _opener() -> urllib.request.OpenerDirector:
    global _JAR
    if _JAR is None:
        _JAR = http.cookiejar.CookieJar()               # 进程内复用：必应、百度不带 cookie 会给垃圾结果或验证码
    px = proxy()
    handlers: List[Any] = [urllib.request.HTTPCookieProcessor(_JAR)]
    handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}) if px
                    else urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _pace(url: str) -> None:
    """同一个站两次请求之间至少隔 STUDIO_SEARCH_INTERVAL 秒，别刷接口。"""
    try:
        interval = float(env("STUDIO_SEARCH_INTERVAL", "1"))
    except ValueError:
        interval = 1.0
    host = urllib.parse.urlparse(url).hostname or ""
    wait = interval - (time.time() - _LAST.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _LAST[host] = time.time()


def _request(url: str, data: bytes = b"", headers: Dict[str, str] = None,
             method: str = "GET") -> Tuple[int, str, bytes]:
    """返回（状态码, 跳转后的地址, 内容）。HTTP 错误码照样返回；网络不通、超时报 Unavailable。"""
    _pace(url)
    req = urllib.request.Request(
        url, data=data or None, method=method,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                 **(headers or {})})
    try:
        with _opener().open(req, timeout=env_int("STUDIO_SEARCH_TIMEOUT", 30)) as resp:
            return resp.status, resp.geturl(), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except Exception:  # noqa: BLE001
            body = b""
        return exc.code, url, body
    except urllib.error.URLError as exc:
        hint = ("（已用代理 %s）" % proxy()) if proxy() else \
               "（未配置 STUDIO_SEARCH_PROXY；本机没有直连外网，不配代理一定失败）"
        raise Unavailable("检索请求失败：%s%s" % (exc.reason, hint)) from None
    except (socket.timeout, TimeoutError):
        raise Unavailable("检索请求超时：%s" % url) from None


def _fetch(url: str, data: bytes = b"", headers: Dict[str, str] = None, method: str = "GET") -> bytes:
    status, _, body = _request(url, data, headers, method)
    if status >= 400:
        raise RuntimeError("检索请求失败：HTTP %s" % status)
    return body


def _page(url: str) -> Tuple[int, str, str]:
    status, final, body = _request(url, headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    return status, final, body.decode("utf-8", "replace")


def _challenged(final: str, page: str, query: str) -> bool:
    """是不是验证码/反爬页：看跳转后的 host + path，以及去掉查询词回显后的页面开头。"""
    p = urllib.parse.urlparse(final)
    if _CHALLENGE.search((p.hostname or "") + p.path):
        return True
    return bool(_CHALLENGE.search(page[:30000].replace(query, "")))


def _warm(engine: str, url: str) -> None:
    """先访问一次首页拿 cookie（必应不带 cookie 偶发给垃圾结果，百度不带 cookie 直接跳安全验证）；失败不影响后面。"""
    if engine in _WARMED:
        return
    _WARMED.add(engine)
    try:
        _request(url)
    except Exception:                                # noqa: BLE001
        pass


# --------------------------------------------------------------------------- 清洗与打分
def _text(raw: str, limit: int = 400) -> str:
    if not raw:
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub("", raw))).strip()[:limit]


def _inner(chunk: str, cls: str) -> str:
    """chunk 里第一个 class 含 cls 的元素的内部 HTML（截到同名闭合标签；嵌套同名标签会截短，当摘要够用）。"""
    m = re.search(r'<(\w+)[^>]*\bclass="[^"]*\b%s[^"]*"[^>]*>' % re.escape(cls), chunk)
    if not m:
        return ""
    end = chunk.find("</%s>" % m.group(1), m.end())
    return chunk[m.end(): end if end >= 0 else m.end() + 1000]


def _units(query: str) -> List[str]:
    """查询拆成比对单位：汉字连续段取相邻两字（单字段取单字），字母数字取整词（小写，去掉虚词和单个字母）。"""
    q = unicodedata.normalize("NFKC", query).lower()
    units: List[str] = []
    singles: List[str] = []
    for run in _UNIT.findall(q):
        if _CJK.match(run):
            if len(run) == 1:
                singles.append(run)
            else:
                units += [run[i:i + 2] for i in range(len(run) - 1)]
        elif len(run) > 1 and run not in _STOP:
            units.append(run)
    if len(units) < 2:
        units += singles
    return list(dict.fromkeys(units))


def _coverage(row: Dict[str, str], units: List[str]) -> float:
    """这条结果的标题 + 摘要覆盖了多大比例的查询单位。用来识别引擎降级给的垃圾结果。"""
    if not units:
        return 1.0
    blob = _WS.sub("", unicodedata.normalize("NFKC", (row.get("title") or "") + (row.get("snippet") or "")).lower())
    return sum(1 for u in units if u in blob) / float(len(units))


def _rate(rows: List[Dict[str, str]], units: List[str], n: int) -> float:
    """一组结果的得分：前 n 条覆盖率的均值；条数不到 min(n, 3) 时按比例打折。"""
    top = rows[:n]
    if not top:
        return 0.0
    mean = sum(_coverage(r, units) for r in top) / len(top)
    return mean * min(1.0, len(top) / float(min(n, 3)))


def _on(host: str, *domains: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _usable(url: str) -> bool:
    """结果链接必须是真实地址。引擎自己的跳转、聚合页没法按域名屏蔽，也没法给模型引用，丢掉。"""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host or re.match(r"^\d+\.\w+\.baidu\.com$", host):
        return False
    if _on(host, "bing.com", "duckduckgo.com", "360kan.com", "ubs.baidu.com"):
        return False
    return host not in ("so.com", "www.so.com", "m.so.com", "baidu.com", "www.baidu.com", "m.baidu.com")


# --------------------------------------------------------------------------- 360
def _so360(query: str, n: int) -> List[Dict[str, str]]:
    status, final, page = _page(SO_URL + "?" + urllib.parse.urlencode({"q": query}))
    if status != 200 or ('class="res-list"' not in page and _challenged(final, page, query)):
        raise _Challenge("360：HTTP %s %s" % (status, urllib.parse.urlparse(final).path))
    page = _NOISE.sub(" ", page)
    rows: List[Dict[str, str]] = []
    for chunk in page.split('<li class="res-list"')[1:]:
        h3 = re.search(r'<h3[^>]*class="res-title[^"]*"[^>]*>(.*?)</h3>', chunk, re.S)
        if not h3:
            continue
        md = re.search(r'data-mdurl="([^"]+)"', h3.group(1))          # 真实地址；href 是 so.com/link 跳转
        url = html.unescape(md.group(1)) if md else ""
        if not _usable(url):
            continue
        rest = chunk[h3.end():]
        snip = _inner(rest, "res-desc") or _inner(rest, "res-list-summary") or _inner(rest, "res-comm-con")
        rows.append({"title": _text(h3.group(1), 200), "url": url, "snippet": _text(snip) or _text(rest, 200)})
        if len(rows) >= n:
            break
    return rows


# --------------------------------------------------------------------------- 百度
def _baidu(query: str, n: int) -> List[Dict[str, str]]:
    _warm("baidu", BAIDU_HOME)
    status, final, page = _page(BAIDU_URL + "?" + urllib.parse.urlencode({"wd": query, "rn": "10"}))
    if status != 200 or ("c-container" not in page and _challenged(final, page, query)):
        raise _Challenge("百度：HTTP %s %s" % (status, urllib.parse.urlparse(final).hostname))
    page = _NOISE.sub(" ", page)
    heads = list(re.finditer(r'<div class="result(?:-op)? c-container[^"]*"([^>]*)>', page))
    rows: List[Dict[str, str]] = []
    for i, m in enumerate(heads):
        block = page[m.end(): heads[i + 1].start() if i + 1 < len(heads) else m.end() + 30000]
        mu = re.search(r'\bmu="([^"]*)"', m.group(1))                  # 真实地址；href 是 baidu.com/link 跳转
        url = html.unescape(mu.group(1)) if mu else ""
        title = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        if not title or not _usable(url):
            continue
        rest = block[title.end():]
        snip = _inner(rest, "summary-text") or _inner(rest, "c-abstract") or _inner(rest, "content-right")
        rows.append({"title": _text(title.group(1), 200), "url": url, "snippet": _text(snip) or _text(rest, 200)})
        if len(rows) >= n:
            break
    return rows


# --------------------------------------------------------------------------- DuckDuckGo
def _ddg_url(href: str) -> str:
    """//duckduckgo.com/l/?uddg=<真实地址>&rut=… → 真实地址；其它原样返回。"""
    if href.startswith("//"):
        href = "https:" + href
    p = urllib.parse.urlparse(href)
    if _on((p.hostname or "").lower(), "duckduckgo.com") and p.path.startswith("/l/"):
        return (urllib.parse.parse_qs(p.query).get("uddg") or [""])[0]
    return href


def _ddg(query: str, n: int) -> List[Dict[str, str]]:
    status, final, page = _page(DDG_URL + "?" + urllib.parse.urlencode({"q": query}))
    if status != 200 or ("result__a" not in page and _challenged(final, page, query)):
        raise _Challenge("DuckDuckGo：HTTP %s" % status)          # 202 + anomaly 页 = 被当成机器人
    rows: List[Dict[str, str]] = []
    for block in page.split('<div class="result results_links')[1:]:
        if "result--ad" in block[:300]:
            continue
        a = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        url = _ddg_url(html.unescape(a.group(1)))
        if not _usable(url):
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        rows.append({"title": _text(a.group(2), 200), "url": url, "snippet": _text(snip.group(1)) if snip else ""})
        if len(rows) >= n:
            break
    return rows


# --------------------------------------------------------------------------- 必应
def _bing_params(query: str, n: int) -> Dict[str, str]:
    params = {"q": query, "count": str(n)}
    if env("STUDIO_SEARCH_MKT"):                     # 默认不传：传 zh-CN 会把「岳阳楼」查成「岳阳市」
        params["setmkt"] = env("STUDIO_SEARCH_MKT")
    if env("STUDIO_SEARCH_LANG"):
        params["setlang"] = env("STUDIO_SEARCH_LANG")
    return params


def _bing_rss(query: str, n: int, extra: Dict[str, str]) -> List[Dict[str, str]]:
    params = dict(_bing_params(query, n), format="rss", **extra)
    try:
        raw = _fetch(BING_URL + "?" + urllib.parse.urlencode(params))
        root = ET.fromstring(raw.decode("utf-8", "replace"))
    except (ET.ParseError, RuntimeError):
        return []                                    # 偶发空包，交给上层换 HTML
    rows: List[Dict[str, str]] = []
    for item in root.iter("item"):
        link = _real_url(_text(item.findtext("link") or "", 500))
        if not _usable(link):
            continue
        rows.append({"title": _text(item.findtext("title") or "", 200), "url": link,
                     "snippet": _text(item.findtext("description") or "")})
        if len(rows) >= n:
            break
    return rows


def _bing_html(query: str, n: int, extra: Dict[str, str]) -> List[Dict[str, str]]:
    params = dict(_bing_params(query, n), **extra)
    page = _fetch(BING_URL + "?" + urllib.parse.urlencode(params)).decode("utf-8", "replace")
    rows: List[Dict[str, str]] = []
    for block in _ALGO.findall(page):
        m = _H2A.search(block)
        if not m:
            continue
        url = _real_url(html.unescape(m.group(1)))   # /ck/a 跳转 → 真实地址；还原不了的丢掉
        if not _usable(url):
            continue
        snip = _SNIP.search(block)
        rows.append({"title": _text(m.group(2), 200), "url": url,
                     "snippet": _text((snip.group(1) or snip.group(2) or "") if snip else "")})
        if len(rows) >= n:
            break
    return rows


def _bing(query: str, n: int) -> List[Dict[str, str]]:
    """RSS 优先，空包时退回 HTML。这个出口上只按第一个词搜（见文件头），排在最后兜底。"""
    _warm("bing", BING_HOME + "?mkt=zh-CN")
    return _bing_rss(query, n, {}) or _bing_html(query, n, {})


_ENGINE_FN: Dict[str, Callable[[str, int], List[Dict[str, str]]]] = {
    "360": _so360, "baidu": _baidu, "ddg": _ddg, "bing": _bing}


def _web(query: str, n: int, fetch: int) -> Tuple[str, List[Dict[str, str]], float, List[str]]:
    """按顺序试引擎：结果先过屏蔽表再打分，够好就停；都不够好就用最好的一组，条数不够时用其它组补齐。
    返回（后端标签, 结果, 得分, 失败说明）。"""
    units = _units(query)
    tried: List[Tuple[str, List[Dict[str, str]], float]] = []
    errors: List[str] = []
    for name in engines(query):
        if _COOL.get(name, 0.0) > time.time():
            errors.append("%s 冷却中（刚给过验证码）" % LABEL[name])
            continue
        try:
            raw = _ENGINE_FN[name](query, fetch)
        except _Challenge as exc:
            _COOL[name] = time.time() + COOLDOWN_S
            errors.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001  某个引擎不通或改版，换下一个
            errors.append("%s：%s" % (LABEL[name], str(exc)[:200]))
            continue
        seen: set = set()
        rows = []
        for r in _filter(query, raw):
            if r["url"] not in seen:
                seen.add(r["url"])
                rows.append(r)
        score = _rate(rows, units, n)
        tried.append((name, rows, score))
        if score >= GOOD and len(rows) >= min(n, 3):
            break
    if not tried:
        return "web", [], 0.0, errors
    tried.sort(key=lambda t: -t[2])
    name, best, score = tried[0]
    label, rows = LABEL[name], list(best[:n])
    seen = {r["url"] for r in rows}
    for other, more, _ in tried[1:]:                 # 不够数：用已经查到的其它引擎补，不多发请求
        added = False
        for r in more:
            if len(rows) >= n:
                break
            if r["url"] not in seen and _coverage(r, units) >= LOW:
                rows.append(r)
                seen.add(r["url"])
                added = True
        if added:
            label += "+" + LABEL[other]
    return label, rows, score, errors


# --------------------------------------------------------------------------- serper
def _serper(query: str, n: int) -> List[Dict[str, str]]:
    url = env("STUDIO_SEARCH_URL", SERPER_DEFAULT)
    body = json.dumps({"q": query, "num": n, "hl": env("STUDIO_SEARCH_HL", "zh-cn")}).encode("utf-8")
    raw = _fetch(url, data=body, method="POST",
                 headers={"Content-Type": "application/json",
                          env("STUDIO_SEARCH_KEY_HEADER", "X-API-KEY"): env("STUDIO_SEARCH_API_KEY")})
    try:
        data: Dict[str, Any] = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RuntimeError("%s 返回的不是 JSON：%s" % (url, exc)) from None
    rows: List[Dict[str, str]] = []
    for item in (data.get("organic") or [])[:n]:
        rows.append({"title": _text(item.get("title", ""), 200), "url": item.get("link", ""),
                     "snippet": _text(item.get("snippet", ""))})
    return rows


# --------------------------------------------------------------------------- 屏蔽原片
_PUNCT = re.compile(r"[\W_]+", re.U)
_BLOCK: Optional[Dict[str, Any]] = None


def _norm(text: str) -> str:
    """比对用：NFKC（全角转半角、14ᵗʰ→14th）、小写、去掉空白和全部标点/符号（书名号、emoji 等）。"""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text or "").lower())


def _real_url(url: str) -> str:
    """必应的 https://www.bing.com/ck/a?…&u=a1<base64url> 跳转链接 → 真实地址；其它原样返回。"""
    parsed = urllib.parse.urlparse(url)
    if not (parsed.hostname or "").endswith("bing.com") or not parsed.path.startswith("/ck/"):
        return url
    u = (urllib.parse.parse_qs(parsed.query).get("u") or [""])[0]
    if u.startswith("a1"):
        import base64
        try:
            raw = u[2:]
            return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return url
    return url


def _block() -> Dict[str, Any]:
    global _BLOCK
    if _BLOCK is None:
        path = env("STUDIO_SEARCH_BLOCK_FILE")
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        _BLOCK = {"url_keys": [k.lower() for k in data.get("url_keys") or [] if k],
                  "domains": [d.lower().lstrip(".") for d in data.get("domains") or [] if d],
                  "phrases": [n for n in (_norm(p) for p in data.get("phrases") or []) if n],
                  "all_of": [g for g in ([_norm(p) for p in grp if _norm(p)] for grp in data.get("all_of") or []) if g]}
    return _BLOCK


def _blocked(row: Dict[str, str]) -> Optional[str]:
    """命中屏蔽表时返回原因（url / domain / phrase / all_of），否则 None。"""
    b = _block()
    url = _real_url(row.get("url") or "").lower()
    host = urllib.parse.urlparse(url).hostname or ""
    blob = _norm((row.get("title") or "") + " " + (row.get("snippet") or ""))
    if any(k in url or _norm(k) in blob for k in b["url_keys"]):
        return "url"
    if any(host == d or host.endswith("." + d) for d in b["domains"]):
        return "domain"
    if any(p in blob for p in b["phrases"]):
        return "phrase"
    if any(all(p in blob for p in grp) for grp in b["all_of"]):
        return "all_of"
    return None


def _filter(query: str, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not env("STUDIO_SEARCH_BLOCK_FILE"):
        return rows
    kept, dropped = [], []
    for r in rows:
        why = _blocked(r)
        (dropped if why else kept).append(dict(r, reason=why) if why else r)
    run_dir = env("STUDIO_RUN_DIR")
    if dropped and run_dir:
        try:
            with open(Path(run_dir) / "search_blocked.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": round(time.time(), 1), "query": query, "dropped": dropped},
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass
    return kept


# --------------------------------------------------------------------------- 入口
def search(query: str, max_results: int = 5) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise ValueError("query 不能为空")
    ok, why = available()
    if not ok:
        raise Unavailable(why)
    n = max(1, min(5, int(max_results)))
    fetch = min(10, n + 5) if env("STUDIO_SEARCH_BLOCK_FILE") else n     # 有屏蔽表时多取几条，丢掉后还够
    out: Dict[str, Any] = {"backend": backend_name()}
    if provider() == "serper":
        rows = _filter(q, _serper(q, fetch))[:n]
    else:
        label, rows, score, errors = _web(q, n, fetch)
        out["backend"] = label
        rows = rows[:n]
        if not rows and errors:
            raise Unavailable("检索失败：%s" % "；".join(errors))
        if rows and score < LOW:
            out["quality"] = "low"
            out["note"] = ("这批结果和查询词几乎不沾边，别直接引用，"
                           "换个更具体的说法再查，或用英文关键词查一次。")
    if not rows:
        raise RuntimeError("检索没有结果（后端=%s，代理=%s）：换更宽的关键词再试"
                           % (out["backend"], proxy() or "直连"))
    out["results"] = rows
    return out
