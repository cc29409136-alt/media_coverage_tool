import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT
from matcher import parse_relative_or_absolute, parse_date_from_url

TODAY = date.today()

_EXTRACT_ALL_LINKS = "els => els.map(e => ({title: e.textContent.trim(), href: e.href}))"

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


def _filter(links, keyword, url_must_contain, min_len=6):
    seen, results = set(), []
    for it in links:
        href, title = it["href"], it["title"]
        if href in seen:
            continue
        if not any(s in href for s in url_must_contain):
            continue
        if len(title) < min_len or keyword not in title:
            continue
        seen.add(href)
        results.append({"title": title, "url": href})
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


def _all_links_requests(soup, base_url):
    """等同於 Playwright 版 `_all_links()`：回傳 list of {"title", "href"} dict。"""
    if soup is None:
        return []
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        title = a.get_text(strip=True)
        links.append({"title": title, "href": href})
    return links


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
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


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
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


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
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_ctwant(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.ctwant.com/search/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["ctwant.com/article/"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


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
    seen, results = set(), []
    for it in items:
        href, title = it["href"], (it["title"] or "").strip()
        if href in seen or "mirrormedia.mg/story/" not in href:
            continue
        if len(title) < 6 or keyword not in title:
            continue
        seen.add(href)
        results.append({"title": title, "url": href, "date": None})
    return results


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
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_ftvnews(page, keyword, start_date, end_date):
    """維持 Playwright：網站有 Cloudflare JS 挑戰頁（"Just a moment..."），
    純 requests 呼叫會被擋下回傳 403，需要真實瀏覽器執行 JS 才能通過驗證並取得搜尋結果。"""
    kw = quote(keyword)
    url = f"https://www.ftvnews.com.tw/search/{kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    links = _all_links(page)
    matched = _filter(links, keyword, ["ftvnews.com.tw/news/detail/"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_owlnews(page, keyword, start_date, end_date):
    """奧丁丁OwlNews報新聞。維持 Playwright：純 requests 取得的 HTML 只有 meta/JSON-LD
    描述文字含關鍵字，實際文章清單由前端 JS 動態載入，需要真實瀏覽器執行 JS 才能取得結果。"""
    kw = quote(keyword)
    url = f"https://news.owlting.com/articles/search/{kw}?locale=zh-TW"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(2500)
    links = _all_links(page)
    matched = _filter(links, keyword, ["news.owlting.com/articles/"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


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
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_juksy(page, keyword, start_date, end_date):
    """JUKSY 街星（清單中的 JUSKY_HOT 應為此站）。已改用 requests + BeautifulSoup
    （伺服器端渲染，已驗證非本關鍵字時也能抓到搜尋結果連結）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.juksy.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["juksy.com/article/"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_premiermedia(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.premiermedia.com.tw/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["premiermedia.com.tw/20"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_findnewstoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://findnewstoday.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["findnewstoday.net/archives/"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_taiwanpost(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://taiwanpost.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["taiwanpost.net/20"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_mypeople(page, keyword, start_date, end_date):
    """民眾新聞（民眾網）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://mypeoplevol.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["mypeoplevol.com/20"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_ponews(page, keyword, start_date, end_date):
    """博新聞網（Blogger 平台）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.po-news.net/search?q={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["po-news.net/20"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_hualientoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://hualien-today.com/?s={kw}"
    soup = _get_soup(url, timeout=30)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["hualien-today.com/news.php?listno="])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


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
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


def search_starsetn(page, keyword, start_date, end_date):
    """娛樂星聞 star.setn.com（三立旗下娛樂站，與主站 setn.com 分屬不同網域，主站目前被阻擋）。
    已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://star.setn.com/search/{kw}/"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    matched = _filter(links, keyword, ["star.setn.com/news/"])
    return [{"title": m["title"], "url": m["url"], "date": None} for m in matched]


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
