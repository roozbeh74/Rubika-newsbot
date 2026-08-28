import hashlib
import os
import re

import feedparser
import requests
from flask import Flask, jsonify

app = Flask(__name__)

RUBIKA_TOKEN = os.environ.get("RUBIKA_TOKEN", "")
CHANNEL_GUID = os.environ.get("CHANNEL_GUID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

NEWS_SOURCES = {
    "کریپتو": "https://cointelegraph.com/rss",
    "فارکس_و_بازار": "https://search.cnbc.com/rs/search/combinedrender?source=cnbcnews&titles=show&pubtime=1450&details=true&select=story&id=10000664",
    "اخبار_جهان": "https://feeds.bbci.co.uk/news/world/rss.xml",
}


def clean_html(raw_html):
    return re.sub(r"<.*?>", "", raw_html or "")


def redis_request(path):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        response = requests.get(
            f"{UPSTASH_REDIS_REST_URL}/{path}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        print(f"Redis error: {exc}")
        return None


def is_duplicate(news_id):
    result = redis_request(f"get/{news_id}")
    return bool(result and result.get("result") is not None)


def save_news_id(news_id):
    redis_request(f"set/{news_id}/1/EX/604800")


def process_with_gemini(title, summary, category):
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is not configured")
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = f"""
تو یک سردبیر خبره بازار سرمایه، فارکس و کریپتو برای کانال تلگرام/روبیکا هستی.
خبر زیر را به فارسی روان و جذاب ترجمه و خلاصه کن.

عنوان: {title}
متن: {summary}
دسته: {category}

خروجی دقیقاً در این قالب باشد:

🔔 **[عنوان جذاب فارسی]**

📌 **خلاصه خبر:**
• [نکته مهم اول]
• [نکته مهم دوم]

💡 **تأثیر بر بازار:**
[یک جمله تحلیل کوتاه درباره اثر خبر]

🆔 @YourChannelID
___________________
#{category} #اخبار_فوری #کریپتو #فارکس #اقتصاد
"""
    try:
        response = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (requests.RequestException, KeyError, IndexError, TypeError) as exc:
        print(f"Gemini error: {exc}")
        return None


def fetch_and_process_news(url, category):
    try:
        feed = feedparser.parse(url)
        if not feed.entries:
            return None

        latest = feed.entries[0]
        raw_title = getattr(latest, "title", "").strip()
        raw_link = getattr(latest, "link", raw_title).strip()
        if not raw_title:
            return None

        news_id = hashlib.md5(raw_link.encode("utf-8")).hexdigest()
        if is_duplicate(news_id):
            print(f"Duplicate news ignored: {category}")
            return None

        raw_summary = clean_html(getattr(latest, "summary", ""))[:1000]
        ai_post = process_with_gemini(raw_title, raw_summary, category)
        if ai_post:
            save_news_id(news_id)
            return ai_post
    except Exception as exc:
        print(f"News fetch error [{category}]: {exc}")
    return None


def send_to_rubika(text):
    if not RUBIKA_TOKEN or not CHANNEL_GUID:
        return {"status": "error", "message": "Rubika environment variables are missing"}

    url = f"https://botapi.rubika.ir/v01/{RUBIKA_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": CHANNEL_GUID, "text": text},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "rubika-newsbot"})


@app.get("/api/cron")
def run_cron():
    results = []
    for category, url in NEWS_SOURCES.items():
        post_text = fetch_and_process_news(url, category)
        if post_text:
            results.append({"category": category, "response": send_to_rubika(post_text)})

    return jsonify({"status": "success", "new_posts_sent": len(results), "details": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
