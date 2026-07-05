import html
import re
from datetime import date, timedelta, datetime
from difflib import SequenceMatcher


def parse_relative_or_absolute(text, today):
    """把「1天前」「23小時前」「2026-07-02 15:30」「2026/06/23」等文字轉成 date"""
    if not text:
        return None
    text = text.strip()

    m = re.search(r'(\d+)\s*分鐘前', text)
    if m:
        return today
    m = re.search(r'(\d+)\s*小時前', text)
    if m:
        hours = int(m.group(1))
        return today if hours < 24 else today - timedelta(days=hours // 24)
    m = re.search(r'(\d+)\s*天前', text)
    if m:
        return today - timedelta(days=int(m.group(1)))

    m = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_date_from_url(url):
    """從網址擷取 YYYYMMDD 格式的日期片段，例如 .../20260701/xxx 或 .../20260701002352-260404"""
    m = re.search(r'/(20\d{6})', url)
    if not m:
        m = re.search(r'(20\d{6})\d{0,6}-\d+', url)
    if m:
        s = m.group(1)
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def in_range(d, start_date, end_date):
    if d is None:
        return True  # 抓不到日期時不主動排除，交給人工判斷
    return start_date <= d <= end_date


def title_similarity(a, b):
    a = re.sub(r'[\s「」！?？、,，.。/／|｜:：\-—]', '', a)
    b = re.sub(r'[\s「」！?？、,，.。/／|｜:：\-—]', '', b)
    return SequenceMatcher(None, a, b).ratio()


# 新聞稿全文比對用的最小長度門檻：原本設 30 字是為了避免把「隨手貼的測試文字」
# 誤當成新聞稿，但使用者實際使用時常常只想貼「大綱」（幾個關鍵詞、一兩句話），
# 不想被字數卡住不能用，所以改成不設字數限制——只要欄位不是空白，就當作真的
# 有提供參考資料，會拿去跟候選文章做相似度比對。內容越短，比對到的相似度分數
# 自然也會越不穩定/越低（`press_release_similarity()` 用整段 SequenceMatcher，
# 參考文字太短時比對力道有限），但至少不會被字數門檻直接擋在門外。
PRESS_RELEASE_MIN_LEN = 1

# 新聞稿相似度分數門檻：只用來「加分」，不用來否決已經通過關鍵字/暱稱鄰接比對的候選
# （2026-07-05 移除了原本的「佐證門檻」——實測發現同一事件不同記者改寫用詞差異大，
# 分數常常低於門檻，導致真正的報導被反否決掉，見 scrapers._final_match_decision()
# 的說明）。這個門檻只用在「關鍵字比對沒命中，但內容跟新聞稿高度相似」的情況，
# 讓文章大幅改寫、暱稱鄰接句型比對不到的候選還有機會被額外納入。
PRESS_RELEASE_HIGH_THRESHOLD = 0.20

_PR_STRIP_RE = re.compile(r'[\s「」！?？、,，.。/／|｜:：\-—()（）\d]')


def press_release_similarity(press_release_text, article_content):
    """計算使用者貼上的新聞稿全文跟候選文章內容（`_extract_content_snippet()` 抓到的
    og:description／內文前幾段摘要，複用同一份 fetch，不重新抓取）之間的相似度。

    比對方式：整段文字（去除標點符號／數字／空白後）直接丟給 `SequenceMatcher.ratio()`，
    跟既有 `title_similarity()` 手法一致（本專案已有先例：`title_similarity` 用在轉載
    來源比對）。曾實測比較過「n-gram shingle 重疊率」（將兩段文字切成固定長度的
    重疊字元窗口，計算文章端窗口有多少比例也出現在新聞稿中）——結果 shingle 重疊率
    在中文語境下太脆弱：新聞稿跟報導幾乎不會有連續 6~12 個字完全相同的片段（記者一定會
    改寫用詞語序），導致真正相關的文章也幾乎測不到重疊（實測 n=8~12 時真陽性分數全部
    掉到 0），反而 `SequenceMatcher.ratio()` 的 opcode 比對機制本身就能找出「不連續但
    大致對齊」的相似片段，對「文章是新聞稿的短版改寫」這種情境更穩健。

    實測數據（見開發時的腳本，使用今天測試過的真實案例）：
    - 真陽性（同一則「我還有個夢」MV／陳漢典好孕棉哏報導的改寫版）：ratio 約 0.26～0.36
    - 假陽性（「為你唱情歌」「原子少年」等文章，宋念宇只是掛名導師，內容其實是另一則新聞）：
      ratio 約 0.07～0.14（含刻意構造的「較難」假陽性案例，內容更長、也提及一些新聞稿
      相關字詞如「五年」「新作品」，ratio 仍只有 0.096）
    因此以 0.20 作為「高信心」門檻、0.12 作為「佐證」門檻，兩者之間有足夠安全邊界。
    """
    if not press_release_text or not article_content:
        return 0.0
    a = _PR_STRIP_RE.sub('', press_release_text)
    b = _PR_STRIP_RE.sub('', article_content)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


_SHARED_TERM_MIN_LEN = 2

# 演藝新聞常見的泛用詞彙，長度剛好 2 個字時不當作「同一事件」的判斷依據（見下方
# `press_release_shares_topic` 說明）：這些詞幾乎任何一篇該藝人的報導都可能出現，
# 沒有辨識力，純粹是新聞稿跟文章都用了同一個常見詞彙的巧合，不代表兩篇真的在講
# 同一件事。清單不求窮舉，實測發現遺漏就繼續擴充。
_GENERIC_TERMS_STOPLIST = {
    "專輯", "演出", "報導", "記者", "粉絲", "網友", "表示", "透露", "現場", "活動",
    "公司", "經紀", "音樂", "歌手", "藝人", "消息", "內容", "工作", "社群", "發文",
    "生活", "近況", "心情", "感動", "開心", "難過", "記得", "知道", "決定", "選擇",
    "機會", "努力", "堅持", "夢想", "未來", "過去", "現在", "當時", "之後", "之前",
    "後來", "最近", "最新", "全新", "正式", "官方", "宣布", "公開", "獨家", "單曲",
    "專訪", "演唱", "巡演", "開唱", "造型", "打扮", "穿搭", "身材", "外表", "身高",
    "體重", "年紀", "生日", "老婆", "老公", "工作室", "事務所", "笑說", "坦言",
    "直呼", "強調", "指出", "澄清", "否認", "證實", "曝光", "爆料", "回應", "對此",
}


def press_release_shares_topic(press_release_text, article_content, exclude_terms=()):
    """檢查新聞稿內容跟候選文章內容之間，除了搜尋關鍵字本身以外，還有沒有共享
    至少一段長度 >= 2 個字的具體詞語／片語，用來判斷「這篇文章是不是在講同一個
    事件/活動」（而不只是「同一個人」）。

    2026-07-05 新增（使用者要求「窄範圍：只要同一事件/活動的報導」）。跟
    `press_release_similarity()` 的整體相似度分數不同：那個分數容易被「同一個人
    但完全不同話題」的雜訊拉到跟「同一事件、記者改寫用詞」差不多低的區間，沒辦法
    用單一門檻切開兩者（實測「蕭敬騰誓詞太洗腦」——同一時期但完全不同話題的舊聞
    重新翻紅——分數落點跟真正相關的花蓮巡演報導幾乎沒有差異）。改用
    `SequenceMatcher.get_matching_blocks()` 找出雙方「連續相同」的具體片段（不是
    整體比例），只要有任何一段跟關鍵字本身無關的共同片段（例如地名「花蓮」、
    專輯名稱、活動名稱），就有很強的證據代表這是同一件事——反之，如果除了藝人
    本名之外完全沒有任何共同片段，很可能只是剛好在相近時間點報導同一個人的
    「另一件事」（例如舊聞翻紅、或報導這個人完全不相關的日常近況），依照使用者
    「窄範圍」的要求應該排除。

    `exclude_terms`：搜尋關鍵字本身（可能有多個別名），任何完全等於其中一個關鍵字
    的共同片段不計入判斷依據——這只代表雙方都提到這個人的名字，本來就一定會出現，
    沒有辨識力；真正有意義的是「除了名字以外還有沒有共同片段」。

    另外也排除 `_GENERIC_TERMS_STOPLIST`（見下方）這類演藝新聞常見的泛用詞彙
    （例如「專輯」「演出」「經紀人」）——實測發現「宋念宇」2022 年一篇完全無關的
    舊人物專訪，剛好跟新聞稿都提到「專輯」兩個字，若不排除會被誤判成同一事件。
    這類泛用詞彙本身沒有辨識力（幾乎任何一篇該藝人的報導都可能提到），跟「花蓮」
    這種真正有辨識力的地名/專有名詞不同，只有長度 2 個字的共同片段才需要這層
    泛用詞過濾（長度 >= 3 的片段已經夠具體、巧合機率低，不套用這層過濾）。
    """
    if not press_release_text or not article_content:
        return False
    a = _PR_STRIP_RE.sub('', press_release_text)
    b = _PR_STRIP_RE.sub('', article_content)
    if not a or not b:
        return False
    sm = SequenceMatcher(None, a, b)
    for block in sm.get_matching_blocks():
        if block.size < _SHARED_TERM_MIN_LEN:
            continue
        segment = a[block.a: block.a + block.size]
        if segment in exclude_terms:
            continue
        if block.size == _SHARED_TERM_MIN_LEN and segment in _GENERIC_TERMS_STOPLIST:
            continue
        return True
    return False


def find_origin_match(title, original_articles, threshold=0.55):
    """在已抓到的原生媒體結果中，找出跟這篇轉載標題最相似的來源，回傳媒體名稱或 None"""
    best_score, best_site = 0, None
    for site_name, articles in original_articles.items():
        for art in articles:
            score = title_similarity(title, art["title"])
            if score > best_score:
                best_score, best_site = score, site_name
    return best_site if best_score >= threshold else None


def format_report(kol_name, start_date, end_date, original_results, syndication_results, original_articles_for_match):
    lines = []
    date_str = f"{start_date.strftime('%Y/%m/%d')}－{end_date.strftime('%Y/%m/%d')}"
    lines.append(f"{kol_name}｜{date_str} 媒體露出整理\n")

    counter = 1
    for site_name, articles in original_results.items():
        if not articles:
            continue
        lines.append(f"#{site_name}")
        for art in articles:
            lines.append(f"{counter}.{art['title']}")
            lines.append(art["url"])
            counter += 1
        lines.append("---")

    for platform_name, articles in syndication_results.items():
        if not articles:
            continue
        lines.append(f"# {platform_name} 轉載")
        for art in articles:
            origin = find_origin_match(art["title"], original_articles_for_match)
            label = f"{platform_name}／{origin}" if origin else platform_name
            lines.append(f"{counter}. {label}")
            lines.append(art["title"])
            lines.append(art["url"])
            lines.append("")
            counter += 1

    return "\n".join(lines)


def build_html_report(kol_name, start_date, end_date, original_results, syndication_results, original_articles_for_match):
    """輸出完整獨立的 HTML 頁面字串，讓瀏覽器可以直接用「另存新檔」／「列印成 PDF」
    這類原生功能保存，不需要經過 JS 觸發下載（那個機制在某些環境會失敗）。"""
    date_str = f"{start_date.strftime('%Y/%m/%d')}－{end_date.strftime('%Y/%m/%d')}"
    esc = html.escape

    sections_html = []
    counter = 1
    for site_name, articles in original_results.items():
        if not articles:
            continue
        items = []
        for art in articles:
            items.append(
                f'<li><span class="idx">{counter}.</span> '
                f'<a href="{esc(art["url"])}" target="_blank" rel="noopener">{esc(art["title"])}</a></li>'
            )
            counter += 1
        sections_html.append(f'<h2>{esc(site_name)}</h2><ul>{"".join(items)}</ul>')

    for platform_name, articles in syndication_results.items():
        if not articles:
            continue
        items = []
        for art in articles:
            origin = find_origin_match(art["title"], original_articles_for_match)
            label = f"{platform_name}／{origin}" if origin else platform_name
            items.append(
                f'<li><span class="idx">{counter}.</span> <span class="origin-label">{esc(label)}</span><br>'
                f'<a href="{esc(art["url"])}" target="_blank" rel="noopener">{esc(art["title"])}</a></li>'
            )
            counter += 1
        sections_html.append(f'<h2>{esc(platform_name)}（轉載）</h2><ul>{"".join(items)}</ul>')

    body = "".join(sections_html) or "<p>沒有找到符合條件的報導。</p>"

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{esc(kol_name)} 媒體露出整理 {esc(date_str)}</title>
<style>
  body {{ font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; color: #2B2523; line-height: 1.6; }}
  h1 {{ color: #FF6B4A; border-bottom: 3px solid #FF6B4A; padding-bottom: 10px; }}
  h2 {{ color: #2B2523; background: #F6F3EF; padding: 8px 12px; border-radius: 6px; margin-top: 28px; }}
  ul {{ list-style: none; padding-left: 0; }}
  li {{ padding: 10px 0; border-bottom: 1px solid #eee; }}
  .idx {{ color: #999; margin-right: 4px; }}
  .origin-label {{ font-size: 0.85em; color: #FF6B4A; font-weight: 600; }}
  a {{ color: #1a5fb4; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  @media print {{ body {{ margin: 0; }} a {{ color: #000; }} }}
</style>
</head>
<body>
<h1>{esc(kol_name)}｜{esc(date_str)} 媒體露出整理</h1>
{body}
</body>
</html>"""
