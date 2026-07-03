# 媒體清單設定
# category: "原生媒體" 或 "轉載平台"
# status: "active"（已驗證可用）或 "manual"（尚未破解，介面上會提示手動確認）

SITES = [
    {"key": "ltn", "name": "自由時報", "category": "原生媒體", "status": "active"},
    {"key": "appledaily", "name": "壹蘋新聞網", "category": "原生媒體", "status": "active"},
    {"key": "tvbs", "name": "TVBS", "category": "原生媒體", "status": "active"},
    {"key": "chinatimes", "name": "中國時報", "category": "原生媒體", "status": "active"},
    {"key": "ettoday", "name": "ETtoday", "category": "原生媒體", "status": "active"},
    {"key": "udn", "name": "聯合報／噓星聞", "category": "原生媒體", "status": "active"},
    {"key": "mirror", "name": "鏡報", "category": "原生媒體", "status": "active"},
    {"key": "setn", "name": "三立新聞網", "category": "原生媒體", "status": "manual"},
    {"key": "yam", "name": "蕃薯藤", "category": "原生媒體", "status": "manual"},
    {"key": "cts", "name": "華視", "category": "原生媒體", "status": "manual"},
    {"key": "yahoo", "name": "Yahoo奇摩新聞", "category": "轉載平台", "status": "active"},
    {"key": "pchome", "name": "PChome新聞", "category": "轉載平台", "status": "manual"},
    {"key": "linetoday", "name": "LINE TODAY", "category": "轉載平台", "status": "manual"},

    # --- 2026 新增媒體 ---
    {"key": "ctwant", "name": "CTWANT", "category": "原生媒體", "status": "active"},
    {"key": "mirrormedia", "name": "鏡週刊", "category": "原生媒體", "status": "active"},
    {"key": "mnews", "name": "鏡新聞", "category": "原生媒體", "status": "active"},
    {"key": "ctinews", "name": "中天新聞網", "category": "原生媒體", "status": "active"},
    {"key": "ftvnews", "name": "民視", "category": "原生媒體", "status": "active"},
    {"key": "owlnews", "name": "奧丁丁OwlNews報新聞", "category": "原生媒體", "status": "active"},
    {"key": "ftnn", "name": "FTNN新聞網", "category": "原生媒體", "status": "active"},
    {"key": "life", "name": "Life.tw台灣生活網", "category": "原生媒體", "status": "active"},
    {"key": "juksy", "name": "JUSKY_HOT", "category": "原生媒體", "status": "active"},
    {"key": "premiermedia", "name": "頂尖傳媒", "category": "原生媒體", "status": "active"},
    {"key": "findnewstoday", "name": "找新聞", "category": "原生媒體", "status": "active"},
    {"key": "taiwanpost", "name": "台灣郵報", "category": "原生媒體", "status": "active"},
    {"key": "mypeople", "name": "民眾新聞", "category": "原生媒體", "status": "active"},
    {"key": "ponews", "name": "博新聞網", "category": "原生媒體", "status": "active"},
    {"key": "hualientoday", "name": "今日花蓮", "category": "原生媒體", "status": "active"},
    {"key": "insightpost", "name": "洞見新聞網", "category": "原生媒體", "status": "active"},  # 僅找到近似站台「洞見國際事務評論網」insight-post.tw
    {"key": "starsetn", "name": "娛樂星聞", "category": "原生媒體", "status": "active"},
    {"key": "googlenews", "name": "Google新聞", "category": "轉載平台", "status": "active"},  # RSS 聚合搜尋，非單一原生媒體

    {"key": "videoland", "name": "緯來新聞網", "category": "原生媒體", "status": "manual"},  # React SPA，搜尋結果無法從 DOM 取得文章網址（僅找到內部 JSON API）
    {"key": "taisounds", "name": "太報", "category": "原生媒體", "status": "manual"},  # Next.js SPA，找不到可用搜尋路由
    {"key": "enews", "name": "eNEWS", "category": "原生媒體", "status": "manual"},  # 搜尋頁為未串接完成的樣板頁（顯示假資料）
    {"key": "nchn", "name": "全國大小事新聞網", "category": "原生媒體", "status": "manual"},  # Cloudflare 阻擋 headless 瀏覽器（403）
    {"key": "ecreative", "name": "E創傳媒", "category": "原生媒體", "status": "manual"},  # Cloudflare 阻擋 headless 瀏覽器（403）
    {"key": "bigtimes", "name": "大時事", "category": "原生媒體", "status": "manual"},  # Cloudflare 阻擋 headless 瀏覽器（403）
    {"key": "businessalert", "name": "商訊快報", "category": "原生媒體", "status": "manual"},  # Cloudflare 阻擋 headless 瀏覽器（403）
    {"key": "hitfm", "name": "HitFM", "category": "原生媒體", "status": "manual"},  # 搜尋為 Google 自訂搜尋（CSE），非站內原生搜尋
    {"key": "yololab", "name": "YOLO LAB01", "category": "原生媒體", "status": "manual"},  # 科技媒體觀察評論網站，無站內搜尋功能
    {"key": "wownews", "name": "WoWoNews", "category": "原生媒體", "status": "manual"},  # 官網網域已失效，現況導向 Facebook 粉專
    {"key": "chasestar", "name": "一起追星去", "category": "原生媒體", "status": "manual"},  # 疑似僅有粉專無獨立網站
    {"key": "ioiotimes", "name": "IOIO TIMES", "category": "原生媒體", "status": "manual"},  # 找不到可辨識的官方網站
    {"key": "amm", "name": "亞洲心動娛樂", "category": "原生媒體", "status": "manual"},  # 僅有Facebook粉專與App，無獨立官方網站
    {"key": "taiwankeypoint", "name": "台灣新聞通訊社", "category": "原生媒體", "status": "manual"},  # 僅找到Facebook粉專，無獨立官方網站
    {"key": "yinnews", "name": "飲新聞", "category": "原生媒體", "status": "manual"},  # 找不到可辨識的官方網站
    {"key": "truemii", "name": "觸mii", "category": "原生媒體", "status": "manual"},  # 僅有社群帳號，無獨立官方網站
    {"key": "entcollect", "name": "娛樂新聞大蒐集", "category": "原生媒體", "status": "manual"},  # 找不到獨立官方網站，疑似為社群整理帳號
    {"key": "fourgtv", "name": "四季線上", "category": "原生媒體", "status": "manual"},  # 影音串流平台，無文字新聞搜尋功能
    {"key": "daydaynews", "name": "天天要聞", "category": "原生媒體", "status": "manual"},  # 疑似為中國大陸內容聚合平台，非台灣媒體，且無明顯站內搜尋功能

    # 以下為使用者提供清單中，經查證與既有媒體屬同一網站，故不重複新增：
    # 鏡報新聞網 = mirrordaily.news，與既有 "mirror"（鏡報）相同
    # 蕃新聞 = n.yam.com，與既有 "yam"（蕃薯藤）相同
    # 噓新聞 = stars.udn.com「噓！星聞」，與既有 "udn" 相同
]

ACTIVE_SITES = [s for s in SITES if s["status"] == "active"]
MANUAL_SITES = [s for s in SITES if s["status"] == "manual"]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
