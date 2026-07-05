import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from config import USER_AGENT
from matcher import (
    press_release_similarity,
    PRESS_RELEASE_MIN_LEN,
    PRESS_RELEASE_HIGH_THRESHOLD,
)

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
    const minLen = title.length + 15;
    let context = null;
    let node = e;
    for (let i = 0; i < 5; i++) {
        const parent = node.parentElement;
        if (!parent) break;
        const text = parent.textContent.trim();
        if (text.length >= minLen) {
            if (text.length <= 600) context = text;
            break;
        }
        node = parent;
    }
    if (context === null) {
        const ownText = e.textContent.trim();
        if (ownText.length >= minLen) context = ownText;
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
# 「老蕭」蕭敬騰，但也有不加引號直接寫「小宇宋念宇」的寫法，或是中間隔著一個
# 空格「小宇 宋念宇」（常見於從多個 DOM 節點拼接文字時自動加入的分隔空格，
# 不是真的列舉符號，所以空格本身不算列舉分隔符，只有逗號/頓號才算）。
# 判斷原則：只要關鍵字前面「不是」逗號/頓號這種列舉分隔符號（也不是文字
# 開頭），就視為緊鄰介紹句型、算命中；如果前面剛好是逗號/頓號，代表這是
# 「一堆人名用頓號列舉」的雜訊寫法（例如某篇報導列出多位音樂人名字，其中
# 剛好包含搜尋的關鍵字，但文章其實跟這個人無關），不算命中。
#
# 實測發現還有一種變形：「小宇（宋念宇）、孫盛希為音樂製作人」——這是在列舉
# 「小宇(宋念宇)」與「孫盛希」兩個人，本名雖然緊鄰暱稱（通過前面的檢查），
# 但緊接著又是「）、」（右括號＋頓號）接另一個人名，代表這整個結構其實還是
# 一份人名清單的其中一項，不是真的在介紹、談論這個人。所以也要檢查關鍵字
# 「後面」有沒有緊跟著列舉分隔符號（可能隔著一個右括號），有的話一樣不算命中。
# 除了頓號，中文列舉也常在最後兩項之間用「與」「和」「跟」「及」這類連接詞
# 取代頓號（例如「小宇（宋念宇）與孫盛希擔任音樂製作人」），一樣要視為列舉、
# 不算命中。
#
# 2026-07-05 修正（實測發現真陽性被誤殺的回歸）：這裡原本把「逗號」跟「頓號」
# 一視同仁都當作列舉分隔符號，但實測發現這個假設太寬鬆——中文逗號是泛用的
# 子句分隔符，「出發前，蕭敬騰先在社群發文詢問」「蕭敬騰，這趟花蓮行收穫滿滿」
# 這類完全正常的敘述句，主詞名字前後緊接著逗號是家常便飯，不代表任何列舉語意
# （反觀頓號在中文裡幾乎專門用在列舉，很少出現在其他語境）。用逗號當列舉訊號
# 會把大量真正相關的報導內文（og:description 通常是完整散文句子，不是標題式
# 短語）錯判成「列舉雜訊」而排除掉。因此改成只用頓號／連接詞判斷列舉，逗號
# 不再視為列舉分隔符號。
_LIST_SEPARATOR_BEFORE = "、"
_LIST_SEPARATOR_AFTER_RE = re.compile(r'^[）)]?(、|與|和|跟|及)')


def _has_nickname_intro(text, keyword):
    idx = text.find(keyword)
    if idx <= 0:
        return False
    if text[idx - 1] in _LIST_SEPARATOR_BEFORE:
        return False
    after = text[idx + len(keyword): idx + len(keyword) + 3]
    if _LIST_SEPARATOR_AFTER_RE.match(after):
        return False
    return True


# 舊版的「只看列表頁摘要」關鍵字比對函式 `_filter()` 已被下方的
# `_candidate_filter()` ＋ `_verify_candidates()`（造訪文章本頁驗證，見該函式群組上方
# 說明）取代，所有 search_ 函式皆已改用新流程。新流程在「內容驗證失敗時」的優雅降級
# 路徑仍然沿用跟 `_filter()` 完全一樣的判斷邏輯（標題命中 or `_has_nickname_intro`
# 鄰接比對），只是直接寫在 `_verify_candidates()` 內部，不再是獨立函式。


# ---------------------------------------------------------------------------
# 輕量版（requests + BeautifulSoup）輔助函式
#
# 以下兩個函式是給「伺服器端渲染」的網站使用的替代方案：不需要啟動 Playwright /
# headless Chromium，只用一次 HTTP GET 抓 HTML 再用 BeautifulSoup 解析連結，
# 大幅降低記憶體與 CPU 用量，適合在 Streamlit Community Cloud 這類資源受限的
# 免費主機上執行。回傳的資料結構刻意與 `_all_links()`／Playwright 版本一致
# （list of {"title", "href", "context"} dict），讓 `_candidate_filter()` 可以直接共用。
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
_CONTEXT_MIN_EXTRA = 15  # context 至少要比純標題多這麼多字，才算真的多了摘要內容


def _extract_link_context(a, title):
    """取得關鍵字比對用的 context 文字（標題＋摘要）。目標是找到「比純標題明顯
    更長」的一段文字（代表真的包含摘要，不是只有標題本身重複一次），依序試兩種
    常見版型：
    1. 往上找父層容器：適用「標題」與「摘要」是分開的兄弟元素（例如 Yahoo 的
       <h3>標題</h3><p>摘要</p>、LTN 的 <li> 卡片）。容器太小（可能只包到標題
       自己，例如 Yahoo 的 <h3> 只比標題本身多一點點）就繼續往上；容器太大
       （可能是「一整頁全部搜尋結果」的共用外層，混進其他文章）就放棄父層路線，
       改試方案 2——這是實際踩到的 bug：CTWANT 的搜尋結果頁把 20 篇文章全部包
       在同一層 <div> 裡，往上找父層容器會拿到 20000+ 字的大雜燴。
    2. <a> 自己的完整文字：適用「整張卡（標題＋摘要＋更多按鈕）」整包塞進同一個
       <a> 標籤的版型（`_extract_link_title` 會從裡面挑出乾淨的標題子元素，但
       <a> 自己完整的 `get_text()` 仍包含摘要全文），例如 CTWANT。
    兩種都找不到「明顯比標題長」的文字，就回傳 None，讓呼叫端退回只比對標題。
    """
    min_len = len(title) + _CONTEXT_MIN_EXTRA
    node = a
    for _ in range(5):
        parent = node.parent
        if parent is None or getattr(parent, "name", None) in (None, "[document]", "html", "body"):
            break
        text = parent.get_text(" ", strip=True)
        if len(text) >= min_len:
            if len(text) <= _CONTEXT_MAX_LEN:
                return text
            break  # 容器已經太大，往上只會更大，放棄父層路線改試 <a> 自己的文字
        node = parent
    own_text = a.get_text(strip=True)
    return own_text if len(own_text) >= min_len else None


def _all_links_requests(soup, base_url):
    """等同於 Playwright 版 `_all_links()`：回傳 list of {"title", "href", "context"} dict。"""
    if soup is None:
        return []
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        title = _extract_link_title(a)
        context = _extract_link_context(a, title)
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

_ARTICLE_DATE_TIMEOUT = 8  # 單篇文章頁請求逾時秒數（`_verify_candidates()` 內容驗證也沿用這個逾時值）

_ISO_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')
_SLASH_DATE_RE = re.compile(r'(\d{4})/(\d{2})/(\d{2})')
# 2026-07-05 新增：FTNN新聞網（見 search_ftnn）搜尋結果列表頁的 `<span class="s-news-time">`
# 用點號分隔（例如 "2026.03.18 15:41"），既有的 ISO／斜線格式都比對不到，導致 `_parse_date_string`
# 回傳 None，讓 search_ftnn 的「驗證前預先過濾」形同虛設（範圍外的候選因為解析失敗而沒被排除）。
# 這是共用的日期解析工具，不只 ftnn 會用到（`_extract_date_from_soup` 的 regex 保底路徑等也共用
# 這個函式），加這個格式不影響既有的 ISO／斜線格式判斷順序，純粹新增一種能辨識的日期字串格式。
_DOT_DATE_RE = re.compile(r'(\d{4})\.(\d{2})\.(\d{2})')


def _parse_date_string(s):
    """把常見的日期字串（ISO 8601、YYYY-MM-DD、YYYY/MM/DD、YYYY.MM.DD 等）轉成 date 物件，
    失敗回傳 None。"""
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
    m = _DOT_DATE_RE.search(s)
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


def _extract_date_from_soup(soup, raw_text):
    """從已解析的文章頁 soup（以及原始 HTML 文字，給 regex 保底用）擷取發布日期，
    嘗試多種方式，失敗回傳 None。給 `_fetch_article_content_and_date()`（日期＋內容
    一次抓）呼叫，抽成獨立函式方便未來如果需要單獨只抓日期時重用，不用複製一份。"""
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

    # 4b. Blogger 平台預設模板的發布時間標籤：<abbr class="published" title="...">
    # （見 search_ponews／博新聞網實測案例：這個站台完全沒有 meta / JSON-LD / <time>
    # 標籤，og:description 也是空字串，唯一找得到的日期線索就是這個 Blogger 樣板固定
    # 會輸出的 `<abbr class="published">`）。`title` 屬性通常是完整 ISO 8601
    # （例如 "2026-07-01T19:36:00+08:00"），直接交給 `_parse_date_string()` 即可；
    # 如果 `title` 屬性不存在或解析失敗，才退回標籤內文文字，格式是「M/DD/YYYY」
    # （月份不補零、年份在最後），既有的 `_SLASH_DATE_RE` 是「YYYY/MM/DD」年份在前的
    # 格式比對不到，這裡改用 month-first regex 另外解析，不動 `_parse_date_string()`
    # 既有的通用邏輯。
    abbr_tag = soup.find("abbr", attrs={"class": re.compile(r'\bpublished\b')})
    if abbr_tag:
        d = _parse_date_string(abbr_tag.get("title"))
        if d:
            return d
        m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', abbr_tag.get_text())
        if m:
            try:
                return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass

    # 4c. 花蓮新聞快報 hualien-today.com（見 search_hualientoday）這類自架 CMS 常見的
    # 「日曆圖示 + <span>日期</span>」版型：沒有 meta／JSON-LD／<time> 標籤，發布日期
    # 純粹是 `<img src="...icondateblack...">` 後面緊接一個純文字 `<span>YYYY-MM-DD</span>`。
    # 這個日期文字本身格式跟 `_ISO_DATE_RE` 相容，問題出在它常常出現在頁面很後段
    # （實測某篇文章日期文字在原始 HTML 第 18541 字元處），超過下面步驟 5「只看前
    # 5000 字」的取樣範圍，導致這類頁面明明有清楚的日期卻抓不到。這裡改成明確找
    # 這個 icon+span 的 DOM 結構（不受文字位置影響），取第一個出現的（同一頁通常
    # 還有「相關文章」列表也用同樣版型附帶日期，但第一個必定是本篇文章自己的日期，
    # 在任何相關文章列表之前）。
    date_icon = soup.find("img", src=re.compile(r'icondate', re.IGNORECASE))
    if date_icon:
        sib = date_icon.find_next_sibling("span")
        if sib:
            d = _parse_date_string(sib.get_text(strip=True))
            if d:
                return d

    # 5. 最後手段：在頁面前段文字中用 regex 找 YYYY-MM-DD / YYYY/MM/DD
    text_sample = raw_text[:5000]
    d = _parse_date_string(text_sample)
    if d:
        return d

    return None


_CONTENT_BODY_SELECTORS = (
    # 常見的文章內文容器 class／tag，依序嘗試，抓到第一個有內容的就停止。
    {"name": "article"},
    {"attrs": {"class": re.compile(r'article-?body', re.IGNORECASE)}},
    {"attrs": {"class": re.compile(r'content', re.IGNORECASE)}},
)
_CONTENT_SNIPPET_MAX_LEN = 800
_CONTENT_SNIPPET_PARAGRAPHS = 3


def _extract_content_snippet(soup):
    """取得用來做關鍵字比對的文章內容摘要，依優先順序嘗試：
    1. <meta property="og:description">：CMS 產生的完整導言摘要，通常比搜尋結果列表頁
       的截斷摘要更完整、更乾淨（見本檔案開頭 mirrormedia 案例：搜尋結果列表頁摘要
       頭尾都被截斷、完全沒出現「宋念宇」，但 og:description 是完整導言，兩端都有）。
    2. 文章內文前幾段 <p>：找常見的內文容器（article / class 含 content / article-body），
       取前 2-3 段串接，避免抓到整篇全文（用不到，也拖慢比對／占用記憶體）。
    3. <meta name="description">：次佳的頁面描述，某些站台沒有 og:description 但有這個。
    4. 都找不到就回傳 None，呼叫端應退回列表頁摘要比對。
    """
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content") and og["content"].strip():
        return og["content"].strip()[:_CONTENT_SNIPPET_MAX_LEN]

    for sel in _CONTENT_BODY_SELECTORS:
        container = soup.find(**sel)
        if not container:
            continue
        paragraphs = container.find_all("p", limit=_CONTENT_SNIPPET_PARAGRAPHS)
        text = " ".join(p.get_text(" ", strip=True) for p in paragraphs).strip()
        if text:
            return text[:_CONTENT_SNIPPET_MAX_LEN]

    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content") and desc["content"].strip():
        return desc["content"].strip()[:_CONTENT_SNIPPET_MAX_LEN]

    return None


def _fetch_article_content_and_date(url, timeout=_ARTICLE_DATE_TIMEOUT):
    """對單篇文章 URL 發一次 GET，同時擷取發布日期與內容摘要（見 `_extract_content_snippet`），
    回傳 (date_or_None, snippet_or_None)。刻意合併成一次 fetch＋一次 BeautifulSoup 解析，
    不要為了日期跟內容各發一次請求——這是為了替代舊版「只驗證列表頁摘要」的比對方式，
    改成造訪文章真正的內容（見本檔案 `_verify_candidates` 上方的說明），如果每篇候選文章
    都發兩次請求，等於直接把總搜尋時間翻倍，沒有必要。
    失敗（網路錯誤、逾時、解析失敗）回傳 (None, None)，呼叫端應退回列表頁摘要比對，
    不要因為多這一次驗證失敗就直接判定不符合。"""
    try:
        resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None, None
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return None, None
    d = _extract_date_from_soup(soup, resp.text)
    snippet = _extract_content_snippet(soup)
    return d, snippet


# 舊版的「先關鍵字比對、再另外抓一次日期」兩階段函式 `_attach_dates()` 已被下方的
# `_verify_candidates()` 取代（日期跟內容摘要現在用同一次文章頁請求一起拿），
# 所有 search_ 函式皆已改用新流程，不再需要獨立的 `_attach_dates()`。
# 日期過濾／保留原則維持不變：只有「明確抓到日期、且確定不在範圍內」才排除，
# 抓不到日期一律保留、標記 date=None，交給人工判斷。


# ---------------------------------------------------------------------------
# 內容驗證比對（取代「只看搜尋結果列表頁摘要」的關鍵字比對方式）
#
# 背景（2026-07-04 實測發現的漏抓案例）：鏡週刊 mirrormedia.mg 的搜尋結果列表頁
# 摘要文字頭尾都被截斷（"...小宇感謝陳漢典...小宇的好友離世帶給他衝擊..."），
# 從頭到尾都沒有出現使用者搜尋的本名「宋念宇」，即使文章本身確實是在講這個人
# （標題只用暱稱「小宇」）。這不是特定站台的 bug，是「列表頁摘要本來就是被截斷過
# 的行銷文案」這個資料來源本身的先天限制，不管怎麼調整比對邏輯都無法從列表頁摘要
# 本身修好——真正能解決的方法只有造訪文章本頁，讀取完整的導言／內文再比對。
#
# 使用者已明確要求「準確比速度重要」，接受用較長的搜尋時間換取正確率，因此新的
# 比對流程改成：先用站台自己的搜尋引擎判斷「這篇文章跟關鍵字有關」（只做 URL pattern
# ＋標題長度的基本過濾，不在這一步驟就用列表頁摘要刷掉候選——因為列表頁摘要正是
# 前述案例證明不可靠的資料來源），對每篇候選文章的「真正內容」（見
# `_fetch_article_content_and_date` / `_extract_content_snippet`：優先用
# og:description，其次抓內文前幾段）做關鍵字比對，比對規則沿用既有的
# `_has_nickname_intro` 鄰接判斷（暱稱緊接本名才算數，避免「一堆人名逗號列舉」
# 的雜訊誤判——這條規則今天稍早已經用來修過「蕭敬騰」案例的誤判，這裡刻意重用
# 同一份邏輯，不另外寫一份更寬鬆的比對，維持前後一致的嚴謹標準）。
# ---------------------------------------------------------------------------

_CONTENT_VERIFY_MAX_CANDIDATES = 20  # 每個站台最多對幾篇候選文章做「造訪本文驗證」（效能考量）
_CONTENT_VERIFY_MAX_WORKERS = 6  # 平行抓取的執行緒數


def _candidate_filter(links, url_must_contain, min_len=6):
    """比 `_filter()` 更寬鬆的第一階段過濾：只檢查 URL pattern（確定是文章頁，不是導覽列
    ／分類頁連結）跟標題長度（濾掉空標題、純圖示連結等明顯雜訊），刻意「不」在這一步
    做關鍵字比對——因為列表頁摘要本身不可靠（見上方模組說明），真正的關鍵字判斷留到
    `_verify_candidates()` 造訪文章本頁之後才做。回傳 list of {"title","url","context"}，
    `context` 保留列表頁摘要，供內容驗證失敗時的退回比對使用。
    """
    seen, results = set(), []
    for it in links:
        href, title = it["href"], it["title"]
        if href in seen:
            continue
        if not any(s in href for s in url_must_contain):
            continue
        if len(title) < min_len:
            continue
        seen.add(href)
        results.append({"title": _clean_title(title), "url": href, "context": it.get("context") or ""})
    return results


# 2026-07-05 新增：標題／內文摘要裡關鍵字「只是被提到所屬公司/團隊/人際關係」的
# 所有格用法偵測。
# 背景（實測案例）：搜尋「蕭敬騰」時出現「才說「不想去台灣發展」！艾薇遭控「忘恩
# 負義」蕭敬騰　公司緊急發聲」這類新聞——文章真正的主角是艾薇（她的爭議發言），
# 蕭敬騰只是被提到「他的經紀公司」出面回應，本人並未真正被報導。這種情況舊版邏輯
# 會誤判命中，因為標題比對是「只要關鍵字出現在標題就算數」，完全沒檢查關鍵字在
# 標題裡扮演的是「文章主角」還是「所有格修飾語」。使用者確認這類新聞不算他要的
# 媒體露出（明確要求濾掉），所以新增判斷：如果關鍵字緊接著「公司／經紀公司／
# 旗下／老闆／東家」這類所有格詞（中間可以夾標點符號或空白，例如「蕭敬騰　公司
# 緊急發聲」的全形空白、「蕭敬騰！經紀公司回應了」的驚嘆號），就視為「純掛名」，
# 這次出現不算數。
#
# 後來又實測到同一類但用不同名詞的案例：「來自馬來西亞的歌手艾薇...並在台灣
# 出道、成為大咖歌王蕭敬騰的師妹」——這篇主角同樣是艾薇，「蕭敬騰的師妹」只是
# 用來介紹艾薇背景的所有格片語（描述艾薇跟蕭敬騰的師徒/同門關係），蕭敬騰本人
# 沒有真正出現在這則新聞裡。所以把所有格名詞清單擴大到常見的演藝圈人際關係詞
# （師妹/師弟/徒弟/學生/粉絲/經紀人，以及配偶/伴侶關係詞），只要清單持續發現
# 遺漏就繼續擴充，這類「掛名所有格修飾語」在中文娛樂新聞標題裡的變化很多，
# 沒辦法一次窮舉，只能實測發現一種、擴充一種。
_AFFILIATION_AFTER_RE = re.compile(
    r'^[的，,、！!？?\s　]{0,3}'
    r'(經紀公司|公司|旗下|老闆|東家|師妹|師弟|徒弟|學生|粉絲|經紀人|'
    r'老婆|老公|太太|丈夫|女友|男友|前女友|前男友)'
)


def _is_affiliation_mention(text, idx, keyword):
    """檢查 `text` 中位置 `idx` 開始的關鍵字，是不是純粹被當作所有格修飾語
    （見上方模組說明）帶出另一個人／團隊，不是文章真正在談論的對象。"""
    after = text[idx + len(keyword):]
    return bool(_AFFILIATION_AFTER_RE.match(after))


def _title_keyword_is_affiliation_only(title, keyword):
    """回傳 True 代表標題裡「每一次」出現關鍵字，都屬於下面兩種非主角情境之一：
    (a) 純掛名所有格修飾語（見上方模組說明），或
    (b) 假冒／開玩笑自稱是這個人（見 `_is_false_self_claim()`，例如「他夜市自稱
        「我是蕭敬騰」笑翻全場」——這篇主角是別人，蕭敬騰只是被拿來當假名開玩笑）。
    只要有任何一次出現不屬於這兩種情境（代表關鍵字本人才是文章討論的對象），
    就不算純掛名／假冒，回傳 False。
    """
    idx = 0
    seen_any = False
    while True:
        idx = title.find(keyword, idx)
        if idx == -1:
            break
        seen_any = True
        if not _is_affiliation_mention(title, idx, keyword) and not _is_false_self_claim(title, idx):
            return False
        idx += len(keyword)
    return seen_any


_SNIPPET_LIST_SEPARATOR_AFTER_RE = re.compile(r'^[）)]?、')

# 2026-07-05 新增：偵測「假冒／開玩笑自稱是這個人」的引用句型，例如「向佐逛夜市
# 買水果乾時，突然自稱「我叫蕭敬騰」，被攤商...」——這篇報導的主角是向佐/郭碧婷，
# 「蕭敬騰」只是向佐開玩笑謊報的假名，本人完全沒有出現在這則新聞裡，卻因為
# 「蕭敬騰」這個字串真的出現在 snippet 裡（不是列舉、也不是所有格修飾語，是
# 前面兩種防呆都沒設計要抓的全新樣式）而被誤判命中。判斷方式：檢查關鍵字前面
# 一小段文字（含引號可能夾在中間）是不是「自稱／冒充／謊稱／假裝是／假冒」＋
# 「我是／我叫」這類假冒句型，是的話這次出現不算數。
_SELF_CLAIM_BEFORE_RE = re.compile(r'(自稱|冒充|謊稱|假裝是|假冒)[「『"]?(我)?(是|叫)?$')


def _is_false_self_claim(text, idx):
    """檢查 `text` 中位置 `idx` 開始的關鍵字，前面一小段文字是不是「自稱／冒充」
    這類假冒句型（見上方模組說明），是的話代表這個關鍵字出現只是被拿來當假名
    開玩笑，不是真的在講這個人。"""
    before_ctx = text[max(0, idx - 12): idx]
    return bool(_SELF_CLAIM_BEFORE_RE.search(before_ctx))


def _snippet_keyword_hit(snippet, keyword):
    """檢查關鍵字是否以「非純列舉雜訊、非假冒句型」的方式出現在文章內文摘要
    （完整句子的散文）裡。

    刻意不重用 `_has_nickname_intro`／`_LIST_SEPARATOR_AFTER_RE`（設計給短標題／
    list-page context 用，包含「與/和/跟/及」這類連接詞判斷），這裡只用「頓號」
    當列舉訊號：2026-07-05 實測發現連接詞在完整句子情境下常常是用來描述「這個人
    跟另一個人的關係／互動」，不是「無關列舉」——例如「金曲歌王蕭敬騰與經紀人
    老婆Summer登記結婚」（真陽性，蕭敬騰是這句話的主詞之一，在做「登記」這個動作）
    跟「被質疑對提攜她出道的蕭敬騰及經紀人Summer缺乏感恩之心」（假陽性，蕭敬騰
    只是「對...」這個介詞片語裡的受詞，句子真正的主詞是「她」＝艾薇）兩句話的
    表面文字結構幾乎一樣（都是「關鍵字＋連接詞＋經紀人[老婆]Summer」），用連接詞
    當雜訊訊號在這裡沒辦法分辨，而且會把前者這種完全正常、描述本人婚姻/感情關係
    的真陽性一併誤殺。頓號則幾乎專門用在中文列舉，很少出現在描述人物關係的語境，
    保留頓號判斷仍能抓到「詹雯婷、羅大佑、蕭敬騰、丁噹」這類真正的雜訊列舉。

    另外也用 `_is_false_self_claim()` 排除「假冒／開玩笑自稱是這個人」的句型
    （見該函式說明），以及 `_is_affiliation_mention()` 排除「純掛名所有格修飾語」
    （見該函式說明——2026-07-05 實測發現這個問題不只發生在標題，內文摘要也會有
    「...並在台灣出道、成為大咖歌王蕭敬騰的師妹...」這類寫法，主角其實是另一個人，
    蕭敬騰只是用來描述背景關係的所有格片語），這兩類都是跟列舉雜訊完全不同的
    假陽性樣式。
    """
    idx = snippet.find(keyword)
    if idx == -1:
        return False
    if _is_false_self_claim(snippet, idx) or _is_affiliation_mention(snippet, idx, keyword):
        return False
    before = snippet[idx - 1] if idx > 0 else ""
    after_ctx = snippet[idx + len(keyword): idx + len(keyword) + 3]
    if before == "、" or _SNIPPET_LIST_SEPARATOR_AFTER_RE.match(after_ctx):
        return False
    return True


def _content_keyword_match(title, snippet, keywords):
    """文章本頁內容驗證用的關鍵字命中判斷。跟 `_filter()` 的邏輯保持一致的嚴謹標準：
    標題命中原則上算數（便宜、可信），但排除「純掛名所屬公司」的情況（見上方
    `_title_keyword_is_affiliation_only` 說明）；內容摘要則用 `_snippet_keyword_hit()`
    （見上方說明，只把頓號當列舉雜訊訊號，不含連接詞）判斷關鍵字是否以非列舉雜訊
    的方式出現。

    這裡原本想單純用「關鍵字有出現在摘要就算命中」（比列表頁摘要比對更寬鬆一點，
    理由是 og:description／內文前幾段通常是 CMS 寫的完整導言散文，不是「一堆人名
    逗號列舉」的雜訊來源）——但實測驗證蕭敬騰基準案例時發現這個假設不成立：
    自由時報一篇報導的 og:description 內容是「...曾參與「Faye」詹雯婷、羅大佑、
    蕭敬騰等大咖音樂人現場演出...」，關鍵字前面剛好是「、」，証明列舉雜訊一樣會
    出現在文章導言裡，不是只有列表頁的「相關文章」推薦區塊才有。

    2026-07-05 新增第二道防呆：實測發現「標題掛名，但內文完全沒提到這個人」的
    clickbait 案例——例如「蕭敬騰提拔出道！艾薇遭爆「忘恩負義抱怨公司」經紀人發聲
    反擊」，這篇文章真正在談的是艾薇的爭議，內文摘要從頭到尾沒再提到「蕭敬騰」
    一次；還有「向佐、郭碧婷同框破婚變　他夜市自稱「我是蕭敬騰」笑翻全場」，這篇
    是在講向佐/郭碧婷，「蕭敬騰」只是被拿來當笑點的名字，內文摘要同樣完全沒再提到
    這個名字。相對地，實測所有真正相關的報導（例如「蕭敬騰花蓮爆吃」「蕭敬騰誓詞
    太洗腦」等），內文摘要都會明確再次提到「蕭敬騰」——這是很自然的道理：記者真的
    在寫這個人的新聞，內文一定會再帶到他的名字，不會只在標題出現過一次就不再提。
    因此新增規則：如果有抓到 `snippet`（代表這篇文章的驗證有真的成功造訪到本頁），
    標題命中「必須」在 snippet 裡也以非列舉雜訊的方式再次出現這個關鍵字才算數；
    抓不到 snippet（驗證失敗、優雅降級）時維持原樣只信任標題，不受這條規則影響
    （沒有 snippet 可以交叉驗證，比照舊版行為）。這比前面「所有格掛名」的判斷更
    通用，同時也覆蓋了「曾與蕭敬騰、羅大佑合作」這類標題列舉雜訊案例（這篇的
    snippet 是「...詹雯婷、羅大佑、蕭敬騰、丁噹...」，關鍵字確實出現在 snippet
    裡，但只是頓號列舉的一部分，不算數）。

    重要：這條「標題命中需要 snippet 佐證」的規則，只套用在「關鍵字真的有出現在
    標題」的情況；如果關鍵字根本不在標題（例如標題只用暱稱，使用者搜尋的是本名），
    改成單獨看 snippet 命中與否即可決定，不需要「雙重確認」——這裡沒有「標題」這個
    第一層訊號可以佐證，單靠 snippet 命中已經是唯一可用的證據，不應該因為標題沒有
    這個名字就連 snippet 命中都不算數。同樣地，如果標題命中被「純掛名所屬公司」
    判定否決（`_title_keyword_is_affiliation_only`），也不允許退回單靠 snippet
    佐證就算數——這是刻意的設計：標題已經明確判斷這個人在文章裡只是所有格修飾語
    （不是文章主角），不應該因為 snippet 剛好也提到（哪怕是用非列舉的方式提到）
    就推翻這個判斷。

    這個函式本身「不」處理新聞稿相似度——那是額外的獨立信號，由呼叫端
    `_verify_candidates()` 在拿到這裡的布林結果之後，再視情況合併新聞稿相似度分數
    做最終判斷（見該函式的說明），這裡維持只做關鍵字／暱稱鄰接判斷，是刻意的
    職責切分，方便沒有貼新聞稿的情境可以單獨呼叫這裡、行為完全不變。
    """
    for k in keywords:
        in_title = k in title
        title_hit = in_title and not _title_keyword_is_affiliation_only(title, k)
        snippet_hit = bool(snippet) and _snippet_keyword_hit(snippet, k)

        if title_hit and (not snippet or snippet_hit):
            return True
        if not in_title and snippet_hit:
            return True
    return False


def _final_match_decision(keyword_match, press_release_text, snippet):
    """在既有的關鍵字／暱稱鄰接判斷（`keyword_match`）之上，疊加「新聞稿全文相似度」
    這個額外信號，回傳 (是否命中, 相似度分數或 None)。

    2026-07-05 修正（實測發現嚴重的漏抓回歸）：舊版邏輯讓相似度分數可以「反否決」
    已經通過 `keyword_match` 的候選（分數低於 `PRESS_RELEASE_CORROBORATE_THRESHOLD`
    就整篇排除），原意是濾掉「掛名提及但其實是另一則新聞」的雜訊。但實測「蕭敬騰
    花蓮開唱」真實案例：Yahoo 站台不貼新聞稿時正確找到 7 篇同一事件的報導，貼上
    新聞稿全文後卻只剩 1 篇——因為同一事件不同媒體記者各自改寫、引用不同的談話
    片段，`press_release_similarity()` 算出來的分數大多落在 0.05~0.15 之間（都低於
    0.12 的佐證門檻），連明顯是同一則新聞的報導都被反否決掉。這證明「相似度不夠
    高就反否決關鍵字命中」這個假設不成立：不同記者改寫的自然變異幅度，跟「這篇
    其實是另一則新聞」的變異幅度沒有清楚的分數界線可以切開（先前只用單一案例
    校準門檻，沒有涵蓋這種「同事件、多記者改寫」的常見情境）。

    這個工具的核心目的是「媒體露出整理」，漏掉真正的報導比多顯示一兩篇邊緣案例
    的代價更高（使用者本來就會人工看過結果，多顯示的可以自己刪，漏掉的卻無從
    發現）。因此改成：新聞稿相似度只能「加分」（讓關鍵字沒命中的候選也有機會
    透過高相似度被納入，見下方 (b)），不能「扣分」否決已經通過既有嚴謹關鍵字／
    暱稱鄰接比對（`_has_nickname_intro`／`_content_keyword_match` 的列舉雜訊防呆）
    的候選——原本要防的「掛名提及」誤判，交給既有的關鍵字/暱稱鄰接比對本身的
    嚴謹度把關即可，不再疊加這層相似度反否決。

    判斷邏輯（`press_release_text` 有效時）：
    - (a) 關鍵字比對已命中：一律命中，相似度分數僅供參考（顯示用），不影響是否收錄。
    - (b) 關鍵字比對沒命中，但相似度 >= 高信心門檻（文章改寫幅度大、暱稱鄰接
      句型比對不到，但內容明顯跟新聞稿是同一則事件）：一樣視為命中。
    - 其餘情況（關鍵字沒命中、相似度也不夠高）：不命中。

    `press_release_text` 為空/太短（見 `PRESS_RELEASE_MIN_LEN`）或沒有 snippet 可比對時，
    完全不套用新聞稿相似度，原樣回傳既有的 `keyword_match` 布林值——維持沒有貼新聞稿
    情境下的既有行為不變。
    """
    if not press_release_text or len(press_release_text.strip()) < PRESS_RELEASE_MIN_LEN or not snippet:
        return keyword_match, None

    score = press_release_similarity(press_release_text, snippet)
    if keyword_match:
        return True, score
    if score >= PRESS_RELEASE_HIGH_THRESHOLD:
        return True, score
    return False, score


def _verify_candidates(candidates, keyword, start_date, end_date,
                        max_candidates=_CONTENT_VERIFY_MAX_CANDIDATES,
                        timeout=_ARTICLE_DATE_TIMEOUT, press_release_text=None):
    """新版核心比對流程，取代舊版「_filter()（列表頁摘要判斷）→ _attach_dates()（另外
    抓日期）」兩階段。輸入 candidates 是 `_candidate_filter()` 的輸出（只做過 URL／
    標題長度過濾，尚未判斷關鍵字是否命中）。

    對前 max_candidates 篇候選文章：平行造訪文章本頁一次，同時拿到日期與內容摘要，
    先算出關鍵字命中判斷＝標題命中 or 內容摘要命中（`_content_keyword_match`）。如果使用者
    有貼新聞稿全文（`press_release_text`，長度需超過 `matcher.PRESS_RELEASE_MIN_LEN`），
    再交給 `_final_match_decision()` 疊加新聞稿相似度分數，可能「反否決」關鍵字命中
    （內容跟新聞稿差太多，判定是另一則新聞）或「額外納入」關鍵字沒命中但相似度極高的
    候選（見該函式 docstring）。相似度比對直接複用這裡已經抓到的 `snippet`，不重新
    發請求。如果這次額外的造訪失敗（網路錯誤、逾時、頁面解析不到任何內容摘要），退回用
    列表頁摘要做舊版 `_has_nickname_intro` 比對（優雅降級，不能因為多做的驗證步驟失敗
    就直接放棄這篇候選）——這個退回路徑沒有內容摘要可用，新聞稿相似度也就無從比對，
    直接沿用舊版邏輯。

    超過 max_candidates 篇的候選，因效能考量不逐篇造訪本頁，同樣退回列表頁摘要比對
    （沿用舊版 `_filter()` 對「量太大」候選的處理精神：與其整批不驗證直接漏掉，
    不如用比較弱但至少有機會抓到的舊版比對規則）。

    日期過濾原則跟舊版 `_attach_dates()` 一致：只有「明確抓到日期、且確定不在範圍內」
    才排除；抓不到日期一律保留、標記 date=None，交給人工判斷。

    `press_release_text` 預設 None：沒有傳入（或傳入空字串／太短）時，`_final_match_decision()`
    會直接回傳原本的 `keyword_match` 布林值，等同完全沒有這次改版，維持既有行為不變。
    """
    keywords = [keyword] if isinstance(keyword, str) else keyword
    to_verify = candidates[:max_candidates]
    overflow = candidates[max_candidates:]
    results = []

    if to_verify:
        with ThreadPoolExecutor(max_workers=_CONTENT_VERIFY_MAX_WORKERS) as executor:
            future_to_item = {
                executor.submit(_fetch_article_content_and_date, c["url"], timeout): c for c in to_verify
            }
            for future in as_completed(future_to_item):
                c = future_to_item[future]
                try:
                    d, snippet = future.result()
                except Exception:
                    d, snippet = None, None

                pr_score = None
                if snippet is not None:
                    # 成功造訪文章本頁：用內容摘要做最終判斷（可能疊加新聞稿相似度）
                    keyword_match = _content_keyword_match(c["title"], snippet, keywords)
                    matched, pr_score = _final_match_decision(keyword_match, press_release_text, snippet)
                    if not matched:
                        continue
                else:
                    # 造訪失敗／抓不到內容摘要：優雅降級，退回列表頁摘要比對。
                    # 2026-07-05 修正：這裡原本是獨立的陽春檢查（`k in title` 完全
                    # 不檢查所有格掛名／假冒句型，`_has_nickname_intro(context, k)`
                    # 也沒有這兩種防呆），導致 Cloudflare 會擋下「文章本頁」請求的
                    # 站台（例如 ftvnews：列表頁本身用 Playwright 能拿到，但
                    # `_fetch_article_content_and_date` 用 plain requests 造訪個別
                    # 文章頁一律被擋、snippet 恆為 None，等於這個站台的每一篇候選
                    # 都會走這條退回路徑）完全繞過本次新增的所有假陽性防呆（純掛名
                    # 公司、內文列舉雜訊、假冒自稱），已修好的「曾與蕭敬騰、羅大佑
                    # 合作」「向佐...自稱我是蕭敬騰」這類案例會在這些站台重新冒出來。
                    # 改成直接重用 `_content_keyword_match()`，把 `context`（列表頁
                    # 摘要）當作退化版的 snippet 傳進去，套用完全相同的判斷標準，
                    # 不再維護第二套比對邏輯。
                    if not _content_keyword_match(c["title"], c["context"], keywords):
                        continue

                if d is not None and not (start_date <= d <= end_date):
                    continue
                item = {"title": c["title"], "url": c["url"], "date": d}
                if pr_score is not None:
                    item["press_release_score"] = pr_score
                results.append(item)

    for c in overflow:
        # 同上，重用 `_content_keyword_match()` 而非獨立的陽春檢查
        if not _content_keyword_match(c["title"], c["context"], keywords):
            continue
        results.append({"title": c["title"], "url": c["url"], "date": None})

    # 保持原始（候選）順序，而不是 as_completed 的完成順序
    order = {c["url"]: i for i, c in enumerate(candidates)}
    results.sort(key=lambda r: order.get(r["url"], 0))
    return results


def search_ltn(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染，HTML 已含搜尋結果，不需 JS）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = (
        f"https://search.ltn.com.tw/list?keyword={kw}"
        f"&start_time={start_date.strftime('%Y%m%d')}&end_time={end_date.strftime('%Y%m%d')}"
        f"&sort=date&type=all"
    )
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["ltn.com.tw/news/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_appledaily(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。
    日期改交給 `_verify_candidates()` 內建的文章頁日期擷取 cascade（比舊版單純從網址猜
    日期更準確，網址裡的日期片段有時是發稿系統的建檔時間，不一定等於實際發布時間）；
    如果文章頁擷取失敗，`_verify_candidates()` 對 date=None 一律保留、交給人工判斷，
    不會比舊版（URL 解析失敗時同樣是 date=None）更嚴格。"""
    kw = quote(keyword)
    url = f"https://news.nextapple.com/search/{kw}?sort=date"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["nextapple.com/entertainment/", "nextapple.com/life/", "nextapple.com/local/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_tvbs(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。

    2026-07-05 修正（實測發現的 overflow bug，跟 setn／taisounds 同一類）：搜尋結果頁
    本身確實嚴格新到舊排序（每頁 25 筆），但 `_all_links_requests()` 用全頁 `<a>` 掃描
    時，會把頁面右側「熱門文章」（`from=Popular_txt_click` query string）等跟搜尋關鍵字
    無關的推薦連結也一起掃進來，導致候選數從 25 筆膨脹到 40 筆，把真正的搜尋結果擠過
    `_CONTENT_VERIFY_MAX_CANDIDATES=20` 的驗證額度，尾端幾篇（含數個月前的舊聞，例如
    2026/03/22 的報導）落入 overflow 桶、date 標記為 None 但仍被顯示。

    修法：實測發現每篇真正的搜尋結果 `<a>` 標籤本身就帶有 `data-publish_date="YYYY/MM/DD
    HH:MM"` 屬性（"熱門文章" 側欄連結沒有這個屬性，可以用它天然排除雜訊），且這個日期
    已驗證跟文章本頁一致。改成直接找出所有帶 `data-publish_date` 的 `<a>`，用這個日期
    做「驗證前預先過濾」，範圍外的候選不會進入 `links` list，同時也順便濾掉了汙染候選
    數的側欄雜訊連結（沒有這個屬性的側欄連結完全不會被加入）。
    """
    kw = quote(keyword)
    url = f"https://news.tvbs.com.tw/news/searchresult/{kw}/news"
    soup = _get_soup(url)
    links = []
    if soup is not None:
        for a in soup.find_all("a", href=True, attrs={"data-publish_date": True}):
            title = _extract_link_title(a)
            if not title:
                continue
            pub_d = _parse_date_string(a["data-publish_date"])
            if pub_d is not None and not (start_date <= pub_d <= end_date):
                continue  # 列表頁自帶可信日期，範圍外提早排除，不佔驗證額度（同 search_setn）
            href = urljoin(url, a["href"])
            context = _extract_link_context(a, title)
            links.append({"title": title, "href": href, "context": context})
    candidates = _candidate_filter(links, ["tvbs.com.tw/entertainment/", "tvbs.com.tw/life/", "tvbs.com.tw/local/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_chinatimes(page, keyword, start_date, end_date, press_release_text=None):
    """搜尋結果列表頁維持 Playwright：網站有 Cloudflare JS 挑戰頁（"Just a moment..."），
    純 requests 呼叫會被擋下回傳 403，需要真實瀏覽器執行 JS 才能通過驗證並取得搜尋結果。
    但個別「文章頁」（不是搜尋結果頁）通常沒有這層 Cloudflare 挑戰，`_verify_candidates()`
    內部用 plain requests 造訪文章頁做內容驗證＋日期擷取即可，不需要另外開瀏覽器分頁
    （已實測確認個別文章頁可用 requests 正常取得）；如果之後發現文章頁其實也被擋，
    `_verify_candidates()` 的「內容抓取失敗就退回列表頁摘要比對」機制會自動優雅降級，
    不會整批漏收。"""
    kw = quote(keyword)
    url = f"https://www.chinatimes.com/search/{kw}?page=1&chdtv"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1200)
    links = _all_links(page)
    candidates = _candidate_filter(links, ["chinatimes.com/realtimenews/", "chinatimes.com/newspapers/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_ettoday(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。
    日期改交給 `_verify_candidates()` 內建的文章頁日期擷取 cascade，取代舊版從列表頁
    附近 <div> 文字用 regex 挖 YYYY-MM-DD 的做法（該做法依賴列表頁版型穩定，文章頁的
    meta tag／JSON-LD 日期更穩定可靠）。"""
    kw = quote(keyword)
    url = f"https://www.ettoday.net/news_search/doSearch.php?keywords={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["star.ettoday.net/news/", "ettoday.net/news/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_udn(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://udn.com/search/word/2/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["stars.udn.com/star/story/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_mirror(page, keyword, start_date, end_date, press_release_text=None):
    """維持 Playwright：mirrordaily.news 的搜尋結果由前端 React 元件動態渲染，
    純 requests 拿到的 HTML 只有頁尾靜態連結，抓不到實際搜尋結果，須執行 JS 才能取得。
    列表頁的卡片標題文字前面會夾帶「YYYY/MM/DD HH:MM:SS」時間戳、後面接搜尋摘要，
    這裡先把標題清乾淨（跟原本邏輯一致），日期部分改交給 `_verify_candidates()` 內建的
    文章頁日期擷取 cascade（比從列表頁標題文字 regex 挖時間戳更穩定）。"""
    kw = quote(keyword)
    url = f"https://www.mirrordaily.news/search?q={kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1200)
    links = _all_links(page)
    for it in links:
        title = it["title"]
        dm = re.match(r'^(\d{4})/(\d{2})/(\d{2})\s+\d{2}:\d{2}:\d{2}\s*(.*)$', title, re.S)
        if dm:
            title = dm.group(4).strip()
        # 清掉搜尋結果摘要（第二行以後的介紹文字）
        it["title"] = title.split("\n")[0].strip()
    candidates = _candidate_filter(links, ["mirrordaily.news/story/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


_YAHOO_RELATIVE_TIME_RE = re.compile(r'([0-9０-９]+)\s*(分鐘前|小時前|天前|週前|個月前|年前)')
_FULLWIDTH_DIGIT_TABLE = str.maketrans("０１２３４５６７８９", "0123456789")


def _parse_yahoo_relative_time(text):
    """把 Yahoo奇摩新聞搜尋結果卡片（`li.stream-card` 最下方的「來源 ・ N天前」文字）
    換算成粗略的 date 物件，抓不到就回傳 None。

    這是「相對時間」，天生比絕對日期（ISO／meta tag）不精確（±1 天可能因為時區／
    「今天」邊界問題而有誤差，這點跟本檔案其他地方提到相對時間換算的風險一致），
    所以只拿來做「驗證前預先過濾」的粗篩，不會拿來當作候選最終顯示的 `date` 欄位
    （最終顯示的日期仍然交給 `_verify_candidates()` 造訪文章本頁的日期擷取 cascade，
    這裡的相對時間只用來決定「要不要讓這個候選進入驗證清單」）。"""
    m = _YAHOO_RELATIVE_TIME_RE.search(text)
    if not m:
        return None
    try:
        n = int(m.group(1).translate(_FULLWIDTH_DIGIT_TABLE))
    except ValueError:
        return None
    unit = m.group(2)
    if unit == "分鐘前" or unit == "小時前":
        days = 0
    elif unit == "天前":
        days = n
    elif unit == "週前":
        days = n * 7
    elif unit == "個月前":
        days = n * 30
    elif unit == "年前":
        days = n * 365
    else:
        return None
    return TODAY - timedelta(days=days)


def search_yahoo(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。

    2026-07-05 修正（實測發現的 overflow bug，跟 tvbs／ftnn 同一類「候選數被非搜尋結果
    的雜訊連結撐爆」）：`_all_links_requests()` 用全頁 `<a>` 掃描時，除了 20 篇真正的
    搜尋結果（每篇包在 `<li class="stream-card">` 裡）以外，還會把頁尾／側欄大量
    `topic/`、分類頁、其他導覽連結一起掃進來（實測「蕭敬騰」關鍵字候選數從 20 筆
    撐到 71 筆），這些雜訊連結的標題／URL 明顯跟搜尋關鍵字無關，理論上不會通過
    `_candidate_filter()` 的關鍵字比對——但因為 `_candidate_filter()` 本來就不做關鍵字
    比對（見該函式 docstring，關鍵字判斷延後到 `_verify_candidates()`），這些雜訊
    還是會先佔用候選名額，把真正的搜尋結果擠向候選清單後段甚至擠出 20 篇驗證額度，
    等於間接造成跟其他站台一樣的 overflow 風險（已實測：不修的話最前面 20 個
    「進入驗證」的候選其實還是 20 篇真正的搜尋結果沒錯，但因為雜訊佔用，某些關鍵字
    下真正的搜尋結果會被擠出 20 篇之外，變成 overflow 桶的 date=None）。

    修法：只掃描 `li.stream-card` 這個真正的搜尋結果卡片容器（雜訊連結不在這個
    容器裡，天然被排除），同時這個容器最下方帶有「來源 ・ N天前」的相對時間文字
    （見 `_parse_yahoo_relative_time()`），拿來做「驗證前預先過濾」，範圍外的候選
    不會進入 `links` list，不佔用驗證額度。相對時間本身有 ±1 天的誤差風險，所以
    這裡沿用其他相對時間換算場景的保守做法：只在「明確算出的日期」確定不在範圍內
    才排除，不是用來當作候選最終顯示的日期（顯示日期仍由 `_verify_candidates()`
    造訪文章本頁取得，準確度不受相對時間誤差影響）。"""
    kw = quote(keyword)
    url = f"https://tw.news.yahoo.com/search?p={kw}"
    soup = _get_soup(url)
    links = []
    if soup is not None:
        for card in soup.find_all("li", class_="stream-card"):
            a = card.find("h3")
            a = a.find("a", href=True) if a else None
            if not a:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            time_div = card.find_all("div")[-1] if card.find_all("div") else None
            pub_d = _parse_yahoo_relative_time(time_div.get_text(strip=True)) if time_div else None
            if pub_d is not None and not (start_date <= pub_d <= end_date):
                continue  # 相對時間換算的粗略日期，範圍外提早排除，不佔驗證額度
            summary_el = card.find("p")
            context = summary_el.get_text(strip=True) if summary_el else title
            links.append({"title": title, "href": a["href"], "context": context})
    candidates = _candidate_filter(links, ["tw.news.yahoo.com/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_ctwant(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.ctwant.com/search/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["ctwant.com/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_mirrormedia(page, keyword, start_date, end_date, press_release_text=None):
    """鏡週刊 mirrormedia.mg（與既有的「鏡報」mirrordaily.news 是不同網站）。
    維持 Playwright：搜尋結果由前端 miso 搜尋元件（React）動態載入，純 requests
    拿到的 HTML 裡 `a.miso-list__item-body` 選擇器抓不到任何項目，需要 JS 執行。

    這裡是本次「造訪文章本頁驗證」改版的起因案例：這個站台的搜尋結果列表頁摘要
    頭尾都被截斷（例如「...小宇感謝陳漢典...小宇的好友離世帶給他衝擊...」），完全
    不會出現使用者搜尋的本名「宋念宇」，即使文章確實是在講這個人（標題只用暱稱
    「小宇」）。改用 `_candidate_filter()`＋`_verify_candidates()`：只用 URL pattern
    做基本過濾，真正的關鍵字判斷改成造訪文章本頁的 og:description（已實測確認會
    包含「小宇 宋念宇」——暱稱緊接本名，`_has_nickname_intro` 抓得到），不再只信任
    列表頁摘要。"""
    kw = quote(keyword)
    url = f"https://www.mirrormedia.mg/search/{kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(3000)
    try:
        items = page.eval_on_selector_all(
            "a.miso-list__item-body",
            "els => els.map(e => ({href: e.href, title: (e.querySelector('.miso-list__item-title')||{}).textContent || '', context: e.textContent.trim()}))",
        )
    except Exception:
        items = []
    links = [{"title": (it["title"] or "").strip(), "href": it["href"], "context": it.get("context")} for it in items]
    candidates = _candidate_filter(links, ["mirrormedia.mg/story/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_mnews(page, keyword, start_date, end_date, press_release_text=None):
    """鏡新聞 mnews.tw（鏡電視旗下新聞台，與「鏡週刊」「鏡報」為不同網站）。
    維持 Playwright：純 requests 取得的 HTML 中關鍵字完全不存在（搜尋結果純前端渲染），
    需要真實瀏覽器執行 JS 才能取得結果（僅指搜尋列表頁；個別文章頁已實測可用 plain
    requests 正常取得，`_verify_candidates()` 內部造訪文章頁不需要瀏覽器）。
    列表頁卡片標題會夾帶「YYYY.MM.DD HH:MM」時間戳，這裡先清掉（跟原本邏輯一致，
    純粹是顯示用標題清理），日期比對改交給 `_verify_candidates()` 內建的文章頁
    日期擷取 cascade。"""
    kw = quote(keyword)
    url = f"https://www.mnews.tw/search/{kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(3000)
    links = _all_links(page)
    for it in links:
        it["title"] = re.sub(r'\d{4}\.\d{2}\.\d{2}\s*\d{2}:\d{2}', '', it["title"]).strip()
    candidates = _candidate_filter(links, ["mnews.tw/story/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def _extract_ctinews_nuxt_items(soup):
    """從中天新聞網搜尋結果頁的 `<script id="__NUXT_DATA__" type="application/json">`
    擷取每篇候選文章的 `news_id`／標題／發布時間。

    背景（2026-07-05 實測發現的 overflow bug，跟 setn／taisounds／tvbs 同一類）：
    這個頁面是 Nuxt SSR，搜尋結果列表頁的 DOM 本身完全沒有任何日期文字（每張卡片只有
    標題＋標籤，見 `.search-card--default`），但搜尋結果常常一次回傳 25 篇候選（超過
    `_CONTENT_VERIFY_MAX_CANDIDATES=20`），且排序是「跟關鍵字的相關度」而非嚴格新到舊
    （實測「蕭敬騰」關鍵字 25 筆候選全部落在 2025-10 ~ 2026-06，即使測試窗口設在
    2026-07 也是一樣這 25 筆，沒有任何一筆真正落在窗口內——原本的寫法會讓最後 5 筆
    落入 overflow 桶、以 date=None 不篩日期全部顯示，等於把半年前的舊聞當作命中結果）。

    真正的日期線索藏在 Nuxt 3 的 `__NUXT_DATA__` payload 裡：這是 devalue 序列化格式
    （一個扁平化的 JSON array，物件用「欄位名 → 陣列索引」的 dict 表示，實際值要
    再去對應索引取一次，例如 `{"news_id": 249, "publish_dt": 254, ...}` 表示這個物件的
    `news_id` 欄位值放在 `data[249]`）。這裡不去完整實作 devalue 反序列化（沒有必要，
    只需要抓日期），改用「掃描整個扁平陣列，找出形狀像搜尋結果項目的 dict（同時具備
    `news_id`／`title`／`publish_dt` 三個 key）」的方式，對每個符合的 schema dict 用
    它自己記錄的索引去 `data[]` 取出真正的 `news_id`／`title`／`publish_dt` 字串——
    這個做法比硬編索引位置更穩健（Nuxt 每次建置索引位置都會變動，但欄位名稱與整體
    形狀不會變），已實測跟直接手動追蹤陣列位置得到的結果完全一致。

    `link`／`url` 欄位在 payload 裡是空字串（Nuxt 這裡沒有把文章網址序列化進去），
    所以呼叫端仍需照舊用 `news_id` 拼出 `ctinews.com/news/items/<news_id>` 網址
    （這個 URL pattern本來就是既有程式碼在用的，這裡沒有改變）。

    回傳 list of (news_id, title, publish_dt_iso_string)，publish_dt 可能是 None
    （欄位存在但值是 null）；解析失敗或找不到 `__NUXT_DATA__` script 回傳空 list，
    呼叫端應視為「沒有可信日期線索」、整批候選照舊流程处理（不影響既有行為）。
    """
    script = soup.find("script", id="__NUXT_DATA__")
    if not script or not script.string:
        return []
    try:
        data = json.loads(script.string)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    seen = set()
    results = []
    for v in data:
        if not (isinstance(v, dict) and "news_id" in v and "publish_dt" in v and "title" in v):
            continue
        try:
            news_id = data[v["news_id"]]
            title = data[v["title"]]
            publish_dt = data[v["publish_dt"]]
        except (IndexError, TypeError):
            continue
        if not isinstance(news_id, str) or not isinstance(title, str) or news_id in seen:
            continue
        seen.add(news_id)
        results.append((news_id, title, publish_dt if isinstance(publish_dt, str) else None))
    return results


def search_ctinews(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。

    2026-07-05 修正：搜尋結果列表頁本身沒有任何可見日期文字，且排序非嚴格新到舊，
    候選數常超過 20 篇驗證額度上限，導致 overflow 桶的候選（date=None）把數月前的
    舊聞當作命中結果顯示（詳見 `_extract_ctinews_nuxt_items()` docstring）。改用該
    函式從 Nuxt SSR payload（`__NUXT_DATA__`）擷取每篇候選的可信發布時間，在丟進
    `_candidate_filter()`／`_verify_candidates()` 之前先排除範圍外的候選，不佔用
    overflow 桶（同 `search_setn()`／`search_tvbs()` 的做法）。如果解析不到
    `__NUXT_DATA__`（例如網站改版），退回原本「不篩日期」的行為，不會比修改前更差。
    """
    kw = quote(keyword)
    url = f"https://ctinews.com/search/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    nuxt_items = _extract_ctinews_nuxt_items(soup) if soup is not None else []
    if nuxt_items:
        date_by_news_id = {}
        for news_id, _title, publish_dt in nuxt_items:
            d = _parse_date_string(publish_dt) if publish_dt else None
            date_by_news_id[news_id] = d
        filtered_links = []
        for it in links:
            m = re.search(r'/news/items/([^/?#]+)', it["href"])
            news_id = m.group(1) if m else None
            pub_d = date_by_news_id.get(news_id) if news_id else None
            if pub_d is not None and not (start_date <= pub_d <= end_date):
                continue  # Nuxt payload 給了可信日期，範圍外提早排除，不佔驗證額度
            filtered_links.append(it)
        links = filtered_links
    candidates = _candidate_filter(links, ["ctinews.com/news/items/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_ftvnews(page, keyword, start_date, end_date, press_release_text=None):
    """搜尋結果列表頁維持 Playwright：網站有 Cloudflare JS 挑戰頁（"Just a moment..."），
    純 requests 呼叫會被擋下回傳 403，需要真實瀏覽器執行 JS 才能通過驗證並取得搜尋結果。

    2026-07-05 修正（實測發現的日期不準確案例，使用者回報搜尋 2026/07/01~07/02 的
    「蕭敬騰」卻出現 2026/06/29 的舊聞）：根本原因比 setn／taisounds 那類「overflow
    桶跳過日期檢查」更嚴重——這個網站的 Cloudflare 挑戰不只擋搜尋結果頁，連「個別
    文章頁」都一樣擋（實測 `requests.get()` 對文章頁一律回傳 403 "Just a moment..."）。
    這代表 `_verify_candidates()` 的 `_fetch_article_content_and_date()`（純 `requests`
    呼叫）對這個站台永遠會失敗，不是只有 overflow 桶的候選、而是「全部」候選都會
    優雅降級成列表頁摘要比對、日期一律 `date=None`，等於這個站台的日期過濾從來沒有
    真正生效過（先前的 docstring 只寫了「拿不到內容驗證的準確度優勢」，沒意識到日期
    過濾也一併完全失效）。

    這裡也一併發現：舊版單純 `page.goto()`（沒有隱藏 headless 特徵）實測有時連
    搜尋結果頁本身都會被 Cloudflare 擋下（"Just a moment..." 標題、抓不到任何搜尋
    結果），只是不是每次都會發生，運氣好時仍能拿到真正的搜尋結果——這解釋了為什麼
    這個 bug 只在部分次數的搜尋中出現。實測發現只要多加一行隱藏
    `navigator.webdriver` 的 init script（`page.context` 既有的 UA／locale 設定
    已經是 app.py 提供的正常 Chrome UA，不需要動 app.py），穩定通過 Cloudflare 的
    JS 挑戰（本機測試多次皆穩定成功，不需要修改 app.py 共用的瀏覽器啟動參數）。

    真正的修法：既然文章本頁永遠拿不到（Cloudflare 擋 `requests`），改用「列表頁
    本身」的日期——每張搜尋結果卡片的 `<div class="time" data-time="YYYY/MM/DD
    HH:MM:SS">` 屬性本身就是可信的發布時間（已實測跟卡片顯示的文字完全一致），
    在丟進 `_candidate_filter()`／`_verify_candidates()` 之前，先用這個日期做預先
    過濾（同 search_cts／search_setn／search_taisounds 的精神）。範圍外的候選
    直接不列入，不會再有機會透過「文章頁驗證必然失敗 → 退回列表摘要比對 →
    date=None 一律通過」這條路徑漏出去。
    """
    kw = quote(keyword)
    url = f"https://www.ftvnews.com.tw/search/{kw}"
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    try:
        items = page.eval_on_selector_all(
            "section.search-list li",
            """els => els.map(e => {
                const a = e.querySelector('a[href*="/news/detail/"]');
                const timeEl = e.querySelector('div.time');
                const titleEl = e.querySelector('div.title');
                const summaryEl = e.querySelector('div.summary');
                return {
                    href: a ? a.href : null,
                    title: titleEl ? titleEl.textContent.trim() : (a ? a.textContent.trim() : ''),
                    dataTime: timeEl ? timeEl.getAttribute('data-time') : null,
                    summary: summaryEl ? summaryEl.textContent.trim() : ''
                };
            })""",
        )
    except Exception:
        items = []
    links = []
    for it in items:
        href = it.get("href")
        if not href or not it.get("title"):
            continue
        pub_d = _parse_date_string(it.get("dataTime")) if it.get("dataTime") else None
        if pub_d is not None and not (start_date <= pub_d <= end_date):
            continue  # 列表頁自帶可信日期，範圍外提早排除，不佔驗證額度（同 search_cts／search_setn）
        links.append({"title": it["title"], "href": href, "context": it.get("summary") or it["title"]})
    candidates = _candidate_filter(links, ["ftvnews.com.tw/news/detail/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_owlnews(page, keyword, start_date, end_date, press_release_text=None):
    """奧丁丁OwlNews報新聞。維持 Playwright：純 requests 取得的 HTML 只有 meta/JSON-LD
    描述文字含關鍵字，實際文章清單由前端 JS 動態載入，需要真實瀏覽器執行 JS 才能取得結果。"""
    kw = quote(keyword)
    url = f"https://news.owlting.com/articles/search/{kw}?locale=zh-TW"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(2500)
    links = _all_links(page)
    candidates = _candidate_filter(links, ["news.owlting.com/articles/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_ftnn(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。

    2026-07-05 修正（實測發現的 overflow bug，跟 setn／tvbs／ctinews 同一類）：這個搜尋
    結果頁本身的真正搜尋結果只有 24 篇（每篇包在 `<div class="search-news">` 裡），但
    `_all_links_requests()` 用全頁 `<a>` 掃描時，會把頁面下半段「熱門新聞／即時新聞」
    等跟搜尋關鍵字完全無關的推薦清單（財經、其他藝人新聞）也一起掃進來，把候選數從
    24 筆撐到 44 筆，超過 `_CONTENT_VERIFY_MAX_CANDIDATES=20`，導致真正搜尋結果的尾端
    幾篇（例如 2026.03.18 的舊聞）落入 overflow 桶、以 date=None 跳過日期檢查顯示。

    修法：每個真正的搜尋結果 `<div class="search-news">` 內都帶有標題
    （`<p class="search-tit">`）與可信發布時間（`<span class="s-news-time">`，格式
    `YYYY.MM.DD HH:MM`）——這兩個 class name 只出現在真正的搜尋結果卡片，天然排除了
    後段的推薦清單雜訊。改成只掃描 `div.search-news` 容器，用 `s-news-time` 做「驗證前
    預先過濾」，範圍外的候選不會進入 `links` list，也不會佔用驗證額度。"""
    kw = quote(keyword)
    url = f"https://www.ftnn.com.tw/search?keyword={kw}&all=true"
    soup = _get_soup(url, timeout=35)
    links = []
    if soup is not None:
        for card in soup.find_all("div", class_="search-news"):
            a = card.find_parent("a", href=True)
            title_el = card.find("p", class_="search-tit")
            if not a or not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue
            time_el = card.find("span", class_="s-news-time")
            pub_d = _parse_date_string(time_el.get_text(strip=True)) if time_el else None
            if pub_d is not None and not (start_date <= pub_d <= end_date):
                continue  # 列表頁自帶可信日期，範圍外提早排除，不佔驗證額度（同 search_setn）
            href = urljoin(url, a["href"])
            links.append({"title": title, "href": href, "context": title})
    candidates = _candidate_filter(links, ["ftnn.com.tw/news/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_life(page, keyword, start_date, end_date, press_release_text=None):
    """Life.tw 台灣生活網。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://life.tw/?app=search&keyword={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["life.tw/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_juksy(page, keyword, start_date, end_date, press_release_text=None):
    """JUKSY 街星（清單中的 JUSKY_HOT 應為此站）。已改用 requests + BeautifulSoup
    （伺服器端渲染，已驗證非本關鍵字時也能抓到搜尋結果連結）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.juksy.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["juksy.com/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_premiermedia(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.premiermedia.com.tw/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["premiermedia.com.tw/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_findnewstoday(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://findnewstoday.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["findnewstoday.net/archives/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_taiwanpost(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://taiwanpost.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["taiwanpost.net/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_mypeople(page, keyword, start_date, end_date, press_release_text=None):
    """民眾新聞（民眾網）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://mypeoplevol.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["mypeoplevol.com/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_ponews(page, keyword, start_date, end_date, press_release_text=None):
    """博新聞網（Blogger 平台）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.po-news.net/search?q={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["po-news.net/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


_HUALIENTODAY_MAX_CANDIDATES = 80  # 見 search_hualientoday()：這個站台的搜尋結果頁沒有任何
# 列表頁層級的可信日期線索，改用「調高驗證額度」這個次佳解法（詳見函式 docstring）。


def search_hualientoday(page, keyword, start_date, end_date, press_release_text=None):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。

    2026-07-05 實測發現的 bug（跟其他站台的 overflow 問題略有不同，是這裡特有的
    「context 汙染」變形）：這個 WordPress 站台的 `?s=` 搜尋等於沒有真正依關鍵字
    過濾（實測「蕭敬騰」關鍵字搜尋結果 66 筆候選裡，只有 1 篇真正提到這個人，
    其餘都是花蓮在地新聞——跟 juksy／po-news 一樣的「搜尋形同虛設」問題，這裡
    不特別處理，只影響有多少候選會被丟進驗證，不影響日期過濾本身）。

    真正影響日期正確性的是：搜尋結果列表頁的 DOM 版型把「每 6 篇一組」的卡片包在
    同一個 `<aside>` 容器裡（沒有各自獨立的外層），導致 `_extract_link_context()`
    往上找父層容器時，會把同一組 6 篇文章的標題全部串在一起當作 context（每篇
    候選的 `context` 內容幾乎一樣，都包含整組 6 篇標題）。這造成 overflow 桶的
    `_has_nickname_intro(context, keyword)` 比對失真：只要組內剛好有 1 篇真的
    提到關鍵字，同一組其餘 5 篇不相關的候選也會一起被誤判為命中（實測「蕭敬騰」
    只出現在 1 篇裡，但同組另外 5 篇花蓮在地新聞——鼓王爭霸戰、購物節、豐年節等
    —— 全部因為這個 context 汙染被一起顯示，且因為 overflow 桶不驗證日期，
    全部以 date=None 顯示，看起來像是同一批「蕭敬騰」相關的命中結果）。

    列表頁本身完全沒有任何日期文字／屬性可用（實測過縮圖網址、`<time>`、data
    屬性都沒有），沒有可信的日期線索可以做「驗證前預先過濾」，所以這裡改用
    次佳解法：調高 `_verify_candidates(max_candidates=...)`，讓更多候選（含
    這種因 context 汙染而被拉近但實際跟關鍵字無關的候選）都能造訪文章本頁做
    真正的內容驗證，而不是退回不可靠的 context 比對——已實測確認調高後：
    (1) 真正相關的候選（夏戀嘉年華蕭敬騰報導）能正確標上文章本頁日期
    (2) 因 context 汙染而混進來的 5 篇不相關花蓮新聞，各自造訪本頁驗證後
        內容不含關鍵字，正確被排除，不再誤判命中。
    這個修法同時解決「日期」與「context 汙染誤判」兩個問題，因為問題根源相同
    （overflow 桶不驗證內容、只信任不可靠的列表頁 context）。
    """
    kw = quote(keyword)
    url = f"https://hualien-today.com/?s={kw}"
    soup = _get_soup(url, timeout=30)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["hualien-today.com/news.php?listno="])
    return _verify_candidates(
        candidates, keyword, start_date, end_date,
        max_candidates=_HUALIENTODAY_MAX_CANDIDATES, press_release_text=press_release_text,
    )


def search_insightpost(page, keyword, start_date, end_date, press_release_text=None):
    """洞見新聞網 → 對應「洞見國際事務評論網」insight-post.tw（清單中僅找到此近似站台）。
    維持 Playwright：整站為前端 JS SPA，純 requests 拿到的 HTML 幾乎沒有內文連結，
    需要真實瀏覽器執行 JS 才能取得頁面內容。"""
    kw = quote(keyword)
    url = f"https://insight-post.tw/?s={kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    links = _all_links(page)
    candidates = _candidate_filter(links, ["insight-post.tw/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_starsetn(page, keyword, start_date, end_date, press_release_text=None):
    """娛樂星聞 star.setn.com（三立旗下娛樂站，與主站 setn.com 分屬不同網域，主站目前被阻擋）。
    已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://star.setn.com/search/{kw}/"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["star.setn.com/news/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_videoland(page, keyword, start_date, end_date, press_release_text=None):
    """緯來新聞網 news.videoland.com.tw。已改用 requests（純 JSON API＋一個固定的
    轉址網址模板，不需要 BeautifulSoup 解析列表頁，也不需要 Playwright）。
    page 參數保留但不使用。

    先前記錄「找到內部 JSON API `newsapi.videoland.com.tw/api/Search`，但無法把
    結果映射到真正的文章網址」——這裡把整個發現過程重跑一遍：

    1. 這個 API 其實是 **POST**，不是 GET（純 GET 呼叫會得到 405 Method Not
       Allowed）。實際 request body 是 `application/x-www-form-urlencoded`：
       `keywords=<urlencoded>&pagecount=10&pageindex=0`（用 Playwright 監看搜尋頁
       實際送出的 XHR 才抓到這個格式，純看 API 網址猜不出來）。
    2. 回應 JSON 是一個 list，每筆有 `sno`（文章代碼）、`title`、`newsdesc`
       （摘要）、`realdate`（`YYYY/MM/DD`，可信的發布日期）——但**沒有任何 URL
       欄位**，這是先前卡關的原因：以為 `sno` 可以直接拼進某個 URL pattern，
       實測全部猜測（`/article/<sno>`、`/article/<sno>.html`、`/news/<sno>`、
       `newsapi.../api/News/<sno>`...）都不是真正的文章頁（有些回 200 但內容是
       空殼 SPA shell，不是真正文章）。
    3. 真正的映射關係要從「使用者實際點擊搜尋結果卡片後去了哪裡」反推：這個
       網站是 React SPA，卡片本身不是 `<a href>`（`_all_links_requests()`／
       `_all_links()` 完全抓不到），點擊會呼叫 `window.open(url)`。用
       `page.evaluate("window.open = (url) => window.__capturedUrl = url")`
       攔截這個呼叫（不讓它真的開新分頁，只記錄參數），點擊後讀出
       `window.__capturedUrl`，拿到的是 `/viewnews.aspx?sno=<sno>`——一個
       ASP.NET 網址，伺服器端會 302 轉址到真正的 SPA 文章網址
       （`/article/<uuid>.html`，跟 API 回傳的 `sno` 完全不同的識別碼）。
       這一步是關鍵：`sno` 本身不是拼網址用的，而是要先組成
       `viewnews.aspx?sno=<sno>` 這個「舊版轉址橋接」網址，再讓伺服器端 302
       轉過去，中間完全不需要瀏覽器執行 JS（`requests.get(..., allow_redirects=True)`
       預設就會自動跟隨這個 302，`_verify_candidates()` 內部的
       `_fetch_article_content_and_date()` 用的也是 `requests.get()`，同樣能
       正確跟隨轉址取得真正文章內容）。

    這裡選擇直接把 `viewnews.aspx?sno=` 網址交給 `_candidate_filter()`／
    `_verify_candidates()`（而不是先自己解析出轉址後的真正網址），因為
    `_verify_candidates()` 本來就會對每個候選網址發一次 requests 造訪＋跟隨
    轉址，沒有必要多一輪「先解析出最終網址」的步驟——`_fetch_article_content_and_date()`
    目前沒有把轉址後的 `resp.url` 回傳出來，所以候選字典裡存的 `url` 欄位
    仍然是轉址前的 `viewnews.aspx?sno=` 網址，但這完全沒問題：跟其他站台
    一樣直接可以點擊使用，使用者點擊時瀏覽器一樣會自動跟著 302 過去真正的
    文章頁，不影響使用者體驗。
    """
    kw = quote(keyword)
    body = f"keywords={kw}&pagecount=20&pageindex=0"
    headers = dict(_REQUEST_HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    headers["Referer"] = "https://news.videoland.com.tw/"
    try:
        resp = requests.post(
            "https://newsapi.videoland.com.tw/api/Search", headers=headers, data=body, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []
    links = []
    for item in data if isinstance(data, list) else []:
        title = (item.get("title") or "").strip()
        sno = item.get("sno")
        if not title or not sno:
            continue
        pub_d = _parse_date_string(item.get("realdate"))
        if pub_d is not None and not (start_date <= pub_d <= end_date):
            continue  # API 已給可信日期，範圍外提早排除，不佔驗證額度（同 search_cts）
        href = f"https://news.videoland.com.tw/viewnews.aspx?sno={sno}"
        links.append({"title": title, "href": href, "context": item.get("newsdesc") or title})
    candidates = _candidate_filter(links, ["news.videoland.com.tw/viewnews.aspx"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_taisounds(page, keyword, start_date, end_date, press_release_text=None):
    """太報 taisounds.com。已改用 requests + BeautifulSoup（伺服器端渲染）。
    page 參數保留但不使用。

    先前記錄「Next.js SPA，找不到可用搜尋路由」——實測首頁完全沒有
    `<script id="__NEXT_DATA__">`，判斷不是 Next.js（可能是 Vue／其他框架，
    或先前的判斷本身就有誤，這裡不深究框架種類，只確認「不需要當成 Next.js
    的 `/_next/data/...json` 模式處理」）。

    真正的搜尋 UI 藏在首頁一個預設 `display:none` 的 Bootstrap modal
    （`#myModal2`）裡：搜尋輸入框 `#keyword` 與搜尋按鈕 `#btnSearch` 都在這個
    modal 內，必須先點擊 `<a class="ico01" data-toggle="modal"
    data-target="#myModal2">`（放大鏡圖示，沒有文字/aria-label，純靠
    `data-target` 屬性找到）才會讓 modal 顯示、輸入框變成可互動狀態——直接對
    `#keyword` 呼叫 `page.fill()` 會因為元素還是 `display:none` 而逾時卡住。

    點擊搜尋按鈕後，實際導航到 `https://www.taisounds.com/lookfor/tag/<urlencoded>`
    ——這是一個純伺服器端渲染的「標籤頁」（不是 AJAX API），`requests.get()` 直接
    呼叫就能拿到跟瀏覽器一致的完整 HTML（已實測「蕭敬騰」關鍵字在頁面中出現
    數十次，不只在少數幾筆真正相關的候選裡）。

    這個頁面本質上是「標籤聚合頁」，且實測發現一個跟其他站台不一樣的重要地雷：
    最前面 20 篇幾乎都是跟關鍵字完全不相關的「網站最新新聞」（這個標籤頁把
    「符合標籤的文章」跟「全站最新」混在同一個列表裡呈現，不是嚴格由新到舊
    排序的關鍵字搜尋結果），真正符合關鍵字的候選反而排在第 20 筆之後——這正好
    卡進 `_CONTENT_VERIFY_MAX_CANDIDATES=20` 的驗證額度之外，導致實測第一版
    直接照抄其他站台的寫法時，全部命中的候選都落入 overflow 桶、日期一律
    `date=None`（不是抓不到日期，文章頁日期其實抓得到，見
    `_fetch_article_content_and_date` 的獨立測試；純粹是候選順序問題）。

    2026-07-05 修正（實測發現上面這版「標題重排序」不夠：這個頁面第一頁根本
    可能整頁都是跟關鍵字完全無關的全站最新新聞，標題排序只是把「本來就沒有」
    的東西挪到前面，並不能保證真正相關的候選會落在 20 篇驗證額度內——實測
    「小宇」關鍵字的第一頁 10 篇 `<li>` 全部是總統教育獎／世足賽這類完全無關的
    頭條，標題排序後順序不變，真正命中「小宇攜手艾薇闖《紅白》」這類舊聞只能
    透過 overflow 桶的列表頁摘要比對，一樣繞過日期檢查，出現 2024 年舊聞當作
    命中結果的問題，跟已經修過的 setn 是同一類 bug）。

    真正的修法：每個 `<li>` 本身就帶有可信的發布時間——`<p class="media-date">`
    （格式 `YYYY-MM-DD HH:MM`，已實測跟文章本頁一致），不需要像 setn 那樣拐個彎
    從縮圖網址猜日期。改成直接解析 `ul#ulnewslist` 底下每個 `<li>`，取得
    標題（`<h4>`）、連結、日期（`<p class="media-date">`）、摘要
    （`<p class="text-truncate-2">` 的 `title` 屬性），範圍外的候選比照
    `search_cts()`／`search_setn()` 提早排除、不進 `_candidate_filter()`／
    `_verify_candidates()`，避免無關的全站最新新聞占用驗證額度，也避免真正
    命中但範圍外的舊聞透過 overflow 桶跳過日期檢查。
    """
    kw = quote(keyword)
    url = f"https://www.taisounds.com/lookfor/tag/{kw}"
    soup = _get_soup(url)
    links = []
    if soup is not None:
        ul = soup.find("ul", id="ulnewslist")
        for li in (ul.find_all("li", recursive=False) if ul else []):
            a = li.find("a", href=True)
            if not a:
                continue
            title_el = a.find(["h1", "h2", "h3", "h4"])
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            if not title:
                continue
            date_el = a.find("p", class_="media-date")
            pub_d = _parse_date_string(date_el.get_text(strip=True)) if date_el else None
            if pub_d is not None and not (start_date <= pub_d <= end_date):
                continue  # 列表頁自帶可信日期，範圍外提早排除，不佔驗證額度（同 search_cts／search_setn）
            summary_el = a.find("p", class_="text-truncate-2")
            context = (summary_el.get("title") or summary_el.get_text(strip=True)) if summary_el else title
            links.append({"title": title, "href": urljoin(url, a["href"]), "context": context})
    candidates = _candidate_filter(links, ["taisounds.com/news/content/"])
    candidates.sort(key=lambda c: 0 if keyword in c["title"] else 1)
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


_ENEWS_CSE_CX = "c2e4ab61825ba4815"  # 從 enews.tw 首頁 `gcse.js?cx=...` 嵌入碼取得的 CSE ID


def search_enews(page, keyword, start_date, end_date, press_release_text=None):
    """eNEWS enews.tw。維持 Playwright：站內原生搜尋頁（`/search?keyword=`）
    重新檢查過一次，確認仍是先前記錄的狀態——頁面本身沒有串接真正的搜尋功能，
    只嵌入一個 Google 自訂搜尋（CSE）小工具（`<div class="gcse-searchbox-only">`
    ＋ `<script src="https://cse.google.com/cse.js?cx=c2e4ab61825ba4815">`），
    首頁本身這個搜尋框只是導向 CSE，不是自建的搜尋結果頁。

    這裡採用跟 `hitfm`（見 config.py／app.py 的手動連結清單）同樣的技巧，但比
    hitfm 更進一步：直接嘗試 CSE 提供的免費、免 API 金鑰的網頁版結果頁
    `https://cse.google.com/cse?cx=<cx>&q=<kw>`。純 `requests.get()` 這個網址
    只會拿到一個等待 JS 執行的空殼（`<div id="cse-hosted">`，實際結果由內嵌的
    `cse.js` 動態 XHR 載入 `https://cse.google.com/cse/element/v1?...&cse_tok=...`
    這個端點才會拿到，而且這個端點需要一個跟瀏覽器 session／頁面載入綁定的
    `cse_tok` 簽章參數，纯 `requests` 沒有這個 token 直接呼叫會被 Google 判定為
    自動化查詢、回傳 403"Sorry..."頁——這點證實了 hitfm 註解裡「需要瀏覽器執行
    JS 才能通過」的判斷，純 requests 這條路線走不通）。

    但用 Playwright 完整載入 `cse.google.com/cse?cx=...&q=...` 這個頁面（讓
    `cse.js` 自己完成 token 簽章＋XHR 請求＋把結果渲染進 DOM）之後，
    `a.gs-title` 選擇器就能抓到乾淨的搜尋結果（標題＋原始網站的真正文章網址，
    例如 `enews.tw/article/1263176`）——這個技巧比直接呼叫內部 XHR 端點更簡單
    可靠（不用自己處理 token 簽章），且因為 CSE 頁面本身跟 enews.tw 沒有網域
    綁定關係，這個函式其實可以直接複用同一套手法處理 hitfm（差別只在
    `cx` 參數），但 hitfm 目前仍先維持手動——本次時間分配優先把這個技巧
    在 enews 驗證過一輪，hitfm 的 `cx` 尚待從其官網嵌入碼確認。
    """
    kw = quote(keyword)
    url = f"https://cse.google.com/cse?cx={_ENEWS_CSE_CX}&q={kw}"
    page.goto(url, timeout=25000, wait_until="load")
    page.wait_for_timeout(3500)
    try:
        items = page.eval_on_selector_all(
            "a.gs-title",
            "els => els.map(e => ({title: e.textContent.trim(), href: e.href}))",
        )
    except Exception:
        items = []
    links = [{"title": it["title"], "href": it["href"], "context": it["title"]} for it in items if it["title"]]
    candidates = _candidate_filter(links, ["enews.tw/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_yam(page, keyword, start_date, end_date, press_release_text=None):
    """蕃薯藤 n.yam.com（清單裡的「蕃新聞」是同一個網站，見 config.py 註解）。
    已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。

    首頁 `#skeyword` input 對應的 `<form action="https://n.yam.com/Home/keywordSearch"
    method="post">`：實測發現直接照抄「表單填字＋按 Enter／點搜尋按鈕」在 Playwright
    裡完全沒有觸發任何導航（GA 只記錄到一個 `form_start` 事件，之後沒有下文，猜測
    前端另外綁了會攔截預設送出行為的 JS，但沒深究到底為什麼失敗）。改用更簡單的
    路線：直接對同一個 action 網址發 **GET**（不是表單宣告的 POST）並帶
    `?keyword=<urlencoded>` query string，伺服器端會 302 轉址到真正的搜尋結果頁
    `https://search.yam.com/Search/news?q=<urlencoded>`——這一步是關鍵發現，之前的
    嘗試卡在「以為要照表單的 POST 方式送出」，但其實 GET＋轉址就會直接到對的地方，
    且這個轉址是伺服器端 302（`requests` 預設 `allow_redirects=True` 就會自動跟過去），
    不需要瀏覽器執行 JS。`search.yam.com` 這個結果頁本身也是伺服器端渲染，直接用
    `requests` 拿到的 HTML 就含有完整搜尋結果（已實測「蕭敬騰」關鍵字在 HTML
    中出現數十次，不是只在 meta 標籤）。

    2026-07-05 修正（實測發現的 overflow bug，跟 setn／tvbs／ctinews 同一類）：搜尋結果
    常一次回傳 30 篇候選（超過 `_CONTENT_VERIFY_MAX_CANDIDATES=20`），且排序不是嚴格
    新到舊（實測「蕭敬騰」關鍵字 30 筆候選裡日期從 2025-02 跳到 2026-07 又跳回
    2025-09，混雜排列），導致真正在查詢範圍內的候選被擠到 overflow 桶、以 date=None
    跳過日期檢查，把數個月甚至一年前的舊聞當作命中結果顯示。

    修法：這個站台的文章網址本身就內嵌可信的發布日期——`https://n.yam.com/Article/
    <YYYYMMDD><流水號>`（已實測跟文章本頁擷取到的日期完全一致，例如
    `/Article/20260701705628` 對應 2026-07-01），不需要額外請求就能從候選列表階段
    的 URL 直接解析出日期，比照 `search_pchome()` 用 URL 路徑日期做「驗證前預先
    過濾」的做法，範圍外的候選不會進入 `links` list，不佔用驗證額度。
    """
    kw = quote(keyword)
    url = f"https://n.yam.com/Home/keywordSearch?keyword={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    filtered_links = []
    for it in links:
        m = re.search(r'/Article/(\d{4})(\d{2})(\d{2})\d+', it["href"])
        pub_d = None
        if m:
            try:
                pub_d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pub_d = None
        if pub_d is not None and not (start_date <= pub_d <= end_date):
            continue  # 網址本身內嵌可信日期，範圍外提早排除，不佔驗證額度（同 search_pchome）
        filtered_links.append(it)
    candidates = _candidate_filter(filtered_links, ["n.yam.com/Article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_linetoday(page, keyword, start_date, end_date, press_release_text=None):
    """LINE TODAY。不需要 Playwright，純 `requests` 呼叫內部 JSON API 即可。
    page 參數保留但不使用。

    首頁／`?q=` 這種直接帶 query string navigate 的方式不會觸發真正的搜尋（純 JS
    路由，`?q=` 只是預填欄位用，實測 `.searchBar-input` 的 value 是空的，也沒有
    對應的搜尋 XHR 被送出）。照這個模組其他站台的做法，改用 Playwright 實際
    操作搜尋 UI（點擊輸入框、打字、按 Enter），透過 `page.on("response")` 監看
    網路請求，找到真正被前端呼叫的搜尋端點：
    `https://today.line.me/webapi/listing/search?country=tw&query=<urlencoded>`。

    找到端點後，實測發現這個 JSON API 本身**完全不需要瀏覽器**：純 `requests.get()`
    不帶任何特殊 header（連 Referer 都不用）就能拿到跟瀏覽器一樣的 200 JSON
    回應，因此這裡整個函式改成 API 直連，不需要啟動 headless Chromium（也不用
    加進 `PLAYWRIGHT_REQUIRED_SITES`）。

    回應是 `{"items": [...], "lastUpdatedTime": ...}`，單次呼叫就回傳最多 200 筆
    （已實測涵蓋數個月的範圍，不需要分頁），每筆結構：
    - `title`：標題（跟其他站台一樣，這是「關鍵字相關」排序，不是嚴格子字串比對，
      例如搜尋「蕭敬騰」，第一筆可能是完全不相關的《VPOP ASIA》報導，靠
      `_verify_candidates()` 的標題／內容關鍵字比對負責篩掉這些雜訊）。
    - `publishTimeUnix`：毫秒 epoch timestamp，已實測換算成台灣時區日期跟文章本頁
      擷取到的日期一致，這裡沿用跟 `search_cts()` 一樣的「API 已經給可信日期，
      先做範圍預先過濾，避免不可靠的候選占用 `_verify_candidates()` 的驗證額度」
      做法（LINE TODAY 的 200 筆結果不是嚴格按日期排序，用 pagination 提早停止
      的技巧在這裡不適用，只能全部先用 API 日期篩過一輪）。
    - `url.hash`：文章網址的識別碼，真正網址是
      `https://today.line.me/tw/v3/article/<hash>`（已實測跟 Playwright 操作 UI
      後在 DOM 裡看到的 `<a href>` 完全一致），這個網址本身也是伺服器端渲染，
      `_verify_candidates()` 內部用 plain requests 造訪即可正常取得內容與日期。
    - `publisher`：這則報導的原始來源媒體名稱（LINE TODAY 本身是轉載聚合平台），
      這裡沒有特別處理來源標註，維持跟其他轉載平台（Yahoo、Google新聞）一致的
      呈現方式，不特別在標題加註來源。
    """
    kw = quote(keyword)
    url = f"https://today.line.me/webapi/listing/search?country=tw&query={kw}"
    try:
        resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []
    links = []
    for item in data.get("items") or []:
        title = (item.get("title") or "").strip()
        url_hash = (item.get("url") or {}).get("hash")
        if not title or not url_hash:
            continue
        ts = item.get("publishTimeUnix")
        pub_d = None
        if isinstance(ts, (int, float)):
            try:
                pub_d = datetime.fromtimestamp(ts / 1000).date()
            except (ValueError, OverflowError, OSError):
                pub_d = None
        if pub_d is not None and not (start_date <= pub_d <= end_date):
            continue  # API 已給可信日期，範圍外提早排除，不佔驗證額度（同 search_cts）
        href = f"https://today.line.me/tw/v3/article/{url_hash}"
        links.append({"title": title, "href": href, "context": item.get("shortDescription") or title})
    candidates = _candidate_filter(links, ["today.line.me/tw/v3/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


_PCHOME_SEARCH_MAX_PAGES = 10  # 效能考量：最多翻幾頁（每頁 12 筆），一旦整頁都比 start_date 早就提早停止


def search_pchome(page, keyword, start_date, end_date, press_release_text=None):
    """PChome新聞。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。

    先前記錄「首頁 timeout / 很慢」——實測今天首頁與搜尋頁都在 1-2 秒內回應，
    未重現逾時，可能是網站狀況已經改善，也可能單純是當時網路環境問題；這裡還是
    保守把 timeout 拉高到 30 秒（見 `_get_soup(url, timeout=30)`）以防偶發變慢。

    搜尋結果頁 `https://news.pchome.com.tw/search/<kw>` 本身是伺服器端渲染，
    每篇搜尋結果包在 `class="channel_newssection"` 的 `<div>` 裡（共用的
    `_all_links_requests()` 掃全頁 `<a>` 會連同側欄「HOT人氣新聞」跟頁首導覽列
    都一起抓進來，所以這裡不用共用的全頁掃描，改成先鎖定 `channel_newssection`
    容器，只在容器內找主要文章連結，並排除容器內「關駉字：」標籤列的
    `/keyword/` 連結——那些指向站內關鍵字聚合頁，不是文章）。

    分頁：路徑加數字（`/search/<kw>/2`、`/search/<kw>/3`...）才是真正翻頁，
    `?page=2` 這種 query string 會被忽略、直接回傳第 1 頁（實測確認）。結果依日期
    新到舊排序，且文章網址本身就含 `YYYYMMDD`（例如
    `.../20260702/index-....html`），可以用這個路徑日期做兩件事：(1) 交給
    `_candidate_filter()`／`_verify_candidates()` 之前先過濾掉日期已經早於
    `start_date` 的候選（跟 `search_cts()` 同樣的預先過濾精神，避免占用驗證額度）；
    (2) 一旦整頁 12 筆都早於 `start_date`，代表後面的頁面只會更舊，提早停止翻頁
    （不像 cts 的 API 排序混亂無法這樣做，pchome 這裡是真的嚴格新到舊排序，可以
    安全提早結束）。
    """
    kw = quote(keyword)
    links = []
    for pg in range(1, _PCHOME_SEARCH_MAX_PAGES + 1):
        url = f"https://news.pchome.com.tw/search/{kw}" if pg == 1 else f"https://news.pchome.com.tw/search/{kw}/{pg}"
        soup = _get_soup(url, timeout=30)
        sections = soup.find_all(class_="channel_newssection") if soup else []
        if not sections:
            break
        page_has_in_range = False
        for sec in sections:
            main_a = sec.find("a", attrs={"data-linkdef": True})
            if not main_a or not main_a.get("href"):
                continue
            href = main_a["href"]
            title = (main_a.get("title") or main_a.get_text(strip=True) or "").strip()
            if not title:
                continue
            m = re.search(r'/(20\d{6})/', href)
            url_date = None
            if m:
                s = m.group(1)
                try:
                    url_date = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                except ValueError:
                    url_date = None
            if url_date is not None:
                if url_date < start_date:
                    continue  # 太舊，跳過（不佔驗證額度），但仍可能同頁有更新的項目排在前面
                if url_date <= end_date:
                    page_has_in_range = True
            links.append({"title": title, "href": href, "context": sec.get_text(" ", strip=True)[:_CONTEXT_MAX_LEN]})
        if not page_has_in_range:
            break  # 這一整頁都早於 start_date（或抓不到日期但已無新項目），後面只會更舊
    candidates = _candidate_filter(links, ["news.pchome.com.tw/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


_CTS_SEARCH_MAX_PAGES = 3  # 效能考量：最多翻幾頁 API（每頁約 29-30 筆），足以涵蓋近期搜尋範圍


def search_cts(page, keyword, start_date, end_date, press_release_text=None):
    """華視 news.cts.com.tw。已改用 requests（純 JSON API，連 BeautifulSoup 都不需要）。
    page 參數保留但不使用。

    首頁沒有任何 `<form>`／搜尋連結可循（實測 Playwright 掃描 `<input>`／`<form>`
    元素都是空陣列），但 `robots.txt` 裡的 `Disallow: /search` 暗示這個路徑其實存在
    （只是不希望被搜尋引擎收錄），純 `requests.get("/search/?q=...")` 直接拿到 500
    錯誤頁——這是 Vue SPA 的路由（頁面 CSS 可見 `news-search-overlay`／
    `news-search-dialog` 這類 scoped class name），伺服器端直接渲染這個路由本身不成立，
    500 是預期中的行為，不是網站掛了。

    真正的做法：用 Playwright 點擊首頁上 `aria-label="開啟搜尋"` 的按鈕開啟搜尋
    對話框，輸入關鍵字送出後，監看瀏覽器發出的 XHR/fetch 請求，找到前端呼叫的
    API 端點：`https://news.cts.com.tw/api/searches/news?keyword=<urlencoded>&page=<n>`。
    這個端點回傳乾淨的 JSON（`{"data": {"news": [...], "total":, "pagination":}}`，
    每筆已含 `title`／`publishTime`（`YYYY-MM-DD HH:MM:SS`，可直接當作候選的日期，
    但仍然照既有慣例交給 `_verify_candidates()` 造訪文章本頁做內容驗證＋日期
    二次確認，不直接信任列表 API 回傳的日期／標題）／`link`（文章網址）——且這個
    JSON API 本身可以直接用 `requests` 呼叫，完全不需要啟動瀏覽器（Playwright
    只是用來「發現」這個 API 端點的偵錯手段，找到之後就不必再依賴它）。

    翻頁與日期範圍：API 回傳結果不是嚴格按日期排序（實測 page=1 最後幾筆已經是數個
    月前的報導，page=2 開頭卻跳到 2024/2020 年的舊聞），這點如果照其他站台的既有
    寫法（直接把所有候選丟給 `_candidate_filter()`／`_verify_candidates()`，靠後者
    `_CONTENT_VERIFY_MAX_CANDIDATES` 上限只驗證前 20 筆、其餘 overflow 候選一律
    `date=None` 不篩日期）會出大問題：因為排序是亂的，「其餘」不是「比較舊、大概
    率不重要」的候選，可能一堆是舊聞卻因為 overflow 邏輯直接以 date=None 全部通過
    標題比對顯示出來（實測若不做這裡的預先過濾，65 筆結果裡有 64 筆 date=None，
    include 一堆 2024/2025 年的舊聞）。這裡的 API 剛好本來就在列表階段直接給
    `publishTime`（真實發布時間，不是像其他站台列表頁那樣不可靠的相對時間字串），
    所以在丟給共用驗證流程之前，先用這個日期做一次陽春的範圍過濾——不算是繞過
    「造訪文章本頁驗證內容」這個核心原則（關鍵字／暱稱鄰接／新聞稿相似度比對
    仍然全部交給 `_verify_candidates()` 對文章本頁做，這裡只是先篩掉日期一看就
    確定不在範圍內的候選，避免它們占用 20 筆的驗證額度、把真正在範圍內但排序
    較後面的候選擠到 overflow 桶去）。
    """
    kw = quote(keyword)
    links = []
    for pg in range(1, _CTS_SEARCH_MAX_PAGES + 1):
        url = f"https://news.cts.com.tw/api/searches/news?keyword={kw}&page={pg}"
        try:
            resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            break
        news_items = (data.get("data") or {}).get("news") or []
        if not news_items:
            break
        for item in news_items:
            title = (item.get("title") or "").strip()
            link = item.get("link") or ""
            if not title or not link:
                continue
            pub_d = _parse_date_string(item.get("publishTime"))
            if pub_d is not None and not (start_date <= pub_d <= end_date):
                continue  # API 本身就給了可信日期，範圍外的候選提早排除，不占驗證額度
            links.append({"title": title, "href": link, "context": title})
        total_pages = ((data.get("data") or {}).get("pagination") or {}).get("totalPages", 1)
        if pg >= total_pages:
            break
    candidates = _candidate_filter(links, ["news.cts.com.tw/"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


def search_setn(page, keyword, start_date, end_date, press_release_text=None):
    """三立新聞網 setn.com（與已支援的子站 star.setn.com「娛樂星聞」為不同網站）。

    維持 Playwright，而且不能只用 `page.goto(url_with_query_string)`：這是 ASP.NET
    WebForms 頁面（`#keyword` input + `__VIEWSTATE` hidden field + `<form method="post"
    action="https://www.setn.com/">`），單純用 `?kw=...` 這種 GET query string 直接
    navigate 到 `search.aspx?kw=...` 只會拿到完全空白的頁面（實測 `requests.get()` 跟
    Playwright `page.goto()` 都一樣，回應是 200 但 body 幾乎是空的 `<html><head></head>
    <body></body></html>`，不是 Cloudflare 或其他阻擋，就是這條路由本身不認 query
    string）。真正可行的方式是複製使用者實際操作：先載入首頁，把關鍵字填進
    `#keyword`，再按 Enter 觸發表單 postback（`page.expect_navigation()` 等表單送出後
    的整頁導航完成），瀏覽器導航後網址會變成不帶 query string 的乾淨
    `search.aspx`，但頁面內容此時才是真正的搜尋結果。

    搜尋結果列在 `div.contLeft` 容器內（其餘同層級的 `sbNewsList`／`owl-item` 等 class
    是首頁承襲下來的側欄「最新新聞」跑馬燈，不是搜尋結果，必須限定在這個容器內找
    連結，否則會把無關的首頁頭條當成搜尋結果——這是實測踩到的坑：一開始用共用的
    `_all_links(page)` 掃全頁面 `<a>`，抓到的全是側欄最新新聞，關鍵字完全對不上）。

    每篇結果在 `div.contLeft` 內固定出現兩個 `<a>`（標題一個、摘要一個），兩者
    href 都指向同一篇文章，只差在標題連結多了 `&Key=<urlencoded keyword>` 查詢參數
    ——如果直接拿完整 href 去重，会把同一篇文章當成兩篇不同候選（多打一次驗證
    請求，且最終報告會出現重複項目），所以這裡先把 href 正規化成只保留
    `NewsID=` 數字（不帶 `From=Search&Key=...` 這些搜尋來源追蹤參數）再交給
    `_candidate_filter()` 去重。

    重要修正（2026-07-05 實測發現的錯誤日期案例）：setn 的搜尋結果「不是」按日期
    排序，常見關鍵字（例如藝人暱稱「小宇」）一次可能回傳 70~80 篇候選，橫跨
    2016~2025 年，但 `_verify_candidates()` 只會逐篇造訪驗證前 `_CONTENT_VERIFY_MAX_CANDIDATES`
    （20）篇、超過的一律進 overflow 桶，而 overflow 桶「不會」擷取文章真正發布日期
    （見 `_verify_candidates()` 說明），只憑列表頁標題/摘要關鍵字比對就放行、標記
    `date=None`——等於完全沒有日期過濾，導致大量真正的舊聞（例如 2021 年的報導）
    被當成命中結果顯示，即使使用者搜尋的日期區間只有一天。

    修法：比照 `search_cts()`／`search_pchome()`／`search_videoland()` 既有的「用
    可信的輕量日期訊號提早過濾範圍外候選，不佔驗證額度」精神——這裡沒有 API
    可用，但實測發現每張搜尋結果卡片的縮圖網址本身就內嵌日期路徑（例如
    `https://attach.setn.com/newsimages/2025/08/05/5218718-L.jpg`），且這個日期
    跟文章頁 `article:published_time` 實測完全一致（見開發時的驗證腳本）。因此
    額外查詢每張卡片的 `<img>`，用 NewsID 對應到縮圖日期，範圍外的候選直接不
    加入 `links`（不是丟給 `_verify_candidates()` 再被 overflow 放行），這樣真正
    需要驗證的候選數量會大幅縮減到真的在範圍附近的文章，不會被 79 篇裡的其他
    76 篇舊聞擠出驗證額度。少數抓不到縮圖日期的卡片（理論上不應該發生，防禦性
    保留）維持原樣交給 `_verify_candidates()`，不主動排除。
    """
    kw = quote(keyword)
    page.goto("https://www.setn.com/", timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    try:
        page.fill("#keyword", keyword)
        # setn.com 掛載大量廣告/分析 script，"load" 事件觸發時間偶爾會拖到 20 秒以上
        # 才完成（實測驗證時遇過幾次 timeout，重跑就正常），這裡拉高到 30 秒降低
        # 偶發逾時導致整批漏抓的機率；失敗時仍優雅降級回傳空列表，不影響其他站台。
        with page.expect_navigation(timeout=30000):
            page.press("#keyword", "Enter")
    except Exception:
        return []
    page.wait_for_timeout(2000)
    try:
        items = page.eval_on_selector_all("div.contLeft a", _EXTRACT_ALL_LINKS)
    except Exception:
        items = []
    try:
        img_items = page.eval_on_selector_all(
            "div.contLeft div[class*='newsimg-area-item']",
            """els => els.map(e => {
                const a = e.querySelector('a[href*="NewsID="]');
                const img = e.querySelector('img');
                return {
                    href: a ? a.href : null,
                    src: img ? (img.getAttribute('data-original') || img.getAttribute('src') || '') : ''
                };
            })""",
        )
    except Exception:
        img_items = []
    date_by_newsid = {}
    for it in img_items:
        href, src = it.get("href"), it.get("src") or ""
        if not href:
            continue
        nm = re.search(r'NewsID=(\d+)', href)
        dm = re.search(r'newsimages/(\d{4})/(\d{2})/(\d{2})', src)
        if nm and dm:
            try:
                date_by_newsid[nm.group(1)] = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            except ValueError:
                pass
    links = []
    for it in items:
        href = it["href"]
        m = re.search(r'NewsID=(\d+)', href)
        news_id = m.group(1) if m else None
        norm_href = f"https://www.setn.com/News.aspx?NewsID={news_id}" if news_id else href
        thumb_date = date_by_newsid.get(news_id) if news_id else None
        if thumb_date is not None and not (start_date <= thumb_date <= end_date):
            continue  # 縮圖日期已可信地確認範圍外，提早排除，不佔驗證額度（同 search_cts）
        links.append({"title": it["title"], "href": norm_href, "context": it.get("context")})
    candidates = _candidate_filter(links, ["setn.com/News.aspx"])
    return _verify_candidates(candidates, keyword, start_date, end_date, press_release_text=press_release_text)


_GOOGLENEWS_MAX_RESOLVE = 60  # 每次搜尋最多解析幾篇 Google 新聞轉址網址（見下方說明）
# 使用者已明確表示「準確比速度重要」，這裡刻意調高（而非跟其他站台一樣用 20），
# 因為 RSS 日期預先過濾後，就算是常見關鍵字＋合理的幾天搜尋範圍，候選數實測仍
# 常常有 30~60 篇（Google 新聞聚合太多站台、同一事件常被多家轉載），沿用 20 會
# 讓超過一半的候選都落入「只信任標題」的舊版降級路徑，等於這次改版的效果打對折。
# 真正限制解析時間的是使用者自己設定的日期區間（區間越窄，RSS 預先過濾後的候選
# 越少），不需要另外用一個偏低的固定上限去限制，只保留這個上限防止極端情況
# （例如關鍵字太籠統＋區間開太寬）真的解析到數百篇拖垮效能。


def _resolve_googlenews_url(page, redirect_url, timeout=15000, poll_ms=200, max_polls=30):
    """用 Playwright 導航到 Google 新聞轉址網址（`news.google.com/rss/articles/CBMi...`），
    等待它自己的 client-side JS 轉址完成後取得真正的文章網址。只等到 `commit`
    （導航開始、HTML 開始下載）就返回，不等整頁資源／圖片全部載入完成（`load`／
    `networkidle`），再用短間隔輪詢等 `page.url` 離開 news.google.com 網域——實測這樣
    平均每篇約 1.5 秒，比等 `load` 快很多（Google 新聞轉址頁本身很肥，載入所有資源
    要好幾秒）。逾時仍停留在 news.google.com 網域，代表這篇轉址解析失敗（可能是
    文章已下架／轉址規則變動），回傳 None，呼叫端會優雅降級退回舊版「只信任標題」
    的比對方式，不會整篇直接漏掉。"""
    try:
        page.goto(redirect_url, timeout=timeout, wait_until="commit")
    except Exception:
        return None
    for _ in range(max_polls):
        if "news.google.com" not in page.url:
            return page.url
        page.wait_for_timeout(poll_ms)
    return None


def search_googlenews(page, keyword, start_date, end_date, press_release_text=None):
    """Google 新聞 RSS 搜尋（聚合各媒體報導）。

    2026-07-05 修正（實測發現：這個站台完全繞過本次新增的整套「內容驗證＋新聞稿
    比對」流程，只看「關鍵字有沒有出現在標題」，導致其他站台已經修好的假陽性
    （例如「蕭敬騰提拔出道！艾薇遭爆...」「才說不想去台灣發展！艾薇遭控...蕭敬騰
    公司緊急發聲」）在 Google 新聞這裡完全沒被濾掉，一樣顯示出來——這些其實是
    同樣幾篇文章，只是透過 Google 新聞這個聚合入口又出現一次）。

    舊版刻意不解析 Google News RSS 的轉址網址（`news.google.com/rss/articles/CBMi...`
    是 Google 自己的轉址短碼，不是文章真正的網址，plain requests 直接 GET 只會拿到
    轉址頁本身，需要瀏覽器執行 JS 才能取得真正網址），理由是「複雜度／額外請求數
    不成比例」——但使用者明確反映寧可慢一點也要準，所以這裡改成：先用 RSS 自帶的
    `pubDate` 做預先過濾（跟 `search_cts()` 同精神，範圍外的候選提早排除、不占用
    下面的解析額度），依日期新到舊排序後，對前 `_GOOGLENEWS_MAX_RESOLVE` 篇候選
    用 `_resolve_googlenews_url()` 解析出真正的文章網址，再用共用的
    `_fetch_article_content_and_date()` 抓文章內容，套用跟其他站台完全一樣的
    `_content_keyword_match()`／`_final_match_decision()` 判斷（含所有已驗證過的
    假陽性防呆：純掛名所屬公司、內文列舉雜訊等），確保 Google 新聞這個聚合入口
    跟直接造訪各家站台的準確度一致，不會又把已經修好的假陽性透過這裡重新洩漏。

    解析額度限制的原因：每篇候選都要開一次真的瀏覽器分頁導航（不像其他站台可以
    純 `requests.get()`），比其他站台貴很多，常見關鍵字一次可能有數十篇候選，
    全部解析會讓這個站台單獨拖慢好幾分鐘。超過額度的候選（以及解析失敗的候選）
    優雅降級為舊版「只信任標題」的比對方式（沿用既有的「抓不到就不主動排除」
    精神），不會整批漏掉，只是拿不到內容驗證的準確度優勢。

    2026-07-05 追加修正（實測發現 RSS `pubDate` 本身不可信，不只是「有時抓不到」
    這麼單純）：直接反覆呼叫 Google 新聞 RSS API 觀察同一篇 2022 年的舊文章
    （「專訪　關於小宇這個人...」），發現它的 `pubDate` 會隨著時間／查詢關鍵字
    不同而改變——同一篇文章，用「宋念宇」查詢時回報 2022/06/24（正確），但過一陣子
    再查（不管用哪個關鍵字）就變成回報「今天」的日期，等於 Google 自己的 RSS
    metadata 就是錯的、而且會隨時間持續飄移，不是單純的解析失敗。

    這代表 `pubDate` 只能拿來當作「值不值得花解析額度」的陽春預篩選訊號（省成本，
    篩錯了頂多是浪費/省下一次解析，不影響最終正確性），絕對不能當作候選最終顯示
    的日期——如果拿真正的文章本頁驗證失敗（`_resolve_googlenews_url()` 解析失敗，
    或 `_fetch_article_content_and_date()` 抓不到內容），舊版曾經直接退回信任這個
    不可靠的 `pubDate`，等於把 Google 自己飄移中的錯誤日期原封不動顯示出來（實測
    案例：這篇 2022 年舊文在某次查詢時 `pubDate` 飄移成當天日期，若解析／內容擷取
    在正式站台環境剛好失敗，就會顯示成「今天」的報導）。修法：拿不到真正文章本頁
    日期時，一律標記 `date=None`（沿用「抓不到日期時不主動排除，交給人工判斷」的
    既有原則），不再退回使用 `pubDate` 本身，避免顯示出一個已知不可靠的日期。
    """
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

    candidates = []
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
        if d is not None and not (start_date <= d <= end_date):
            continue  # RSS 本身就給了發布時間，範圍外提早排除，不佔解析額度（同 search_cts）
        candidates.append({"title": title, "redirect_url": link, "rss_date": d})

    # 依 RSS 日期新到舊排序，確保有限的解析額度優先用在最新的候選
    candidates.sort(key=lambda c: c["rss_date"] or date.min, reverse=True)
    to_resolve = candidates[:_GOOGLENEWS_MAX_RESOLVE]
    overflow = candidates[_GOOGLENEWS_MAX_RESOLVE:]

    results = []
    for c in to_resolve:
        real_url = _resolve_googlenews_url(page, c["redirect_url"])
        if real_url is None:
            # 轉址解析失敗：不退回信任 pubDate（已證實不可靠），標記日期不明
            results.append({"title": c["title"], "url": c["redirect_url"], "date": None})
            continue
        d, snippet = _fetch_article_content_and_date(real_url)
        keyword_match = _content_keyword_match(c["title"], snippet, [keyword])
        matched, pr_score = _final_match_decision(keyword_match, press_release_text, snippet)
        if not matched:
            continue
        if d is not None and not (start_date <= d <= end_date):
            continue
        item_out = {"title": c["title"], "url": real_url, "date": d}
        if pr_score is not None:
            item_out["press_release_score"] = pr_score
        results.append(item_out)

    for c in overflow:
        # 同上，不退回信任 pubDate，標記日期不明
        results.append({"title": c["title"], "url": c["redirect_url"], "date": None})

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
    "setn": search_setn,
    "yam": search_yam,
    "cts": search_cts,
    "pchome": search_pchome,
    "linetoday": search_linetoday,
    "videoland": search_videoland,
    "taisounds": search_taisounds,
    "enews": search_enews,
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
    "setn",         # ASP.NET WebForms postback，需要真的填表單＋按 Enter 觸發搜尋
    "enews",        # 依賴 Google 自訂搜尋（CSE）小工具，結果由 cse.js 動態 XHR＋簽章 token 載入，需要真實瀏覽器執行
    "googlenews",   # RSS 本身不需要瀏覽器，但 2026-07-05 起改用 Playwright 解析轉址網址（見 _resolve_googlenews_url）以套用內容驗證
}
