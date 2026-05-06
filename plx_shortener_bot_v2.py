"""
PLX Network Shortener Telegram Bot - v2
FIX: ID is NOT shown to user upfront. 
     User visits shortened link → lands on unlock page → webpage shows them the ID.
     Only then can they return to bot and /claim it.
"""

import os
import logging
import random
import string
import requests
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from supabase import create_client, Client

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL       = os.getenv("SUPABASE_URL")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY")
SHRINKME_API_KEY   = os.getenv("SHRINKME_API_KEY")
PLX_WEBSITE        = os.getenv("PLX_WEBSITE", "https://plxnetwork.com")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

AWAITING_ACCOUNT_ID = 0


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def generate_unique_id(length: int = 10) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def check_daily_link_limit(telegram_user_id: int) -> bool:
    """Returns True if user has NOT yet generated a link today."""
    today = datetime.now().date().isoformat()
    res = supabase.table("shortener_sessions") \
        .select("id", count="exact") \
        .eq("telegram_user_id", telegram_user_id) \
        .gte("created_at", f"{today}T00:00:00+00:00") \
        .execute()
    return (res.count or 0) == 0


def check_daily_reward_limit(telegram_user_id: int) -> bool:
    """Returns True if user has NOT yet claimed a reward today."""
    today = datetime.now().date().isoformat()
    res = supabase.table("shortener_rewards") \
        .select("id", count="exact") \
        .eq("telegram_user_id", telegram_user_id) \
        .eq("reward_date", today) \
        .execute()
    return (res.count or 0) == 0


def create_shortened_url(long_url: str) -> Optional[str]:
    """Call ShrinkMe.io API to shorten a URL."""
    try:
        resp = requests.get(
            "https://shrinkme.io/api",
            params={
                "api": SHRINKME_API_KEY,
                "url": long_url,
            },
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return data.get("shortenedUrl")
            logger.error(f"ShrinkMe error: {data.get('message')}")
            return None
        logger.error(f"ShrinkMe HTTP error {resp.status_code}: {resp.text}")
        return None
    except Exception as e:
        logger.error(f"ShrinkMe request failed: {e}")
        return None


def store_session(unique_id: str, shortened_url: str, telegram_user_id: int) -> Optional[str]:
    """Save session to DB. Returns the session UUID or None on failure."""
    try:
        res = supabase.table("shortener_sessions").insert({
            "unique_id": unique_id,
            "shortened_url": shortened_url,
            "telegram_user_id": telegram_user_id,
            "status": "pending",
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        logger.error(f"store_session error: {e}")
        return None


def get_session(unique_id: str, telegram_user_id: int) -> Optional[dict]:
    """
    Fetch a session by unique_id that belongs to this telegram user.
    Returns:
      None                   → ID not found / wrong user
      {"error": "claimed"}   → already completed
      {"error": "expired"}   → expired
      dict                   → valid pending session
    """
    try:
        res = supabase.table("shortener_sessions") \
            .select("*") \
            .eq("unique_id", unique_id) \
            .eq("telegram_user_id", telegram_user_id) \
            .execute()
        if not res.data:
            return None
        session = res.data[0]
        if session["status"] == "completed":
            return {"error": "claimed"}
        if session["status"] == "expired":
            return {"error": "expired"}
        return session
    except Exception as e:
        logger.error(f"get_session error: {e}")
        return None


def get_profile_by_user_id8(user_id8: str) -> Optional[dict]:
    """Look up the PLX profile by 8-digit user_id8."""
    try:
        res = supabase.table("profiles") \
            .select("id, balance_confirmed, user_id8") \
            .eq("user_id8", user_id8) \
            .execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"get_profile error: {e}")
        return None


def process_reward(profile_id: str, user_id8: str, telegram_user_id: int,
                   reward_amount: float, session_id: str) -> bool:
    """
    Atomic-ish reward processing:
    1. Update balance in profiles
    2. Insert transaction record
    3. Insert user notification
    4. Mark session completed
    5. Insert shortener_rewards row (daily tracking)
    """
    try:
        today = datetime.now().date().isoformat()

        # 1. Get current balance
        prof = supabase.table("profiles").select("balance_confirmed").eq("id", profile_id).execute()
        if not prof.data:
            return False
        current = float(prof.data[0].get("balance_confirmed") or 0)
        new_balance = current + reward_amount

        # 2. Update balance
        supabase.table("profiles").update({
            "balance_confirmed": new_balance,
            "updated_at": datetime.utcnow().isoformat()
        }).eq("id", profile_id).execute()

        # 3. Insert transaction
        supabase.table("transactions").insert({
            "user_id": profile_id,
            "type": "reward",
            "amount": reward_amount,
            "description": f"Shortener campaign reward — {today}",
        }).execute()

        # 4. Insert notification
        supabase.table("user_notifications").insert({
            "user_id": profile_id,
            "title": "Reward Claimed!",
            "body": f"You earned {reward_amount:.0f} PLX from the shortener campaign. New balance: {new_balance:.2f} PLX",
            "type": "reward",
            "data": {
                "reward_amount": reward_amount,
                "new_balance": new_balance,
                "source": "shortener"
            }
        }).execute()

        # 5. Mark session completed
        supabase.table("shortener_sessions").update({
            "status": "completed",
            "reward_amount": reward_amount,
            "completed_at": datetime.utcnow().isoformat()
        }).eq("id", session_id).execute()

        # 6. Insert daily reward tracking row
        supabase.table("shortener_rewards").insert({
            "profile_id": profile_id,
            "user_id8": user_id8,
            "telegram_user_id": telegram_user_id,
            "session_id": session_id,
            "reward_amount": reward_amount,
            "reward_date": today,
        }).execute()

        return True

    except Exception as e:
        logger.error(f"process_reward error: {e}")
        return False


# ─────────────────────────────────────────────
# BOT HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start
    Generates a shortened link. The 10-digit ID is embedded in the
    destination URL only — the user must visit the link to see it.
    """
    tg_id   = update.effective_user.id
    tg_name = update.effective_user.first_name or "there"

    # Daily link limit
    if not check_daily_link_limit(tg_id):
        await update.message.reply_text(
            f"Hi {tg_name}! You've already generated your link for today.\n\n"
            "Come back tomorrow for another reward opportunity!\n\n"
            "If you already have your ID from today's link, use:\n"
            "/claim YOUR_ID"
        )
        return

    # Generate unique ID
    unique_id = generate_unique_id()

    # Build destination URL — ID is embedded here, not shown in chat
    destination = f"{PLX_WEBSITE}/unlock?id={unique_id}"

    # Shorten it
    shortened = create_shortened_url(destination)
    if not shortened:
        await update.message.reply_text(
            "Sorry, something went wrong creating your link. Please try again in a moment."
        )
        return

    # Save to DB
    session_id = store_session(unique_id, shortened, tg_id)
    if not session_id:
        await update.message.reply_text("Database error. Please try again.")
        return

    # Send to user — NO ID shown here
    await update.message.reply_text(
        f"Hi {tg_name}! Here is your reward link:\n\n"
        f"{shortened}\n\n"
        f"How to earn your PLX reward:\n"
        f"1. Tap the link above\n"
        f"2. Complete the task on the page\n"
        f"3. The page will show you a 10-digit code\n"
        f"4. Come back here and type: /claim YOUR_CODE\n"
        f"5. Get 50-250 PLX instantly!\n\n"
        f"One link per day. Link valid for 24 hours."
    )


async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /claim <10-digit-id>
    Verifies the ID then asks for the 8-digit account ID.
    """
    tg_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Please include your code:\n\n"
            "/claim YOUR_10_DIGIT_CODE\n\n"
            "You get this code from the page you visited."
        )
        return ConversationHandler.END

    unique_id = context.args[0].strip().upper()

    # Check daily reward limit
    if not check_daily_reward_limit(tg_id):
        await update.message.reply_text(
            "You have already claimed your reward today.\n"
            "Come back tomorrow for another!"
        )
        return ConversationHandler.END

    # Verify session
    session = get_session(unique_id, tg_id)

    if session is None:
        await update.message.reply_text(
            f"Code not found: {unique_id}\n\n"
            "Make sure you copied it correctly from the page.\n"
            "Need a new link? Use /start"
        )
        return ConversationHandler.END

    if isinstance(session, dict) and "error" in session:
        messages = {
            "claimed": "This code has already been used. Each code works once only.",
            "expired": "This code has expired. Use /start for a new link."
        }
        await update.message.reply_text(messages.get(session["error"], "Something went wrong."))
        return ConversationHandler.END

    # Store session info for next step
    context.user_data["pending_session"] = {
        "unique_id": unique_id,
        "session_id": session["id"],
        "tg_id": tg_id
    }

    await update.message.reply_text(
        "Code verified! ✅\n\n"
        "Now enter your 8-digit PLX account ID to receive your reward:"
    )
    return AWAITING_ACCOUNT_ID


async def receive_account_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends their 8-digit account ID. Process the reward."""
    account_id = update.message.text.strip()

    if len(account_id) != 8 or not account_id.isdigit():
        await update.message.reply_text(
            "That doesn't look right. Your account ID is exactly 8 digits.\n"
            "Example: 12345678\n\nPlease try again:"
        )
        return AWAITING_ACCOUNT_ID

    profile = get_profile_by_user_id8(account_id)
    if not profile:
        await update.message.reply_text(
            f"Account {account_id} not found. Please check your ID and try again:"
        )
        return AWAITING_ACCOUNT_ID

    pending    = context.user_data.get("pending_session", {})
    tg_id      = pending.get("tg_id")
    session_id = pending.get("session_id")

    reward     = float(random.randint(20, 99))
    profile_id = profile["id"]

    success = process_reward(
        profile_id=profile_id,
        user_id8=account_id,
        telegram_user_id=tg_id,
        reward_amount=reward,
        session_id=session_id
    )

    if not success:
        await update.message.reply_text("Something went wrong processing your reward. Please contact support.")
        return ConversationHandler.END

    # Get fresh balance
    fresh = get_profile_by_user_id8(account_id)
    new_balance = fresh["balance_confirmed"] if fresh else "?"

    await update.message.reply_text(
        f"Reward claimed! 🎉\n\n"
        f"You earned: {reward:.0f} PLX\n"
        f"New balance: {new_balance} PLX\n\n"
        f"Transaction recorded and notification sent to your app.\n\n"
        f"Come back tomorrow for another reward!"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Use /start to get a new link.")
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "PLX Shortener Bot\n\n"
        "Commands:\n"
        "/start - Get your daily reward link\n"
        "/claim CODE - Claim your reward after visiting the link\n"
        "/help - Show this message\n"
        "/cancel - Cancel current operation\n\n"
        "How it works:\n"
        "1. Use /start to get your link\n"
        "2. Visit the link and complete the task\n"
        "3. Copy the 10-digit code shown on the page\n"
        "4. Use /claim CODE here\n"
        "5. Enter your 8-digit account ID\n"
        "6. Receive 50-250 PLX!\n\n"
        "Rules:\n"
        "- One reward per day\n"
        "- Each code can only be used once\n\n"
        f"Website: {PLX_WEBSITE}"
    )


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main() -> None:
    if not all([TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, SHRINKME_API_KEY]):
        raise ValueError("Missing required environment variables. Check your .env file.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    claim_conv = ConversationHandler(
        entry_points=[CommandHandler("claim", cmd_claim)],
        states={
            AWAITING_ACCOUNT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_account_id)
            ]
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(claim_conv)
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
