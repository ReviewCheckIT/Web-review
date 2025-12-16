import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
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

# ENV ভেরিয়েবল (Render থেকে আসবে)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = os.environ.get("OWNER_ID", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "") # গ্রুপ আইডি (-100...)
FIREBASE_JSON = os.environ.get("FIREBASE_CREDENTIALS", "firebase_key.json") 
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', "")
PORT = int(os.environ.get("PORT", 8080))

# Gemini AI সেটআপ
model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        logger.error(f"Gemini AI Error: {e}")

# Firebase কানেকশন
if not firebase_admin._apps:
    try:
        if FIREBASE_JSON.startswith("{"):
            # যদি Env Var এ সরাসরি JSON স্ট্রিং থাকে
            cred_dict = json.loads(FIREBASE_JSON)
            cred = credentials.Certificate(cred_dict)
        else:
            # যদি লোকাল ফাইল পাথ থাকে
            cred = credentials.Certificate(FIREBASE_JSON)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Connected Successfully!")
    except Exception as e:
        print(f"❌ Firebase Connection Failed: {e}")

db = firestore.client()

# ==========================================
# 2. গ্লোবাল কনফিগারেশন ও স্টেট
# ==========================================

DEFAULT_CONFIG = {
    "task_price": 20.0,
    "referral_bonus": 5.0,
    "min_withdraw": 50.0,
    "monitored_apps": [] 
}

# Conversation States
T_APP_SELECT, T_REVIEW_NAME, T_EMAIL, T_DEVICE, T_SS = range(5)
ADD_APP_ID, ADD_APP_NAME = range(5, 7)
SET_PRICE, SET_REF_BONUS, SET_MIN_WITHDRAW = range(7, 10)
USER_MNG_ID, USER_MNG_ACTION, USER_MNG_AMOUNT = range(10, 13)
WD_METHOD, WD_NUMBER, WD_AMOUNT = range(13, 16)
REMOVE_APP_SELECT, = range(16, 17)

# ==========================================
# 3. হেল্পার ফাংশন
# ==========================================

def get_config():
    try:
        ref = db.collection('settings').document('main_config')
        doc = ref.get()
        if doc.exists: return doc.to_dict()
        else:
            ref.set(DEFAULT_CONFIG)
            return DEFAULT_CONFIG
    except: return DEFAULT_CONFIG

def update_config(data):
    try: db.collection('settings').document('main_config').set(data, merge=True)
    except: pass

def is_admin(user_id):
    if str(user_id) == str(OWNER_ID): return True
    try:
        user = db.collection('users').document(str(user_id)).get()
        return user.exists and user.to_dict().get('is_admin', False)
    except: return False

def get_user(user_id):
    try:
        doc = db.collection('users').document(str(user_id)).get()
        if doc.exists: return doc.to_dict()
    except: pass
    return None

def create_user(user_id, first_name, referrer_id=None):
    if not get_user(user_id):
        try:
            user_data = {
                "id": str(user_id),
                "name": first_name,
                "balance": 0.0,
                "total_tasks": 0,
                "joined_at": datetime.now(),
                "referrer": referrer_id if referrer_id and referrer_id.isdigit() and str(referrer_id) != str(user_id) else None,
                "is_blocked": False,
                "is_admin": str(user_id) == str(OWNER_ID)
            }
            db.collection('users').document(str(user_id)).set(user_data)
        except: pass

def send_telegram_message(message, chat_id=TELEGRAM_CHAT_ID):
    if not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: logger.error(f"Telegram Send Error: {e}")

def get_ai_summary(text, rating):
    if not model: return "AI Analysis Unavailable"
    try:
        prompt = f"Review: '{text}' ({rating}/5). Summarize sentiment in Bangla (max 10 words). Start with 'মুড:'"
        response = model.generate_content(prompt)
        return response.text.strip()
    except: return "N/A"

# ==========================================
# 4. ইউজার সাইড ফাংশন
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer = args[0] if args and args[0].isdigit() else None
    create_user(user.id, user.first_name, referrer)
    
    welcome_msg = f"আসসালামু আলাইকুম, {user.first_name}! 🌙\n\n💸 **App Review Bot** এ স্বাগতম।"
    keyboard = [
        [InlineKeyboardButton("💰 কাজ জমা দিন", callback_data="submit_task"),
         InlineKeyboardButton("👤 প্রোফাইল", callback_data="my_profile")],
        [InlineKeyboardButton("📤 উইথড্র", callback_data="start_withdraw"),
         InlineKeyboardButton("📢 রেফার", callback_data="refer_friend")]
    ]
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(welcome_msg, reply_markup=reply_markup)
    else:
        await update.message.reply_text(welcome_msg, reply_markup=reply_markup)

async def common_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_home":
        await start(update, context)
    elif query.data == "my_profile":
        user = get_user(query.from_user.id)
        msg = f"👤 **প্রোফাইল**\n🆔: `{user['id']}`\n💰: ৳{user['balance']:.2f}\n✅ টাস্ক: {user['total_tasks']}"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    elif query.data == "refer_friend":
        config = get_config()
        link = f"https://t.me/{context.bot.username}?start={query.from_user.id}"
        await query.edit_message_text(f"📢 **রেফার লিংক:**\n`{link}`\n\nবোনাস: ৳{config['referral_bonus']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))

# Withdrawal
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = get_user(query.from_user.id)
    config = get_config()
    if user['balance'] < config['min_withdraw']:
        await query.answer(f"সর্বনিম্ন ৳{config['min_withdraw']}", show_alert=True)
        return ConversationHandler.END
    await query.edit_message_text("মেথড সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Bkash", callback_data="m_bkash"), InlineKeyboardButton("Nagad", callback_data="m_nagad")], [InlineKeyboardButton("❌ বাতিল", callback_data="cancel")]]))
    return WD_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    context.user_data['wd_method'] = "Bkash" if "bkash" in query.data else "Nagad"
    await query.edit_message_text("নাম্বার দিন:")
    return WD_NUMBER

async def withdraw_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['wd_number'] = update.message.text
    await update.message.reply_text("টাকার পরিমাণ লিখুন:")
    return WD_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        user_id = str(update.effective_user.id)
        user = get_user(user_id)
        if amount > user['balance']:
            await update.message.reply_text("❌ ব্যালেন্স নেই।")
            return ConversationHandler.END
        
        db.collection('users').document(user_id).update({"balance": firestore.Increment(-amount)})
        db.collection('withdrawals').add({
            "user_id": user_id, "amount": amount, "method": context.user_data['wd_method'],
            "number": context.user_data['wd_number'], "status": "pending", "time": datetime.now()
        })
        send_telegram_message(f"💸 **Withdraw Request**\nUser: `{user_id}`\nTk: {amount}")
        await update.message.reply_text("✅ রিকোয়েস্ট সফল!")
    except: await update.message.reply_text("❌ ভুল ইনপুট।")
    return ConversationHandler.END

# Task Submission
async def start_task_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    config = get_config()
    apps = config.get('monitored_apps', [])
    if not apps:
        await query.answer("কাজ নেই।", show_alert=True)
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(f"📱 {app['name']} (৳{config['task_price']})", callback_data=f"sel_{app['id']}")] for app in apps]
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel")])
    await query.edit_message_text("অ্যাপ সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
    return T_APP_SELECT

async def app_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    context.user_data['tid'] = query.data.split("sel_")[1]
    await query.edit_message_text("প্লে-স্টোর নাম (Name) দিন:")
    return T_REVIEW_NAME

async def get_review_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['rname'] = update.message.text
    await update.message.reply_text("ইমেইল দিন:")
    return T_EMAIL

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text
    await update.message.reply_text("ডিভাইস নাম:")
    return T_DEVICE

async def get_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['dev'] = update.message.text
    await update.message.reply_text("স্ক্রিনশট লিংক:")
    return T_SS

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    config = get_config()
    db.collection('tasks').add({
        "user_id": str(update.effective_user.id), "app_id": data['tid'],
        "review_name": data['rname'], "email": data['email'],
        "device": data['dev'], "screenshot": update.message.text,
        "status": "pending", "submitted_at": datetime.now(), "price": config['task_price']
    })
    await update.message.reply_text("✅ জমা হয়েছে!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("বাতিল।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    return ConversationHandler.END

# ==========================================
# 5. অটোমেশন ও গ্রুপ নোটিফিকেশন
# ==========================================

def approve_task(task_id, user_id, amount):
    task_ref = db.collection('tasks').document(task_id)
    if task_ref.get().to_dict()['status'] == 'pending':
        task_ref.update({"status": "approved", "approved_at": datetime.now()})
        db.collection('users').document(str(user_id)).update({
            "balance": firestore.Increment(amount),
            "total_tasks": firestore.Increment(1)
        })
        return True
    return False

def run_automation():
    logger.info("Automation Started...")
    while True:
        try:
            config = get_config()
            apps = config.get('monitored_apps', [])
            for app in apps:
                try:
                    reviews, _ = play_reviews(app['id'], count=30, sort=Sort.NEWEST)
                    for r in reviews[:5]:
                        rid = r['reviewId']
                        if not db.collection('seen_reviews').document(rid).get().exists:
                            ai_txt = get_ai_summary(r['content'], r['score'])
                            msg = (f"🔔 **নতুন রিভিউ!**\n📱 {app['name']}\n👤 {r['userName']} ({r['score']}★)\n"
                                   f"💬 {r['content']}\n🤖 AI: {ai_txt}")
                            send_telegram_message(msg)
                            db.collection('seen_reviews').document(rid).set({"t": datetime.now()})
                    
                    # Auto Approve
                    p_tasks = db.collection('tasks').where('app_id', '==', app['id']).where('status', '==', 'pending').stream()
                    for t in p_tasks:
                        td = t.to_dict()
                        for r in reviews:
                            if td['review_name'].lower().strip() == r['userName'].lower().strip():
                                if r['score'] == 5:
                                    if approve_task(t.id, td['user_id'], td['price']):
                                        send_telegram_message(f"🎉 **Auto Approved!**\nUser: `{td['user_id']}`")
                                else:
                                    db.collection('tasks').document(t.id).update({"status": "rejected"})
                                break
                except Exception as e: print(f"App Check Error: {e}")
        except Exception as e: print(f"Loop Error: {e}")
        time.sleep(300) # 5 Minutes

# ==========================================
# 6. এডমিন প্যানেল
# ==========================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    kb = [[InlineKeyboardButton("➕ Add App", callback_data="add_app"), InlineKeyboardButton("➖ Remove App", callback_data="rmv_app")],
          [InlineKeyboardButton("🔙 Back", callback_data="back_home")]]
    await update.callback_query.edit_message_text("⚙️ Admin Panel", reply_markup=InlineKeyboardMarkup(kb))

# Admin Handlers (Shortened)
async def add_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("App Package ID:")
    return ADD_APP_ID

async def add_app_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['nid'] = update.message.text
    await update.message.reply_text("App Name:")
    return ADD_APP_NAME

async def add_app_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config()
    apps = config.get('monitored_apps', [])
    apps.append({"id": context.user_data['nid'], "name": update.message.text})
    update_config({"monitored_apps": apps})
    await update.message.reply_text("✅ Added!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def rmv_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config()
    apps = config.get('monitored_apps', [])
    btns = [[InlineKeyboardButton(f"🗑️ {a['name']}", callback_data=f"rm_{i}")] for i, a in enumerate(apps)]
    btns.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await update.callback_query.edit_message_text("Remove which?", reply_markup=InlineKeyboardMarkup(btns))
    return REMOVE_APP_SELECT

async def rmv_app_sel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "cancel": return await cancel_conv(update, context)
    idx = int(query.data.split("rm_")[1])
    config = get_config()
    apps = config.get('monitored_apps', [])
    if 0 <= idx < len(apps):
        del apps[idx]
        update_config({"monitored_apps": apps})
        await query.edit_message_text("✅ Removed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    return ConversationHandler.END

# ==========================================
# 7. মেইন রানার
# ==========================================

app = Flask(__name__)
@app.route('/')
def home(): return "Bot is Alive!"

if __name__ == '__main__':
    # Start Flask in separate thread
    threading.Thread(target=app.run, kwargs={'host':'0.0.0.0','port':PORT}, daemon=True).start()
    # Start Automation Loop
    threading.Thread(target=run_automation, daemon=True).start()
    
    # Start Bot
    application = ApplicationBuilder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_task_submission, pattern="^submit_task$")],
        states={T_APP_SELECT:[CallbackQueryHandler(app_selected, pattern="^sel_")], T_REVIEW_NAME:[MessageHandler(filters.TEXT, get_review_name)],
                T_EMAIL:[MessageHandler(filters.TEXT, get_email)], T_DEVICE:[MessageHandler(filters.TEXT, get_device)], T_SS:[MessageHandler(filters.TEXT, save_task)]},
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern="^start_withdraw$")],
        states={WD_METHOD:[CallbackQueryHandler(withdraw_method)], WD_NUMBER:[MessageHandler(filters.TEXT, withdraw_number)], WD_AMOUNT:[MessageHandler(filters.TEXT, withdraw_amount)]},
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_app_start, pattern="^add_app$")],
        states={ADD_APP_ID:[MessageHandler(filters.TEXT, add_app_id)], ADD_APP_NAME:[MessageHandler(filters.TEXT, add_app_name)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_app_start, pattern="^rmv_app$")],
        states={REMOVE_APP_SELECT:[CallbackQueryHandler(rmv_app_sel)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(CallbackQueryHandler(common_callback, pattern="^(my_profile|refer_friend|back_home)$"))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))

    print("🚀 Bot Started on Render...")
    application.run_polling(drop_pending_updates=True)
