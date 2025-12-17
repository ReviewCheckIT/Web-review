import os
import json
import logging
import threading
import time
import asyncio
from datetime import datetime, timedelta
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)
from google_play_scraper import Sort, reviews as play_reviews
from flask import Flask

# --- AI Import Safeguard ---
try:
    import google.generativeai as genai
    AI_AVAILABLE = True
except Exception as e:
    print(f"⚠️ Google AI Library Error (Skipping AI features): {e}")
    AI_AVAILABLE = False
    genai = None

# ==========================================
# 1. কনফিগারেশন এবং সেটআপ
# ==========================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ENV ভেরিয়েবল
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = os.environ.get("OWNER_ID", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FIREBASE_JSON = os.environ.get("FIREBASE_CREDENTIALS", "firebase_key.json")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', "")
PORT = int(os.environ.get("PORT", 8080))

# Gemini AI সেটআপ
model = None
if AI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        logger.error(f"Gemini AI Config Error: {e}")

# Firebase কানেকশন
if not firebase_admin._apps:
    try:
        if FIREBASE_JSON.startswith("{"):
            cred_dict = json.loads(FIREBASE_JSON)
            cred = credentials.Certificate(cred_dict)
        else:
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
    "monitored_apps": [],
    "rules_text": "⚠️ কাজের নিয়ম: সঠিকভাবে রিভিউ দিন এবং স্ক্রিনশট জমা দিন।",
    "schedule_text": "⏰ কাজের সময়: সকাল ১০টা থেকে রাত ১০টা।",
    "buttons": {
        "submit": {"text": "💰 কাজ জমা দিন", "show": True},
        "profile": {"text": "👤 প্রোফাইল", "show": True},
        "withdraw": {"text": "📤 উইথড্র", "show": True},
        "refer": {"text": "📢 রেফার", "show": True},
        "schedule": {"text": "📅 সময়সূচী", "show": True}
    },
    "custom_buttons": [] 
}

# Conversation States (Total 20 Variables)
(
    T_APP_SELECT, T_REVIEW_NAME, T_EMAIL, T_DEVICE, T_SS,           # 1-5
    ADD_APP_ID, ADD_APP_NAME,                                       # 6-7
    WD_METHOD, WD_NUMBER, WD_AMOUNT,                                # 8-10
    REMOVE_APP_SELECT,                                              # 11
    ADMIN_USER_SEARCH, ADMIN_USER_ACTION, ADMIN_USER_AMOUNT,        # 12-14
    ADMIN_EDIT_TEXT_KEY, ADMIN_EDIT_TEXT_VAL,                       # 15-16
    ADMIN_EDIT_BTN_KEY, ADMIN_EDIT_BTN_NAME,                        # 17-18
    ADMIN_ADD_BTN_NAME, ADMIN_ADD_BTN_LINK                          # 19-20
) = range(20) # Must match the number of variables exactly

# ==========================================
# 3. হেল্পার ফাংশন
# ==========================================

def get_config():
    try:
        ref = db.collection('settings').document('main_config')
        doc = ref.get()
        if doc.exists:
            data = doc.to_dict()
            for key, val in DEFAULT_CONFIG.items():
                if key not in data:
                    data[key] = val
            return data
        else:
            ref.set(DEFAULT_CONFIG)
            return DEFAULT_CONFIG
    except:
        return DEFAULT_CONFIG

def update_config(data):
    try:
        db.collection('settings').document('main_config').set(data, merge=True)
    except Exception as e:
        logger.error(f"Config Update Error: {e}")

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

def send_telegram_message(message, chat_id=TELEGRAM_CHAT_ID, reply_markup=None):
    if not chat_id: return
    try:
        payload = {
            "chat_id": chat_id, 
            "text": message, 
            "parse_mode": "Markdown"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup.to_dict()
            
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram Send Error: {e}")

def get_ai_summary(text, rating):
    if not model: return "N/A"
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
    
    db_user = get_user(user.id)
    if db_user and db_user.get('is_blocked'):
        await update.message.reply_text("⛔ আপনাকে ব্লক করা হয়েছে।")
        return

    config = get_config()
    btns_conf = config.get('buttons', DEFAULT_CONFIG['buttons'])
    
    welcome_msg = (
        f"আসসালামু আলাইকুম ওয়ারাহমাতুল্লাহি ওয়াবারাকাতুহ, {user.first_name}! 🌙\n\n"
        f"🗒 **কাজের নিয়মাবলী:**\n{config.get('rules_text', '')}\n\n"
        "নিচের মেনু থেকে অপশন সিলেক্ট করুন:"
    )

    keyboard = []
    row1 = []
    if btns_conf['submit']['show']: row1.append(InlineKeyboardButton(btns_conf['submit']['text'], callback_data="submit_task"))
    if btns_conf['profile']['show']: row1.append(InlineKeyboardButton(btns_conf['profile']['text'], callback_data="my_profile"))
    if row1: keyboard.append(row1)
    
    row2 = []
    if btns_conf['withdraw']['show']: row2.append(InlineKeyboardButton(btns_conf['withdraw']['text'], callback_data="start_withdraw"))
    if btns_conf['refer']['show']: row2.append(InlineKeyboardButton(btns_conf['refer']['text'], callback_data="refer_friend"))
    if row2: keyboard.append(row2)

    row3 = []
    if btns_conf.get('schedule', {}).get('show', True): row3.append(InlineKeyboardButton(btns_conf.get('schedule', {}).get('text', "📅 সময়সূচী"), callback_data="show_schedule"))
    if row3: keyboard.append(row3)

    custom_btns = config.get('custom_buttons', [])
    for btn in custom_btns:
        keyboard.append([InlineKeyboardButton(btn['text'], url=btn['url'])])

    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ এডমিন প্যানেল", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(welcome_msg, reply_markup=reply_markup, parse_mode="Markdown")

async def common_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "back_home":
        await start(update, context)
        
    elif query.data == "my_profile":
        user = get_user(query.from_user.id)
        msg = f"👤 **প্রোফাইল**\n\n🆔 ID: `{user['id']}`\n💰 ব্যালেন্স: ৳{user['balance']:.2f}\n✅ সম্পন্ন টাস্ক: {user['total_tasks']}"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        
    elif query.data == "refer_friend":
        config = get_config()
        link = f"https://t.me/{context.bot.username}?start={query.from_user.id}"
        await query.edit_message_text(f"📢 **রেফার লিংক:**\n`{link}`\n\nপ্রতি রেফারে বোনাস: ৳{config['referral_bonus']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    
    elif query.data == "show_schedule":
        config = get_config()
        await query.edit_message_text(f"📅 **সময়সূচী:**\n\n{config.get('schedule_text', 'No info')}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))

# --- Withdrawal System ---

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = get_user(query.from_user.id)
    config = get_config()
    
    if user['balance'] < config['min_withdraw']:
        await query.answer(f"সর্বনিম্ন উইথড্র: ৳{config['min_withdraw']}", show_alert=True)
        return ConversationHandler.END
        
    await query.edit_message_text("পেমেন্ট মেথড সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Bkash", callback_data="m_bkash"), InlineKeyboardButton("Nagad", callback_data="m_nagad")],
        [InlineKeyboardButton("❌ বাতিল", callback_data="cancel")]
    ]))
    return WD_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    context.user_data['wd_method'] = "Bkash" if "bkash" in query.data else "Nagad"
    await query.edit_message_text(f"আপনার {context.user_data['wd_method']} নাম্বারটি দিন:")
    return WD_NUMBER

async def withdraw_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['wd_number'] = update.message.text
    await update.message.reply_text("কত টাকা উইথড্র করতে চান? (সংখ্যা লিখুন)")
    return WD_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        user_id = str(update.effective_user.id)
        user = get_user(user_id)
        config = get_config()
        
        if amount < config['min_withdraw']:
             await update.message.reply_text(f"❌ সর্বনিম্ন উইথড্র ৳{config['min_withdraw']}")
             return ConversationHandler.END

        if amount > user['balance']:
            await update.message.reply_text("❌ আপনার একাউন্টে পর্যাপ্ত ব্যালেন্স নেই।")
            return ConversationHandler.END

        db.collection('users').document(user_id).update({"balance": firestore.Increment(-amount)})
        
        wd_ref = db.collection('withdrawals').add({
            "user_id": user_id,
            "user_name": update.effective_user.first_name,
            "amount": amount,
            "method": context.user_data['wd_method'],
            "number": context.user_data['wd_number'],
            "status": "pending",
            "time": datetime.now()
        })
        
        wd_id = wd_ref[1].id
        admin_msg = (
            f"💸 **New Withdrawal Request**\n"
            f"👤 User: `{user_id}`\n"
            f"💰 Amount: ৳{amount}\n"
            f"📱 Method: {context.user_data['wd_method']} ({context.user_data['wd_number']})\n"
            f"🔢 Balance Left: ৳{user['balance'] - amount:.2f}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"wd_apr_{wd_id}_{user_id}"), 
             InlineKeyboardButton("❌ Reject (Refund)", callback_data=f"wd_rej_{wd_id}_{user_id}")]
        ])
        
        if OWNER_ID:
            await context.bot.send_message(chat_id=OWNER_ID, text=admin_msg, reply_markup=kb, parse_mode="Markdown")

        await update.message.reply_text("✅ উইথড্র রিকোয়েস্ট সফল হয়েছে! এডমিন চেক করে পেমেন্ট করবে।")
        
    except ValueError:
        await update.message.reply_text("❌ ভুল ইনপুট। শুধু সংখ্যা ব্যবহার করুন।")
    except Exception as e:
        logger.error(f"Withdraw Error: {e}")
        await update.message.reply_text("❌ সমস্যা হয়েছে। পরে চেষ্টা করুন।")
        
    return ConversationHandler.END

async def handle_withdrawal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    
    data = query.data.split('_')
    action = data[1]
    wd_id = data[2]
    user_id = data[3]
    
    wd_doc = db.collection('withdrawals').document(wd_id).get()
    if not wd_doc.exists or wd_doc.to_dict()['status'] != 'pending':
        await query.answer("Already processed", show_alert=True)
        await query.edit_message_reply_markup(None)
        return

    amount = wd_doc.to_dict()['amount']

    if action == "apr":
        db.collection('withdrawals').document(wd_id).update({"status": "approved"})
        await query.edit_message_text(f"✅ Approved Withdrawal for {user_id} (৳{amount})")
        await context.bot.send_message(chat_id=user_id, text=f"✅ আপনার ৳{amount} উইথড্র সফল হয়েছে!")
        
    elif action == "rej":
        db.collection('withdrawals').document(wd_id).update({"status": "rejected"})
        db.collection('users').document(user_id).update({"balance": firestore.Increment(amount)})
        await query.edit_message_text(f"❌ Rejected & Refunded for {user_id} (৳{amount})")
        await context.bot.send_message(chat_id=user_id, text=f"❌ আপনার ৳{amount} উইথড্র বাতিল হয়েছে এবং ব্যালেন্স ফেরত দেওয়া হয়েছে।")

# --- Task Submission System ---

async def start_task_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    config = get_config()
    apps = config.get('monitored_apps', [])
    
    if not apps:
        await query.answer("বর্তমানে কোনো কাজ নেই।", show_alert=True)
        return ConversationHandler.END
        
    buttons = [[InlineKeyboardButton(f"📱 {app['name']} (৳{config['task_price']})", callback_data=f"sel_{app['id']}")] for app in apps]
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel")])
    
    await query.edit_message_text("কোন অ্যাপে কাজ করতে চান সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
    return T_APP_SELECT

async def app_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel": return await cancel_conv(update, context)
    
    context.user_data['tid'] = query.data.split("sel_")[1]
    
    msg = (
        "✍️ **রিভিউ নাম (Review Name)** দিন:\n\n"
        "⚠️ **সতর্কতা:** প্লে-স্টোরে যে নাম দিয়ে রিভিউ দিয়েছেন, হুবহু সেই নাম দিতে হবে। "
        "ভুল নাম দিলে ব্যালেন্স এড হবে না।"
    )
    await query.edit_message_text(msg, parse_mode="Markdown")
    return T_REVIEW_NAME

async def get_review_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['rname'] = update.message.text.strip()
    await update.message.reply_text("আপনার ইমেইল এড্রেস দিন:")
    return T_EMAIL

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text
    await update.message.reply_text("মোবাইল মডেল/ডিভাইস নাম:")
    return T_DEVICE

async def get_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['dev'] = update.message.text
    await update.message.reply_text("স্ক্রিনশট এর লিংক দিন:")
    return T_SS

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    config = get_config()
    
    # Save the task to Firestore
    db.collection('tasks').add({
        "user_id": str(update.effective_user.id),
        "app_id": data['tid'],
        "review_name": data['rname'],
        "email": data['email'],
        "device": data['dev'],
        "screenshot": update.message.text,
        "status": "pending",
        "submitted_at": datetime.now(),
        "price": config['task_price']
    })
    
    await update.message.reply_text("✅ কাজ জমা হয়েছে! রিভিউ ভেরিফাই হলে ব্যালেন্স এড হবে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.callback_query.edit_message_text("❌ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    except:
        await update.message.reply_text("❌ বাতিল করা হয়েছে।")
    return ConversationHandler.END

# ==========================================
# 5. অটোমেশন ও গ্রুপ নোটিফিকেশন
# ==========================================

def approve_task_logic(task_id, user_id, amount):
    task_ref = db.collection('tasks').document(task_id)
    t_data = task_ref.get().to_dict()
    if t_data and t_data['status'] == 'pending':
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
                    # Fetch only top 40 reviews
                    reviews, _ = play_reviews(app['id'], count=40, sort=Sort.NEWEST)
                    
                    for r in reviews:
                        rid = r['reviewId']
                        
                        # New Review Notification Logic
                        if not db.collection('seen_reviews').document(rid).get().exists:
                            r_date = r['at']
                            date_str = r_date.strftime("%d-%m-%Y %I:%M %p")
                            ai_txt = get_ai_summary(r['content'], r['score'])
                            
                            msg = (
                                f"🔔 **নতুন রিভিউ!**\n\n"
                                f"📱 অ্যাপ: `{app['name']}`\n"
                                f"👤 নাম: **{r['userName']}**\n"
                                f"📅 তারিখ: `{date_str}`\n"
                                f"⭐ রেটিং: {r['score']}/5\n"
                                f"💬 কমেন্ট: {r['content']}\n"
                                f"🤖 AI মুড: {ai_txt}"
                            )
                            send_telegram_message(msg)
                            db.collection('seen_reviews').document(rid).set({"t": datetime.now()})

                            # Auto-Approval Logic (Within 48 hours and 5-star)
                            if r_date >= datetime.now() - timedelta(hours=48):
                                p_tasks = db.collection('tasks').where('app_id', '==', app['id']).where('status', '==', 'pending').stream()
                                for t in p_tasks:
                                    td = t.to_dict()
                                    if td['review_name'].lower().strip() == r['userName'].lower().strip():
                                        if r['score'] == 5:
                                            if approve_task_logic(t.id, td['user_id'], td['price']):
                                                send_telegram_message(f"🎉 **Auto Approved!**\nUser: `{td['user_id']}`\nName: {td['review_name']}")
                                        break
                except Exception as e:
                    logger.error(f"App Check Error: {e}")
        except Exception as e:
            logger.error(f"Loop Error: {e}")
        time.sleep(300)

# ==========================================
# 6. এডমিন প্যানেল (নতুন ফিচার সহ)
# ==========================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return

    kb = [
        [InlineKeyboardButton("👥 Users & Balance", callback_data="adm_users"), InlineKeyboardButton("💰 Finance & Bonus", callback_data="adm_finance")],
        [InlineKeyboardButton("✅ View Pending Tasks", callback_data="adm_tasks")], # NEW BUTTON
        [InlineKeyboardButton("📱 Apps Manage", callback_data="adm_apps"), InlineKeyboardButton("🎨 Buttons & Text", callback_data="adm_content")],
        [InlineKeyboardButton("🔙 Back to User Mode", callback_data="back_home")]
    ]
    await query.edit_message_text("⚙️ **Super Admin Panel**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def admin_sub_handlers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "adm_users":
        users = db.collection('users').stream()
        total_u = 0
        total_bal = 0.0
        for u in users:
            total_u += 1
            total_bal += u.to_dict().get('balance', 0)
            
        msg = (
            f"📊 **Statistics**\n\n"
            f"👥 Total Users: `{total_u}`\n"
            f"💰 Total Liability: `৳{total_bal:.2f}`\n\n"
            "Select Action:"
        )
        kb = [[InlineKeyboardButton("🔍 Manage Specific User", callback_data="find_user")],
              [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "adm_finance":
        config = get_config()
        msg = f"💸 **Finance Config**\n\nTask Price: ৳{config['task_price']}\nRefer Bonus: ৳{config['referral_bonus']}\nMin Withdraw: ৳{config['min_withdraw']}"
        kb = [[InlineKeyboardButton("✏️ Change Task Price", callback_data="ed_txt_task_price")],
              [InlineKeyboardButton("✏️ Change Ref Bonus", callback_data="set_ref_bonus")],
              [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_apps":
        kb = [[InlineKeyboardButton("➕ Add App", callback_data="add_app"), InlineKeyboardButton("➖ Remove App", callback_data="rmv_app")],
              [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text("📱 **App Management**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_content":
        kb = [
            [InlineKeyboardButton("📝 Edit Rules Text", callback_data="ed_txt_rules"), InlineKeyboardButton("⏰ Edit Schedule Text", callback_data="ed_txt_schedule")],
            [InlineKeyboardButton("🔘 Button Names/Visibility", callback_data="ed_btns"), InlineKeyboardButton("➕ Add Custom Button", callback_data="add_cus_btn")],
            [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]
        ]
        await query.edit_message_text("🎨 **Content Management**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# --- New: Task Management Handlers ---

async def admin_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    
    # Fetch pending tasks and sort by submission time
    tasks = db.collection('tasks').where('status', '==', 'pending').order_by('submitted_at').limit(20).stream()
    
    task_list = list(tasks)
    
    if not task_list:
        msg = "🎉 কোনো পেন্ডিং কাজ নেই!"
        kb = [[InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        return

    msg = "✅ **পেন্ডিং কাজের তালিকা (সর্বশেষ ২০টি):**\n"
    kb = []
    
    config = get_config()
    app_map = {app['id']: app['name'] for app in config['monitored_apps']}
    
    for t in task_list:
        td = t.to_dict()
        app_name = app_map.get(td['app_id'], 'Unknown App')
        submit_time = td['submitted_at'].strftime("%H:%M:%S")
        
        msg += f"\n- {submit_time}: {app_name} | {td['review_name'][:20]}..."
        kb.append([InlineKeyboardButton(f"👁️‍🗨️ {td['review_name']} ({app_name})", callback_data=f"task_details_{t.id}")])
    
    kb.append([InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")])
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def admin_task_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    
    task_id = query.data.split("task_details_")[1]
    task_doc = db.collection('tasks').document(task_id).get()
    
    if not task_doc.exists:
        await query.answer("❌ টাস্কটি পাওয়া যায়নি বা মুছে ফেলা হয়েছে।", show_alert=True)
        return await admin_task_list(update, context)

    td = task_doc.to_dict()
    config = get_config()
    app_map = {app['id']: app['name'] for app in config['monitored_apps']}
    app_name = app_map.get(td['app_id'], 'Unknown App')
    
    msg = (
        f"📝 **টাস্ক ডিটেইলস (ID: `{task_id}`)**\n\n"
        f"📱 অ্যাপ: `{app_name}`\n"
        f"👤 ইউজার ID: `{td['user_id']}`\n"
        f"💰 পাবে: ৳{td['price']:.2f}\n"
        f"---"
        f"\n**জমা দেওয়া তথ্য:**\n"
        f"✍️ রিভিউ নাম: **{td['review_name']}**\n"
        f"📧 ইমেইল: `{td['email']}`\n"
        f"⚙️ ডিভাইস: `{td['device']}`\n"
        f"🔗 স্ক্রিনশট লিংক: [Click to View]({td['screenshot']})\n"
        f"---"
        f"\n**স্ট্যাটাস:** `{td['status']}`"
    )
    
    kb = [[InlineKeyboardButton("✅ Approve & Pay", callback_data=f"task_apr_t_{task_id}"), 
           InlineKeyboardButton("❌ Reject (Wrong Info)", callback_data=f"task_rej_t_{task_id}")],
          [InlineKeyboardButton("🔙 Task List", callback_data="adm_tasks")]]
          
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def handle_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    
    data = query.data.split('_')
    action = data[1] # apr or rej
    task_id = data[3]
    
    task_doc = db.collection('tasks').document(task_id).get()
    if not task_doc.exists or task_doc.to_dict()['status'] != 'pending':
        await query.answer("Already processed", show_alert=True)
        return await admin_task_list(update, context)

    td = task_doc.to_dict()
    user_id = td['user_id']
    amount = td['price']

    if action == "apr":
        if approve_task_logic(task_id, user_id, amount):
            await query.edit_message_text(f"✅ টাস্ক অ্যাপ্রুভড! (৳{amount} Added to {user_id})")
            await context.bot.send_message(chat_id=user_id, text=f"🎉 আপনার `{td['review_name']}` নামের কাজটি অ্যাপ্রুভ করা হয়েছে! আপনার ব্যালেন্সে ৳{amount} যোগ হয়েছে।")
        else:
            await query.answer("❌ Error during approval.", show_alert=True)
        
    elif action == "rej":
        db.collection('tasks').document(task_id).update({"status": "rejected", "rejected_at": datetime.now()})
        await query.edit_message_text(f"❌ টাস্ক রিজেক্টেড! ({user_id})")
        await context.bot.send_message(chat_id=user_id, text=f"❌ দুঃখিত, আপনার `{td['review_name']}` নামের কাজটি বাতিল করা হয়েছে। ভুল তথ্যের জন্য আবার চেষ্টা করুন।")
        
    await admin_task_list(update, context) # Go back to list after action

# --- End New Task Management Handlers ---

async def find_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🔍 Enter User ID to manage:")
    return ADMIN_USER_SEARCH

async def find_user_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    user = get_user(uid)
    if not user:
        await update.message.reply_text("❌ User not found. Try again or /cancel.")
        return ADMIN_USER_SEARCH
    
    context.user_data['mng_uid'] = uid
    status = "🔴 Blocked" if user.get('is_blocked') else "🟢 Active"
    role = "👑 Admin" if user.get('is_admin') else "👤 User"
    
    msg = (
        f"👤 **User Found**\n"
        f"ID: `{uid}`\nName: {user['name']}\n"
        f"Balance: ৳{user['balance']}\n"
        f"Status: {status} | Role: {role}"
    )
    
    kb = [
        [InlineKeyboardButton("➕ Add Money", callback_data="u_add_bal"), InlineKeyboardButton("➖ Deduct Money", callback_data="u_cut_bal")],
        [InlineKeyboardButton("⛔ Block/Unblock", callback_data="u_toggle_block"), InlineKeyboardButton("👑 Make/Remove Admin", callback_data="u_toggle_admin")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="cancel")]
    ]
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_USER_ACTION

async def user_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = context.user_data['mng_uid']
    
    if data == "cancel": return await cancel_conv(update, context)
    
    if data == "u_toggle_block":
        user = get_user(uid)
        new_stat = not user.get('is_blocked', False)
        db.collection('users').document(uid).update({"is_blocked": new_stat})
        await query.answer(f"User {'Blocked' if new_stat else 'Unblocked'}")
        await query.edit_message_text("✅ Status Updated!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
        return ConversationHandler.END
        
    elif data == "u_toggle_admin":
        user = get_user(uid)
        new_stat = not user.get('is_admin', False)
        db.collection('users').document(uid).update({"is_admin": new_stat})
        await query.answer(f"User is now {'Admin' if new_stat else 'User'}")
        await query.edit_message_text("✅ Role Updated!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
        return ConversationHandler.END
        
    elif data in ["u_add_bal", "u_cut_bal"]:
        context.user_data['bal_action'] = "add" if "add" in data else "cut"
        await query.edit_message_text(f"Enter amount to {'Add' if 'add' in data else 'Deduct'}:")
        return ADMIN_USER_AMOUNT

async def user_balance_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        uid = context.user_data['mng_uid']
        action = context.user_data['bal_action']
        
        final_amt = amount if action == "add" else -amount
        db.collection('users').document(uid).update({"balance": firestore.Increment(final_amt)})
        
        await update.message.reply_text(f"✅ Successfully {'Added' if action=='add' else 'Deducted'} ৳{amount}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    except:
        await update.message.reply_text("❌ Invalid Amount.")
    return ConversationHandler.END

async def edit_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Added 'ed_txt_task_price' to key_map
    key_map = {"ed_txt_rules": "rules_text", "ed_txt_schedule": "schedule_text", "set_ref_bonus": "referral_bonus", "ed_txt_task_price": "task_price"}
    
    key = key_map.get(query.data)
    if not key: return ConversationHandler.END
    
    context.user_data['edit_key'] = key
    await query.edit_message_text(f"📝 Enter new value for {key}:")
    return ADMIN_EDIT_TEXT_VAL

async def edit_text_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    key = context.user_data['edit_key']
    
    if key in ["referral_bonus", "task_price"]:
        try: val = float(val)
        except: 
            await update.message.reply_text("❌ Must be a number")
            return ConversationHandler.END
            
    update_config({key: val})
    await update.message.reply_text("✅ Saved!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def edit_buttons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    config = get_config()
    btns = config.get('buttons', DEFAULT_CONFIG['buttons'])
    
    kb = []
    for key, data in btns.items():
        status = "✅" if data['show'] else "❌"
        kb.append([
            InlineKeyboardButton(f"{status} {data['text']}", callback_data=f"btntog_{key}"),
            InlineKeyboardButton("✏️ Rename", callback_data=f"btnren_{key}")
        ])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_content")])
    
    if query.message.text == "Select Button to Edit:":
        await query.edit_message_reply_markup(InlineKeyboardMarkup(kb))
    else:
        await query.edit_message_text("Select Button to Edit:", reply_markup=InlineKeyboardMarkup(kb))

async def button_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data.startswith("btntog_"):
        key = data.split("_")[1]
        config = get_config()
        curr = config['buttons'][key]['show']
        config['buttons'][key]['show'] = not curr
        update_config({"buttons": config['buttons']})
        await edit_buttons_menu(update, context)
        
    elif data.startswith("btnren_"):
        context.user_data['ren_key'] = data.split("_")[1]
        await query.edit_message_text(f"Enter new name for button:")
        return ADMIN_EDIT_BTN_NAME

async def button_rename_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    key = context.user_data['ren_key']
    config = get_config()
    config['buttons'][key]['text'] = new_name
    update_config({"buttons": config['buttons']})
    await update.message.reply_text("✅ Renamed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def add_custom_btn_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Enter Button Name:")
    return ADMIN_ADD_BTN_NAME

async def add_custom_btn_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_btn_name'] = update.message.text
    await update.message.reply_text("Enter Button Link (URL):")
    return ADMIN_ADD_BTN_LINK

async def add_custom_btn_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    name = context.user_data['c_btn_name']
    
    config = get_config()
    c_btns = config.get('custom_buttons', [])
    c_btns.append({"text": name, "url": link})
    update_config({"custom_buttons": c_btns})
    
    await update.message.reply_text("✅ Button Added!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def add_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("App Package ID (e.g. com.example.app):")
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
    await update.message.reply_text("✅ App Added!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def rmv_app_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config()
    apps = config.get('monitored_apps', [])
    if not apps:
        await update.callback_query.answer("No apps", show_alert=True)
        return ConversationHandler.END
        
    btns = [[InlineKeyboardButton(f"🗑️ {a['name']}", callback_data=f"rm_{i}")] for i, a in enumerate(apps)]
    btns.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await update.callback_query.edit_message_text("Remove which app?", reply_markup=InlineKeyboardMarkup(btns))
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
        await query.edit_message_text("✅ App Removed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    else:
        await query.edit_message_text("❌ Error.")
        
    return ConversationHandler.END

# ==========================================
# 7. মেইন রানার
# ==========================================

app = Flask(__name__)
@app.route('/')
def home(): return "Bot is Alive & Updated!"

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=run_automation, daemon=True).start()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    # Updated: Added adm_tasks
    application.add_handler(CallbackQueryHandler(admin_sub_handlers, pattern="^(adm_users|adm_finance|adm_apps|adm_content)$"))
    
    # New Task Management Handlers
    application.add_handler(CallbackQueryHandler(admin_task_list, pattern="^adm_tasks$"))
    application.add_handler(CallbackQueryHandler(admin_task_details, pattern="^task_details_"))
    application.add_handler(CallbackQueryHandler(handle_task_action, pattern="^task_(apr|rej)_t_"))
    
    application.add_handler(CallbackQueryHandler(edit_buttons_menu, pattern="^ed_btns$"))
    application.add_handler(CallbackQueryHandler(button_action_handler, pattern="^(btntog_|btnren_)"))
    application.add_handler(CallbackQueryHandler(handle_withdrawal_action, pattern="^wd_(apr|rej)_"))

    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_task_submission, pattern="^submit_task$")],
        states={
            T_APP_SELECT: [CallbackQueryHandler(app_selected, pattern="^sel_")],
            T_REVIEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_review_name)],
            T_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            T_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_device)],
            T_SS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_task)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern="^start_withdraw$")],
        states={
            WD_METHOD: [CallbackQueryHandler(withdraw_method)],
            WD_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_number)],
            WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel")]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_app_start, pattern="^add_app$")],
        states={
            ADD_APP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app_id)],
            ADD_APP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app_name)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_app_start, pattern="^rmv_app$")],
        states={REMOVE_APP_SELECT: [CallbackQueryHandler(rmv_app_sel)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(find_user_start, pattern="^find_user$")],
        states={
            ADMIN_USER_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, find_user_result)],
            ADMIN_USER_ACTION: [CallbackQueryHandler(user_action_handler)],
            ADMIN_USER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_balance_update)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_text_start, pattern="^(ed_txt_|set_ref)"),
            CallbackQueryHandler(edit_buttons_menu, pattern="^btnren_")
        ],
        states={
            ADMIN_EDIT_TEXT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_text_save)],
            ADMIN_EDIT_BTN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, button_rename_save)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_custom_btn_start, pattern="^add_cus_btn$")],
        states={
            ADMIN_ADD_BTN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_custom_btn_link)],
            ADMIN_ADD_BTN_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_custom_btn_save)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    application.add_handler(CallbackQueryHandler(common_callback, pattern="^(my_profile|refer_friend|back_home|show_schedule)$"))

    print("🚀 Bot Started on Render...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
