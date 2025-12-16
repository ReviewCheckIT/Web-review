import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
import pytz
import requests # টেলিগ্রাম গ্রুপ মেসেজের জন্য
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)
from google_play_scraper import Sort, reviews as play_reviews
from flask import Flask
# Gemini AI এর জন্য
import google.generativeai as genai 

# ==========================================
# 1. কনফিগারেশন এবং সেটআপ
# ==========================================

# লগিং সেটআপ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# এনভায়রনমেন্ট ভেরিয়েবল লোড
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID") # আপনার টেলিগ্রাম আইডি (সংখ্যা)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") # গ্রুপ/চ্যানেলের আইডি
FIREBASE_JSON = os.environ.get("FIREBASE_CREDENTIALS") # ফায়ারবেস JSON কন্টেন্ট
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
PORT = int(os.environ.get("PORT", 8080))

# জেমিনি কনফিগারেশন
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    print("FATAL ERROR: GEMINI_API_KEY is missing.")

# ফায়ারবেস ইনিশিয়ালাইজেশন
if not firebase_admin._apps:
    try:
        cred_dict = json.loads(FIREBASE_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Connected Successfully!")
    except Exception as e:
        print(f"❌ Firebase Connection Failed: {e}")
        exit(1)

db = firestore.client()

# ==========================================
# 2. গ্লোবাল কনফিগারেশন এবং কনভারসেশন স্টেজ
# ==========================================

# ডিফল্ট কনফিগারেশন
DEFAULT_CONFIG = {
    "task_price": 20.0,       # প্রতি টাস্কের দাম
    "referral_bonus": 5.0,    # রেফার বোনাস
    "min_withdraw": 50.0,     # সর্বনিম্ন উইথড্র
    "monitored_apps": []    # অ্যাপের লিস্ট
}

# কনভারসেশন স্টেজ
(
    TASK_NAME, TASK_EMAIL, TASK_DEVICE, TASK_SS,
    ADMIN_ADD_APP_ID, ADMIN_ADD_APP_NAME,
    ADMIN_ADD_USER, ADMIN_ADD_MONEY_ID, ADMIN_ADD_MONEY_AMOUNT
) = range(9)

# ==========================================
# 3. ডাটাবেস এবং ইউটিলিটি ফাংশন
# ==========================================

def get_config():
    """কনফিগারেশন লোড করা"""
    ref = db.collection('settings').document('main_config')
    doc = ref.get()
    if doc.exists:
        return doc.to_dict()
    else:
        ref.set(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

def is_admin(user_id):
    """ইউজার এডমিন কি না চেক করা"""
    user = db.collection('users').document(str(user_id)).get()
    return user.exists and user.to_dict().get('is_admin', False)

def get_user(user_id):
    """ইউজার ডাটা আনা"""
    doc = db.collection('users').document(str(user_id)).get()
    if doc.exists:
        return doc.to_dict()
    return None

def create_user(user_id, first_name, referrer_id=None):
    """নতুন ইউজার তৈরি করা"""
    if not get_user(user_id):
        user_data = {
            "id": user_id,
            "name": first_name,
            "balance": 0.0,
            "total_tasks": 0,
            "joined_at": datetime.now(),
            "referrer": referrer_id if referrer_id and referrer_id.isdigit() else None,
            "is_blocked": False,
            "is_admin": str(user_id) == str(OWNER_ID)
        }
        db.collection('users').document(str(user_id)).set(user_data)
        return True
    return False

def send_telegram_message(message, chat_id=TELEGRAM_CHAT_ID):
    """টেলিগ্রামে মেসেজ পাঠানোর ফাংশন (গ্রুপ অ্যালার্টের জন্য)"""
    if not chat_id:
        print("❌ Cannot send message: TELEGRAM_CHAT_ID is missing.")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            print(f"✅ টেলিগ্রামে অ্যালার্ট পাঠানো হয়েছে। Chat ID: {chat_id}")
            return
        print(f"❌ টেলিগ্রামে মেসেজ পাঠাতে ব্যর্থ। HTTP কোড: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"❌ মেসেজ পাঠাতে সমস্যা (নেটওয়ার্ক এরর): {e}")

def get_ai_summary(text, rating):
    """রিভিউ ছোট করে জেমিনি দিয়ে সামারি করা"""
    if not GEMINI_API_KEY:
        return "AI বিশ্লেষণ বন্ধ (API Key নেই)।"
    try:
        prompt = (
            f"Review: '{text}' (Rating: {rating}/5)\n"
            "Read this review and summarize the main point and user sentiment in 5-6 words, in Bengali only. If the sentiment is positive, start with 'খুশি' (Happy). If negative, start with 'অখুশি' (Unhappy)."
        )
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"⚠️ জেমিনি বিশ্লেষণ এরর: {e}")
        return "বিশ্লেষণ করা যায়নি।"

# ==========================================
# 4. বট হ্যান্ডলার (ইউজার সাইড)
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer = args[0] if args and args[0].isdigit() else None
    
    # ইসলামিক গ্রিটিং
    welcome_msg = f"আসসালামু আলাইকুম, {user.first_name}! 🌙\nআমাদের অ্যাপ রিভিউ আর্নিং বটে আপনাকে স্বাগতম।"
    
    create_user(user.id, user.first_name, referrer)
    
    # মেইন মেনু কীবোর্ড
    keyboard = [
        [InlineKeyboardButton("💰 কাজ জমা দিন", callback_data="submit_task"),
         InlineKeyboardButton("👤 আমার একাউন্ট", callback_data="my_profile")],
        [InlineKeyboardButton("📢 রেফার করুন", callback_data="refer_friend"),
         InlineKeyboardButton("💸 উইথড্র", callback_data="withdraw_money")],
        [InlineKeyboardButton("📞 সাপোর্ট", url="https://t.me/YOUR_SUPPORT_LINK")]
    ]
    
    # এডমিন হলে এডমিন প্যানেল বাটন
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])

    await update.message.reply_text(welcome_msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "my_profile":
        user = get_user(query.from_user.id)
        msg = (
            f"👤 **আপনার প্রোফাইল**\n\n"
            f"🆔 আইডি: `{user['id']}`\n"
            f"💰 ব্যালেন্স: ৳{user['balance']:.2f}\n"
            f"✅ মোট কাজ: {user['total_tasks']}\n"
            f"🔗 আপনার রেফারার আইডি: {user.get('referrer', 'N/A')}"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ফিরে যান", callback_data="back_home")]]))
    
    elif query.data == "refer_friend":
        config = get_config()
        link = f"https://t.me/{context.bot.username}?start={query.from_user.id}"
        msg = (
            f"📢 **রেফার প্রোগ্রাম**\n\n"
            f"আপনার বন্ধুকে ইনভাইট করুন এবং জিতে নিন বোনাস!\n"
            f"প্রতি রেফারে বোনাস: ৳{config['referral_bonus']:.2f} (যখন রেফারের কাজ এপ্রুভ হবে)\n\n"
            f"আপনার রেফার লিংক:\n`{link}`"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ফিরে যান", callback_data="back_home")]]))

    elif query.data == "back_home":
        # মেইন মেনুতে ফিরে যাওয়া
        keyboard = [
            [InlineKeyboardButton("💰 কাজ জমা দিন", callback_data="submit_task"),
             InlineKeyboardButton("👤 আমার একাউন্ট", callback_data="my_profile")],
            [InlineKeyboardButton("📢 রেফার করুন", callback_data="refer_friend"),
             InlineKeyboardButton("💸 উইথড্র", callback_data="withdraw_money")],
             [InlineKeyboardButton("📞 সাপোর্ট", url="https://t.me/YOUR_SUPPORT_LINK")]
        ]
        if is_admin(query.from_user.id):
            keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])
        
        await query.edit_message_text("প্রধান মেনু:", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 5. কাজ জমা দেওয়া (Conversation Handler)
# ==========================================

async def start_task_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = get_user(query.from_user.id)
    
    if user['is_blocked']:
        await query.answer("⛔ আপনি ব্লক আছেন। এডমিনের সাথে যোগাযোগ করুন।", show_alert=True)
        return ConversationHandler.END

    config = get_config()
    apps = config.get('monitored_apps', [])
    
    if not apps:
        await query.answer("বর্তমানে কোনো কাজ নেই।", show_alert=True)
        return ConversationHandler.END
        
    # অ্যাপ লিস্ট দেখানো
    buttons = []
    for app in apps:
        buttons.append([InlineKeyboardButton(f"📱 {app['name']}", callback_data=f"select_app_{app['id']}")])
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel_task")])
    
    await query.edit_message_text(
        "নিচের তালিকা থেকে একটি অ্যাপ বেছে নিন এবং প্লে-স্টোরে গিয়ে ৫ স্টার রিভিউ দিন।\n\n"
        f"✅ কাজের মূল্য: ৳{config['task_price']:.2f}\n"
        "⚠️ সতর্কতা: রিভিউ দেওয়ার পর সেই **নামটি হুবহু** এখানে দিতে হবে।",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return TASK_NAME

# (TASK_NAME স্টেজ - অ্যাপ সিলেক্ট করার পর)
async def app_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_task":
        await query.edit_message_text("কাজ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    app_id = query.data.split("select_app_")[1]
    context.user_data['task_app_id'] = app_id
    
    await query.edit_message_text(
        f"✅ অ্যাপ সিলেক্ট করা হয়েছে।\n\n"
        "এখন প্লে স্টোরে যে **নাম (Name)** দিয়ে রিভিউ দিয়েছেন, সেই নামটি হুবহু লিখুন:"
    )
    return TASK_EMAIL # পরবর্তী স্টেজ: রিভিউ নাম ইনপুট

# (TASK_EMAIL স্টেজ - রিভিউ নাম ইনপুট)
async def get_review_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['review_name'] = update.message.text.strip()
    await update.message.reply_text("ভালো। এবার আপনার **ইমেইল এড্রেসটি** দিন:")
    return TASK_DEVICE # পরবর্তী স্টেজ: ইমেইল ইনপুট

# (TASK_DEVICE স্টেজ - ইমেইল ইনপুট)
async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text.strip()
    await update.message.reply_text("আপনার **ডিভাইসের নাম** (যেমন: Samsung S21) লিখুন:")
    return TASK_SS # পরবর্তী স্টেজ: ডিভাইস নাম ইনপুট

# (TASK_SS স্টেজ - ডিভাইস নাম ইনপুট, তারপর সেভ)
async def get_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['device'] = update.message.text.strip()
    await update.message.reply_text(
        "শেষ ধাপ: আপনার রিভিউর একটি **স্ক্রিনশট লিংক** দিন।\n"
        "(আপনি imgbb বা অন্য কোথাও আপলোড করে লিংক দিতে পারেন, অথবা 'N/A' লিখুন)"
    )
    # এখানে শেষ মেসেজ চাইছি, তাই ফলব্যাক হ্যান্ডলার save_task কে কল করবে
    return ConversationHandler.END 

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    screenshot = update.message.text.strip()
    user_id = update.effective_user.id
    data = context.user_data
    
    # ডাটাবেসে টাস্ক সেভ করা (Pending)
    task_ref = db.collection('tasks').document()
    task_data = {
        "task_id": task_ref.id,
        "user_id": user_id,
        "app_id": data.get('task_app_id'),
        "review_name": data.get('review_name'),
        "email": data.get('email'),
        "device": data.get('device'),
        "screenshot": screenshot,
        "status": "pending",
        "submitted_at": datetime.now(),
        "price": get_config()['task_price'] 
    }
    task_ref.set(task_data)
    
    await update.message.reply_text(
        "✅ আপনার কাজ সফলভাবে জমা হয়েছে!\n\n"
        "🤖 বট এখন যাচাই করছে...\n"
        "যদি ২৪ ঘন্টার মধ্যে প্লে-স্টোরে আপনার রিভিউ পাওয়া যায় এবং ৫ স্টার হয়, তবে অটোমেটিক ব্যালেন্স এড হবে।"
    )
    return ConversationHandler.END

# ==========================================
# 6. অটোমেশন সিস্টেম (Auto Approve/Reject & Group Alert)
# ==========================================

def approve_task(task_id, user_id, amount):
    """টাস্ক এপ্রুভ এবং ব্যালেন্স যোগ করা"""
    db.collection('tasks').document(task_id).update({
        "status": "approved",
        "approved_at": datetime.now()
    })
    
    user_ref = db.collection('users').document(str(user_id))
    user_ref.update({
        "balance": firestore.Increment(amount),
        "total_tasks": firestore.Increment(1)
    })
    
    # রেফার কমিশন
    user_doc = user_ref.get().to_dict()
    if user_doc.get('referrer'):
        bonus = get_config()['referral_bonus']
        db.collection('users').document(str(user_doc['referrer'])).update({
            "balance": firestore.Increment(bonus)
        })

def reject_task(task_id, reason):
    """টাস্ক রিজেক্ট করা"""
    db.collection('tasks').document(task_id).update({
        "status": "rejected",
        "rejection_reason": reason,
        "rejected_at": datetime.now()
    })

def check_group_alerts(apps):
    """ফেজ 1: নতুন রিভিউ চেক করে গ্রুপে পাঠানো (পুরোনো সিস্টেম)"""
    for app in apps:
        try:
            current_reviews, _ = play_reviews(app['id'], count=5, sort=Sort.NEWEST)
            
            for review in current_reviews:
                r_id = review['reviewId']
                
                # ফায়ারবেসে দেখা রিভিউ আইডি চেক করা
                seen_ref = db.collection('seen_reviews').document(r_id)
                if not seen_ref.get().exists:
                    
                    user_name = review['userName']
                    rating = review['score']
                    content = review['content']
                    date_str = review['at'].strftime("%d %B, %Y at %I:%M %p")
                    
                    ai_note = get_ai_summary(content, rating)
                    
                    msg = (
                        f"🔔 **নতুন রিভিউ (Group Alert)!**\n"
                        f"📱 **অ্যাপ:** {app['name']}\n"
                        f"👤 **নাম:** {user_name}\n"
                        f"⭐ **রেটিং:** {rating}/5\n"
                        f"📅 **তারিখ:** {date_str}\n" # তারিখ অন্তর্ভুক্ত করা হয়েছে
                        f"💬 **রিভিউ:** {content}\n\n"
                        f"🤖 **AI মন্তব্য:** {ai_note}"
                    )
                    
                    send_telegram_message(msg, chat_id=TELEGRAM_CHAT_ID)
                    
                    # Firebase-এ সেভ করা
                    seen_ref.set({"app_id": app['id'], "time": datetime.now()})
                    print(f"✅ Group Alert Sent: {app['name']} - {user_name}")

        except Exception as e:
            print(f"⚠️ Group Alert Check Error for {app['name']}: {e}")


def check_task_approvals(apps):
    """ফেজ 2: ইউজার সাবমিশন চেক করে অটো এপ্রুভ/রিজেক্ট করা"""
    
    for app in apps:
        try:
            # প্লে স্টোর থেকে লেটেস্ট রিভিউ আনা (৫০টি যথেষ্ট)
            result, _ = play_reviews(app['id'], count=50, sort=Sort.NEWEST)
            
            # পেন্ডিং টাস্কগুলো আনা এই অ্যাপের জন্য
            pending_tasks = db.collection('tasks')\
                .where('app_id', '==', app['id'])\
                .where('status', '==', 'pending')\
                .stream()
            
            for task_doc in pending_tasks:
                task = task_doc.to_dict()
                task_user_name = task['review_name'].strip().lower()
                submitted_time = task['submitted_at'].replace(tzinfo=None)
                
                found = False
                
                # রিভিউর সাথে ম্যাচ করা
                for review in result:
                    play_name = review['userName'].strip().lower()
                    
                    if task_user_name == play_name:
                        found = True
                        if review['score'] == 5:
                            approve_task(task_doc.id, task['user_id'], task['price'])
                            # ইউজারকে পার্সোনাল মেসেজ পাঠানো
                            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": task['user_id'], "text": f"🎉 **অভিনন্দন!** আপনার কাজ '{task['review_name']}' সফলভাবে এপ্রুভ হয়েছে এবং ৳{task['price']:.2f} আপনার একাউন্টে যোগ হয়েছে।", "parse_mode": "Markdown"})
                        else:
                            reject_task(task_doc.id, f"Low Rating (< 5 Star). Found name: {play_name}, but rating was {review['score']}.")
                            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": task['user_id'], "text": f"❌ **দুঃখিত!** আপনার কাজ '{task['review_name']}' রিজেক্ট হয়েছে। কারণ: ৫ স্টারের কম রেটিং দেওয়া হয়েছে।", "parse_mode": "Markdown"})
                        break 
                
                # যদি রিভিউ না পাওয়া যায় এবং ২৪ ঘন্টা পার হয়ে যায়
                if not found:
                    if datetime.now() - submitted_time > timedelta(hours=24):
                        reject_task(task_doc.id, "Review not found within 24h")
                        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": task['user_id'], "text": f"❌ **দুঃখিত!** আপনার কাজ '{task['review_name']}' রিজেক্ট হয়েছে। কারণ: ২৪ ঘন্টার মধ্যে প্লে স্টোরে আপনার রিভিউ খুঁজে পাওয়া যায়নি।", "parse_mode": "Markdown"})

        except Exception as e:
            print(f"⚠️ Task Approval Check Error for {app['name']}: {e}")

def run_automation_and_alerts():
    """ব্যাকগ্রাউন্ডে চলবে: গ্রুপ অ্যালার্ট এবং টাস্ক এপ্রুভালের জন্য"""
    while True:
        config = get_config()
        apps = config.get('monitored_apps', [])
        
        print(f"🔄 Automation Cycle Started. Monitoring {len(apps)} apps.")
        
        # Phase 1: Group Alert (Original System)
        check_group_alerts(apps)
        
        # Phase 2: Task Submission Processing (New System)
        check_task_approvals(apps)

        time.sleep(300) # প্রতি ৫ মিনিট পর পর

# ==========================================
# 7. এডমিন প্যানেল এবং অন্যান্য হ্যান্ডলার
# (এই অংশটি কোডের দৈর্ঘ্যের কারণে সংক্ষেপিত, তবে প্রধান লজিক সংরক্ষিত)
# ==========================================

# ... (Previous Admin, Withdraw, and Conversation Handler functions remain the same) ...

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("⛔ শুধুমাত্র এডমিনদের জন্য!", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton("➕ অ্যাপ যুক্ত করুন", callback_data="adm_add_app"),
         InlineKeyboardButton("➖ অ্যাপ রিমুভ করুন", callback_data="adm_rmv_app")],
        [InlineKeyboardButton("📊 অ্যাপ স্ট্যাটাস (24h)", callback_data="adm_app_stats"),
         InlineKeyboardButton("💰 মোট দায় (Liability)", callback_data="adm_liability")],
        [InlineKeyboardButton("👥 ইউজার ম্যানেজ", callback_data="adm_manage_usr"),
         InlineKeyboardButton("⚙️ সেটিংস", callback_data="adm_settings")],
        [InlineKeyboardButton("🔙 মেইন মেনু", callback_data="back_home")]
    ]
    
    await query.edit_message_text("⚙️ **এডমিন প্যানেল**", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_app_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    config = get_config()
    apps = config.get('monitored_apps', [])
    
    msg = "📊 **গত ২৪ ঘন্টার রিভিউ রিপোর্ট (প্লে-স্টোর):**\n\n"
    
    for app in apps:
        try:
            reviews_list, _ = play_reviews(app['id'], count=50, sort=Sort.NEWEST)
            count_24h = 0
            now_utc = datetime.now(pytz.utc).replace(tzinfo=None)
            
            for r in reviews_list:
                # প্লে-স্টোর স্ক্র্যাপার UTC টাইমস্ট্যাম্প দেয়
                review_time_utc = r['at'].replace(tzinfo=None)
                if (now_utc - review_time_utc) < timedelta(hours=24):
                    count_24h += 1
            
            msg += f"📱 **{app['name']}**\n🆔 `{app['id']}`\n🕒 ২৪ ঘন্টায় রিভিউ: {count_24h}টি\n\n"
        except:
            msg += f"📱 {app['name']} (Error Fetching)\n\n"
            
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))


# ... (Other admin functions like add/remove app, manage user are too long to show here but exist in the original full script logic) ...

# ==========================================
# 8. মেইন এক্সিকিউশন
# ==========================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running with Firebase & Automation!"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == '__main__':
    # ১. ব্যাকগ্রাউন্ড থ্রেড রান করা
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_automation_and_alerts, daemon=True).start()
    
    # ২. টেলিগ্রাম বট রান করা
    application = ApplicationBuilder().token(TOKEN).build()
    
    # ... (Handlers setup remains the same, ensuring all new and old functionalities are covered) ...
    # Task Submission Conversation
    task_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_task_submission, pattern="^submit_task$")],
        states={
            TASK_NAME: [CallbackQueryHandler(app_selected, pattern="^select_app_"), 
                        CallbackQueryHandler(app_selected, pattern="^cancel_task$")],
            TASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_review_name)],
            TASK_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            TASK_SS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_device)],
        },
        fallbacks=[MessageHandler(filters.TEXT & ~filters.COMMAND, save_task)] 
    )
    application.add_handler(task_conv)
    
    # [Other Handlers need to be re-added here based on the full code]

    print("🤖 Bot Started Polling...")
    application.run_polling()
