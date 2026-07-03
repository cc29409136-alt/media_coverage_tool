import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT
from matcher import parse_relative_or_absolute, parse_date_from_url

TODAY = date.today()

# 跟 Python 版 `_extract_link_title()` 邏輯一致：卡片式版型常把整張卡（圖片＋
# 標題＋摘要＋更多按鈕）包在同一個 <a> 裡，優先找內部的標題子元素（h1~h4 或
# class 含 title/headline），找不到才退回整個 <a> 的文字。
# 同時往上找最近一層「合理大小」的父層容器文字當作 context，用來比對關鍵字——
# 很多新聞標題只寫藝人的暱稱/藝名（例如「小宇」），本名（例如「宋念宇」）只會
# 出現在摘要段落裡，只看標題會漏掉這些真正相關的報導。
_EXTRACT_ALL_LINKS = """
els => els.map(e => {
    const titleEl = e.querySelector('h1, h2, h3, h4, [class*="title" i], [class*="headline" i]');
    const title = (titleEl ? titleEl.textContent : e.textContent).trim();
    const ownText = e.textContent.trim();
    let context = null;
    if (ownText.length > 30) {
        context = ownText;
    } else {
        let node = e;
        for (let i = 0; i < 4; i++) {
            const parent = node.parentElement;
            if (!parent) break;
            const text = parent.textContent.trim();
            if (text.length >= 20) {
                if (text.length <= 600) context = text;
                break;
            }
            node = parent;
        }
    }
    return {title, href: e.href, context};
})
"""

_REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _all_links(page):
    try:
        return page.eval_on_selector_all("a", _EXTRACT_ALL_LINKS)
    except Exception:
        return []


_TITLE_TRAILING_BOILERPLATE_RE = re.compile(
    r'(繼續閱讀|閱讀全文|read more|更多內容|看更多).*$', re.IGNORECASE | re.DOTALL
)
_TITLE_MAX_LEN = 80


def _clean_title(title):
    """有些卡片式版型（image + 標題 + 摘要段落 + "繼續閱讀" CTA）整包被塞進同一個
    <a> 標籤，`a.get_text(strip=True)` 會把標題、摘要、日期、CTA 文字全部串在一起。
    這裡做防禦性清理：先砍掉常見的「繼續閱讀」之類的結尾 CTA 文字，
    再用長度上限截斷，避免摘要全文被當成標題顯示。真正首選的清理方式是在
    `_all_links_requests()` 裡優先找卡片內的標題子元素（見下方），這個函式
    只是抓不到專屬標題元素時的保底防線。
    """
    if not title:
        return title
    title = _TITLE_TRAILING_BOILERPLATE_RE.sub('', title).strip()
    if len(title) > _TITLE_MAX_LEN:
        # 嘗試在標點符號處截斷，找不到就硬切
        cut = title[:_TITLE_MAX_LEN]
        m = re.search(r'^(.*[。！？!?])', cut)
        title = m.group(1) if m else cut
    return title.strip()


# 中文新聞介紹藝人本名的慣用句型：暱稱緊接著本名，例如「小宇」宋念宇、
# 「老蕭」蕭敬騰，但也有不加引號直接寫「小宇宋念宇」的寫法。判斷原則：
# 只要關鍵字前面「不是」逗號/頓號這種列舉分隔符號（也不是文字開頭），就視為
# 緊鄰介紹句型、算命中；如果前面剛好是逗號/頓號，代表這是「一堆人名用頓號
# 列舉」的雜訊寫法（例如某篇報導列出多位音樂人名字，其中剛好包含搜尋的關鍵字，
# 但文章其實跟這個人無關），不算命中。
def _has_nickname_intro(text, keyword):
    idx = text.find(keyword)
    if idx <= 0:
        return False
    return text[idx - 1] not in "，,、 \n\t"


def _filter(links, keyword, url_must_contain, min_len=6):
    """keyword 可以是單一字串，也可以是多個別名組成的 list（由 app.py 拆解使用者
    輸入的「宋念宇、小宇」這類多別名字串而來）；符合任一別名即算命中。
    命中條件：關鍵字出現在標題裡，或標題用了暱稱、但摘要裡有「『暱稱』關鍵字」
    這種新聞慣用介紹句型（見 `_has_nickname_intro`）。不接受「關鍵字只是出現在
    摘要某處」這種寬鬆比對，避免把單純提及的雜訊文章也算進來。
    """
    keywords = [keyword] if isinstance(keyword, str) else keyword
    seen, results = set(), []
    for it in links:
        href, title = it["href"], it["title"]
        if href in seen:
            continue
        if not any(s in href for s in url_must_contain):
            continue
        if len(title) < min_len:
            continue
        context = it.get("context") or ""
        if not any(k in title or _has_nickname_intro(context, k) for k in keywords):
            continue
        seen.add(href)
        results.append({"title": _clean_title(title), "url": href})
    return results


# ---------------------------------------------------------------------------
# 輕量版（requests + BeautifulSoup）輔助函式
#
# 以下兩個函式是給「伺服器端渲染」的網站使用的替代方案：不需要啟動 Playwright /
# headless Chromium，只用一次 HTTP GET 抓 HTML 再用 BeautifulSoup 解析連結，
# 大幅降低記憶體與 CPU 用量，適合在 Streamlit Community Cloud 這類資源受限的
# 免費主機上執行。回傳的資料結構刻意與 `_all_links()`／Playwright 版本一致
# （list of {"title", "href"} dict），讓 `_filter()` 可以直接共用。
# ---------------------------------------------------------------------------

def _get_soup(url, timeout=20, extra_headers=None):
    """發送 GET 請求並回傳 BeautifulSoup 物件；失敗時回傳 None（呼叫端應視為 0 筆結果）。"""
    headers = dict(_REQUEST_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return BeautifulSoup(resp.text, "lxml")


_TITLE_ELEMENT_SELECTORS = (
    # 常見的「卡片式」版型會把整張卡（圖片＋標題＋摘要＋更多按鈕）包在同一個
    # <a> 裡，這裡優先找卡片內專屬的標題子元素，避免把摘要全文也當成標題。
    {"name": ["h1", "h2", "h3", "h4"]},
    {"attrs": {"class": re.compile(r'title|headline', re.IGNORECASE)}},
)


def _extract_link_title(a):
    """從 <a> 標籤取出標題文字：優先找內部的標題子元素（h1~h4 或 class 含
    title/headline），找不到才退回整個 <a> 的文字（並交給 `_clean_title()` 防禦性截斷）。
    """
    for sel in _TITLE_ELEMENT_SELECTORS:
        el = a.find(**sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return a.get_text(strip=True)


_CONTEXT_MAX_LEN = 600


def _extract_link_context(a):
    """取得關鍵字比對用的 context 文字（標題＋摘要）。
    有些卡片式版型把整張卡（標題＋摘要＋更多按鈕）整包塞進同一個 <a> 標籤
    （`_extract_link_title` 會從裡面挑出乾淨的標題子元素，但 <a> 自己完整的
    `get_text()` 仍然包含摘要全文）——這種情況下 <a> 自己的完整文字就是最準確
    的 context，不需要、也不應該往上找父層容器，因為父層容器常常是「一整頁全部
    搜尋結果」的共用外層，會把好幾篇文章的文字混在一起，反而抓不到正確範圍
    （這是實際踩到的 bug：CTWANT 的搜尋結果頁把 20 篇文章全部包在同一層 <div>
    裡，往上找父層容器只會拿到 20000+ 字的大雜燴，遠超長度上限直接被放棄）。
    只有當 <a> 自己的文字就只是標題本身、沒有額外摘要內容時，才退而求其次往上
    找父層容器（適用「標題」與「摘要」是分開的兄弟元素、而非同一個 <a> 的版型，
    例如 LTN 的 <li> 卡片）；父層容器一樣要做長度上限防呆，避免抓到共用外層。
    """
    own_text = a.get_text(strip=True)
    if len(own_text) > 30:
        return own_text
    node = a
    for _ in range(4):
        parent = node.parent
        if parent is None or getattr(parent, "name", None) in (None, "[document]", "html", "body"):
            break
        text = parent.get_text(" ", strip=True)
        if len(text) >= 20:
            return text if len(text) <= _CONTEXT_MAX_LEN else None
        node = parent
    return None


def _all_links_requests(soup, base_url):
    """等同於 Playwright 版 `_all_links()`：回傳 list of {"title", "href", "context"} dict。"""
    if soup is None:
        return []
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        title = _extract_link_title(a)
        context = _extract_link_context(a)
        links.append({"title": title, "href": href, "context": context})
    return links


# ---------------------------------------------------------------------------
# 文章發布日期擷取（給搜尋結果頁本身沒有日期資訊的站台使用）
#
# 這些站台的搜尋結果列表頁只有標題／連結，沒有日期，過去的作法是直接把
# date 設成 None，而 matcher.in_range() 對 date=None 的處理是「不主動排除」
# （交給人工判斷）——這造成 app.py 顯示端完全沒有做日期過濾，導致舊文章
# （例如好幾個月前的報導）跟著新文章一起顯示，使用者以為是「今天」的報導。
#
# 修正方式：對每篇候選文章额外發一次請求，抓文章本身的 HTML，嘗試從常見的
# meta tag / JSON-LD / <time> 標籤 / 內文日期文字擷取真正的發布日期，再拿
# 這個日期做 start_date <= d <= end_date 過濾。
# ---------------------------------------------------------------------------

_ARTICLE_DATE_MAX_CANDIDATES = 15  # 每個站台最多抓幾篇候選文章的日期（效能考量）
_ARTICLE_DATE_TIMEOUT = 8  # 單篇文章頁請求逾時秒數
_ARTICLE_DATE_MAX_WORKERS = 5  # 平行抓取的執行緒數

_ISO_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')
_SLASH_DATE_RE = re.compile(r'(\d{4})/(\d{2})/(\d{2})')


def _parse_date_string(s):
    """把常見的日期字串（ISO 8601、YYYY-MM-DD、YYYY/MM/DD 等）轉成 date 物件，失敗回傳 None。"""
    if not s:
        return None
    s = s.strip()
    # ISO 8601，含時區／時間部分，例如 2026-07-03T10:20:00+08:00
    iso_candidate = s
    # 常見的 "Z" 結尾轉成 fromisoformat 看得懂的格式
    if iso_candidate.endswith("Z"):
        iso_candidate = iso_candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass
    m = _ISO_DATE_RE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _SLASH_DATE_RE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _extract_date_from_jsonld(soup):
    """在 <script type="application/ld+json"> 區塊中尋找 datePublished 欄位。"""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            # 有些站台把實際內容包在 @graph 裡
            graph = item.get("@graph")
            sub_items = graph if isinstance(graph, list) else [item]
            for sub in sub_items:
                if not isinstance(sub, dict):
                    continue
                val = sub.get("datePublished") or sub.get("dateCreated") or sub.get("dateModified")
                d = _parse_date_string(val) if isinstance(val, str) else None
                if d:
                    return d
    return None


def _fetch_article_date(url, timeout=_ARTICLE_DATE_TIMEOUT):
    """對單篇文章 URL 發一次 GET，嘗試多種方式擷取發布日期，失敗回傳 None。"""
    try:
        resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return None

    # 1. <meta property="article:published_time" content="...">
    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta and meta.get("content"):
        d = _parse_date_string(meta["content"])
        if d:
            return d

    # 2. 其他常見的 meta 日期標籤
    for attrs in (
        {"name": "pubdate"},
        {"name": "date"},
        {"itemprop": "datePublished"},
        {"property": "og:updated_time"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            d = _parse_date_string(meta["content"])
            if d:
                return d

    # 3. JSON-LD 區塊裡的 datePublished
    d = _extract_date_from_jsonld(soup)
    if d:
        return d

    # 4. <time datetime="...">
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag and time_tag.get("datetime"):
        d = _parse_date_string(time_tag["datetime"])
        if d:
            return d
    if time_tag:
        d = _parse_date_string(time_tag.get_text())
        if d:
            return d

    # 5. 最後手段：在頁面前段文字中用 regex 找 YYYY-MM-DD / YYYY/MM/DD
    text_sample = resp.text[:5000]
    d = _parse_date_string(text_sample)
    if d:
        return d

    return None


def _attach_dates(matched, start_date, end_date, max_candidates=_ARTICLE_DATE_MAX_CANDIDATES):
    """對 matched（list of {"title","url"}）逐篇（限制筆數、平行處理）抓發布日期，
    並依 start_date/end_date 過濾。跟專案裡其他日期比對邏輯（見 matcher.in_range）
    採取一致的保守原則：只有「明確抓到日期、且確定不在範圍內」才排除；
    抓不到日期（頁面結構特殊、逾時等）一律保留、標記 date=None，交給人工判斷，
    不因為抓不到證據就主動當作不符合——這跟關鍵字比對本身已經抓到候選文章的
    前提矛盾（沒道理標題明明符合，卻因為抓不到日期就整篇消失不見）。
    超過 max_candidates 篇的候選文章，因效能考量不逐篇抓日期，同樣保留、標記 date=None。
    """
    to_check = matched[:max_candidates]
    overflow = matched[max_candidates:]
    results = []
    if to_check:
        with ThreadPoolExecutor(max_workers=_ARTICLE_DATE_MAX_WORKERS) as executor:
            future_to_item = {executor.submit(_fetch_article_date, m["url"]): m for m in to_check}
            for future in as_completed(future_to_item):
                m = future_to_item[future]
                try:
                    d = future.result()
                except Exception:
                    d = None
                if d is not None and not (start_date <= d <= end_date):
                    continue
                results.append({"title": m["title"], "url": m["url"], "date": d})
    for m in overflow:
        results.append({"title": m["title"], "url": m["url"], "date": None})
    # 保持原始（關鍵字比對）順序，而不是 as_completed 的完成順序
    order = {m["url"]: i for i, m in enumerate(matched)}
    results.sort(key=lambda r: order.get(r["url"], 0))
    return results


def search_ltn(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染，HTML 已含搜尋結果，不需 JS）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = (
        f"https://search.ltn.com.tw/list?keyword={kw}"
        f"&start_time={start_date.strftime('%Y%m%d')}&end_time={end_date.strftime('%Y%m%d')}"
        f"&sort=date&type=all"
    )
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["ltn.com.tw/news/"])
    return _attach_dates(matched, start_date, end_date)


def search_appledaily(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://news.nextapple.com/search/{kw}?sort=date"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["nextapple.com/entertainment/", "nextapple.com/life/", "nextapple.com/local/"])
    results = []
    for m in matched:
        d = parse_date_from_url(m["url"])
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": m["title"], "url": m["url"], "date": d})
    return results


def search_tvbs(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://news.tvbs.com.tw/news/searchresult/{kw}/news"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["tvbs.com.tw/entertainment/", "tvbs.com.tw/life/", "tvbs.com.tw/local/"])
    url_to_text = {}
    if soup is not None:
        for li in soup.find_all("li"):
            a = li.find("a", href=lambda h: h and "tvbs.com.tw" in h)
            if a:
                href = urljoin(url, a["href"])
                url_to_text.setdefault(href, li.get_text())
    results = []
    for m in matched:
        text = url_to_text.get(m["url"], "")
        d = parse_relative_or_absolute(text, TODAY)
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": m["title"], "url": m["url"], "date": d})
    return results


def search_chinatimes(page, keyword, start_date, end_date):
    """維持 Playwright：網站有 Cloudflare JS 挑戰頁（"Just a moment..."），
    純 requests 呼叫會被擋下回傳 403，需要真實瀏覽器執行 JS 才能通過驗證並取得搜尋結果。"""
    kw = quote(keyword)
    url = f"https://www.chinatimes.com/search/{kw}?page=1&chdtv"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1200)
    links = _all_links(page)
    matched = _filter(links, keyword, ["chinatimes.com/realtimenews/", "chinatimes.com/newspapers/"])
    results = []
    for m in matched:
        d = parse_date_from_url(m["url"])
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": m["title"], "url": m["url"], "date": d})
    return results


def search_ettoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.ettoday.net/news_search/doSearch.php?keywords={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["star.ettoday.net/news/", "ettoday.net/news/20"])
    url_to_text = {}
    if soup is not None:
        for div in soup.find_all("div"):
            a = div.find("a", href=lambda h: h and "ettoday.net/news" in h)
            text = div.get_text()
            if a and re.search(r'\d{4}-\d{2}-\d{2}', text):
                href = urljoin(url, a["href"])
                url_to_text.setdefault(href, text)
    results = []
    for m in matched:
        text = url_to_text.get(m["url"], "")
        d = None
        dm = re.search(r'(\d{4})-(\d{2})-(\d{2})', text)
        if dm:
            d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": m["title"], "url": m["url"], "date": d})
    return results


def search_udn(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://udn.com/search/word/2/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["stars.udn.com/star/story/"])
    return _attach_dates(matched, start_date, end_date)


def search_mirror(page, keyword, start_date, end_date):
    """維持 Playwright：mirrordaily.news 的搜尋結果由前端 React 元件動態渲染，
    純 requests 拿到的 HTML 只有頁尾靜態連結，抓不到實際搜尋結果，須執行 JS 才能取得。"""
    kw = quote(keyword)
    url = f"https://www.mirrordaily.news/search?q={kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1200)
    links = _all_links(page)
    matched = _filter(links, keyword, ["mirrordaily.news/story/"])
    results = []
    for m in matched:
        title = m["title"]
        d = None
        dm = re.match(r'^(\d{4})/(\d{2})/(\d{2})\s+\d{2}:\d{2}:\d{2}\s*(.*)$', title, re.S)
        if dm:
            d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            title = dm.group(4).strip()
        # 清掉搜尋結果摘要（第二行以後的介紹文字）
        title = title.split("\n")[0].strip()
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": title, "url": m["url"], "date": d})
    return results


def search_yahoo(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://tw.news.yahoo.com/search?p={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["tw.news.yahoo.com/"])
    return _attach_dates(matched, start_date, end_date)


def search_ctwant(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.ctwant.com/search/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["ctwant.com/article/"])
    return _attach_dates(matched, start_date, end_date)


def search_mirrormedia(page, keyword, start_date, end_date):
    """鏡週刊 mirrormedia.mg（與既有的「鏡報」mirrordaily.news 是不同網站）。
    維持 Playwright：搜尋結果由前端 miso 搜尋元件（React）動態載入，純 requests
    拿到的 HTML 裡 `a.miso-list__item-body` 選擇器抓不到任何項目，需要 JS 執行。"""
    kw = quote(keyword)
    url = f"https://www.mirrormedia.mg/search/{kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(3000)
    try:
        items = page.eval_on_selector_all(
            "a.miso-list__item-body",
            "els => els.map(e => ({href: e.href, title: (e.querySelector('.miso-list__item-title')||{}).textContent || ''}))",
        )
    except Exception:
        items = []
    seen, matched = set(), []
    for it in items:
        href, title = it["href"], (it["title"] or "").strip()
        if href in seen or "mirrormedia.mg/story/" not in href:
            continue
        if len(title) < 6 or keyword not in title:
            continue
        seen.add(href)
        matched.append({"title": title, "url": href})
    return _attach_dates(matched, start_date, end_date)


def search_mnews(page, keyword, start_date, end_date):
    """鏡新聞 mnews.tw（鏡電視旗下新聞台，與「鏡週刊」「鏡報」為不同網站）。
    維持 Playwright：純 requests 取得的 HTML 中關鍵字完全不存在（搜尋結果純前端渲染），
    需要真實瀏覽器執行 JS 才能取得結果。"""
    kw = quote(keyword)
    url = f"https://www.mnews.tw/search/{kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(3000)
    links = _all_links(page)
    matched = _filter(links, keyword, ["mnews.tw/story/"])
    results = []
    for m in matched:
        title = m["title"]
        d = None
        dm = re.search(r'(\d{4})\.(\d{2})\.(\d{2})', title)
        if dm:
            try:
                d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            except ValueError:
                d = None
            title = re.sub(r'\d{4}\.\d{2}\.\d{2}\s*\d{2}:\d{2}', '', title).strip()
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": title, "url": m["url"], "date": d})
    return results


def search_ctinews(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://ctinews.com/search/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["ctinews.com/news/items/"])
    return _attach_dates(matched, start_date, end_date)


def search_ftvnews(page, keyword, start_date, end_date):
    """維持 Playwright：網站有 Cloudflare JS 挑戰頁（"Just a moment..."），
    純 requests 呼叫會被擋下回傳 403，需要真實瀏覽器執行 JS 才能通過驗證並取得搜尋結果。"""
    kw = quote(keyword)
    url = f"https://www.ftvnews.com.tw/search/{kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    links = _all_links(page)
    matched = _filter(links, keyword, ["ftvnews.com.tw/news/detail/"])
    return _attach_dates(matched, start_date, end_date)


def search_owlnews(page, keyword, start_date, end_date):
    """奧丁丁OwlNews報新聞。維持 Playwright：純 requests 取得的 HTML 只有 meta/JSON-LD
    描述文字含關鍵字，實際文章清單由前端 JS 動態載入，需要真實瀏覽器執行 JS 才能取得結果。"""
    kw = quote(keyword)
    url = f"https://news.owlting.com/articles/search/{kw}?locale=zh-TW"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(2500)
    links = _all_links(page)
    matched = _filter(links, keyword, ["news.owlting.com/articles/"])
    return _attach_dates(matched, start_date, end_date)


def search_ftnn(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.ftnn.com.tw/search?keyword={kw}&all=true"
    soup = _get_soup(url, timeout=35)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["ftnn.com.tw/news/"])
    results = []
    for m in matched:
        title = m["title"]
        d = None
        dm = re.search(r'(\d{4})\.(\d{2})\.(\d{2})', title)
        if dm:
            try:
                d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            except ValueError:
                d = None
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": title, "url": m["url"], "date": d})
    return results


def search_life(page, keyword, start_date, end_date):
    """Life.tw 台灣生活網。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://life.tw/?app=search&keyword={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["life.tw/article/"])
    return _attach_dates(matched, start_date, end_date)


def search_juksy(page, keyword, start_date, end_date):
    """JUKSY 街星（清單中的 JUSKY_HOT 應為此站）。已改用 requests + BeautifulSoup
    （伺服器端渲染，已驗證非本關鍵字時也能抓到搜尋結果連結）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.juksy.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["juksy.com/article/"])
    return _attach_dates(matched, start_date, end_date)


def search_premiermedia(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.premiermedia.com.tw/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["premiermedia.com.tw/20"])
    return _attach_dates(matched, start_date, end_date)


def search_findnewstoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://findnewstoday.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["findnewstoday.net/archives/"])
    return _attach_dates(matched, start_date, end_date)


def search_taiwanpost(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://taiwanpost.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["taiwanpost.net/20"])
    return _attach_dates(matched, start_date, end_date)


def search_mypeople(page, keyword, start_date, end_date):
    """民眾新聞（民眾網）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://mypeoplevol.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["mypeoplevol.com/20"])
    return _attach_dates(matched, start_date, end_date)


def search_ponews(page, keyword, start_date, end_date):
    """博新聞網（Blogger 平台）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.po-news.net/search?q={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["po-news.net/20"])
    return _attach_dates(matched, start_date, end_date)


def search_hualientoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://hualien-today.com/?s={kw}"
    soup = _get_soup(url, timeout=30)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["hualien-today.com/news.php?listno="])
    return _attach_dates(matched, start_date, end_date)


def search_insightpost(page, keyword, start_date, end_date):
    """洞見新聞網 → 對應「洞見國際事務評論網」insight-post.tw（清單中僅找到此近似站台）。
    維持 Playwright：整站為前端 JS SPA，純 requests 拿到的 HTML 幾乎沒有內文連結，
    需要真實瀏覽器執行 JS 才能取得頁面內容。"""
    kw = quote(keyword)
    url = f"https://insight-post.tw/?s={kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    links = _all_links(page)
    matched = _filter(links, keyword, ["insight-post.tw/"])
    return _attach_dates(matched, start_date, end_date)


def search_starsetn(page, keyword, start_date, end_date):
    """娛樂星聞 star.setn.com（三立旗下娛樂站，與主站 setn.com 分屬不同網域，主站目前被阻擋）。
    已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://star.setn.com/search/{kw}/"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["star.setn.com/news/"])
    return _attach_dates(matched, start_date, end_date)


def search_googlenews(page, keyword, start_date, end_date):
    """Google 新聞 RSS 搜尋（聚合各媒體報導，不需要 Playwright page）"""
    url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_bytes = resp.read()
    except Exception:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    results = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if not title or not link or keyword not in title:
            continue
        d = None
        if pubdate_el is not None and pubdate_el.text:
            try:
                d = parsedate_to_datetime(pubdate_el.text).date()
            except (TypeError, ValueError):
                d = None
        if d and not (start_date <= d <= end_date):
            continue
        results.append({"title": title, "url": link, "date": d})
    return results


SEARCH_FUNCS = {
    "ltn": search_ltn,
    "appledaily": search_appledaily,
    "tvbs": search_tvbs,
    "chinatimes": search_chinatimes,
    "ettoday": search_ettoday,
    "udn": search_udn,
    "mirror": search_mirror,
    "yahoo": search_yahoo,
    "ctwant": search_ctwant,
    "mirrormedia": search_mirrormedia,
    "mnews": search_mnews,
    "ctinews": search_ctinews,
    "ftvnews": search_ftvnews,
    "owlnews": search_owlnews,
    "ftnn": search_ftnn,
    "life": search_life,
    "juksy": search_juksy,
    "premiermedia": search_premiermedia,
    "findnewstoday": search_findnewstoday,
    "taiwanpost": search_taiwanpost,
    "mypeople": search_mypeople,
    "ponews": search_ponews,
    "hualientoday": search_hualientoday,
    "insightpost": search_insightpost,
    "starsetn": search_starsetn,
    "googlenews": search_googlenews,
}

# 仍需要 Playwright（真實瀏覽器執行 JS）才能取得搜尋結果的站台清單。
# 其餘 19 個站台已改用 requests + BeautifulSoup，不需要啟動 headless Chromium，
# 大幅降低雲端部署（如 Streamlit Community Cloud）的記憶體／CPU 用量。
# app.py 可用這份清單判斷是否需要啟動瀏覽器，避免每次搜尋都無謂地啟動 Chromium 行程。
PLAYWRIGHT_REQUIRED_SITES = {
    "chinatimes",   # Cloudflare JS 挑戰頁，requests 會被擋 403
    "ftvnews",      # Cloudflare JS 挑戰頁，requests 會被擋 403
    "mirror",       # 搜尋結果由前端 React 動態渲染
    "mirrormedia",  # 搜尋結果由前端 miso 搜尋元件動態渲染
    "mnews",        # 搜尋結果純前端渲染，requests 抓不到關鍵字
    "owlnews",      # 文章清單由前端 JS 動態載入
    "insightpost",  # 整站為前端 JS SPA
}
