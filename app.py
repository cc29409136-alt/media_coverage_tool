import base64
import os
import re
import subprocess
import streamlit as st
from datetime import date, timedelta
from urllib.parse import quote
from playwright.sync_api import sync_playwright

from config import ACTIVE_SITES, MANUAL_SITES, USER_AGENT
from scrapers import SEARCH_FUNCS, PLAYWRIGHT_REQUIRED_SITES
from matcher import format_report, build_html_report
from docx_export import build_docx_report


def _ensure_chromium_installed():
    """雲端環境（如 Streamlit Community Cloud）沒有 postinstall 機制可以在部署時
    先跑 `playwright install chromium`，所以改成在第一次真的需要瀏覽器時才檢查、
    必要的話才下載——本機開發環境（venv 已手動裝好 chromium）幾乎是瞬間通過檢查。
    只有在搜尋範圍包含 PLAYWRIGHT_REQUIRED_SITES 內的站台時才會呼叫這個函式，
    避免每次啟動 app 都做這個檢查。
    """
    try:
        with sync_playwright() as p:
            exe_path = p.chromium.executable_path
        if exe_path and os.path.exists(exe_path):
            return
    except Exception:
        pass
    subprocess.run(["playwright", "install", "chromium"], check=False)

def _safe_filename(keyword, start_date, end_date, ext):
    """下載檔名避免用中文——中文檔名經過雲端代理（如 Cloudflare Tunnel）時，
    HTTP 標頭裡的檔名常常會被吃掉或轉成亂碼，導致瀏覽器抓不到正確檔名/副檔名。
    關鍵字本身還是會完整寫在文件內容裡，這裡只是檔名改成安全的英數字。
    """
    ascii_keyword = re.sub(r"[^A-Za-z0-9]+", "", keyword).strip("_")
    prefix = f"media_coverage_{ascii_keyword}" if ascii_keyword else "media_coverage"
    return f"{prefix}_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.{ext}"


def _data_uri_download_button(label, data_bytes, file_name, mime, key):
    """st.download_button 在這個環境測試會失敗（檔案送不到 Downloads 資料夾），
    改用把檔案內容直接編碼進 <a> 連結本身（data URI），瀏覽器點擊時完全不需要
    再跟 Streamlit 後端要資料，是完全不同的下載路徑，用來繞開前面那個問題。
    """
    b64 = base64.b64encode(data_bytes).decode()
    href = f"data:{mime};base64,{b64}"
    st.markdown(
        f'<a href="{href}" download="{file_name}" '
        f'style="display:inline-block;width:100%;text-align:center;padding:0.5em 0;'
        f'background-color:#FF6B4A;color:white;border-radius:8px;font-weight:600;'
        f'text-decoration:none;" id="{key}">{label}</a>',
        unsafe_allow_html=True,
    )


st.set_page_config(page_title="媒體露出整理小工具", page_icon="📰", layout="wide")

# 一點點客製化 CSS：讓按鈕與間距更緊湊、更有質感，但不與 Streamlit 框架對抗。
st.markdown(
    """
    <style>
    div.stButton > button, div.stDownloadButton > button {
        border-radius: 8px;
        font-weight: 600;
    }
    div[data-testid="stMetric"] {
        background-color: rgba(255, 255, 255, 0.5);
        border-radius: 10px;
        padding: 10px 14px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📰 媒體露出整理小工具")
st.caption(
    "設定日期區間 ＋ 關鍵字，自動搜尋各大媒體與轉載平台，整理成可直接複製、下載的露出清單。"
)
st.caption("🔧 build-check: 2026-07-05-narrow-scope-v1（如果你沒看到這行，代表 Reboot 沒有真的套用最新程式碼）")

active_original_count = len([s for s in ACTIVE_SITES if s["category"] == "原生媒體"])
active_syndication_count = len([s for s in ACTIVE_SITES if s["category"] == "轉載平台"])

with st.container(border=True):
    m1, m2, m3 = st.columns(3)
    m1.metric("自動搜尋站台", f"{len(ACTIVE_SITES)} 個", help="原生媒體 + 轉載平台，已驗證可自動搜尋")
    m2.metric("原生媒體 / 轉載平台", f"{active_original_count} / {active_syndication_count}")
    m3.metric("待手動確認站台", f"{len(MANUAL_SITES)} 個", help="尚未支援自動搜尋，可點連結手動確認")

with st.container(border=True):
    st.subheader("搜尋設定")
    with st.form("search_form"):
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            start_date = st.date_input("開始日期", value=date.today() - timedelta(days=2))
        with col2:
            end_date = st.date_input("結束日期", value=date.today())
        with col3:
            keyword = st.text_input(
                "關鍵字（藝人／品牌／活動名稱，可用「、」「,」分隔多個別名）",
                placeholder="例如：宋念宇、小宇",
            )

        press_release = st.text_area(
            "新聞稿全文或大綱（選填，貼上後會用來比對文章內容相似度，提高比對準確率）",
            height=120,
            help="貼上新聞稿全文，或只貼幾句話的大綱／關鍵詞也可以，沒有字數限制："
                 "系統會拿候選文章的內容跟你貼的內容比對相似度，用來排除「有提到關鍵字"
                 "但其實是別的新聞」的誤判，並抓出用詞改寫但確實是同一則新聞的報導。"
                 "內容越完整，比對會越準；留空則沿用原本只看關鍵字的比對方式。",
        )
        submitted = st.form_submit_button("🔍 開始搜尋", use_container_width=True, type="primary")

if submitted:
    if not keyword.strip():
        st.error("請輸入關鍵字")
        st.stop()
    if start_date > end_date:
        st.error("開始日期不能晚於結束日期")
        st.stop()

    # 支援多個別名（例如藝人本名 + 藝名）：用「、」「,」「，」或空白分隔，
    # 每個站台會逐一用每個別名搜尋一次再合併去重，只要標題符合其中一個別名就算命中
    # ——很多新聞標題只會寫藝名（如「小宇」）不會寫本名（如「宋念宇」），
    # 單一關鍵字比對會漏掉這些真正相關的報導。
    keywords = [k.strip() for k in re.split(r"[、,，\s]+", keyword) if k.strip()]

    progress = st.progress(0, text="準備搜尋...")
    original_results = {}
    syndication_results = {}

    active_original = [s for s in ACTIVE_SITES if s["category"] == "原生媒體"]
    active_syndication = [s for s in ACTIVE_SITES if s["category"] == "轉載平台"]
    all_active = active_original + active_syndication
    total = len(all_active)

    # 大多數站台（19/26）已改用 requests + BeautifulSoup，不需要瀏覽器。
    # 只有在搜尋範圍內確實包含需要 JS 執行的站台時，才啟動 headless Chromium，
    # 避免在雲端主機上無謂消耗記憶體與 CPU。
    needs_browser = any(site["key"] in PLAYWRIGHT_REQUIRED_SITES for site in all_active)

    def _run_search(page):
        for i, site in enumerate(all_active):
            progress.progress((i) / total, text=f"搜尋中：{site['name']}...")
            func = SEARCH_FUNCS.get(site["key"])
            articles_by_url = {}
            for kw in keywords:
                try:
                    kw_articles = (
                        func(page, kw, start_date, end_date, press_release_text=press_release) if func else []
                    )
                except Exception as e:
                    kw_articles = []
                    st.warning(f"{site['name']}（{kw}）搜尋失敗：{e}")
                for art in kw_articles:
                    articles_by_url[art["url"]] = art
            articles = list(articles_by_url.values())
            if site["category"] == "原生媒體":
                original_results[site["name"]] = articles
            else:
                syndication_results[site["name"]] = articles

    if needs_browser:
        _ensure_chromium_installed()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, locale="zh-TW")
            page = context.new_page()
            _run_search(page)
            page.close()
            context.close()
            browser.close()
    else:
        _run_search(None)

    progress.progress(1.0, text="完成")

    # 存進 session_state，這樣之後點下載按鈕觸發的 rerun 才不會把結果清空
    # （下載按鈕本身也是一種互動，Streamlit 會整個腳本重跑一次；如果結果只存在
    # 區域變數裡，重跑時 submitted 會變回 False，畫面就會整個跳掉）。
    st.session_state["search_results"] = {
        "keyword": keyword,
        "start_date": start_date,
        "end_date": end_date,
        "original_results": original_results,
        "syndication_results": syndication_results,
        "total": total,
    }

if "search_results" in st.session_state:
    sr = st.session_state["search_results"]
    keyword = sr["keyword"]
    start_date = sr["start_date"]
    end_date = sr["end_date"]
    original_results = sr["original_results"]
    syndication_results = sr["syndication_results"]
    total = sr["total"]

    report = format_report(
        kol_name=keyword,
        start_date=start_date,
        end_date=end_date,
        original_results=original_results,
        syndication_results=syndication_results,
        original_articles_for_match=original_results,
    )

    total_found = sum(len(v) for v in original_results.values()) + sum(len(v) for v in syndication_results.values())
    hit_sites = sum(1 for v in list(original_results.values()) + list(syndication_results.values()) if v)

    st.success(f"搜尋完成，共找到 {total_found} 篇報導")

    r1, r2, r3 = st.columns(3)
    r1.metric("找到報導總數", f"{total_found} 篇")
    r2.metric("有露出的站台", f"{hit_sites} / {total} 個")
    r3.metric("搜尋關鍵字", keyword)

    st.divider()
    st.subheader("📌 快速預覽（可點擊連結）")
    for site_name, articles in list(original_results.items()) + list(syndication_results.items()):
        if not articles:
            continue
        with st.expander(f"{site_name}（{len(articles)} 篇）", expanded=False):
            for art in articles:
                st.markdown(f"- [{art['title']}]({art['url']})")

    st.divider()
    st.subheader("📋 整理結果（可直接全選複製）")
    st.text_area("整理結果文字", value=report, height=500, label_visibility="collapsed")

    html_report = build_html_report(
        kol_name=keyword,
        start_date=start_date,
        end_date=end_date,
        original_results=original_results,
        syndication_results=syndication_results,
        original_articles_for_match=original_results,
    )

    docx_bytes = build_docx_report(
        keyword=keyword,
        start_date=start_date,
        end_date=end_date,
        original_results=original_results,
        syndication_results=syndication_results,
        original_articles_for_match=original_results,
    )

    st.divider()
    st.subheader("⬇️ 下載")
    dl1, dl2 = st.columns(2)
    with dl1:
        _data_uri_download_button(
            "📝 下載為 Word 檔",
            data_bytes=docx_bytes,
            file_name=_safe_filename(keyword, start_date, end_date, "docx"),
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            key="dl-docx",
        )
    with dl2:
        _data_uri_download_button(
            "📄 下載為文字檔",
            data_bytes=report.encode("utf-8"),
            file_name=_safe_filename(keyword, start_date, end_date, "txt"),
            mime="application/octet-stream",
            key="dl-txt",
        )

    st.divider()
    st.subheader("🌐 HTML 版本（備用方案）")
    st.caption(
        "如果上面的下載按鈕還是沒反應，這裡改用網頁直接顯示，不需要觸發下載："
        "可以直接用瀏覽器的 **⌘P 列印 → 儲存為 PDF** 保存成 PDF 檔，"
        "或展開下方「原始 HTML 原始碼」全選複製，貼到記事本存成 .html 檔。"
    )
    st.components.v1.html(html_report, height=500, scrolling=True)
    with st.expander("查看原始 HTML 原始碼（可全選複製）"):
        st.code(html_report, language="html")

    with st.expander("⚠️ 以下媒體尚未支援自動搜尋，需要手動確認"):
        for site in MANUAL_SITES:
            manual_url = {
                "nchn": f"https://nchn.news/?s={quote(keyword)}",
                "ecreative": f"https://e-creative.media/?s={quote(keyword)}",
                "bigtimes": f"https://bigtimes.net/?s={quote(keyword)}",
                "businessalert": f"https://businessalert.today/?s={quote(keyword)}",
                "hitfm": f"https://www.google.com/search?q=site:hitoradio.com+{quote(keyword)}",
                "yololab": "https://yololab.net/",
                "wownews": "https://www.facebook.com/wownews.tw/",
                "chasestar": f"https://www.google.com/search?q=一起追星去+{quote(keyword)}",
                "ioiotimes": f"https://www.google.com/search?q=IOIO+TIMES+{quote(keyword)}",
                "amm": "https://www.facebook.com/ammtv.tw/",
                "taiwankeypoint": "https://www.facebook.com/taiwankeypoint/",
                "yinnews": f"https://www.google.com/search?q=飲新聞+{quote(keyword)}",
                "truemii": "https://www.facebook.com/truemii2020/",
                "entcollect": f"https://www.google.com/search?q=娛樂新聞大蒐集+{quote(keyword)}",
                "fourgtv": "https://www.4gtv.tv/",
                "daydaynews": f"https://daydaynews.cc/zh-tw/search?q={quote(keyword)}",
            }.get(site["key"], "#")
            st.markdown(f"- **{site['name']}**（{site['category']}）— [點此手動搜尋]({manual_url})")
