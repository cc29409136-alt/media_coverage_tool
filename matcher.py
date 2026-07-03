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
