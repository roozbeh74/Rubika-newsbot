import hashlib
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------
RUBIKA_TOKEN = os.getenv("RUBIKA_TOKEN", "").strip()
CHANNEL_GUID = os.getenv("CHANNEL_GUID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()
MAX_ARTICLES_PER_RUN = max(1, min(int(os.getenv("MAX_ARTICLES_PER_RUN", "10")), 20))
MAX_WORKERS = max(1, min(int(os.getenv("MAX_WORKERS", "5")), 10))
REQUEST_TIMEOUT = max(5, min(int(os.getenv("REQUEST_TIMEOUT", "12")), 30))

# قابل تنظیم از طریق NEWS_SOURCES_JSON بدون تغییر کد.
DEFAULT_SOURCES = {
    "کریپتو": [
        {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
        {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    ],
    "اقتصاد_جهان": [
        {"name": "CNBC", "url": "https://search.cnbc.com/rs/search/combinedrender?source=cnbcnews&titles=show&pubtime=1450&details=true&select=story&id=100003114"},
        {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    ],
    "ایران": [
        {"name": "Google News ایران", "url": "https://news.google.com/rss/search?q=%D8%A7%DB%8C%D8%B1%D8%A7%D9%86&hl=fa&gl=IR&ceid=IR:fa"},
        {"name": "Google News اقتصاد ایران", "url": "https://news.google.com/rss/search?q=%D8%A7%D9%82%D8%AA%D8%B5%D8%A7%D8%AF+%D8%A7%DB%8C%D8%B1%D8%A7%D9%86&hl=fa&gl=IR&ceid=IR:fa"},
    ],
    "سیاست_جهان": [
        {"name": "Google News جهان", "url": "https://news.google.com/rss/search?q=%D8%B3%DB%8C%D8%A7%D8%B3%D8%AA+%D8%AC%D9%87%D8%A7%D9%86&hl=fa&gl=IR&ceid=IR:fa"},
    ],
}

try:
    NEWS_SOURCES = json.loads(os.getenv("NEWS_SOURCES_JSON", "")) or DEFAULT_SOURCES
except json.JSONDecodeError:
    NEWS_SOURCES = DEFAULT_SOURCES

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def redis_command(command: str, *args):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        encoded = "/".join(quote(str(a), safe="") for a in args)
        url = f"{UPSTASH_REDIS_REST_URL}/{quote(command, safe='')}/{encoded}"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=5,
        )
        response.raise_for_status()
        return response.json().get("result")
    except Exception as exc:
        print(f"Redis error: {type(exc).__name__}: {exc}")
        return None


def is_duplicate(news_id: str) -> bool:
    return redis_command("get", news_id) is not None


def save_news_id(news_id: str, ttl: int = 604800) -> None:
    redis_command("set", news_id, "1", "EX", ttl)


def make_news_id(link: str, title: str) -> str:
    return hashlib.sha256(f"{link}|{title}".encode("utf-8")).hexdigest()


def fetch_feed(source: dict, category: str):
    try:
        feed = feedparser.parse(source["url"])
        entries = []
        for entry in feed.entries[:5]:
            title = strip_html(getattr(entry, "title", ""))
            link = getattr(entry, "link", "") or title
            summary = strip_html(getattr(entry, "summary", ""))[:1800]
            if not title:
                continue
            news_id = make_news_id(link, title)
            if is_duplicate(news_id):
                continue
            entries.append({
                "id": news_id,
                "category": category,
                "source": source["name"],
                "title": title,
                "summary": summary,
                "link": link,
            })
        return entries
    except Exception as exc:
        print(f"Feed error [{source.get('name')}]: {type(exc).__name__}: {exc}")
        return []


def select_articles():
    candidates = []
    futures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for category, sources in NEWS_SOURCES.items():
            for source in sources:
                futures.append(pool.submit(fetch_feed, source, category))
        for future in as_completed(futures):
            candidates.extend(future.result())

    # جدیدترین/اولین آیتم‌های هر فید در اولویت‌اند؛ برای جلوگیری از بمباران کانال سقف داریم.
    unique = {}
    for item in candidates:
        unique.setdefault(item["id"], item)
    return list(unique.values())[:MAX_ARTICLES_PER_RUN]


# -----------------------------------------------------------------------------
# Gemini
# -----------------------------------------------------------------------------
def process_with_gemini(article: dict):
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEY missing"

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    prompt = f"""
تو سردبیر حرفه‌ای اخبار فارسی برای یک کانال روبیکا هستی.
خبر را فقط بر اساس اطلاعات داده‌شده بازنویسی کن و چیزی از خودت اضافه نکن.
خبر را به فارسی روان، کوتاه، دقیق و قابل انتشار تبدیل کن.

دسته: {article['category']}
منبع: {article['source']}
عنوان: {article['title']}
متن: {article['summary']}

خروجی دقیقاً شامل این بخش‌ها باشد:
🔴 تیتر: یک تیتر خبری جذاب و غیراغراق‌آمیز

📰 خلاصه:
۲ تا ۴ جمله فارسی

📌 نکات مهم:
• نکته اول
• نکته دوم

📊 اهمیت: یکی از این سه مقدار: کم / متوسط / مهم

🔗 منبع: {article['source']}
{('📣 ' + CHANNEL_USERNAME) if CHANNEL_USERNAME else ''}
""".strip()

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
    }
    try:
        response = requests.post(
            f"{endpoint}?key={quote(GEMINI_API_KEY, safe='')}",
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text, None
    except Exception as exc:
        print(f"Gemini error: {type(exc).__name__}: {exc}")
        return None, str(exc)


# -----------------------------------------------------------------------------
# Rubika
# -----------------------------------------------------------------------------
def send_to_rubika(text: str):
    if not RUBIKA_TOKEN or not CHANNEL_GUID:
        return {"ok": False, "error": "RUBIKA_TOKEN or CHANNEL_GUID missing"}
    url = f"https://botapi.rubika.ir/v01/{RUBIKA_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": CHANNEL_GUID, "text": text},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return {"ok": True, "response": response.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# -----------------------------------------------------------------------------
# Pipeline
# -----------------------------------------------------------------------------
def run_pipeline():
    started = datetime.now(timezone.utc).isoformat()
    articles = select_articles()
    results = []

    for article in articles:
        ai_post, ai_error = process_with_gemini(article)
        if not ai_post:
            results.append({"source": article["source"], "status": "ai_error", "error": ai_error})
            continue

        delivery = send_to_rubika(ai_post)
        if delivery.get("ok"):
            save_news_id(article["id"])
            results.append({
                "source": article["source"],
                "category": article["category"],
                "title": article["title"],
                "status": "sent",
            })
        else:
            results.append({
                "source": article["source"],
                "category": article["category"],
                "title": article["title"],
                "status": "rubika_error",
                "error": delivery.get("error"),
            })

    sent = sum(1 for item in results if item["status"] == "sent")
    return {
        "status": "success",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "candidates": len(articles),
        "sent": sent,
        "results": results,
    }


# -----------------------------------------------------------------------------
# Dashboard / API
# -----------------------------------------------------------------------------
DASHBOARD = """
<!doctype html><html lang='fa' dir='rtl'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Rubika News Bot</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#0b1020;color:#eef2ff;margin:0}
.wrap{max-width:1100px;margin:auto;padding:28px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:24px}
.card{background:#141b31;border:1px solid #263152;border-radius:18px;padding:20px;margin:12px 0;box-shadow:0 10px 35px #0003}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.metric{font-size:28px;font-weight:800}.muted{color:#aab4d0}.ok{color:#71e1a1}.bad{color:#ff7b8a}
button{background:#6d7cff;color:#fff;border:0;border-radius:12px;padding:12px 18px;font-weight:700;cursor:pointer}button:disabled{opacity:.5}
code{background:#0d1326;padding:3px 7px;border-radius:7px}.item{padding:12px 0;border-bottom:1px solid #263152}.item:last-child{border-bottom:0}
</style></head><body><div class='wrap'>
<div class='hero'><div><h1>🤖 Rubika News Bot</h1><div class='muted'>مانیتورینگ اخبار، Gemini و ارسال خودکار به روبیکا</div></div>
<button id='run'>اجرای دستی</button></div>
<div class='grid'>
<div class='card'><div class='muted'>وضعیت</div><div class='metric {{status_class}}'>{{status}}</div></div>
<div class='card'><div class='muted'>دسته‌ها</div><div class='metric'>{{categories}}</div></div>
<div class='card'><div class='muted'>منابع</div><div class='metric'>{{sources}}</div></div>
<div class='card'><div class='muted'>Cron</div><div class='metric'>هر ۱ دقیقه</div></div></div>
<div class='card'><h2>🔐 اتصال‌ها</h2><div>Gemini: <b class='{{gemini_class}}'>{{gemini}}</b></div><div>Rubika: <b class='{{rubika_class}}'>{{rubika}}</b></div><div>Redis: <b class='{{redis_class}}'>{{redis}}</b></div></div>
<div class='card'><h2>🧩 ساختار خبر</h2><div class='muted'>{{source_summary}}</div></div>
<div class='card'><h2>⚡ عملیات</h2><div id='out' class='muted'>برای اجرای فوری روی «اجرای دستی» بزن.</div></div>
</div><script>
document.getElementById('run').onclick=async()=>{const b=document.getElementById('run');const o=document.getElementById('out');b.disabled=true;o.textContent='در حال اجرا...';try{const r=await fetch('/api/run',{method:'POST'});const j=await r.json();o.textContent=JSON.stringify(j,null,2);}catch(e){o.textContent=e.toString()}finally{b.disabled=false}};
</script></body></html>
"""


@app.get("/")
def dashboard():
    source_count = sum(len(v) for v in NEWS_SOURCES.values())
    return render_template_string(
        DASHBOARD,
        status="آماده" if GEMINI_API_KEY and RUBIKA_TOKEN and CHANNEL_GUID else "نیازمند تنظیم",
        status_class="ok" if GEMINI_API_KEY and RUBIKA_TOKEN and CHANNEL_GUID else "bad",
        categories=len(NEWS_SOURCES),
        sources=source_count,
        gemini="متصل" if GEMINI_API_KEY else "تنظیم نشده",
        gemini_class="ok" if GEMINI_API_KEY else "bad",
        rubika="متصل" if RUBIKA_TOKEN and CHANNEL_GUID else "تنظیم نشده",
        rubika_class="ok" if RUBIKA_TOKEN and CHANNEL_GUID else "bad",
        redis="متصل" if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN else "اختیاری / تنظیم نشده",
        redis_class="ok" if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN else "muted",
        source_summary="، ".join(f"{k}: {len(v)} منبع" for k, v in NEWS_SOURCES.items()),
    )


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "rubika-newsbot",
        "gemini_configured": bool(GEMINI_API_KEY),
        "rubika_configured": bool(RUBIKA_TOKEN and CHANNEL_GUID),
        "redis_configured": bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/cron", methods=["GET", "POST"])
def cron():
    return jsonify(run_pipeline())


@app.post("/api/run")
def manual_run():
    return jsonify(run_pipeline())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
