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


def _content_keyword_match(title, snippet, keywords):
    """文章本頁內容驗證用的關鍵字命中判斷。跟 `_filter()` 的邏輯保持一致的嚴謹標準：
    標題命中永遠算數（便宜、可信）；內容摘要則同時接受「暱稱緊接本名」的鄰接比對
    （`_has_nickname_intro`，跟列表頁摘要比對用同一份規則），以及「關鍵字整段直接
    出現在摘要、且不是逗號/頓號列舉雜訊的一部分」的子字串比對。

    這裡原本想單純用「關鍵字有出現在摘要就算命中」（比列表頁摘要比對更寬鬆一點，
    理由是 og:description／內文前幾段通常是 CMS 寫的完整導言散文，不是「一堆人名
    逗號列舉」的雜訊來源）——但實測驗證蕭敬騰基準案例時發現這個假設不成立：
    自由時報一篇報導的 og:description 內容是「...曾參與「Faye」詹雯婷、羅大佑、
    蕭敬騰等大咖音樂人現場演出...」，關鍵字前面剛好是「、」，証明列舉雜訊一樣會
    出現在文章導言裡，不是只有列表頁的「相關文章」推薦區塊才有。所以子字串比對
    這裡加一道跟 `_has_nickname_intro` 同精神的列舉分隔符號防呆：只要關鍵字緊鄰
    （前一個字或後一個字）逗號/頓號，就視為列舉雜訊、不算單純子字串命中（但仍可能
    透過 `_has_nickname_intro` 命中，只是這裡故意更嚴格一點，避免走回頭路重現
    今天稍早修過的「蕭敬騰／羅大佑」誤判）。
    """
    for k in keywords:
        if k in title:
            return True
        if not snippet:
            continue
        if _has_nickname_intro(snippet, k):
            return True
        idx = snippet.find(k)
        if idx == -1:
            continue
        before = snippet[idx - 1] if idx > 0 else ""
        after_idx = idx + len(k)
        after = snippet[after_idx] if after_idx < len(snippet) else ""
        if before in "，,、" or after in "，,、":
            continue  # 列舉雜訊（例如「詹雯婷、羅大佑、蕭敬騰等」），不算命中
        return True
    return False


def _verify_candidates(candidates, keyword, start_date, end_date,
                        max_candidates=_CONTENT_VERIFY_MAX_CANDIDATES,
                        timeout=_ARTICLE_DATE_TIMEOUT):
    """新版核心比對流程，取代舊版「_filter()（列表頁摘要判斷）→ _attach_dates()（另外
    抓日期）」兩階段。輸入 candidates 是 `_candidate_filter()` 的輸出（只做過 URL／
    標題長度過濾，尚未判斷關鍵字是否命中）。

    對前 max_candidates 篇候選文章：平行造訪文章本頁一次，同時拿到日期與內容摘要，
    最終命中判斷＝標題命中 or 內容摘要命中（`_content_keyword_match`）。如果這次額外的
    造訪失敗（網路錯誤、逾時、頁面解析不到任何內容摘要），退回用列表頁摘要做舊版
    `_has_nickname_intro` 比對（優雅降級，不能因為多做的驗證步驟失敗就直接放棄這篇候選）。

    超過 max_candidates 篇的候選，因效能考量不逐篇造訪本頁，同樣退回列表頁摘要比對
    （沿用舊版 `_filter()` 對「量太大」候選的處理精神：與其整批不驗證直接漏掉，
    不如用比較弱但至少有機會抓到的舊版比對規則）。

    日期過濾原則跟舊版 `_attach_dates()` 一致：只有「明確抓到日期、且確定不在範圍內」
    才排除；抓不到日期一律保留、標記 date=None，交給人工判斷。
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

                if snippet is not None:
                    # 成功造訪文章本頁：用內容摘要做最終判斷
                    if not _content_keyword_match(c["title"], snippet, keywords):
                        continue
                else:
                    # 造訪失敗／抓不到內容摘要：優雅降級，退回列表頁摘要比對
                    if not any(k in c["title"] or _has_nickname_intro(c["context"], k) for k in keywords):
                        continue

                if d is not None and not (start_date <= d <= end_date):
                    continue
                results.append({"title": c["title"], "url": c["url"], "date": d})

    for c in overflow:
        if not any(k in c["title"] or _has_nickname_intro(c["context"], k) for k in keywords):
            continue
        results.append({"title": c["title"], "url": c["url"], "date": None})

    # 保持原始（候選）順序，而不是 as_completed 的完成順序
    order = {c["url"]: i for i, c in enumerate(candidates)}
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
    candidates = _candidate_filter(links, ["ltn.com.tw/news/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_appledaily(page, keyword, start_date, end_date):
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
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_tvbs(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。
    日期改交給 `_verify_candidates()` 內建的文章頁日期擷取 cascade，比舊版從列表頁
    「3小時前」這類相對時間字串換算更精確（相對時間換算本來就有±1天的誤差風險）。"""
    kw = quote(keyword)
    url = f"https://news.tvbs.com.tw/news/searchresult/{kw}/news"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["tvbs.com.tw/entertainment/", "tvbs.com.tw/life/", "tvbs.com.tw/local/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_chinatimes(page, keyword, start_date, end_date):
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
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_ettoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。
    日期改交給 `_verify_candidates()` 內建的文章頁日期擷取 cascade，取代舊版從列表頁
    附近 <div> 文字用 regex 挖 YYYY-MM-DD 的做法（該做法依賴列表頁版型穩定，文章頁的
    meta tag／JSON-LD 日期更穩定可靠）。"""
    kw = quote(keyword)
    url = f"https://www.ettoday.net/news_search/doSearch.php?keywords={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["star.ettoday.net/news/", "ettoday.net/news/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_udn(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://udn.com/search/word/2/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["stars.udn.com/star/story/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_mirror(page, keyword, start_date, end_date):
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
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_yahoo(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://tw.news.yahoo.com/search?p={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["tw.news.yahoo.com/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_ctwant(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.ctwant.com/search/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["ctwant.com/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_mirrormedia(page, keyword, start_date, end_date):
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
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_mnews(page, keyword, start_date, end_date):
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
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_ctinews(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://ctinews.com/search/{kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["ctinews.com/news/items/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_ftvnews(page, keyword, start_date, end_date):
    """搜尋結果列表頁維持 Playwright：網站有 Cloudflare JS 挑戰頁（"Just a moment..."），
    純 requests 呼叫會被擋下回傳 403，需要真實瀏覽器執行 JS 才能通過驗證並取得搜尋結果。
    個別文章頁若同樣被 Cloudflare 擋下，`_verify_candidates()` 的 `_fetch_article_content_and_date`
    會直接請求失敗（RequestException），此時會自動退回列表頁摘要比對／date=None 保留，
    不會整批漏收，只是這種情況下拿不到內容驗證的準確度優勢。"""
    kw = quote(keyword)
    url = f"https://www.ftvnews.com.tw/search/{kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    links = _all_links(page)
    candidates = _candidate_filter(links, ["ftvnews.com.tw/news/detail/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_owlnews(page, keyword, start_date, end_date):
    """奧丁丁OwlNews報新聞。維持 Playwright：純 requests 取得的 HTML 只有 meta/JSON-LD
    描述文字含關鍵字，實際文章清單由前端 JS 動態載入，需要真實瀏覽器執行 JS 才能取得結果。"""
    kw = quote(keyword)
    url = f"https://news.owlting.com/articles/search/{kw}?locale=zh-TW"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(2500)
    links = _all_links(page)
    candidates = _candidate_filter(links, ["news.owlting.com/articles/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_ftnn(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。
    日期改交給 `_verify_candidates()` 內建的文章頁日期擷取 cascade，取代舊版從標題文字
    regex 挖 YYYY.MM.DD 的做法。"""
    kw = quote(keyword)
    url = f"https://www.ftnn.com.tw/search?keyword={kw}&all=true"
    soup = _get_soup(url, timeout=35)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["ftnn.com.tw/news/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_life(page, keyword, start_date, end_date):
    """Life.tw 台灣生活網。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://life.tw/?app=search&keyword={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["life.tw/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_juksy(page, keyword, start_date, end_date):
    """JUKSY 街星（清單中的 JUSKY_HOT 應為此站）。已改用 requests + BeautifulSoup
    （伺服器端渲染，已驗證非本關鍵字時也能抓到搜尋結果連結）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.juksy.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["juksy.com/article/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_premiermedia(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.premiermedia.com.tw/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["premiermedia.com.tw/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_findnewstoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://findnewstoday.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["findnewstoday.net/archives/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_taiwanpost(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://taiwanpost.net/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["taiwanpost.net/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_mypeople(page, keyword, start_date, end_date):
    """民眾新聞（民眾網）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://mypeoplevol.com/?s={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["mypeoplevol.com/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_ponews(page, keyword, start_date, end_date):
    """博新聞網（Blogger 平台）。已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://www.po-news.net/search?q={kw}"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["po-news.net/20"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_hualientoday(page, keyword, start_date, end_date):
    """已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://hualien-today.com/?s={kw}"
    soup = _get_soup(url, timeout=30)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["hualien-today.com/news.php?listno="])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_insightpost(page, keyword, start_date, end_date):
    """洞見新聞網 → 對應「洞見國際事務評論網」insight-post.tw（清單中僅找到此近似站台）。
    維持 Playwright：整站為前端 JS SPA，純 requests 拿到的 HTML 幾乎沒有內文連結，
    需要真實瀏覽器執行 JS 才能取得頁面內容。"""
    kw = quote(keyword)
    url = f"https://insight-post.tw/?s={kw}"
    page.goto(url, timeout=20000, wait_until="load")
    page.wait_for_timeout(1500)
    links = _all_links(page)
    candidates = _candidate_filter(links, ["insight-post.tw/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_starsetn(page, keyword, start_date, end_date):
    """娛樂星聞 star.setn.com（三立旗下娛樂站，與主站 setn.com 分屬不同網域，主站目前被阻擋）。
    已改用 requests + BeautifulSoup（伺服器端渲染）。page 參數保留但不使用。"""
    kw = quote(keyword)
    url = f"https://star.setn.com/search/{kw}/"
    soup = _get_soup(url)
    links = _all_links_requests(soup, url)
    candidates = _candidate_filter(links, ["star.setn.com/news/"])
    return _verify_candidates(candidates, keyword, start_date, end_date)


def search_googlenews(page, keyword, start_date, end_date):
    """Google 新聞 RSS 搜尋（聚合各媒體報導，不需要 Playwright page）。
    刻意不套用本次新增的「造訪文章本頁驗證」流程（`_verify_candidates()`）：Google News RSS
    回傳的 <link> 不是文章真正的網址，而是 Google 自己的轉址短碼（例如
    news.google.com/rss/articles/CBMi...），要拿到真正的文章網址得先讓瀏覽器執行一段
    client-side redirect，plain requests 直接 GET 這個網址通常只會拿到轉址頁本身，
    抓不到目標文章的內容——這跟其他站台「文章網址本來就是真的，只是列表頁摘要不可靠」
    的情境不一樣，這裡要驗證的話等於要多一層「先解析出真正網址」的機制，複雜度／
    額外請求數不成比例（這個來源本身是聚合上百篇其他站台文章的入口，只是給使用者
    看有沒有露出、真正的完整報導使用者會點進去看，不是本比對系統唯一的正確性防線）。
    維持原本「關鍵字必須出現在標題」的比對（比 `_filter()` 標準略嚴格，因為沒有摘要
    可以做暱稱鄰接比對），跟其他站台一致的地方是：仍然不接受更寬鬆的比對規則。"""
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
