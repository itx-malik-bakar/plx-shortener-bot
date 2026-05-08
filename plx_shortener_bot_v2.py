"""
PLX Network Shortener Telegram Bot - v3
- 24-hour cooldown (not calendar-day reset)
- Shows exact time remaining if user tries again too early
- Detailed help & error messages
- Better user guidance throughout
"""

import os
import logging
import random
import string
import requests
from datetime import datetime, timezone, timedelta
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

# ── Reward range — only change these two numbers ──
REWARD_MIN = 20
REWARD_MAX = 120
# ─────────────────────────────────────────────────

COOLDOWN_HOURS = 24   # hours between link generations
BOT_USERNAME   = os.getenv("BOT_USERNAME", "your_bot")  # e.g. plx_shortener_bot

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


def format_time_remaining(seconds: float) -> str:
    """Turn a number of seconds into a human-readable string like '13 hours 42 minutes'."""
    seconds = int(seconds)
    hours   = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0 and minutes > 0:
        return f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"
    if hours > 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def get_link_cooldown_remaining(telegram_user_id: int) -> Optional[float]:
    """
    Returns seconds remaining in the link-generation cooldown, or None if user is free to generate.
    Looks at the most recent session for this user.
    """
    try:
        res = supabase.table("shortener_sessions") \
            .select("created_at") \
            .eq("telegram_user_id", telegram_user_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if not res.data:
            return None
        last_created = datetime.fromisoformat(res.data[0]["created_at"].replace("Z", "+00:00"))
        now          = datetime.now(timezone.utc)
        elapsed      = (now - last_created).total_seconds()
        remaining    = COOLDOWN_HOURS * 3600 - elapsed
        return remaining if remaining > 0 else None
    except Exception as e:
        logger.error(f"get_link_cooldown_remaining error: {e}")
        return None


def get_reward_cooldown_remaining(telegram_user_id: int) -> Optional[float]:
    """
    Returns seconds remaining in the claim cooldown, or None if user is free to claim.
    """
    try:
        res = supabase.table("shortener_rewards") \
            .select("created_at") \
            .eq("telegram_user_id", telegram_user_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if not res.data:
            return None
        last_claimed = datetime.fromisoformat(res.data[0]["created_at"].replace("Z", "+00:00"))
        now          = datetime.now(timezone.utc)
        elapsed      = (now - last_claimed).total_seconds()
        remaining    = COOLDOWN_HOURS * 3600 - elapsed
        return remaining if remaining > 0 else None
    except Exception as e:
        logger.error(f"get_reward_cooldown_remaining error: {e}")
        return None


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
    try:
        res = supabase.table("shortener_sessions").insert({
            "unique_id":        unique_id,
            "shortened_url":    shortened_url,
            "telegram_user_id": telegram_user_id,
            "status":           "pending",
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        logger.error(f"store_session error: {e}")
        return None


def get_session(unique_id: str, telegram_user_id: int) -> Optional[dict]:
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
    try:
        today = datetime.now().date().isoformat()

        prof = supabase.table("profiles").select("balance_confirmed").eq("id", profile_id).execute()
        if not prof.data:
            return False
        current     = float(prof.data[0].get("balance_confirmed") or 0)
        new_balance = current + reward_amount

        supabase.table("profiles").update({
            "balance_confirmed": new_balance,
            "updated_at":        datetime.utcnow().isoformat()
        }).eq("id", profile_id).execute()

        supabase.table("transactions").insert({
            "user_id":     profile_id,
            "type":        "reward",
            "amount":      reward_amount,
            "description": f"Shortener campaign reward — {today}",
        }).execute()

        supabase.table("user_notifications").insert({
            "user_id": profile_id,
            "title":   "Reward Claimed!",
            "body":    (
                f"You earned {reward_amount:.0f} PLX from the shortener campaign. "
                f"New balance: {new_balance:.2f} PLX"
            ),
            "type": "reward",
            "data": {
                "reward_amount": reward_amount,
                "new_balance":   new_balance,
                "source":        "shortener"
            }
        }).execute()

        supabase.table("shortener_sessions").update({
            "status":       "completed",
            "reward_amount": reward_amount,
            "completed_at": datetime.utcnow().isoformat()
        }).eq("id", session_id).execute()

        supabase.table("shortener_rewards").insert({
            "profile_id":        profile_id,
            "user_id8":          user_id8,
            "telegram_user_id":  telegram_user_id,
            "session_id":        session_id,
            "reward_amount":     reward_amount,
            "reward_date":       today,
        }).execute()

        return True

    except Exception as e:
        logger.error(f"process_reward error: {e}")
        return False


# ─────────────────────────────────────────────
# BOT HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id   = update.effective_user.id
    tg_name = update.effective_user.first_name or "there"

    remaining = get_link_cooldown_remaining(tg_id)
    if remaining:
        time_str = format_time_remaining(remaining)
        await update.message.reply_text(
            f"Hi {tg_name}!\n\n"
            f"You already generated a link recently.\n"
            f"You can get a new one in: {time_str}\n\n"
            f"If you already visited your link and have your 10-digit code, "
            f"you can still claim your reward right now:\n"
            f"/claim YOUR_CODE\n\n"
            f"Your 10-digit code is on the page you visited. "
            f"It is NOT the link itself — it is the code shown on the PLX unlock page."
        )
        return

    unique_id   = generate_unique_id()
    destination = f"{PLX_WEBSITE}/unlock?id={unique_id}"
    shortened   = create_shortened_url(destination)

    if not shortened:
        await update.message.reply_text(
            "Something went wrong creating your link right now.\n"
            "Please try again in a few minutes.\n\n"
            "If this keeps happening, contact PLX support."
        )
        return

    session_id = store_session(unique_id, shortened, tg_id)
    if not session_id:
        await update.message.reply_text(
            "A database error occurred. Please try again.\n"
            "If it keeps failing, contact PLX support."
        )
        return

    await update.message.reply_text(
        f"Hi {tg_name}! Your reward link is ready.\n\n"
        f"YOUR LINK:\n"
        f"{shortened}\n\n"
        f"WHAT TO DO:\n"
        f"1. Tap the link above\n"
        f"2. You will land on a page with a task (e.g. watch a short ad)\n"
        f"3. Complete the task — the PLX unlock page will then show you a 10-digit code\n"
        f"4. Copy that code and come back here\n"
        f"5. Type: /claim YOURCODE  (example: /claim AB12CD34EF)\n"
        f"6. Enter your 8-digit PLX account ID\n"
        f"7. Receive {REWARD_MIN}–{REWARD_MAX} PLX instantly!\n\n"
        f"IMPORTANT:\n"
        f"- The 10-digit code is shown on the PLX page AFTER you complete the task\n"
        f"- It is NOT the link itself\n"
        f"- Your link is valid for {COOLDOWN_HOURS} hours\n"
        f"- One reward per {COOLDOWN_HOURS} hours per user"
    )


async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "How to claim your reward:\n\n"
            "Type: /claim YOURCODE\n"
            "Example: /claim AB12CD34EF\n\n"
            "Where do I find the code?\n"
            f"1. Use /start to get your shortened link\n"
            f"2. Visit the link and complete the task\n"
            f"3. The PLX unlock page ({PLX_WEBSITE}) will show you a 10-digit code\n"
            f"4. Copy that code and use it here with /claim\n\n"
            f"The code is NOT the shortened link — it is the code on the destination page."
        )
        return ConversationHandler.END

    unique_id = context.args[0].strip().upper()

    # Basic format check
    if len(unique_id) != 10 or not unique_id.isalnum():
        await update.message.reply_text(
            f"That code doesn't look right: {unique_id}\n\n"
            f"The code must be exactly 10 characters (letters and numbers).\n"
            f"Example: AB12CD34EF\n\n"
            f"Where to find it:\n"
            f"- Visit your shortened link\n"
            f"- Complete the task on the page\n"
            f"- The PLX unlock page shows the code after completion\n\n"
            f"Need a new link? Use /start"
        )
        return ConversationHandler.END

    # Check claim cooldown
    remaining = get_reward_cooldown_remaining(tg_id)
    if remaining:
        time_str = format_time_remaining(remaining)
        await update.message.reply_text(
            f"You already claimed a reward recently.\n\n"
            f"You can claim again in: {time_str}\n\n"
            f"Come back then and use /start to get a fresh link."
        )
        return ConversationHandler.END

    session = get_session(unique_id, tg_id)

    if session is None:
        await update.message.reply_text(
            f"Code not found: {unique_id}\n\n"
            f"Possible reasons:\n"
            f"- You may have copied it incorrectly (check for 0 vs O, 1 vs I)\n"
            f"- The code belongs to a different Telegram account\n"
            f"- You typed the shortened link instead of the code — "
            f"the code is the 10-digit text shown on the PLX page, not the shrinkme link\n\n"
            f"To get a new link: /start\n"
            f"To see how this works: /help"
        )
        return ConversationHandler.END

    if isinstance(session, dict) and "error" in session:
        if session["error"] == "claimed":
            await update.message.reply_text(
                f"This code has already been used: {unique_id}\n\n"
                f"Each code can only be claimed once.\n"
                f"Use /start to generate a new link and get a fresh code."
            )
        elif session["error"] == "expired":
            await update.message.reply_text(
                f"This code has expired: {unique_id}\n\n"
                f"Links and their codes are valid for {COOLDOWN_HOURS} hours.\n"
                f"Use /start to generate a fresh link."
            )
        return ConversationHandler.END

    context.user_data["pending_session"] = {
        "unique_id":  unique_id,
        "session_id": session["id"],
        "tg_id":      tg_id
    }

    await update.message.reply_text(
        f"Code verified!\n\n"
        f"Now enter your 8-digit PLX account ID.\n\n"
        f"Where to find your account ID:\n"
        f"- Open the PLX Network app\n"
        f"- Go to your Profile or Settings\n"
        f"- It is labelled as your User ID — exactly 8 digits (numbers only)\n"
        f"- Example: 12345678\n\n"
        f"Type your 8-digit ID now:"
    )
    return AWAITING_ACCOUNT_ID


async def receive_account_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    account_id = update.message.text.strip()

    if not account_id.isdigit():
        await update.message.reply_text(
            f"Your account ID must contain numbers only — no letters or spaces.\n\n"
            f"Where to find it:\n"
            f"- Open the PLX Network app\n"
            f"- Go to your Profile or Settings\n"
            f"- It is your User ID — 8 digits\n"
            f"- Example: 12345678\n\n"
            f"Please try again:"
        )
        return AWAITING_ACCOUNT_ID

    if len(account_id) != 8:
        await update.message.reply_text(
            f"You entered {len(account_id)} digits — your account ID must be exactly 8.\n\n"
            f"Where to find it:\n"
            f"- Open the PLX Network app\n"
            f"- Go to your Profile or Settings\n"
            f"- It is your User ID — 8 digits, like 12345678\n\n"
            f"Please try again:"
        )
        return AWAITING_ACCOUNT_ID

    profile = get_profile_by_user_id8(account_id)
    if not profile:
        await update.message.reply_text(
            f"No account found with ID: {account_id}\n\n"
            f"Please check:\n"
            f"- Open the PLX Network app at {PLX_WEBSITE}\n"
            f"- Go to Profile or Settings to find your 8-digit User ID\n"
            f"- Make sure you have a PLX account (create one at {PLX_WEBSITE} if not)\n\n"
            f"Try again with the correct ID:"
        )
        return AWAITING_ACCOUNT_ID

    pending    = context.user_data.get("pending_session", {})
    tg_id      = pending.get("tg_id")
    session_id = pending.get("session_id")

    reward     = float(random.randint(REWARD_MIN, REWARD_MAX))
    profile_id = profile["id"]

    success = process_reward(
        profile_id=profile_id,
        user_id8=account_id,
        telegram_user_id=tg_id,
        reward_amount=reward,
        session_id=session_id
    )

    if not success:
        await update.message.reply_text(
            "Something went wrong while processing your reward.\n\n"
            "Your code is still valid — please try again in a moment.\n"
            "If it keeps failing, contact PLX support with your 10-digit code."
        )
        return ConversationHandler.END

    fresh       = get_profile_by_user_id8(account_id)
    new_balance = f"{fresh['balance_confirmed']:.2f}" if fresh else "?"

    await update.message.reply_text(
        f"Reward claimed!\n\n"
        f"You earned:    {reward:.0f} PLX\n"
        f"New balance:   {new_balance} PLX\n\n"
        f"Your balance has been updated in the PLX app.\n"
        f"A notification has also been sent to your app.\n\n"
        f"You can earn again in {COOLDOWN_HOURS} hours.\n"
        f"Use /start then to get a new link."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Cancelled.\n\n"
        "Use /start to get a new reward link whenever you are ready."
    )
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "PLX Network — Shortener Reward Bot\n\n"
        "WHAT THIS BOT DOES:\n"
        "This bot gives you a shortened link. You visit that link, complete a short task "
        "(like watching an ad), and in return you earn PLX tokens credited directly to your "
        "PLX Network wallet. One reward every 24 hours.\n\n"

        "COMMANDS:\n"
        "/start    — Get your reward link for today\n"
        "/claim    — Claim your reward after visiting the link\n"
        "/help     — Show this message\n"
        "/cancel   — Cancel whatever you are currently doing\n\n"

        "STEP-BY-STEP:\n"
        "1. Type /start\n"
        "2. The bot sends you a shortened link\n"
        "3. Tap the link — you will go through a short task page\n"
        "4. After completing the task you land on the PLX unlock page\n"
        "5. That page shows you a 10-digit code (example: AB12CD34EF)\n"
        "6. Copy the code and come back to this bot\n"
        "7. Type /claim AB12CD34EF  (use your actual code)\n"
        "8. The bot asks for your 8-digit PLX account ID\n"
        "9. Open the PLX app, go to Profile → copy your 8-digit User ID\n"
        "10. Send that ID here\n"
        "11. Done — you receive between "
        f"{REWARD_MIN} and {REWARD_MAX} PLX instantly!\n\n"

        "COMMON MISTAKES:\n"
        "- Do NOT send the shortened link as the code. The code is on the PLX page "
        "AFTER you complete the task — it is 10 characters like AB12CD34EF\n"
        "- Your account ID is 8 digits from the PLX app, not your email or username\n"
        "- If the code says 'not found', make sure you are using the same Telegram "
        "account that generated the link\n\n"

        "RULES:\n"
        f"- One reward per {COOLDOWN_HOURS} hours per user\n"
        "- Each code can only be claimed once\n"
        "- Links expire after 24 hours\n\n"

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
