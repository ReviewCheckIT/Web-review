import os
import json
import logging
import threading
import time
import asyncio
import csv
import io
from datetime import datetime, time as dt_time
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
    "log_channel_id": "",
    "work_start_time": "10:00", # 24H Format
    "work_end_time": "22:00",   # 24H Format
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

# Conversation States
(
    T_APP_SELECT, T_REVIEW_NAME, T_EMAIL, T_DEVICE, T_SS,           # 1-5
    ADD_APP_ID, ADD_APP_NAME,                                       # 6-7
    WD_METHOD, WD_NUMBER, WD_AMOUNT,                                # 8-10
    REMOVE_APP_SELECT,                                              # 11
    ADMIN_USER_SEARCH, ADMIN_USER_ACTION, ADMIN_USER_AMOUNT,        # 12-14
    ADMIN_EDIT_TEXT_KEY, ADMIN_EDIT_TEXT_VAL,                       # 15-16
    ADMIN_EDIT_BTN_KEY, ADMIN_EDIT_BTN_NAME,                        # 17-18
    ADMIN_ADD_BTN_NAME, ADMIN_ADD_BTN_LINK,                         # 19-20
    ADMIN_SET_LOG_CHANNEL,                                          # 21
    ADMIN_ADD_ADMIN_ID, ADMIN_RMV_ADMIN_ID,                         # 22-23
    ADMIN_SET_START_TIME, ADMIN_SET_END_TIME                        # 24-25 (New for Time Settings)
) = range(26)

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

def is_working_hour():
    config = get_config()
    start_str = config.get("work_start_time", "10:00")
    end_str = config.get("work_end_time", "22:00")
    
    try:
        now = datetime.now().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        
        if start < end:
            return start <= now <= end
        else: # Crosses midnight (e.g. 10 PM to 2 AM)
            return now >= start or now <= end
    except:
        return True # Fallback if time format error

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

async def send_log_message(context, text, reply_markup=None):
    config = get_config()
    chat_id = config.get('log_channel_id')
    target_id = chat_id if chat_id else OWNER_ID
    if target_id:
        try:
            await context.bot.send_message(chat_id=target_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Log Send Error: {e}")

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
        if btn.get('text') and btn.get('url'):
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
        if user:
            msg = f"👤 **প্রোফাইল**\n\n🆔 ID: `{user['id']}`\n💰 ব্যালেন্স: ৳{user['balance']:.2f}\n✅ সম্পন্ন টাস্ক: {user['total_tasks']}"
        else:
            msg = "👤 **প্রোফাইল**\n\nডেটা লোড করা যায়নি। আবার /start দিন।"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        
    elif query.data == "refer_friend":
        config = get_config()
        link = f"https://t.me/{context.bot.username}?start={query.from_user.id}"
        await query.edit_message_text(f"📢 **রেফার লিংক:**\n`{link}`\n\nপ্রতি রেফারে বোনাস: ৳{config['referral_bonus']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
    
    elif query.data == "show_schedule":
        config = get_config()
        s_time = datetime.strptime(config.get('work_start_time', '10:00'), "%H:%M").strftime("%I:%M %p")
        e_time = datetime.strptime(config.get('work_end_time', '22:00'), "%H:%M").strftime("%I:%M %p")
        
        msg = (
            f"📅 **সময়সূচী:**\n\n"
            f"{config.get('schedule_text', '')}\n\n"
            f"🕒 **কাজ জমা দেওয়ার সময়:**\n"
            f"শুরু: `{s_time}`\n"
            f"শেষ: `{e_time}`"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))

# --- Withdrawal System ---

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = get_user(query.from_user.id)
    config = get_config()
    
    if user['balance'] < config['min_withdraw']:
        await query.edit_message_text(f"❌ উইথড্র বাতিল। সর্বনিম্ন উইথড্র অ্যামাউন্ট: ৳{config['min_withdraw']:.2f}", 
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
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
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    config = get_config()
    
    try:
        amount = float(update.message.text)
        
        if amount < config['min_withdraw']:
             await update.message.reply_text(f"❌ সর্বনিম্ন উইথড্র ৳{config['min_withdraw']:.2f}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
             return ConversationHandler.END

        if amount > user['balance']:
            await update.message.reply_text("❌ আপনার একাউন্টে পর্যাপ্ত ব্যালেন্স নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
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
            f"👤 User: `{user_id}` ({update.effective_user.first_name})\n"
            f"💰 Amount: ৳{amount:.2f}\n"
            f"📱 Method: {context.user_data['wd_method']} ({context.user_data['wd_number']})\n"
            f"🔢 Balance Left: ৳{user['balance'] - amount:.2f}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"wd_apr_{wd_id}_{user_id}"), 
             InlineKeyboardButton("❌ Reject", callback_data=f"wd_rej_{wd_id}_{user_id}")]
        ])
        
        await send_log_message(context, admin_msg, kb)
        await update.message.reply_text("✅ উইথড্র রিকোয়েস্ট সফল হয়েছে! এডমিন চেক করে পেমেন্ট করবে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        
    except ValueError:
        await update.message.reply_text("❌ ভুল ইনপুট। শুধু সংখ্যা ব্যবহার করুন।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    except Exception as e:
        logger.error(f"Withdraw Error: {e}")
        await update.message.reply_text("❌ সমস্যা হয়েছে। পরে চেষ্টা করুন।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        
    return ConversationHandler.END

async def handle_withdrawal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⚠️ Only Admins can do this!", show_alert=True)
        return
    
    data = query.data.split('_')
    action = data[1]
    wd_id = data[2]
    user_id = data[3]
    
    wd_doc = db.collection('withdrawals').document(wd_id).get()
    if not wd_doc.exists:
        await query.answer("Withdrawal request not found.", show_alert=True)
        return
    
    wd_data = wd_doc.to_dict()
    if wd_data['status'] != 'pending':
        await query.answer(f"Already processed ({wd_data['status']})", show_alert=True)
        await query.edit_message_reply_markup(None)
        return

    amount = wd_data['amount']

    if action == "apr":
        db.collection('withdrawals').document(wd_id).update({"status": "approved", "processed_by": query.from_user.id})
        await query.edit_message_text(f"✅ Approved Withdrawal for `{user_id}` (৳{amount:.2f})\nBy: {query.from_user.first_name}", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"✅ আপনার ৳{amount:.2f} উইথড্র সফল হয়েছে!")
        
    elif action == "rej":
        db.collection('withdrawals').document(wd_id).update({"status": "rejected", "processed_by": query.from_user.id})
        db.collection('users').document(user_id).update({"balance": firestore.Increment(amount)})
        await query.edit_message_text(f"❌ Rejected & Refunded for `{user_id}` (৳{amount:.2f})\nBy: {query.from_user.first_name}", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"❌ আপনার ৳{amount:.2f} উইথড্র বাতিল হয়েছে এবং ব্যালেন্স ফেরত দেওয়া হয়েছে।")

# --- Task Submission System ---

async def start_task_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    config = get_config()
    
    # --- TIME CHECK START ---
    if not is_working_hour():
        s_time = datetime.strptime(config.get('work_start_time', '10:00'), "%H:%M").strftime("%I:%M %p")
        e_time = datetime.strptime(config.get('work_end_time', '22:00'), "%H:%M").strftime("%I:%M %p")
        
        await query.edit_message_text(
            f"⛔ **এখন কাজের সময় নয়!**\n\n"
            f"⏰ কাজের সময়: `{s_time}` থেকে `{e_time}` পর্যন্ত।\n"
            f"অনুগ্রহ করে নির্দিষ্ট সময়ে চেষ্টা করুন।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]])
        )
        return ConversationHandler.END
    # --- TIME CHECK END ---

    apps = config.get('monitored_apps', [])
    
    if not apps:
        await query.edit_message_text("❌ বর্তমানে কোনো কাজ নেই।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_home")]]))
        return ConversationHandler.END
        
    buttons = [[InlineKeyboardButton(f"📱 {app['name']} (৳{config['task_price']:.2f})", callback_data=f"sel_{app['id']}")] for app in apps]
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
    await update.message.reply_text("স্ক্রিনশট এর লিংক বা টেক্সট দিন:")
    return T_SS

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    config = get_config()
    user = update.effective_user
    
    app_name = next((a['name'] for a in config['monitored_apps'] if a['id'] == data['tid']), data['tid'])
    
    task_ref = db.collection('tasks').add({
        "user_id": str(user.id),
        "app_id": data['tid'],
        "review_name": data['rname'],
        "email": data['email'],
        "device": data['dev'],
        "screenshot": update.message.text,
        "status": "pending",
        "submitted_at": datetime.now(),
        "price": config['task_price']
    })
    
    task_id = task_ref[1].id
    
    log_msg = (
        f"📝 **New Task Submitted**\n"
        f"👤 User: `{user.id}` ({user.first_name})\n"
        f"📱 App: **{app_name}**\n"
        f"✍️ Name: {data['rname']}\n"
        f"📧 Email: {data['email']}\n"
        f"📱 Device: {data['dev']}\n"
        f"🖼 Proof: [Link/Text]({update.message.text})\n"
        f"💰 Price: ৳{config['task_price']:.2f}"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"t_apr_{task_id}_{user.id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"t_rej_{task_id}_{user.id}")]
    ])
    
    await send_log_message(context, log_msg, kb)
    await update.message.reply_text("✅ কাজ জমা হয়েছে! এডমিন চেক করে এপ্রুভ করবেন অথবা অটোমেটিক এপ্রুভ হবে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
        else:
            await update.message.reply_text("❌ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="back_home")]]))
    except:
         try: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ বাতিল করা হয়েছে।")
         except: pass
    return ConversationHandler.END

async def handle_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⚠️ Only Admins can do this!", show_alert=True)
        return

    data = query.data.split('_')
    action = data[1]
    task_id = data[2]
    user_id = data[3]
    
    task_ref = db.collection('tasks').document(task_id)
    task_doc = task_ref.get()
    
    if not task_doc.exists:
        await query.answer("Task not found", show_alert=True)
        return
        
    t_data = task_doc.to_dict()
    if t_data['status'] != 'pending':
        await query.answer(f"Task is already {t_data['status']}", show_alert=True)
        await query.edit_message_reply_markup(None)
        return

    price = t_data.get('price', 0)
    
    if action == "apr":
        approve_task(task_id, user_id, price)
        await query.edit_message_text(f"✅ Task Approved Manually\nUser: `{user_id}` (৳{price:.2f})\nBy: {query.from_user.first_name}", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text=f"🎉 আপনার কাজটি এপ্রুভ হয়েছে! ৳{price:.2f} যোগ হয়েছে।")
        
    elif action == "rej":
        task_ref.update({"status": "rejected", "processed_by": query.from_user.id})
        await query.edit_message_text(f"❌ Task Rejected Manually\nUser: `{user_id}`\nBy: {query.from_user.first_name}", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text="❌ আপনার কাজটি রিজেক্ট করা হয়েছে। সঠিক তথ্য দিয়ে আবার চেষ্টা করুন।")

# ==========================================
# 5. অটোমেশন ও গ্রুপ নোটিফিকেশন
# ==========================================

def approve_task(task_id, user_id, amount):
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
            log_id = config.get('log_channel_id', OWNER_ID)
            
            for app in apps:
                try:
                    reviews, _ = play_reviews(app['id'], count=10, sort=Sort.NEWEST)
                    for r in reviews:
                        rid = r['reviewId']
                        r_date = r['at']
                        if r_date < datetime.now() - timedelta(hours=48):
                            continue
                        
                        if not db.collection('seen_reviews').document(rid).get().exists:
                            date_str = r_date.strftime("%d-%m-%Y %I:%M %p")
                            ai_txt = get_ai_summary(r['content'], r['score'])
                            
                            msg = (
                                f"🔔 **Play Store Review Found**\n"
                                f"📱 App: `{app['name']}`\n"
                                f"👤 Name: **{r['userName']}**\n"
                                f"📅 Date: `{date_str}`\n"
                                f"⭐ Rating: {r['score']}/5\n"
                                f"💬 Comment: {r['content']}\n"
                                f"🤖 AI Mood: {ai_txt}"
                            )
                            send_telegram_message(msg, chat_id=log_id)
                            db.collection('seen_reviews').document(rid).set({"t": datetime.now()})

                            if r['score'] == 5:
                                p_tasks = db.collection('tasks').where('app_id', '==', app['id']).where('status', '==', 'pending').stream()
                                for t in p_tasks:
                                    td = t.to_dict()
                                    if td['review_name'].lower().strip() == r['userName'].lower().strip():
                                        price = td.get('price', 0)
                                        if approve_task(t.id, td['user_id'], price):
                                            send_telegram_message(
                                                f"🤖 **Auto Approved!**\nUser: `{td['user_id']}`\nApp: {app['name']}\nName: {td['review_name']}", 
                                                chat_id=log_id
                                            )
                                            send_telegram_message(
                                                f"🎉 আপনার কাজটি **অটোমেটিক এপ্রুভ** হয়েছে! ৳{price:.2f} যোগ হয়েছে।", 
                                                chat_id=td['user_id']
                                            )
                                        break
                except Exception as e:
                    logger.error(f"App Check Error: {e}")
        except Exception as e:
            logger.error(f"Loop Error: {e}")
        time.sleep(300)

def send_telegram_message(message, chat_id=None, reply_markup=None):
    if not chat_id: return
    try:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        if reply_markup:
            if hasattr(reply_markup, 'to_dict'):
                 payload["reply_markup"] = reply_markup.to_dict()
            else:
                 payload["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram Send Error: {e}")

# ==========================================
# 6. এডমিন প্যানেল
# ==========================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return

    kb = [
        [InlineKeyboardButton("👥 Users & Balance", callback_data="adm_users"), InlineKeyboardButton("💰 Finance & Bonus", callback_data="adm_finance")],
        [InlineKeyboardButton("📱 Apps Manage", callback_data="adm_apps"), InlineKeyboardButton("👮 Manage Admins", callback_data="adm_admins")],
        [InlineKeyboardButton("🎨 Buttons & Time", callback_data="adm_content"), InlineKeyboardButton("📢 Log Channel", callback_data="adm_log")],
        [InlineKeyboardButton("📊 Reports & Export", callback_data="adm_reports")],
        [InlineKeyboardButton("🔙 Back to User Mode", callback_data="back_home")]
    ]
    await query.edit_message_text("⚙️ **Super Admin Panel**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# --- REPORT HANDLING FUNCTIONS (UPDATED) ---

async def admin_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return
    
    msg = (
        "📊 **Reports & Export**\n\n"
        "Download Approved Tasks data as **CSV/Spreadsheet**.\n"
        "You can share this file with buyers as proof."
    )
    
    kb = [
        [InlineKeyboardButton("📜 All Time History", callback_data="rep_all")],
        [InlineKeyboardButton("📅 Last 7 Days", callback_data="rep_7d")],
        [InlineKeyboardButton("🕒 Last 24 Hours", callback_data="rep_24h")],
        [InlineKeyboardButton("📱 By Specific App", callback_data="rep_apps")], # NEW
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ]
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def admin_reports_apps_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    config = get_config()
    apps = config.get('monitored_apps', [])
    
    if not apps:
        await query.answer("No apps found!", show_alert=True)
        return
        
    kb = []
    for app in apps:
        kb.append([InlineKeyboardButton(f"📄 Report: {app['name']}", callback_data=f"rep_app_{app['id']}")])
        
    kb.append([InlineKeyboardButton("🔙 Back to Reports", callback_data="adm_reports")])
    await query.edit_message_text("📊 Select App to download report:", reply_markup=InlineKeyboardMarkup(kb))

async def export_report_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Generating report... Please wait.")
    
    mode = query.data
    now = datetime.now()
    cutoff_date = None
    target_app_id = None
    file_prefix = "All_Time"
    
    if mode == "rep_7d":
        cutoff_date = now - timedelta(days=7)
        file_prefix = "Last_7_Days"
    elif mode == "rep_24h":
        cutoff_date = now - timedelta(hours=24)
        file_prefix = "Last_24_Hours"
    elif mode.startswith("rep_app_"):
        target_app_id = mode.split("rep_app_")[1]
        file_prefix = f"App_{target_app_id}"
        
    # Fetch Approved Tasks
    if target_app_id:
        tasks_ref = db.collection('tasks').where('status', '==', 'approved').where('app_id', '==', target_app_id).stream()
    else:
        tasks_ref = db.collection('tasks').where('status', '==', 'approved').stream()
    
    # Prepare Data List
    data_rows = []
    
    for t in tasks_ref:
        t_data = t.to_dict()
        approved_at = t_data.get('approved_at')
        
        if approved_at:
            if cutoff_date:
                # Handle timezone naive comparison
                if approved_at.replace(tzinfo=None) < cutoff_date.replace(tzinfo=None):
                    continue
            date_str = approved_at.strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_str = "N/A"
            if cutoff_date: continue

        data_rows.append([
            t.id,
            t_data.get('user_id', ''),
            t_data.get('app_id', ''),
            t_data.get('review_name', ''),
            t_data.get('email', ''),
            t_data.get('device', ''),
            t_data.get('screenshot', ''),
            t_data.get('price', 0),
            date_str
        ])
    
    if not data_rows:
        await query.message.reply_text("❌ No data found for this selection.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Task ID", "User ID", "App ID", "Review Name", "Email", "Device", "Screenshot Proof", "Price", "Approved Date"])
    writer.writerows(data_rows)
    
    output.seek(0)
    byte_output = io.BytesIO(output.getvalue().encode('utf-8'))
    
    filename = f"Report_{file_prefix}_{now.strftime('%Y%m%d')}.csv"
    
    caption_msg = (
        f"📊 **Export: {file_prefix}**\n"
        f"✅ Total Rows: {len(data_rows)}\n"
        f"📅 Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    await context.bot.send_document(
        chat_id=query.from_user.id,
        document=byte_output,
        filename=filename,
        caption=caption_msg,
        parse_mode="Markdown"
    )

# ----------------------------------------

async def admin_sub_handlers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
            f"💰 Total Liability (User Balances): `৳{total_bal:.2f}`\n\n"
            "Select Action:"
        )
        kb = [[InlineKeyboardButton("🔍 Manage Specific User", callback_data="find_user")],
              [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "adm_finance":
        config = get_config()
        msg = (
            f"💸 **Finance Config**\n\n"
            f"Current Refer Bonus: ৳{config['referral_bonus']:.2f}\n"
            f"Min Withdraw: ৳{config['min_withdraw']:.2f}"
        )
        kb = [[InlineKeyboardButton("✏️ Change Ref Bonus", callback_data="ed_txt_referral_bonus")],
              [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_apps":
        config = get_config()
        apps_list = "\n".join([f"- {a['name']} (`{a['id']}`)" for a in config['monitored_apps']]) if config['monitored_apps'] else "No apps added."
        msg = f"📱 **App Management**\n\n**Current Apps:**\n{apps_list}"
        kb = [[InlineKeyboardButton("➕ Add App", callback_data="add_app"), InlineKeyboardButton("➖ Remove App", callback_data="rmv_app")],
              [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_content":
        config = get_config()
        st = config.get("work_start_time", "10:00")
        et = config.get("work_end_time", "22:00")
        
        kb = [
            [InlineKeyboardButton(f"⏰ Start: {st}", callback_data="set_time_start"), InlineKeyboardButton(f"⏰ End: {et}", callback_data="set_time_end")],
            [InlineKeyboardButton("📝 Edit Rules Text", callback_data="ed_txt_rules"), InlineKeyboardButton("⏰ Edit Schedule Text", callback_data="ed_txt_schedule")],
            [InlineKeyboardButton("🔘 Button Names/Visibility", callback_data="ed_btns"), InlineKeyboardButton("➕ Add Custom Button", callback_data="add_cus_btn")],
            [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]
        ]
        await query.edit_message_text("🎨 **Content & Time Settings**\nSet Working Hours (24H Format)", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "adm_admins":
        kb = [
            [InlineKeyboardButton("➕ Add New Admin", callback_data="add_new_admin")],
            [InlineKeyboardButton("➖ Remove Admin", callback_data="rmv_admin_role")],
            [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]
        ]
        await query.edit_message_text("👮 **Admin Management**\nAdd or Remove admins by Telegram ID.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "adm_log":
        config = get_config()
        curr_log = config.get('log_channel_id', 'Not Set')
        msg = (
            f"📢 **Log Channel Configuration**\n\n"
            f"Current ID: `{curr_log}`\n\n"
            "All Tasks and Withdrawals will be sent to this group/channel."
            " Make sure the Bot is an Admin there!"
        )
        kb = [[InlineKeyboardButton("✏️ Set Channel ID", callback_data="set_log_id")],
              [InlineKeyboardButton("🔙 Admin Home", callback_data="admin_panel")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

# --- Admin Management Functions ---

async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🆔 Enter the Telegram User ID to make Admin:")
    return ADMIN_ADD_ADMIN_ID

async def add_admin_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    if not uid.isdigit():
        await update.message.reply_text("❌ ID must be numeric.")
        return ConversationHandler.END
        
    db.collection('users').document(uid).set({"is_admin": True}, merge=True)
    await update.message.reply_text(f"✅ User `{uid}` is now an Admin!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def rmv_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🆔 Enter the Telegram User ID to Remove from Admin:")
    return ADMIN_RMV_ADMIN_ID

async def rmv_admin_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    if uid == str(OWNER_ID):
        await update.message.reply_text("❌ Cannot remove Owner.")
        return ConversationHandler.END
        
    user_ref = db.collection('users').document(uid)
    if user_ref.get().exists:
        user_ref.update({"is_admin": False})
        await update.message.reply_text(f"✅ User `{uid}` removed from Admin.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    else:
        db.collection('users').document(uid).set({"is_admin": False, "id": uid, "name": "Unknown"}, merge=True)
        await update.message.reply_text(f"✅ User `{uid}` removed from Admin.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))

    return ConversationHandler.END

# --- Log Channel Config ---

async def set_log_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("📢 Enter Group/Channel ID (e.g. -100123456789):")
    return ADMIN_SET_LOG_CHANNEL

async def set_log_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.message.text.strip()
    update_config({"log_channel_id": cid})
    await update.message.reply_text(f"✅ Log Channel Set to `{cid}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

# --- TIME SETTING HANDLERS ---

async def set_time_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("⏰ Enter START Time (24 Hour Format, e.g., 10:00 or 08:30):")
    return ADMIN_SET_START_TIME

async def set_time_start_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t_str = update.message.text.strip()
    try:
        datetime.strptime(t_str, "%H:%M")
        update_config({"work_start_time": t_str})
        await update.message.reply_text(f"✅ Start Time set to {t_str}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    except ValueError:
        await update.message.reply_text("❌ Invalid Format! Use HH:MM (e.g. 10:00).")
    return ConversationHandler.END

async def set_time_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("⏰ Enter END Time (24 Hour Format, e.g., 22:00 or 20:30):")
    return ADMIN_SET_END_TIME

async def set_time_end_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t_str = update.message.text.strip()
    try:
        datetime.strptime(t_str, "%H:%M")
        update_config({"work_end_time": t_str})
        await update.message.reply_text(f"✅ End Time set to {t_str}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    except ValueError:
        await update.message.reply_text("❌ Invalid Format! Use HH:MM (e.g. 22:00).")
    return ConversationHandler.END


# --- Existing Admin Functions ---

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
        f"ID: `{uid}`\nName: {user.get('name', 'N/A')}\n"
        f"Balance: ৳{user.get('balance', 0):.2f}\n"
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
    await query.answer()
    data = query.data
    uid = context.user_data['mng_uid']
    
    if data == "cancel": return await cancel_conv(update, context)
    
    if data == "u_toggle_block":
        user = get_user(uid)
        new_stat = not user.get('is_blocked', False)
        db.collection('users').document(uid).update({"is_blocked": new_stat})
        await query.edit_message_text(f"✅ User {'Blocked' if new_stat else 'Unblocked'}!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
        return ConversationHandler.END
        
    elif data == "u_toggle_admin":
        if uid == str(OWNER_ID):
            await query.answer("Cannot change owner role", show_alert=True)
            return
        user = get_user(uid)
        new_stat = not user.get('is_admin', False)
        db.collection('users').document(uid).update({"is_admin": new_stat})
        await query.edit_message_text(f"✅ User role changed to {'Admin' if new_stat else 'User'}!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
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
        
        await update.message.reply_text(f"✅ Successfully {'Added' if action=='add' else 'Deduct'} ৳{amount:.2f}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    except:
        await update.message.reply_text("❌ Invalid Amount.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def edit_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key_map = {"ed_txt_rules": "rules_text", "ed_txt_schedule": "schedule_text", "ed_txt_referral_bonus": "referral_bonus"}
    
    key = key_map.get(query.data)
    if not key: return ConversationHandler.END
    
    context.user_data['edit_key'] = key
    
    config = get_config()
    curr_val = config.get(key, "N/A")
    
    await query.edit_message_text(f"📝 **Editing {key}**\nCurrent Value: `{curr_val}`\n\nEnter new value:")
    return ADMIN_EDIT_TEXT_VAL

async def edit_text_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    key = context.user_data['edit_key']
    
    if key in ["referral_bonus", "min_withdraw"]:
        try: val = float(val)
        except: 
            await update.message.reply_text("❌ Must be a number")
            return ConversationHandler.END
            
    update_config({key: val})
    await update.message.reply_text("✅ Saved!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
    return ConversationHandler.END

async def edit_buttons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
    
    await query.edit_message_text("Select Button to Edit:", reply_markup=InlineKeyboardMarkup(kb))

async def button_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
    
    try:
        idx = int(query.data.split("rm_")[1])
        config = get_config()
        apps = config.get('monitored_apps', [])
        
        if 0 <= idx < len(apps):
            del apps[idx]
            update_config({"monitored_apps": apps})
            await query.edit_message_text("✅ App Removed!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]))
        else:
            await query.edit_message_text("❌ Error: Invalid selection index.")
    except:
        await query.edit_message_text("❌ Error during removal.")
        
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
    
    application.add_handler(CallbackQueryHandler(admin_sub_handlers, pattern="^(adm_users|adm_finance|adm_apps|adm_content|adm_admins|adm_log)$"))
    
    # --- HANDLERS FOR REPORTS ---
    application.add_handler(CallbackQueryHandler(admin_reports_menu, pattern="^adm_reports$"))
    application.add_handler(CallbackQueryHandler(admin_reports_apps_selection, pattern="^rep_apps$")) # NEW
    application.add_handler(CallbackQueryHandler(export_report_data, pattern="^(rep_all|rep_7d|rep_24h|rep_app_.*)$")) # UPDATED
    # --------------------------------

    application.add_handler(CallbackQueryHandler(edit_buttons_menu, pattern="^ed_btns$"))
    application.add_handler(CallbackQueryHandler(button_action_handler, pattern="^(btntog_|btnren_)"))
    
    application.add_handler(CallbackQueryHandler(handle_withdrawal_action, pattern="^wd_(apr|rej)_"))
    application.add_handler(CallbackQueryHandler(handle_task_action, pattern="^t_(apr|rej)_"))

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
            WD_METHOD: [CallbackQueryHandler(withdraw_method, pattern="^m_(bkash|nagad)$|^cancel$")],
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
            ADMIN_USER_ACTION: [CallbackQueryHandler(user_action_handler, pattern="^(u_add_bal|u_cut_bal|u_toggle_block|u_toggle_admin)$|^cancel$")],
            ADMIN_USER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_balance_update)]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_text_start, pattern="^(ed_txt_rules|ed_txt_schedule|ed_txt_referral_bonus)$"),
            CallbackQueryHandler(button_action_handler, pattern="^btnren_")
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

    # --- TIME SETTING CONVERSATIONS ---
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_time_start_handler, pattern="^set_time_start$")],
        states={ADMIN_SET_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_start_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_time_end_handler, pattern="^set_time_end$")],
        states={ADMIN_SET_END_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_end_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    # ----------------------------------

    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_admin_start, pattern="^add_new_admin$")],
        states={ADMIN_ADD_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rmv_admin_start, pattern="^rmv_admin_role$")],
        states={ADMIN_RMV_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, rmv_admin_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_log_start, pattern="^set_log_id$")],
        states={ADMIN_SET_LOG_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_log_save)]},
        fallbacks=[CallbackQueryHandler(cancel_conv)]
    ))

    application.add_handler(CallbackQueryHandler(common_callback, pattern="^(my_profile|refer_friend|back_home|show_schedule)$"))

    print("🚀 Bot Started on Render...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
