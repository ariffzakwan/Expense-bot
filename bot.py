import os
import json
import base64
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Simple in-memory storage (ganti dengan database untuk production)
user_expenses: dict[int, list] = {}

CATEGORIES = {
    "Makanan/Minuman": "🍔",
    "Rumah/Sewa": "🏠",
    "Transport": "🚗",
    "Shopee/Online": "🛒",
    "Kesihatan": "💊",
    "Hiburan": "🎮",
    "Bil/Utiliti": "💡",
    "Lain-lain": "📦",
}

EXTRACT_PROMPT = """Kau adalah AI pembaca resit. Analisa resit/dokumen ini dan extract maklumat dalam format JSON.

Pulangkan HANYA JSON, tanpa teks lain:
{
  "merchant": "nama kedai/syarikat",
  "amount": 0.00,
  "currency": "MYR",
  "date": "YYYY-MM-DD",
  "category": "pilih satu: Makanan/Minuman, Rumah/Sewa, Transport, Shopee/Online, Kesihatan, Hiburan, Bil/Utiliti, Lain-lain",
  "items": ["senarai item jika ada"],
  "confidence": "high/medium/low"
}

Jika tarikh tidak jelas, guna tarikh hari ini. Jika jumlah tidak jelas, letak 0.00."""


def get_user_expenses(user_id: int) -> list:
    return user_expenses.get(user_id, [])


def add_expense(user_id: int, expense: dict):
    if user_id not in user_expenses:
        user_expenses[user_id] = []
    user_expenses[user_id].append(expense)


def format_summary_message(expense: dict) -> str:
    cat = expense.get("category", "Lain-lain")
    icon = CATEGORIES.get(cat, "📦")
    items_text = ""
    if expense.get("items"):
        items_text = "\n📋 Item: " + ", ".join(expense["items"][:3])

    return (
        f"✅ *Rekod disimpan!*\n\n"
        f"🏪 *{expense['merchant']}*\n"
        f"💰 RM {expense['amount']:.2f}\n"
        f"{icon} {cat}\n"
        f"📅 {expense['date']}"
        f"{items_text}"
    )


def format_report(expenses: list) -> str:
    if not expenses:
        return "📭 Tiada rekod lagi. Forward resit untuk mula!"

    total = sum(e["amount"] for e in expenses)
    cat_totals: dict[str, float] = {}
    for e in expenses:
        cat = e.get("category", "Lain-lain")
        cat_totals[cat] = cat_totals.get(cat, 0) + e["amount"]

    sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)

    lines = [f"📊 *Laporan Perbelanjaan*\n", f"💳 Jumlah: *RM {total:.2f}*\n"]

    for cat, amount in sorted_cats:
        icon = CATEGORIES.get(cat, "📦")
        pct = (amount / total * 100) if total else 0
        bar_len = int(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"{icon} {cat}\n`{bar}` {pct:.0f}%\n    RM {amount:.2f}\n")

    lines.append(f"\n🧾 *{len(expenses)} transaksi* direkod")
    return "\n".join(lines)


async def analyze_image(image_data: bytes, mime_type: str) -> dict | None:
    try:
        b64 = base64.standard_b64encode(image_data).decode("utf-8")
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": EXTRACT_PROMPT},
                    ],
                }
            ],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"Image analysis error: {e}")
        return None


async def analyze_pdf(pdf_data: bytes) -> dict | None:
    try:
        b64 = base64.standard_b64encode(pdf_data).decode("utf-8")
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": EXTRACT_PROMPT},
                    ],
                }
            ],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"PDF analysis error: {e}")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Selamat datang ke Expense Tracker Bot!*\n\n"
        "Cara guna:\n"
        "📸 Forward atau hantar *gambar resit*\n"
        "📄 Forward *PDF* resit (MyMaybank, Shopee, dll)\n"
        "📊 /report — tengok laporan & carta perbelanjaan\n"
        "🗑 /clear — padam semua rekod\n\n"
        "Bot akan auto-detect kategori dan simpan rekod! ✨"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    expenses = get_user_expenses(user_id)
    await update.message.reply_text(format_report(expenses), parse_mode="Markdown")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_expenses[user_id] = []
    await update.message.reply_text("🗑 Semua rekod telah dipadam.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("⏳ AI sedang baca resit...")

    photo = update.message.photo[-1]  # ambil resolusi tertinggi
    file = await context.bot.get_file(photo.file_id)
    async with httpx.AsyncClient() as hx:
        resp = await hx.get(file.file_path)
    image_data = resp.content

    result = await analyze_image(image_data, "image/jpeg")
    if not result or result.get("amount", 0) == 0:
        await update.message.reply_text(
            "❌ Tak dapat baca resit ni. Cuba hantar gambar yang lebih jelas."
        )
        return

    result.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    add_expense(user_id, result)
    await update.message.reply_text(
        format_summary_message(result), parse_mode="Markdown"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    doc = update.message.document
    mime = doc.mime_type or ""

    if mime not in ("application/pdf", "image/jpeg", "image/png", "image/jpg"):
        await update.message.reply_text(
            "⚠️ Hantar PDF atau gambar resit sahaja (JPG, PNG, PDF)."
        )
        return

    await update.message.reply_text("⏳ AI sedang baca resit...")

    file = await context.bot.get_file(doc.file_id)
    async with httpx.AsyncClient() as hx:
        resp = await hx.get(file.file_path)
    file_data = resp.content

    if mime == "application/pdf":
        result = await analyze_pdf(file_data)
    else:
        result = await analyze_image(file_data, mime)

    if not result or result.get("amount", 0) == 0:
        await update.message.reply_text(
            "❌ Tak dapat baca fail ni. Cuba hantar semula atau pastikan resit jelas."
        )
        return

    result.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    add_expense(user_id, result)
    await update.message.reply_text(
        format_summary_message(result), parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Hantar *gambar* atau *PDF* resit untuk direkod.\n"
        "Taip /report untuk tengok laporan.",
        parse_mode="Markdown",
    )


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
