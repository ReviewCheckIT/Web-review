import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
import pytz
import requests
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
import google.generativeai as genai

# ==========================================
# 1. কনফিগারেশন এবং সেটআপ
# ==========================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# এনভায়রনমেন্ট ভেরিয়েবল
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FIREBASE_JSON = os.environ.get("FIREBASE_CREDENTIALS")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
PORT = int(os.environ.get("PORT", 8080))

# জেমিনি কনফিগারেশন
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

# ফায়ারবেস ইনিশিয়ালাইজেশন
if not firebase_admin._apps:
    try:
        if FIREBASE_JSON:
            cred_dict = json.loads(FIREBASE_JSON)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            print("✅ Firebase Connected Successfully!")
        else:
            print("⚠️ Warning: FIREBASE_CREDENTIALS not found.")
    except Exception as e:
        print(f"❌ Firebase Connection Failed: {e}")

db = firestore.client()

# ==========================================
# 2. স্টেজ এবং কনস্ট্যান্টস
# ==========================================

DEFAULT_CONFIG = {
    "task_price": 20.0,
    "referral_bonus": 5.0,
    "min_withdraw": 50.0,
    "monitored_apps": []
}

# কনভারসেশন স্টেজ (Conversation Stages)
(
    TASK_NAME, TASK_EMAIL, TASK_DEVICE, TASK_SS,
    ADMIN_APP_NAME, ADMIN_APP_ID,
    WITHDRAW_AMOUNT, WITHDRAW_NUMBER, WITHDRAW_METHOD
) = range(9)

# ==========================================
# 3. ডাটাবেস হেল্পার ফাংশন
# ==========================================

def get_config():
    try:
        ref = db.collection('settings').document('main_config')
        doc = ref.get()
        if doc.exists:
            return doc.to_dict()
        else:
            ref.set(DEFAULT_CONFIG)
            return DEFAULT_CONFIG
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return DEFAULT_CONFIG

def update_config(key, value):
    try:
        db.collection('settings').document('main_config').update({key: value})
        return True
    except:
        return False

def is_admin(user_id):
    if str(user_id) == str(OWNER_ID):
        return True
    try:
        user = db.collection('users').document(str(user_id)).get()
        return user.exists and user.to_dict().get('is_admin', False)
    except:
        return False

def get_user(user_id):
    try:
        doc = db.collection('users').document(str(user_id)).get()
        if doc.exists:
            return doc.to_dict()
    except:
        pass
    return None

def create_user(user_id, first_name, referrer_id=None):
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
    if not chat_id or not TOKEN: return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Error sending msg: {e}")

def get_ai_summary(text, rating):
    if not GEMINI_API_KEY: return "AI বিশ্লেষণ বন্ধ।"
    try:
        prompt = f"Review: '{text}' (Rating: {rating}/5). Summarize sentiment in Bangla in 5 words."
        response = model.generate_content(prompt)
        return response.text.strip()
    except:
        return "বিশ্লেষণ ব্যর্থ।"

# ==========================================
# 4. অটোমেশন সিস্টেম (Automation System)
# ==========================================

def approve_task(task_id, user_id, amount):
    try:
        db.collection('tasks').document(task_id).update({
            "status": "approved",
            "approved_at": datetime.now()
        })
        
        user_ref = db.collection('users').document(str(user_id))
        user_ref.update({
            "balance": firestore.Increment(amount),
            "total_tasks": firestore.Increment(1)
        })
        
        user_doc = user_ref.get().to_dict()
        if user_doc.get('referrer'):
            bonus = get_config().get('referral_bonus', 5.0)
            db.collection('users').document(str(user_doc['referrer'])).update({
                "balance": firestore.Increment(bonus)
            })
    except Exception as e:
        logger.error(f"Auto Approve Error: {e}")

def reject_task(task_id, reason):
    try:
        db.collection('tasks').document(task_id).update({
            "status": "rejected",
            "rejection_reason": reason,
            "rejected_at": datetime.now()
        })
    except Exception as e:
        logger.error(f"Auto Reject Error: {e}")

def run_automation_and_alerts():
    """ব্যাকগ্রাউন্ড অটোমেশন লজিক"""
    while True:
        try:
            config = get_config()
            apps = config.get('monitored_apps', [])
            
            if apps:
                print(f"🔄 Automation Running: Monitoring {len(apps)} apps.")
                # 1. Group Alert Check
                for app in apps:
                    try:
                        current_reviews, _ = play_reviews(app['id'], count=5, sort=Sort.NEWEST)
                        for review in current_reviews:
                            r_id = review['reviewId']
                            seen_ref = db.collection('seen_reviews').document(r_id)
                            if not seen_ref.get().exists:
                                msg = (
                                    f"🔔 **নতুন রিভিউ!**\n📱 {app['name']}\n"
                                    f"👤 {review['userName']} ({review['score']}★)\n"
                                    f"💬 {review['content']}\n🤖 AI: {get_ai_summary(review['content'], review['score'])}"
                                )
                                send_telegram_message(msg, chat_id=TELEGRAM_CHAT_ID)
                                seen_ref.set({"app_id": app['id'], "time": datetime.now()})
                    except Exception as e:
                        print(f"Scraper Error ({app.get('name', 'N/A')}): {e}")

                # 2. Task Verification Logic
                for app in apps:
                    try:
                        result, _ = play_reviews(app['id'], count=50, sort=Sort.NEWEST)
                        pending_tasks = db.collection('tasks').where('app_id', '==', app['id']).where('status', '==', 'pending').stream()
                        
                        for task_doc in pending_tasks:
                            task = task_doc.to_dict()
                            task_name = task.get('review_name', '').strip().lower()
                            submitted_time = task['submitted_at'].replace(tzinfo=None)
                            found = False
                            
                            for review in result:
                                if task_name == review['userName'].strip().lower():
                                    found = True
                                    if review['score'] == 5:
                                        approve_task(task_doc.id, task['user_id'], task['price'])
                                        send_telegram_message(f"✅ কাজ এপ্রুভ হয়েছে: {task['review_name']}", chat_id=task['user_id'])
                                    else:
                                        reject_task(task_doc.id, f"Low Rating: {review['score']}")
                                        send_telegram_message(f"❌ কাজ রিজেক্ট (কম রেটিং): {task['review_name']}", chat_id=task['user_id'])
                                    break
                            
                            if not found and (datetime.now() - submitted_time > timedelta(hours=24)):
                                reject_task(task_doc.id, "Review not found in 24h")
                                send_telegram_message(f"❌ কাজ রিজেক্ট (খুঁজে পাওয়া যায়নি): {task['review_name']}", chat_id=task['user_id'])
                    except Exception as e:
                        print(f"Verification Error: {e}")

        except Exception as e:
            print(f"Global Auto Error: {e}")
        time.sleep(300)

# ==========================================
# 5. মেইন মেনু ডিসপ্লে ফাংশন (Fix for 'back_home')
# ==========================================

async def display_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, user_name):
    """স্টার্ট এবং ব্যাক বাটন উভয় থেকেই মেইন মেনু ডিসপ্লে করবে"""
    
    keyboard = [
        [InlineKeyboardButton("💰 কাজ জমা দিন", callback_data="submit_task"),
         InlineKeyboardButton("👤 আমার একাউন্ট", callback_data="my_profile")],
        [InlineKeyboardButton("📢 রেফার করুন", callback_data="refer_friend"),
         InlineKeyboardButton("💸 উইথড্র", callback_data="withdraw_money")]
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])

    if update.callback_query:
        # Callback থেকে এলে মেসেজ এডিট করবে
        await update.callback_query.edit_message_text(
            "প্রধান মেনু:", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Command (/start) থেকে এলে নতুন মেসেজ পাঠাবে
        await update.message.reply_text(
            f"আসসালামু আলাইকুম, {user_name}! আমাদের রিভিউ বটে স্বাগতম।", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer = args[0] if args and args[0].isdigit() else None
    create_user(user.id, user.first_name, referrer)
    
    # নতুন ডিসপ্লে ফাংশন ব্যবহার
    await display_main_menu(update, context, user.id, user.first_name)


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "my_profile":
        user = get_user(user_id)
        msg = f"👤 আইডি: `{user['id']}`\n💰 ব্যালেন্স: ৳{user.get('balance', 0):.2f}\n✅ টাস্ক: {user.get('total_tasks', 0)}"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    
    elif query.data == "refer_friend":
        config = get_config()
        link = f"https://t.me/{context.bot.username}?start={user_id}"
        msg = f"বোনাস: ৳{config.get('referral_bonus', 5)}\nলিংক:\n`{link}`"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))

    elif query.data == "back_home":
        # FIXED: সরাসরি display_main_menu ফাংশনকে কল করা হয়েছে
        await display_main_menu(update, context, user_id, query.from_user.first_name)


# --- Withdraw Conversation ---
async def start_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    config = get_config()
    min_w = config.get('min_withdraw', 50)
    
    if user['balance'] < min_w:
        await query.edit_message_text(f"❌ পর্যাপ্ত ব্যালেন্স নেই। মিনিমাম: ৳{min_w}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    await query.edit_message_text("কত টাকা উইথড্র করতে চান? (সংখ্যা লিখুন):")
    return WITHDRAW_AMOUNT

async def get_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        user = get_user(update.effective_user.id)
        config = get_config()
        if amount < config.get('min_withdraw', 50) or amount > user['balance']:
            await update.message.reply_text("❌ ভুল অ্যামাউন্ট। আবার চেষ্টা করুন।")
            return ConversationHandler.END
        context.user_data['w_amount'] = amount
        await update.message.reply_text("কোন নাম্বারে টাকা নিবেন?")
        return WITHDRAW_NUMBER
    except:
        await update.message.reply_text("দয়া করে ইংরেজি সংখ্যা লিখুন।")
        return ConversationHandler.END

async def get_withdraw_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['w_number'] = update.message.text
    keyboard = [[InlineKeyboardButton("bKash", callback_data="bkash"), InlineKeyboardButton("Nagad", callback_data="nagad")]]
    await update.message.reply_text("মেথড সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WITHDRAW_METHOD

async def save_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data
    data = context.user_data
    user_id = query.from_user.id
    
    # ব্যালেন্স কাটা এবং রিকোয়েস্ট সেভ করা
    db.collection('users').document(str(user_id)).update({"balance": firestore.Increment(-data['w_amount'])})
    db.collection('withdraws').add({
        "user_id": user_id,
        "amount": data['w_amount'],
        "number": data['w_number'],
        "method": method,
        "status": "pending",
        "time": datetime.now()
    })
    await query.edit_message_text("✅ উইথড্র রিকোয়েস্ট সফল হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    return ConversationHandler.END

# --- Task Submission Conversation ---
async def start_task_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    config = get_config()
    apps = config.get('monitored_apps', [])
    
    if not apps:
        await query.edit_message_text("কোনো কাজ নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    buttons = [[InlineKeyboardButton(f"📱 {app['name']}", callback_data=f"select_app_{app['id']}")] for app in apps]
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel_task")])
    await query.edit_message_text("অ্যাপ সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
    return TASK_NAME

async def app_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_task":
        await query.edit_message_text("বাতিল করা হলো।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
    context.user_data['task_app_id'] = query.data.split("select_app_")[1]
    await query.edit_message_text("রিভিউ দেওয়া নামটি লিখুন:")
    return TASK_EMAIL

async def get_review_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['review_name'] = update.message.text
    await update.message.reply_text("ইমেইল দিন:")
    return TASK_DEVICE

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text
    await update.message.reply_text("ডিভাইসের নাম:")
    return TASK_SS

async def get_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['device'] = update.message.text
    await update.message.reply_text("স্ক্রিনশট লিংক দিন (বা N/A):")
    return ConversationHandler.END 

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    screenshot = update.message.text
    user_id = update.effective_user.id
    data = context.user_data
    
    db.collection('tasks').add({
        "user_id": user_id,
        "app_id": data['task_app_id'],
        "review_name": data['review_name'],
        "email": data['email'],
        "device": data['device'],
        "screenshot": screenshot,
        "status": "pending",
        "submitted_at": datetime.now(),
        "price": get_config().get('task_price', 20)
    })
    await update.message.reply_text("✅ কাজ জমা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    return ConversationHandler.END

# ==========================================
# 6. এডমিন প্যানেল হ্যান্ডলার
# ==========================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query and not is_admin(query.from_user.id):
        await query.answer("Access Denied", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton("➕ অ্যাপ অ্যাড করুন", callback_data="adm_add_app")],
        [InlineKeyboardButton("📊 স্ট্যাটাস চেক", callback_data="adm_stats")],
        [InlineKeyboardButton("🔙 মেইন মেনু", callback_data="back_home")]
    ]
    msg = "⚙️ **এডমিন প্যানেল**"
    
    if query:
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    config = get_config()
    apps = config.get('monitored_apps', [])
    msg = "📊 **অ্যাপ স্ট্যাটাস:**\n\n"
    for app in apps:
        msg += f"📱 {app['name']} (ID: `{app['id']}`)\n"
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))

# --- Admin Add App Conversation ---
async def start_add_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("নতুন অ্যাপের নাম লিখুন:")
    return ADMIN_APP_NAME

async def get_app_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_app_name'] = update.message.text
    await update.message.reply_text("প্লে-স্টোর অ্যাপ আইডি (Package Name) লিখুন:")
    return ADMIN_APP_ID

async def get_app_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = update.message.text.strip()
    name = context.user_data['new_app_name']
    
    config = get_config()
    apps = config.get('monitored_apps', [])
    apps.append({"id": app_id, "name": name})
    update_config('monitored_apps', apps)
    
    await update.message.reply_text(f"✅ অ্যাপ যুক্ত হয়েছে:\nনাম: {name}\nআইডি: {app_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    return ConversationHandler.END

# ==========================================
# 7. রানার (Main Execution)
# ==========================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_automation_and_alerts, daemon=True).start()
    
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN missing")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()
    
    # Handlers Registration
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(menu_handler, pattern="^(my_profile|refer_friend|back_home)$"))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="^adm_stats$"))
    
    # Task Conversation
    task_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_task_submission, pattern="^submit_task$")],
        states={
            TASK_NAME: [CallbackQueryHandler(app_selected, pattern="^select_app_"), CallbackQueryHandler(app_selected, pattern="^cancel_task$")],
            TASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_review_name)],
            TASK_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            TASK_SS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_device)],
        },
        fallbacks=[MessageHandler(filters.TEXT & ~filters.COMMAND, save_task)]
    )
    application.add_handler(task_conv)
    
    # Withdraw Conversation
    withdraw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_withdraw, pattern="^withdraw_money$")],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_withdraw_amount)],
            WITHDRAW_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_withdraw_number)],
            WITHDRAW_METHOD: [CallbackQueryHandler(save_withdraw, pattern="^(bkash|nagad)$")]
        },
        fallbacks=[CallbackQueryHandler(menu_handler, pattern="^back_home$")]
    )
    application.add_handler(withdraw_conv)

    # Admin Add App Conversation
    add_app_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_app, pattern="^adm_add_app$")],
        states={
            ADMIN_APP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_app_name)],
            ADMIN_APP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_app_id)]
        },
        fallbacks=[CallbackQueryHandler(admin_panel, pattern="^admin_panel$")]
    )
    application.add_handler(add_app_conv)
    
    print("🤖 Bot is polling...")
    application.run_polling()
