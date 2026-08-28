import os
import re
import hashlib
import requests
import feedparser
from flask import Flask, jsonify

app = Flask(__name__)

# کلیدهای امنیتی (Environment Variables)
RUBIKA_TOKEN = os.environ.get("RUBIKA_TOKEN", "")
CHANNEL_GUID = os.environ.get("CHANNEL_GUID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

NEWS_SOURCES = {
    "کریپتو": "https://cointelegraph.com/rss",
    "فارکس_و_بازار": "https://search.cnbc.com/rs/search/combinedrender?source=cnbcnews&titles=show&pubtime=1450&details=true&select=story&id=10000664",
    "اخبار_جهان": "http://feeds.bbci.co.uk/news/world/rss.xml"
}

def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html)

def is_duplicate(news_id):
    """بررسی تکراری بودن خبر در دیتابیس Redis"""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return False  # اگر رادیس تنظیم نشده بود، پردازش ادامه می‌یابد
        
    url = f"{UPSTASH_REDIS_REST_URL}/get/{news_id}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        res = requests.get(url, headers=headers, timeout=5).json()
        return res.get("result") is not None
    except Exception as e:
        print(f"Redis check error: {e}")
        return False

def save_news_id(news_id):
    """ذخیره شناسه خبر در Redis با انقضای ۷ روزه (604800 ثانیه)"""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
        
    url = f"{UPSTASH_REDIS_REST_URL}/set/{news_id}/1/EX/604800"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        requests.get(url, headers=headers, timeout=5)
    except Exception as e:
        print(f"Redis save error: {e}")

def process_with_gemini(title, summary, category):
    """ترجمه و خلاصه‌سازی با Gemini API"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""
    تو یک سردبیر خبره بازار سرمایه، فارکس و کریپتو برای کانال تلگرام/روبیکا هستی.
    خبر زیر را بخوان، به فارسی روان و جذاب ترجمه و خلاصه‌سازی کن:
    
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

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        res_data = response.json()
        return res_data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        print(f"Gemini Error: {e}")
        return None

def fetch_and_process_news(url, category):
    try:
        feed = feedparser.parse(url)
        if not feed.entries:
            return None

        latest = feed.entries[0]
        raw_title = latest.title
        raw_link = getattr(latest, 'link', raw_title)
        
        # تولید یک شناسه منحصر‌به‌فرد (هش) برای خبر
        news_id = hashlib.md5(raw_link.encode('utf-8')).hexdigest()

        # چك کردن تکراری بودن
        if is_duplicate(news_id):
            print(f"خبر تکراری است و نادیده گرفته شد: {category}")
            return None

        raw_summary = clean_html(getattr(latest, 'summary', ''))[:500]
        ai_post = process_with_gemini(raw_title, raw_summary, category)

        if ai_post:
            save_news_id(news_id)  # ذخیره شناسه پس از تولید موفق خبر
            return ai_post

    except Exception as e:
        print(f"Error fetching {category}: {e}")
    return None

def send_to_rubika(text):
    url = f"https://botapi.rubika.ir/v01/{RUBIKA_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_GUID, "text": text}
    headers = {"Content-Type": "application/json"}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.route('/api/cron', methods=['GET'])
def run_cron():
    results = []
    for category, url in NEWS_SOURCES.items():
        post_text = fetch_and_process_news(url, category)
        if post_text:
            response = send_to_rubika(post_text)
            results.append({"category": category, "response": response})
            
    return jsonify({"status": "success", "new_posts_sent": len(results), "details": results})

if __name__ == '__main__':
    app.run()
