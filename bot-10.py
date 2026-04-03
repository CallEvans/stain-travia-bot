"""
Stain Trivia Bot
================
Private chat  — admin registers their group, checks uptime, gets support
Group chat    — auto-drops trivia every 2 hours, /ans to answer, /leaderboard

Join gate     — users must join @stainprojectss before using the bot
Points reset  — every 72 hours automatically
Questions     — Open Trivia Database (no API key needed)

Env vars (set in Render):
  BOT_TOKEN           - from @BotFather
  RENDER_EXTERNAL_URL - auto set by Render
  PORT                - auto set by Render
"""

import asyncio
import html
import json
import logging
import os
import random
import string
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import aiohttp
from flask import Flask, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================================================================
#  CONFIG
# =============================================================================

BOT_TOKEN        = os.environ["BOT_TOKEN"]
WEBHOOK_URL      = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT             = int(os.environ.get("PORT", 10000))
CHANNEL_USERNAME = "stainprojectss"
CHANNEL_LINK     = "https://t.me/stainprojectss"

DATA_FILE        = "data.json"           # persists group permissions + scores
QUESTION_INTERVAL = 2 * 60 * 60         # 2 hours in seconds
POINTS_RESET_INTERVAL = 72 * 60 * 60    # 72 hours in seconds
POINTS_PER_CORRECT = 10
OPENTDB_URL      = "https://opentdb.com/api.php"

START_TIME       = time.time()           # for /ping uptime

# =============================================================================
#  LOGGING
# =============================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =============================================================================
#  DATA LAYER
# =============================================================================
# Schema:
# {
#   "groups": {
#     "<group_id>": {
#       "added_by": <user_id>,
#       "added_at": <timestamp>,
#       "scores": { "<user_id>": { "name": str, "points": int } },
#       "last_reset": <timestamp>,
#       "last_question_at": <timestamp>,
#       "active_question": {
#         "question": str, "answer": str, "options": [...], "asked_at": float
#       } | null
#     }
#   },
#   "pending_group": { "<user_id>": true }   # waiting for group ID input
# }

def _load() -> dict:
    if Path(DATA_FILE).exists():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"groups": {}, "pending_group": {}}


def _save(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


data = _load()


def get_group(group_id: int) -> dict | None:
    return data["groups"].get(str(group_id))


def is_registered(group_id: int) -> bool:
    return str(group_id) in data["groups"]


def register_group(group_id: int, user_id: int):
    gid = str(group_id)
    if gid not in data["groups"]:
        data["groups"][gid] = {
            "added_by":         user_id,
            "added_at":         time.time(),
            "scores":           {},
            "last_reset":       time.time(),
            "last_question_at": 0,
            "active_question":  None,
        }
    _save(data)


def add_score(group_id: int, user_id: int, name: str):
    gid = str(group_id)
    uid = str(user_id)
    g   = data["groups"][gid]
    if uid not in g["scores"]:
        g["scores"][uid] = {"name": name, "points": 0}
    g["scores"][uid]["name"]    = name
    g["scores"][uid]["points"] += POINTS_PER_CORRECT
    _save(data)


def set_active_question(group_id: int, q: dict | None):
    data["groups"][str(group_id)]["active_question"] = q
    if q:
        data["groups"][str(group_id)]["last_question_at"] = time.time()
    _save(data)


def reset_scores_if_due(group_id: int) -> bool:
    """Reset scores if 72h have passed. Returns True if reset happened."""
    g = data["groups"][str(group_id)]
    if time.time() - g.get("last_reset", 0) >= POINTS_RESET_INTERVAL:
        g["scores"]     = {}
        g["last_reset"] = time.time()
        _save(data)
        return True
    return False


def time_until_reset(group_id: int) -> str:
    g        = data["groups"][str(group_id)]
    elapsed  = time.time() - g.get("last_reset", 0)
    remaining = max(0, POINTS_RESET_INTERVAL - elapsed)
    h, rem   = divmod(int(remaining), 3600)
    m, s     = divmod(rem, 60)
    return str(h) + "h " + str(m) + "m " + str(s) + "s"


# =============================================================================
#  OPEN TRIVIA DB
# =============================================================================

_q_cache: list[dict] = []


async def _fetch_questions(amount: int = 20) -> list[dict]:
    params = {"amount": amount, "type": "multiple", "encode": "url3986"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                OPENTDB_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return []
                d = await r.json()
                if d.get("response_code") != 0:
                    return []
                return [_normalise(q) for q in d["results"]]
    except Exception as e:
        logger.warning("OpenTDB fetch error: %s", e)
        return []


def _normalise(raw: dict) -> dict:
    def dec(s):
        return html.unescape(unquote(s))
    correct  = dec(raw["correct_answer"])
    options  = [dec(a) for a in raw["incorrect_answers"]] + [correct]
    random.shuffle(options)
    return {
        "question": dec(raw["question"]),
        "answer":   correct,
        "options":  options,
        "category": dec(raw.get("category", "")),
        "difficulty": raw.get("difficulty", ""),
    }


async def get_question() -> dict | None:
    global _q_cache
    if len(_q_cache) < 3:
        fresh = await _fetch_questions(20)
        if fresh:
            _q_cache.extend(fresh)
    if _q_cache:
        return _q_cache.pop()
    return None


# =============================================================================
#  FLASK
# =============================================================================

flask_app = Flask(__name__)
ptb_app: Application = None


@flask_app.get("/")
def health():
    return jsonify({"status": "ok", "bot": "stain-trivia"}), 200


@flask_app.post("/webhook")
def webhook():
    d = request.get_json(force=True)
    asyncio.run_coroutine_threadsafe(
        ptb_app.update_queue.put(Update.de_json(d, ptb_app.bot)),
        ptb_app.bot_data["event_loop"],
    )
    return "ok", 200


# =============================================================================
#  JOIN GATE
# =============================================================================

def _join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Stain Projects", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ I have joined — verify me", callback_data="verify_join")],
    ])


def _menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Commands", callback_data="show_commands"),
            InlineKeyboardButton("🆘 Support",  callback_data="show_support"),
        ],
    ])


async def _is_member(user_id: int, bot) -> bool:
    try:
        member = await bot.get_chat_member("@" + CHANNEL_USERNAME, user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logger.warning("Membership check error: %s", e)
        return True  # fail open


async def _gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user can proceed. Only applies to private chats."""
    if update.effective_chat.type != "private":
        return True  # no gate in groups
    user = update.effective_user
    if await _is_member(user.id, context.bot):
        return True
    text = (
        "👋 Hello *" + user.first_name + "*!\n\n"
        "To use this bot you must join *Stain Projects* first.\n\n"
        "1️⃣ Tap *Join* below\n"
        "2️⃣ Come back and tap *I have joined*"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_join_keyboard())
    return False


# =============================================================================
#  UPTIME
# =============================================================================

def _uptime_str() -> str:
    elapsed = int(time.time() - START_TIME)
    d, rem  = divmod(elapsed, 86400)
    h, rem  = divmod(rem, 3600)
    m, s    = divmod(rem, 60)
    parts   = []
    if d: parts.append(str(d) + "d")
    if h: parts.append(str(h) + "h")
    if m: parts.append(str(m) + "m")
    parts.append(str(s) + "s")
    return " ".join(parts)


# =============================================================================
#  PRIVATE CHAT HANDLERS
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user

    if not await _is_member(user.id, context.bot):
        text = (
            "👋 Hello *" + user.first_name + "*!\n\n"
            "To use this bot you must join *Stain Projects* first.\n\n"
            "1️⃣ Tap *Join* below\n"
            "2️⃣ Come back and tap *I have joined*"
        )
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_join_keyboard())
        return

    text = (
        "👋 Hello *" + user.first_name + "*, welcome to *Stain Trivia Bot*!\n\n"
        "🎯 *How it works:*\n"
        "• Add this bot to your group\n"
        "• Register your group using /give\n"
        "• The bot drops a trivia question every *2 hours*\n"
        "• Members answer with /ans in the group\n"
        "• First correct answer wins *10 points*\n\n"
        "⚠️ *Points reset every 72 hours* — stay active!\n\n"
        "Use the buttons below to explore commands."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_menu_keyboard())


async def give_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ This command only works in private chat.")
        return
    if not await _gate(update, context):
        return

    user = update.effective_user
    data["pending_group"][str(user.id)] = True
    _save(data)
    await update.message.reply_text(
        "📋 Please send your *Group ID*.\n\n"
        "To get your group ID, forward any message from your group to @userinfobot.",
        parse_mode="Markdown"
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not await _gate(update, context):
        return
    await update.message.reply_text(
        "🏓 *Pong!*\n\n⏱ Uptime: *" + _uptime_str() + "*",
        parse_mode="Markdown"
    )


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not await _gate(update, context):
        return
    await update.message.reply_text(
        "🆘 *Support*\n\n"
        "💬 Telegram: https://t.me/heisevanss\n"
        "🔗 Links: https://linktr.ee/iamevanss",
        parse_mode="Markdown"
    )


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch group ID submission after /give."""
    if update.effective_chat.type != "private":
        return
    if not await _gate(update, context):
        return

    user = update.effective_user
    uid  = str(user.id)

    if not data["pending_group"].get(uid):
        return

    text = update.message.text.strip()

    # Group IDs are negative integers
    if not text.lstrip("-").isdigit():
        await update.message.reply_text(
            "❌ That doesn't look like a valid Group ID.\n"
            "Group IDs are numbers like `-1001234567890`.\n\n"
            "Try again or use /give to restart."
        )
        return

    group_id = int(text)

    if group_id > 0:
        await update.message.reply_text(
            "❌ Group IDs are negative numbers (e.g. `-1001234567890`).\n"
            "Make sure you copied the full ID.",
            parse_mode="Markdown"
        )
        return

    if is_registered(group_id):
        await update.message.reply_text(
            "ℹ️ This group is already registered!\n\n"
            "The bot is active in that group.",
        )
        del data["pending_group"][uid]
        _save(data)
        return

    register_group(group_id, user.id)
    del data["pending_group"][uid]
    _save(data)

    await update.message.reply_text(
        "✅ *Group registered successfully!*\n\n"
        "The bot will now drop trivia questions every *2 hours* in that group.\n"
        "Make sure the bot is added to the group and has permission to send messages.",
        parse_mode="Markdown"
    )


# =============================================================================
#  GROUP HANDLERS
# =============================================================================

async def drop_question(group_id: int, bot):
    """Fetch a question and send it to the group."""
    g = get_group(group_id)
    if not g:
        return

    # Check and reset scores if 72h passed
    reset_scores_if_due(group_id)

    q = await get_question()
    if not q:
        logger.warning("No question available for group %s", group_id)
        return

    set_active_question(group_id, {
        "question":  q["question"],
        "answer":    q["answer"],
        "options":   q["options"],
        "category":  q["category"],
        "difficulty": q["difficulty"],
        "asked_at":  time.time(),
    })

    opts_text = "\n".join(
        ["A", "B", "C", "D"][i] + ". " + opt
        for i, opt in enumerate(q["options"])
    )

    text = (
        "🎯 *Trivia Time!*\n\n"
        "📂 Category: " + q["category"] + "\n"
        "⚡ Difficulty: " + q["difficulty"].capitalize() + "\n\n"
        "❓ *" + q["question"] + "*\n\n" +
        opts_text + "\n\n"
        "👉 Reply with `/ans <your answer>` to answer!\n"
        "First correct answer wins *" + str(POINTS_PER_CORRECT) + " points*! 🏆"
    )

    try:
        await bot.send_message(
            chat_id=group_id,
            text=text,
            parse_mode="Markdown"
        )
        logger.info("Question dropped in group %s", group_id)
    except Exception as e:
        logger.warning("Failed to send question to group %s: %s", group_id, e)


async def ans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ans in group chats."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ /ans only works in group chats.")
        return

    group_id = update.effective_chat.id
    if not is_registered(group_id):
        return  # bot not registered for this group, stay silent

    g = get_group(group_id)
    if not g["active_question"]:
        await update.message.reply_text("❓ No active question right now. Wait for the next one!")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/ans <your answer>`\nExample: `/ans Paris`",
            parse_mode="Markdown"
        )
        return

    user_answer  = " ".join(context.args).strip()
    correct      = g["active_question"]["answer"]
    user         = update.effective_user

    if user_answer.lower() == correct.lower():
        add_score(group_id, user.id, user.first_name)
        set_active_question(group_id, None)  # close question

        await update.message.reply_text(
            "🎉 *" + user.first_name + "* got it right!\n\n"
            "✅ Answer: *" + correct + "*\n"
            "💰 +" + str(POINTS_PER_CORRECT) + " points awarded!\n\n"
            "Next question drops in *2 hours*. ⏰",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Wrong answer, *" + user.first_name + "*! Keep trying.",
            parse_mode="Markdown"
        )


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top 7 scorers in the group."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ /leaderboard only works in group chats.")
        return

    group_id = update.effective_chat.id
    if not is_registered(group_id):
        return

    g      = get_group(group_id)
    scores = g.get("scores", {})

    if not scores:
        await update.message.reply_text(
            "📊 No scores yet! First question drops soon. 🎯"
        )
        return

    sorted_scores = sorted(scores.values(), key=lambda x: x["points"], reverse=True)[:7]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]

    lines = ["🏆 *LEADERBOARD* 🏆\n"]
    for i, entry in enumerate(sorted_scores):
        lines.append(
            medals[i] + " *" + entry["name"] + "* — " + str(entry["points"]) + " pts"
        )

    lines.append("\n⏳ Resets in: *" + time_until_reset(group_id) + "*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# =============================================================================
#  CALLBACK HANDLER
# =============================================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    user   = update.effective_user
    action = query.data

    if action == "verify_join":
        if await _is_member(user.id, context.bot):
            text = (
                "✅ *Verified! Welcome, " + user.first_name + "*.\n\n"
                "👋 Welcome to *Stain Trivia Bot*!\n\n"
                "🎯 *How it works:*\n"
                "• Add this bot to your group\n"
                "• Register your group using /give\n"
                "• The bot drops a trivia question every *2 hours*\n"
                "• Members answer with /ans in the group\n"
                "• First correct answer wins *10 points*\n\n"
                "⚠️ *Points reset every 72 hours* — stay active!"
            )
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=_menu_keyboard()
            )
        else:
            await query.answer(
                "You have not joined yet! Tap the join button first.",
                show_alert=True
            )

    elif action == "show_commands":
        text = (
            "📋 *Commands*\n\n"
            "*Private chat (admin):*\n"
            "/start — Welcome message\n"
            "/give — Register your group\n"
            "/ping — Check bot uptime\n"
            "/support — Get help\n\n"
            "*Group chat:*\n"
            "/ans — Answer current trivia question\n"
            "/leaderboard — Show top 7 scores"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back_menu")]
            ])
        )

    elif action == "show_support":
        text = (
            "🆘 *Support*\n\n"
            "💬 Telegram: https://t.me/heisevanss\n"
            "🔗 Links: https://linktr.ee/iamevanss"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back_menu")]
            ])
        )

    elif action == "back_menu":
        text = (
            "👋 Welcome to *Stain Trivia Bot*!\n\n"
            "🎯 *How it works:*\n"
            "• Add this bot to your group\n"
            "• Register your group using /give\n"
            "• The bot drops a trivia question every *2 hours*\n"
            "• Members answer with /ans in the group\n"
            "• First correct answer wins *10 points*\n\n"
            "⚠️ *Points reset every 72 hours* — stay active!"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=_menu_keyboard()
        )


# =============================================================================
#  BACKGROUND SCHEDULER
# =============================================================================

async def question_scheduler(bot):
    """Runs forever — drops a question in every registered group every 2 hours."""
    logger.info("Question scheduler started.")
    while True:
        await asyncio.sleep(60)  # check every minute
        now = time.time()
        for gid, g in list(data["groups"].items()):
            last = g.get("last_question_at", 0)
            if now - last >= QUESTION_INTERVAL:
                # Only drop if no active question open
                if not g.get("active_question"):
                    logger.info("Dropping question in group %s", gid)
                    await drop_question(int(gid), bot)


# =============================================================================
#  MAIN
# =============================================================================

async def main_async():
    global ptb_app

    ptb_app = Application.builder().token(BOT_TOKEN).build()
    ptb_app.bot_data["event_loop"] = asyncio.get_running_loop()

    # Private chat handlers
    ptb_app.add_handler(CommandHandler("start",       start))
    ptb_app.add_handler(CommandHandler("give",        give_command))
    ptb_app.add_handler(CommandHandler("ping",        ping_command))
    ptb_app.add_handler(CommandHandler("support",     support_command))

    # Group handlers
    ptb_app.add_handler(CommandHandler("ans",         ans_command))
    ptb_app.add_handler(CommandHandler("leaderboard", leaderboard_command))

    # Callbacks
    ptb_app.add_handler(CallbackQueryHandler(button_callback))

    # Private text messages (group ID submission)
    ptb_app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private_message
    ))

    # Webhook
    await ptb_app.bot.set_webhook(WEBHOOK_URL + "/webhook")
    logger.info("Webhook set -> %s/webhook", WEBHOOK_URL)

    await ptb_app.initialize()
    await ptb_app.start()
    logger.info("Bot started.")

    # Flask in background thread
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False),
        daemon=True,
    ).start()
    logger.info("Flask listening on port %s", PORT)

    # Start question scheduler
    asyncio.create_task(question_scheduler(ptb_app.bot))

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main_async())
