#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         ForceHub Bot — Force Subscribe Platform      ║
║    Creator | Campaign | Analytics | Broadcast System  ║
║              python-telegram-bot v21                  ║
╚══════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ForceHub")

# ─────────────────────────────────────────────────────────────────
# ENVIRONMENT CONFIG
# ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: List[int] = [
    int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()
]

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_FILE = DATA_DIR / "forcehub_data.json"
CONFIG_FILE = DATA_DIR / "forcehub_config.json"

# ─────────────────────────────────────────────────────────────────
# CONVERSATION STATES
# ─────────────────────────────────────────────────────────────────
(
    SETUP_CHANNEL,
    SETUP_MATERIAL_TYPE,
    SETUP_MATERIAL_TITLE,
    SETUP_MATERIAL_CONTENT,
    SETUP_REFERRAL_COUNT,
) = range(5)

# ─────────────────────────────────────────────────────────────────
# DATA MANAGER  (single JSON, loaded once, saved periodically)
# ─────────────────────────────────────────────────────────────────
class DataManager:
    """Thread-safe (async) JSON data manager with in-memory cache."""

    SAVE_INTERVAL = 30  # seconds

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._config: Dict[str, Any] = {}
        self._dirty = False
        self._last_save = time.monotonic()
        self._lock = asyncio.Lock()
        self._ensure_dirs()
        self._load_sync()

    # ── Init helpers ──────────────────────────────────────────────

    def _ensure_dirs(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _skeleton_data(self) -> Dict[str, Any]:
        return {
            "users": {},
            "creators": {},
            "materials": {},
            "campaigns": {},
            "analytics": {
                "campaign_clicks": {},
                "verification_success": {},
                "unlock_success": {},
                "referral_unlocks": {},
                "daily": {},
            },
            "settings": {
                "trial_days": 90,
                "upi_id": "yourname@upi",
                "price": 199,
                "admin_ids": ADMIN_IDS,
            },
        }

    def _skeleton_config(self) -> Dict[str, Any]:
        return {
            "version": "2.0.0",
            "bot_name": "ForceHub",
            "created_at": datetime.now().isoformat(),
        }

    def _load_sync(self):
        if DATA_FILE.exists():
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info("✅ Data loaded from disk (%d users, %d creators)",
                            len(self._data.get("users", {})),
                            len(self._data.get("creators", {})))
            except Exception as exc:
                logger.error("Failed to load data.json: %s — using defaults", exc)
                self._data = self._skeleton_data()
        else:
            self._data = self._skeleton_data()
            self._flush()
            logger.info("📁 Created new forcehub_data.json")

        # Back-fill missing top-level keys
        for key, val in self._skeleton_data().items():
            self._data.setdefault(key, val)

        # Config file
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
            except Exception:
                self._config = self._skeleton_config()
        else:
            self._config = self._skeleton_config()
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2)
            logger.info("📁 Created new forcehub_config.json")

    def _flush(self):
        """Write to disk immediately (blocking)."""
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            self._last_save = time.monotonic()
            self._dirty = False
        except Exception as exc:
            logger.error("Flush failed: %s", exc)

    def save(self, force: bool = False):
        """Mark dirty; flush if forced or interval elapsed."""
        self._dirty = True
        if force or (time.monotonic() - self._last_save >= self.SAVE_INTERVAL):
            self._flush()

    async def periodic_save(self):
        """Background coroutine: flush dirty data every SAVE_INTERVAL seconds."""
        while True:
            await asyncio.sleep(self.SAVE_INTERVAL)
            if self._dirty:
                self._flush()
                logger.debug("Periodic save completed.")

    # ── Convenience accessors ──────────────────────────────────────

    @property
    def users(self) -> Dict:     return self._data["users"]
    @property
    def creators(self) -> Dict:  return self._data["creators"]
    @property
    def materials(self) -> Dict: return self._data["materials"]
    @property
    def campaigns(self) -> Dict: return self._data["campaigns"]
    @property
    def analytics(self) -> Dict: return self._data["analytics"]
    @property
    def settings(self) -> Dict:  return self._data["settings"]

    # ── User helpers ──────────────────────────────────────────────

    def get_or_create_user(self, user_id: int, username: str = "", first_name: str = "") -> Dict:
        uid = str(user_id)
        if uid not in self.users:
            self.users[uid] = {
                "username": username,
                "first_name": first_name,
                "joined_at": datetime.now().isoformat(),
                "unlocked_campaigns": [],
                "referral_count": 0,
                "referred_by": None,
                "banned": False,
            }
            self._bump_daily("joins")
            self.save()
        else:
            # Keep username fresh
            if username:
                self.users[uid]["username"] = username
            if first_name:
                self.users[uid]["first_name"] = first_name
        return self.users[uid]

    def get_user(self, user_id: int) -> Optional[Dict]:
        return self.users.get(str(user_id))

    # ── Creator helpers ───────────────────────────────────────────

    def get_creator(self, creator_id: int) -> Optional[Dict]:
        return self.creators.get(str(creator_id))

    def create_creator(self, creator_id: int, username: str, name: str) -> Dict:
        cid = str(creator_id)
        trial_days = self.settings.get("trial_days", 90)
        self.creators[cid] = {
            "username": username,
            "name": name,
            "trial_start": datetime.now().isoformat(),
            "trial_days": trial_days,
            "channels": [],
            "materials": [],
            "campaigns": [],
            "joined_at": datetime.now().isoformat(),
        }
        self.save(force=True)
        return self.creators[cid]

    def is_creator_active(self, creator_id: int) -> bool:
        creator = self.get_creator(creator_id)
        if not creator:
            return False
        try:
            trial_start = datetime.fromisoformat(creator["trial_start"])
            trial_days = creator.get("trial_days", 90)
            return datetime.now() < trial_start + timedelta(days=trial_days)
        except Exception:
            return False

    def creator_days_left(self, creator_id: int) -> int:
        creator = self.get_creator(creator_id)
        if not creator:
            return 0
        try:
            trial_start = datetime.fromisoformat(creator["trial_start"])
            trial_days = creator.get("trial_days", 90)
            expiry = trial_start + timedelta(days=trial_days)
            return max(0, (expiry - datetime.now()).days)
        except Exception:
            return 0

    def renew_creator(self, creator_id: int, days: Optional[int] = None):
        creator = self.get_creator(creator_id)
        if not creator:
            return
        creator["trial_start"] = datetime.now().isoformat()
        creator["trial_days"] = days if days is not None else self.settings.get("trial_days", 90)
        self.save(force=True)

    # ── Campaign helpers ──────────────────────────────────────────

    def create_campaign(
        self,
        creator_id: int,
        material_id: str,
        channels: List[str],
        referral_required: int,
    ) -> str:
        # Generate unique 8-char ID, prevent duplicates
        campaign_id = str(uuid.uuid4())[:8].upper()
        while campaign_id in self.campaigns:
            campaign_id = str(uuid.uuid4())[:8].upper()

        self.campaigns[campaign_id] = {
            "creator_id": str(creator_id),
            "material_id": material_id,
            "channels": channels,
            "referral_required": referral_required,
            "created_at": datetime.now().isoformat(),
            "is_active": True,
        }
        creator = self.get_creator(creator_id)
        if creator is not None:
            creator.setdefault("campaigns", []).append(campaign_id)
        self.save(force=True)
        return campaign_id

    # ── Analytics helpers ─────────────────────────────────────────

    def _bump_daily(self, field: str):
        today = datetime.now().strftime("%Y-%m-%d")
        day = self.analytics["daily"].setdefault(today, {"joins": 0, "unlocks": 0})
        day[field] = day.get(field, 0) + 1

    def track(self, event: str, campaign_id: Optional[str] = None):
        if campaign_id:
            bucket = self.analytics.setdefault(event, {})
            bucket[campaign_id] = bucket.get(campaign_id, 0) + 1
        if event == "unlock_success":
            self._bump_daily("unlocks")
        self.save()

    # ── Settings helpers ──────────────────────────────────────────

    def set_trial_days(self, days: int):
        self.settings["trial_days"] = days
        self.save(force=True)

    def set_upi(self, upi: str):
        self.settings["upi_id"] = upi
        self.save(force=True)

    def set_price(self, price: int):
        self.settings["price"] = price
        self.save(force=True)

    # ── Global stats ──────────────────────────────────────────────

    def global_stats(self) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        today_data = self.analytics.get("daily", {}).get(today, {})
        return {
            "total_users": len(self.users),
            "total_creators": len(self.creators),
            "total_campaigns": len(self.campaigns),
            "total_materials": len(self.materials),
            "today_unlocks": today_data.get("unlocks", 0),
            "today_joins": today_data.get("joins", 0),
        }


# ── Singleton ─────────────────────────────────────────────────────
db = DataManager()


# ─────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in db.settings.get("admin_ids", [])


def is_creator(user_id: int) -> bool:
    return str(user_id) in db.creators


def now_str() -> str:
    return datetime.now().strftime("%d %b %Y, %H:%M IST")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def check_channel_membership(bot, user_id: int, channels: List[str]) -> List[str]:
    """Return channels the user has NOT joined."""
    not_joined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(ch)
        except TelegramError:
            not_joined.append(ch)
    return not_joined


def parse_inline_buttons(text: str) -> Optional[InlineKeyboardMarkup]:
    """
    Parse buttons from text:
      Button Label - https://url.com
    One button per line. Returns None if text is 'skip' or unparseable.
    """
    if not text or text.strip().lower() in ("skip", "no", "none"):
        return None
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if " - " in line:
            label, url = line.split(" - ", 1)
            label = label.strip()
            url = url.strip()
            if label and url.startswith("http"):
                rows.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(rows) if rows else None


async def batch_broadcast(
    app: Application,
    user_ids: List[int],
    content_type: str,
    content: Any,
    caption: str = "",
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Dict[str, int]:
    """
    Broadcast content to a list of user IDs.
    Returns {"sent": int, "failed": int}.
    Batched with 0.05 s delay to respect Telegram rate limits.
    """
    sent = failed = 0
    km = reply_markup

    for uid in user_ids:
        try:
            if content_type == "text":
                await app.bot.send_message(
                    uid, content,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=km,
                    disable_web_page_preview=True,
                )
            elif content_type == "photo":
                await app.bot.send_photo(uid, content, caption=caption or None,
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=km)
            elif content_type == "video":
                await app.bot.send_video(uid, content, caption=caption or None,
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=km)
            elif content_type == "document":
                await app.bot.send_document(uid, content, caption=caption or None,
                                            parse_mode=ParseMode.MARKDOWN, reply_markup=km)
            sent += 1
        except (Forbidden, BadRequest):
            failed += 1
        except Exception as exc:
            logger.warning("Broadcast to %d failed: %s", uid, exc)
            failed += 1
        await asyncio.sleep(0.05)

    return {"sent": sent, "failed": failed}


async def deliver_material(bot, chat_id: int, campaign: Dict) -> bool:
    """Send the material linked to a campaign to a chat. Returns True on success."""
    material_id = campaign.get("material_id", "")
    material = db.materials.get(material_id)
    if not material:
        await bot.send_message(
            chat_id,
            "✅ *Unlocked!* Material not found — contact the creator.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False

    file_type  = material.get("file_type", "text")
    title      = material.get("title", "Unlocked Content")
    description = material.get("description", "")
    file_id    = material.get("file_id")
    header     = f"🎉 *{title}* — Unlocked!\n\n"

    try:
        if file_type == "text":
            await bot.send_message(chat_id, header + description,
                                   parse_mode=ParseMode.MARKDOWN)
        elif file_type == "photo":
            await bot.send_photo(chat_id, file_id,
                                 caption=header + description,
                                 parse_mode=ParseMode.MARKDOWN)
        elif file_type == "video":
            await bot.send_video(chat_id, file_id,
                                 caption=header + description,
                                 parse_mode=ParseMode.MARKDOWN)
        elif file_type == "document":
            await bot.send_document(chat_id, file_id,
                                    caption=header + description,
                                    parse_mode=ParseMode.MARKDOWN)
        return True
    except Exception as exc:
        logger.error("Deliver material failed: %s", exc)
        await bot.send_message(chat_id,
                               "✅ Content unlocked! Delivery failed — contact creator.",
                               parse_mode=ParseMode.MARKDOWN)
        return False


# ─────────────────────────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────────────────────────

def kb_user() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Unlock Content",     callback_data="u_unlock")],
        [InlineKeyboardButton("📚 My Unlocks",         callback_data="u_unlocks"),
         InlineKeyboardButton("👥 Referral Progress",  callback_data="u_referral")],
        [InlineKeyboardButton("❓ Help",               callback_data="u_help")],
    ])


def kb_creator() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard",          callback_data="c_dash")],
        [InlineKeyboardButton("➕ Setup Material",     callback_data="c_setup"),
         InlineKeyboardButton("📦 Materials",          callback_data="c_materials")],
        [InlineKeyboardButton("📢 Channels",           callback_data="c_channels"),
         InlineKeyboardButton("📈 Stats",              callback_data="c_stats")],
        [InlineKeyboardButton("📣 Broadcast Users",    callback_data="c_broadcast")],
        [InlineKeyboardButton("🔄 Renew Panel",        callback_data="c_renew")],
    ])


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡️ Admin Panel",        callback_data="a_panel")],
        [InlineKeyboardButton("📊 Global Stats",       callback_data="a_stats"),
         InlineKeyboardButton("📣 Broadcast",          callback_data="a_broadcast")],
        [InlineKeyboardButton("📤 Export Data",        callback_data="a_export"),
         InlineKeyboardButton("🚫 Ban Creator",        callback_data="a_ban")],
        [InlineKeyboardButton("💰 Set Price",          callback_data="a_price"),
         InlineKeyboardButton("⏱ Set Trial",           callback_data="a_trial")],
        [InlineKeyboardButton("💳 Set UPI",            callback_data="a_upi")],
    ])


def kb_broadcast_target() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Broadcast Users",    callback_data="bcast_users")],
        [InlineKeyboardButton("🎨 Broadcast Creators", callback_data="bcast_creators")],
        [InlineKeyboardButton("📢 Broadcast Everyone", callback_data="bcast_everyone")],
        [InlineKeyboardButton("❌ Cancel",             callback_data="a_panel")],
    ])


def kb_back_user()    -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="u_back")]])

def kb_back_creator() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="c_dash")]])

def kb_back_admin()   -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="a_panel")]])


# ─────────────────────────────────────────────────────────────────
# /start  — entry point + deep-link router
# ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id

    db.get_or_create_user(uid,
                          username=user.username or "",
                          first_name=user.first_name or "")

    # ── Deep-link args ────────────────────────────────────────────
    args = context.args or []
    if args:
        arg = args[0]

        # Referral tracking: /start ref_<user_id>
        if arg.startswith("ref_"):
            referrer_id = arg[4:]
            u = db.get_user(uid)
            if u and u.get("referred_by") is None and referrer_id != str(uid):
                u["referred_by"] = referrer_id
                referrer = db.users.get(referrer_id)
                if referrer:
                    referrer["referral_count"] = referrer.get("referral_count", 0) + 1
                    db.track("referral_unlocks", referrer_id)
                db.save()

        # Campaign access: /start <CAMPAIGN_ID>
        elif len(arg) == 8 and arg.isupper():
            return await _handle_campaign(update, context, arg)

    # ── Show appropriate menu ─────────────────────────────────────
    if is_admin(uid):
        s = db.global_stats()
        await update.message.reply_text(
            f"👋 Welcome back, *{user.first_name}*!\n\n"
            f"🛡️ *Super Admin Panel — ForceHub*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Users: `{s['total_users']}` | "
            f"🎨 Creators: `{s['total_creators']}`\n"
            f"🎯 Campaigns: `{s['total_campaigns']}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin(),
        )

    elif is_creator(uid):
        cr = db.get_creator(uid)
        days = db.creator_days_left(uid)
        status = "✅ Active" if db.is_creator_active(uid) else "❌ Expired"
        await update.message.reply_text(
            f"👋 Welcome, *{cr.get('name', user.first_name)}*!\n\n"
            f"🎨 *Creator Dashboard — ForceHub*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {status} | ⏳ Days Left: `{days}`\n"
            f"🎯 Campaigns: `{len(cr.get('campaigns', []))}` | "
            f"📢 Channels: `{len(cr.get('channels', []))}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_creator(),
        )

    else:
        await update.message.reply_text(
            f"🚀 Welcome to *ForceHub*, {user.first_name}!\n\n"
            f"The premium content unlock platform.\n"
            f"📢 Join channels → 🔓 Unlock exclusive content!\n\n"
            f"Use the menu below to get started 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_user(),
        )


# ─────────────────────────────────────────────────────────────────
# CAMPAIGN ACCESS FLOW
# ─────────────────────────────────────────────────────────────────

async def _handle_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE, campaign_id: str):
    user = update.effective_user
    uid  = user.id

    campaign = db.campaigns.get(campaign_id)
    if not campaign or not campaign.get("is_active"):
        await update.message.reply_text("❌ This campaign is not active or doesn't exist.")
        return

    db.track("campaign_clicks", campaign_id)

    channels    = campaign.get("channels", [])
    not_joined  = await check_channel_membership(context.bot, uid, channels)

    if not_joined:
        buttons = [
            [InlineKeyboardButton(f"📢 Join Channel {i + 1}",
                                  url=f"https://t.me/{c.lstrip('@')}")]
            for i, c in enumerate(not_joined)
        ]
        buttons.append(
            [InlineKeyboardButton("✅ I've Joined — Verify Now",
                                  callback_data=f"verify_{campaign_id}")]
        )
        await update.message.reply_text(
            f"🔐 *Content Locked*\n\n"
            f"Join the channel(s) below to unlock this content:\n\n"
            + "\n".join(f"• `{c}`" for c in not_joined)
            + "\n\nAfter joining, tap *Verify* ✅",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── Check referral requirement ────────────────────────────────
    ref_req = campaign.get("referral_required", 0)
    u       = db.get_or_create_user(uid)
    user_refs = u.get("referral_count", 0)

    if ref_req > 0 and user_refs < ref_req:
        bot_me = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
        needed   = ref_req - user_refs
        await update.message.reply_text(
            f"👥 *Referral Required*\n\n"
            f"This content requires *{ref_req} referrals*.\n"
            f"Your count: `{user_refs}/{ref_req}`\n\n"
            f"🔗 Your referral link:\n`{ref_link}`\n\n"
            f"*{needed} more referral(s) needed!*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── All checks passed — unlock! ───────────────────────────────
    await deliver_material(context.bot, update.message.chat_id, campaign)
    db.track("unlock_success", campaign_id)
    u["unlocked_campaigns"] = list(set(u.get("unlocked_campaigns", []) + [campaign_id]))
    db.save()


# ─────────────────────────────────────────────────────────────────
# MAIN CALLBACK QUERY ROUTER
# ─────────────────────────────────────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid  = query.from_user.id

    # ══════════════════════════════════════════
    #  VERIFY CALLBACK
    # ══════════════════════════════════════════
    if data.startswith("verify_"):
        campaign_id = data[7:]
        campaign    = db.campaigns.get(campaign_id)
        if not campaign:
            await query.edit_message_text("❌ Campaign not found.")
            return

        channels   = campaign.get("channels", [])
        not_joined = await check_channel_membership(context.bot, uid, channels)

        if not_joined:
            buttons = [
                [InlineKeyboardButton(f"📢 Join Channel {i+1}",
                                      url=f"https://t.me/{c.lstrip('@')}")]
                for i, c in enumerate(not_joined)
            ]
            buttons.append(
                [InlineKeyboardButton("✅ Verify Again", callback_data=f"verify_{campaign_id}")]
            )
            await query.edit_message_text(
                "❌ *Still not joined!*\n\nYou haven't joined:\n"
                + "\n".join(f"• `{c}`" for c in not_joined),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        db.track("verification_success", campaign_id)

        ref_req  = campaign.get("referral_required", 0)
        u        = db.get_or_create_user(uid)
        user_refs = u.get("referral_count", 0)

        if ref_req > 0 and user_refs < ref_req:
            bot_me   = await context.bot.get_me()
            ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
            needed   = ref_req - user_refs
            await query.edit_message_text(
                f"👥 *{needed} more referral(s) needed!*\n\n"
                f"Your referral link:\n`{ref_link}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await query.edit_message_text("🎁 *Unlocking content…*", parse_mode=ParseMode.MARKDOWN)
        await deliver_material(context.bot, query.message.chat_id, campaign)
        db.track("unlock_success", campaign_id)
        u["unlocked_campaigns"] = list(set(u.get("unlocked_campaigns", []) + [campaign_id]))
        db.save()
        return

    # ══════════════════════════════════════════
    #  USER MENU CALLBACKS
    # ══════════════════════════════════════════
    if data == "u_back":
        await query.edit_message_text(
            "🚀 *ForceHub — Main Menu*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_user(),
        )

    elif data == "u_unlock":
        await query.edit_message_text(
            "🔓 *Unlock Content*\n\n"
            "Get a campaign link from a creator and open it!\n\n"
            "Format: `t.me/YourBot?start=CAMPAIGN_ID`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_user(),
        )

    elif data == "u_unlocks":
        u = db.get_user(uid)
        unlocked = u.get("unlocked_campaigns", []) if u else []
        text = "📚 *Your Unlocked Content:*\n\n"
        if unlocked:
            for cid in unlocked:
                c   = db.campaigns.get(cid, {})
                mat = db.materials.get(c.get("material_id", ""), {})
                text += f"✅ `{cid}` — {mat.get('title', 'Unknown')}\n"
        else:
            text += "📭 Nothing unlocked yet."
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_back_user())

    elif data == "u_referral":
        u        = db.get_or_create_user(uid)
        bot_me   = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
        await query.edit_message_text(
            f"👥 *Your Referral Stats*\n\n"
            f"Total Referrals: `{u.get('referral_count', 0)}`\n\n"
            f"🔗 Your link:\n`{ref_link}`\n\n"
            f"Share this to earn referral unlocks!",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_user(),
        )

    elif data == "u_help":
        await query.edit_message_text(
            "❓ *Help*\n\n"
            "🔓 *Unlock Content:* Open a campaign link from a creator\n"
            "📚 *My Unlocks:* View content you've already unlocked\n"
            "👥 *Referrals:* Invite friends to earn bonus unlocks\n\n"
            "Need support? Contact the creator or admin.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_user(),
        )

    # ══════════════════════════════════════════
    #  CREATOR MENU CALLBACKS
    # ══════════════════════════════════════════
    elif data == "c_dash":
        if not is_creator(uid):
            await query.answer("❌ Not a creator!", show_alert=True); return
        cr   = db.get_creator(uid)
        days = db.creator_days_left(uid)
        status = "✅ Active" if db.is_creator_active(uid) else "❌ Expired"
        camps  = cr.get("campaigns", [])
        total_unlocks = sum(
            db.analytics.get("unlock_success", {}).get(cid, 0) for cid in camps
        )
        await query.edit_message_text(
            f"📊 *Creator Dashboard*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *{cr.get('name', 'Creator')}*\n"
            f"Status: {status} | ⏳ `{days}` days left\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Materials: `{len(cr.get('materials', []))}`\n"
            f"🎯 Campaigns: `{len(camps)}`\n"
            f"📢 Channels:  `{len(cr.get('channels', []))}`\n"
            f"🔓 Total Unlocks: `{total_unlocks}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_creator(),
        )

    elif data == "c_setup":
        if not is_creator(uid):
            await query.answer("❌ Not a creator!", show_alert=True); return
        if not db.is_creator_active(uid):
            await query.answer("⏰ Subscription expired!", show_alert=True); return
        # Trigger setup conversation via fake command context
        await query.edit_message_text(
            "🔧 Use the /setup command to create a new campaign.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_creator(),
        )

    elif data == "c_channels":
        if not is_creator(uid):
            await query.answer("❌ Not a creator!", show_alert=True); return
        if not db.is_creator_active(uid):
            await query.answer("⏰ Subscription expired!", show_alert=True); return
        cr = db.get_creator(uid)
        ch_list = cr.get("channels", [])
        text = "📢 *Your Channels*\n\n"
        text += ("\n".join(f"{i+1}. `{ch}`" for i, ch in enumerate(ch_list))
                 if ch_list else "No channels added yet.\nUse /setup to add channels.")
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_back_creator())

    elif data == "c_materials":
        if not is_creator(uid):
            await query.answer("❌ Not a creator!", show_alert=True); return
        if not db.is_creator_active(uid):
            await query.answer("⏰ Subscription expired!", show_alert=True); return
        cr  = db.get_creator(uid)
        mat_ids = cr.get("materials", [])
        text = "📦 *Your Materials*\n\n"
        if mat_ids:
            for mid in mat_ids[-10:]:
                mat = db.materials.get(mid, {})
                text += f"• `{mid}` — *{mat.get('title','Untitled')}* ({mat.get('file_type','?')})\n"
        else:
            text += "No materials yet. Use /setup to add one."
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_back_creator())

    elif data == "c_stats":
        if not is_creator(uid):
            await query.answer("❌ Not a creator!", show_alert=True); return
        cr    = db.get_creator(uid)
        camps = cr.get("campaigns", [])
        text  = "📈 *Campaign Analytics*\n\n"
        for cid in camps[-10:]:
            c   = db.campaigns.get(cid, {})
            mat = db.materials.get(c.get("material_id", ""), {})
            clicks  = db.analytics.get("campaign_clicks", {}).get(cid, 0)
            verif   = db.analytics.get("verification_success", {}).get(cid, 0)
            unlocks = db.analytics.get("unlock_success", {}).get(cid, 0)
            text += (
                f"🎯 *{mat.get('title', cid)}*\n"
                f"   👆 Clicks: `{clicks}` | ✅ Verified: `{verif}` | 🔓 Unlocked: `{unlocks}`\n\n"
            )
        if not camps:
            text += "No campaigns yet."
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_back_creator())

    elif data == "c_renew":
        price = db.settings.get("price", 199)
        upi   = db.settings.get("upi_id", "N/A")
        days  = db.creator_days_left(uid)
        await query.edit_message_text(
            f"🔄 *Renew Creator Panel*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Days Remaining: `{days}`\n"
            f"💰 Price: *₹{price}*\n"
            f"💳 UPI: `{upi}`\n\n"
            f"*Steps:*\n"
            f"1️⃣ Pay ₹{price} → `{upi}`\n"
            f"2️⃣ Screenshot the payment\n"
            f"3️⃣ Send to admin with your ID: `{uid}`\n\n"
            f"Admin activates within 24h.\n"
            f"Or use /renewpanel for details.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_creator(),
        )

    elif data == "c_broadcast":
        if not is_creator(uid):
            await query.answer("❌ Not a creator!", show_alert=True); return
        if not db.is_creator_active(uid):
            await query.answer("⏰ Subscription expired!", show_alert=True); return
        context.user_data["cbcast_step"] = "content"
        await query.edit_message_text(
            "📣 *Broadcast to Your Users*\n\n"
            "Send the content to broadcast.\n"
            "Supports: text, photo, video, document (with optional caption)\n\n"
            "Send your content now:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="c_dash")]]
            ),
        )

    # ══════════════════════════════════════════
    #  ADMIN MENU CALLBACKS
    # ══════════════════════════════════════════
    elif data == "a_panel":
        if not is_admin(uid): await query.answer("❌ Not admin!", show_alert=True); return
        s = db.global_stats()
        await query.edit_message_text(
            f"🛡️ *Super Admin Panel — ForceHub*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Users:     `{s['total_users']}`\n"
            f"🎨 Creators:  `{s['total_creators']}`\n"
            f"🎯 Campaigns: `{s['total_campaigns']}`\n"
            f"📦 Materials: `{s['total_materials']}`\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Trial: `{db.settings.get('trial_days', 90)}` days | "
            f"💰 Price: `₹{db.settings.get('price', 199)}`\n"
            f"💳 UPI: `{db.settings.get('upi_id', 'Not set')}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin(),
        )

    elif data == "a_stats":
        if not is_admin(uid): return
        s = db.global_stats()
        await query.edit_message_text(
            f"📊 *Global Stats — ForceHub*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users:    `{s['total_users']}`\n"
            f"🎨 Total Creators: `{s['total_creators']}`\n"
            f"🎯 Total Campaigns:`{s['total_campaigns']}`\n"
            f"📦 Total Materials:`{s['total_materials']}`\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🆕 Today's Joins:   `{s['today_joins']}`\n"
            f"🔓 Today's Unlocks: `{s['today_unlocks']}`\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 `{now_str()}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data == "a_broadcast":
        if not is_admin(uid): return
        await query.edit_message_text(
            "📣 *Super Admin Broadcast*\n\nChoose target audience:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_broadcast_target(),
        )

    elif data in ("bcast_users", "bcast_creators", "bcast_everyone"):
        if not is_admin(uid): return
        context.user_data["bcast_target"] = data.split("_")[1]
        context.user_data["bcast_step"]   = "content"
        await query.edit_message_text(
            f"📝 *Send Broadcast Content*\n\n"
            f"Target: *{context.user_data['bcast_target'].title()}*\n\n"
            f"Send your content (text / photo / video / document).\n"
            f"Captions are supported for media.\n\n"
            f"👇 Send now:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]
            ),
        )

    elif data == "bcast_skip_buttons":
        if not is_admin(uid): return
        await _execute_broadcast(update, context, reply_markup=None)

    elif data == "a_export":
        if not is_admin(uid): return
        await query.edit_message_text(
            "📤 *Export Data*\n\nUse the /export command for a full JSON export.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data == "a_ban":
        if not is_admin(uid): return
        await query.edit_message_text(
            "🚫 *Ban Creator*\n\nCommand: `/bancreator <user_id>`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data == "a_price":
        if not is_admin(uid): return
        await query.edit_message_text(
            f"💰 *Set Price*\n\nCurrent: ₹{db.settings.get('price', 199)}\n\n"
            f"Command: `/setprice <amount>`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data == "a_trial":
        if not is_admin(uid): return
        await query.edit_message_text(
            f"⏱ *Set Trial Days*\n\nCurrent: {db.settings.get('trial_days', 90)} days\n\n"
            f"Command: `/settrial <days>`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data == "a_upi":
        if not is_admin(uid): return
        await query.edit_message_text(
            f"💳 *Set UPI ID*\n\nCurrent: `{db.settings.get('upi_id', 'Not set')}`\n\n"
            f"Command: `/setupi <upi_id>`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )


# ─────────────────────────────────────────────────────────────────
# BROADCAST EXECUTION HELPERS
# ─────────────────────────────────────────────────────────────────

async def _execute_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              reply_markup: Optional[InlineKeyboardMarkup]):
    """Build target list and fire the broadcast."""
    target       = context.user_data.get("bcast_target", "users")
    content_type = context.user_data.get("bcast_content_type", "text")
    content      = context.user_data.get("bcast_content")
    caption      = context.user_data.get("bcast_caption", "")

    if target == "users":
        user_ids = [int(k) for k in db.users]
    elif target == "creators":
        user_ids = [int(k) for k in db.creators]
    else:  # everyone
        user_ids = list({int(k) for k in db.users} | {int(k) for k in db.creators})

    count = len(user_ids)

    # Show progress
    progress_text = f"📣 Broadcasting to *{count}* recipients…"
    try:
        if update.callback_query:
            progress = await update.callback_query.edit_message_text(
                progress_text, parse_mode=ParseMode.MARKDOWN
            )
        else:
            progress = await update.message.reply_text(
                progress_text, parse_mode=ParseMode.MARKDOWN
            )
    except Exception:
        progress = None

    stats = await batch_broadcast(
        context.application, user_ids, content_type, content, caption, reply_markup
    )

    rate = round(stats["sent"] / max(count, 1) * 100)
    result = (
        f"✅ *Broadcast Complete!*\n\n"
        f"👥 Target:    *{target.title()}*\n"
        f"📊 Total:     `{count}`\n"
        f"✅ Sent:      `{stats['sent']}`\n"
        f"❌ Failed:    `{stats['failed']}`\n"
        f"📈 Success:   `{rate}%`\n"
        f"🕐 `{now_str()}`"
    )
    try:
        if progress:
            await progress.edit_text(result, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

    # Clear broadcast state
    for k in ("bcast_step", "bcast_target", "bcast_content_type", "bcast_content", "bcast_caption"):
        context.user_data.pop(k, None)


# ─────────────────────────────────────────────────────────────────
# GENERAL MESSAGE HANDLER  (catches broadcast inputs)
# ─────────────────────────────────────────────────────────────────

async def general_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = update.message

    # ── Admin broadcast: collect content ──────────────────────────
    if is_admin(uid) and context.user_data.get("bcast_step") == "content":
        ct = "text"
        content = None
        caption = msg.caption or ""

        if msg.text:
            ct, content = "text", msg.text
        elif msg.photo:
            ct, content = "photo",    msg.photo[-1].file_id
        elif msg.video:
            ct, content = "video",    msg.video.file_id
        elif msg.document:
            ct, content = "document", msg.document.file_id
        else:
            await msg.reply_text("❌ Unsupported content type.")
            return

        context.user_data["bcast_content_type"] = ct
        context.user_data["bcast_content"]      = content
        context.user_data["bcast_caption"]      = caption
        context.user_data["bcast_step"]         = "buttons"

        await msg.reply_text(
            "🔘 *Add Inline Buttons?* (optional)\n\n"
            "Format (one per line):\n"
            "`Button Label - https://url.com`\n\n"
            "Or send `skip` to broadcast without buttons:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⏭ Skip Buttons", callback_data="bcast_skip_buttons")]]
            ),
        )
        return

    # ── Admin broadcast: collect buttons ─────────────────────────
    if is_admin(uid) and context.user_data.get("bcast_step") == "buttons":
        btn_text = msg.text or "skip"
        reply_markup = parse_inline_buttons(btn_text)
        await _execute_broadcast(update, context, reply_markup)
        return

    # ── Creator broadcast: collect content ───────────────────────
    if is_creator(uid) and context.user_data.get("cbcast_step") == "content":
        if not db.is_creator_active(uid):
            await msg.reply_text("⏰ Subscription expired. Use /renewpanel")
            context.user_data.pop("cbcast_step", None)
            return

        ct = "text"
        content = None
        caption = msg.caption or ""

        if msg.text:
            ct, content = "text", msg.text
        elif msg.photo:
            ct, content = "photo",    msg.photo[-1].file_id
        elif msg.video:
            ct, content = "video",    msg.video.file_id
        elif msg.document:
            ct, content = "document", msg.document.file_id
        else:
            await msg.reply_text("❌ Unsupported type.")
            return

        # Build target: users who unlocked this creator's campaigns
        cr      = db.get_creator(uid)
        camp_ids = set(cr.get("campaigns", []))
        user_ids = [
            int(uid_str)
            for uid_str, u_data in db.users.items()
            if any(c in camp_ids for c in u_data.get("unlocked_campaigns", []))
        ]

        if not user_ids:
            await msg.reply_text("📭 No users have unlocked your content yet.")
            context.user_data.pop("cbcast_step", None)
            return

        progress = await msg.reply_text(
            f"📣 Broadcasting to *{len(user_ids)}* user(s)…",
            parse_mode=ParseMode.MARKDOWN,
        )
        stats = await batch_broadcast(context.application, user_ids, ct, content, caption)
        await progress.edit_text(
            f"✅ *Broadcast Done!*\n\n"
            f"✅ Sent: `{stats['sent']}` | ❌ Failed: `{stats['failed']}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.pop("cbcast_step", None)


# ─────────────────────────────────────────────────────────────────
# SETUP MATERIAL CONVERSATION
# ─────────────────────────────────────────────────────────────────

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_creator(uid):
        await update.message.reply_text("❌ Not a registered creator. Contact admin.")
        return ConversationHandler.END
    if not db.is_creator_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["setup"] = {}

    await update.message.reply_text(
        "🔧 *Create New Campaign — Step 1 / 5*\n\n"
        "Send the channel username(s) users must join.\n"
        "Format: `@channel1 @channel2`\n\n"
        "⚠️ Bot must be *admin* in those channels!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="setup_cancel")]]
        ),
    )
    return SETUP_CHANNEL


async def setup_recv_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text     = update.message.text.strip()
    channels = [t.strip() for t in text.split() if t.startswith("@") or t.lstrip("-").isdigit()]

    if not channels:
        await update.message.reply_text(
            "❌ Invalid format.\nSend: `@channel1 @channel2`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return SETUP_CHANNEL

    # Validate bot is admin
    valid, invalid = [], []
    me = await context.bot.get_me()
    for ch in channels:
        try:
            m = await context.bot.get_chat_member(ch, me.id)
            if m.status in ("administrator", "creator"):
                valid.append(ch)
            else:
                invalid.append(ch)
        except Exception:
            invalid.append(ch)

    if not valid:
        await update.message.reply_text(
            "❌ Bot is NOT admin in any channel you sent.\n\n"
            "Add the bot as admin first, then retry.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return SETUP_CHANNEL

    if invalid:
        await update.message.reply_text(
            f"⚠️ Skipped (bot not admin): {', '.join(invalid)}\n"
            f"✅ Will use: {', '.join(valid)}",
            parse_mode=ParseMode.MARKDOWN,
        )

    context.user_data["setup"]["channels"] = valid

    # Persist to creator
    cr = db.get_creator(update.effective_user.id)
    for ch in valid:
        if ch not in cr.get("channels", []):
            cr.setdefault("channels", []).append(ch)
    db.save()

    await update.message.reply_text(
        f"✅ Channels: {', '.join(valid)}\n\n"
        f"*Step 2 / 5:* Choose material type:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Text",     callback_data="mtype_text"),
             InlineKeyboardButton("🖼 Photo",    callback_data="mtype_photo")],
            [InlineKeyboardButton("🎥 Video",    callback_data="mtype_video"),
             InlineKeyboardButton("📄 Document", callback_data="mtype_document")],
            [InlineKeyboardButton("❌ Cancel",   callback_data="setup_cancel")],
        ]),
    )
    return SETUP_MATERIAL_TYPE


async def setup_recv_material_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mtype = query.data.split("_", 1)[1]
    context.user_data["setup"]["file_type"] = mtype

    await query.edit_message_text(
        f"*Step 3 / 5:* Enter a title for this material\n\n"
        f"Example: `CUET BST Notes 2026`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SETUP_MATERIAL_TITLE


async def setup_recv_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if len(title) < 3:
        await update.message.reply_text("❌ Title too short (min 3 chars).")
        return SETUP_MATERIAL_TITLE

    context.user_data["setup"]["title"] = title
    ftype = context.user_data["setup"]["file_type"]
    label = {"text": "text message", "photo": "photo", "video": "video",
             "document": "document/PDF"}.get(ftype, ftype)

    await update.message.reply_text(
        f"*Step 4 / 5:* Send your {label}\n"
        f"(Captions accepted for media)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SETUP_MATERIAL_CONTENT


async def setup_recv_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data["setup"]
    ftype = setup["file_type"]
    msg   = update.message

    if ftype == "text":
        if not msg.text:
            await update.message.reply_text("❌ Send a text message.")
            return SETUP_MATERIAL_CONTENT
        setup["file_id"]     = None
        setup["description"] = msg.text

    elif ftype == "photo":
        if not msg.photo:
            await update.message.reply_text("❌ Send a photo.")
            return SETUP_MATERIAL_CONTENT
        setup["file_id"]     = msg.photo[-1].file_id
        setup["description"] = msg.caption or ""

    elif ftype == "video":
        if not msg.video:
            await update.message.reply_text("❌ Send a video.")
            return SETUP_MATERIAL_CONTENT
        setup["file_id"]     = msg.video.file_id
        setup["description"] = msg.caption or ""

    elif ftype == "document":
        if not msg.document:
            await update.message.reply_text("❌ Send a document/PDF.")
            return SETUP_MATERIAL_CONTENT
        setup["file_id"]     = msg.document.file_id
        setup["description"] = msg.caption or ""

    await update.message.reply_text(
        f"*Step 5 / 5:* Referral requirement\n\n"
        f"How many referrals must a user have to unlock?\n"
        f"Send `0` = no referral needed (channel join only).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SETUP_REFERRAL_COUNT


async def setup_recv_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text.strip())
        if count < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send 0 or a positive integer.")
        return SETUP_REFERRAL_COUNT

    setup = context.user_data["setup"]
    uid   = update.effective_user.id

    # ── Persist material ──────────────────────────────────────────
    material_id = str(uuid.uuid4())[:10]
    db.materials[material_id] = {
        "creator_id":  str(uid),
        "title":       setup["title"],
        "description": setup.get("description", ""),
        "file_id":     setup.get("file_id"),
        "file_type":   setup["file_type"],
        "created_at":  datetime.now().isoformat(),
    }
    cr = db.get_creator(uid)
    cr.setdefault("materials", []).append(material_id)

    # ── Create campaign ───────────────────────────────────────────
    campaign_id = db.create_campaign(uid, material_id, setup["channels"], count)
    db.save(force=True)

    bot_me = await context.bot.get_me()
    link   = f"https://t.me/{bot_me.username}?start={campaign_id}"

    await update.message.reply_text(
        f"🎉 *Campaign Created!*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Material:   *{setup['title']}*\n"
        f"🆔 Campaign:   `{campaign_id}`\n"
        f"📢 Channels:   {', '.join(setup['channels'])}\n"
        f"👥 Referrals:  `{count}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 *Share Link:*\n`{link}`\n\n"
        f"Share this link with your audience!",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Setup cancelled.")
    elif update.message:
        await update.message.reply_text("❌ Setup cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────

async def cmd_settrial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/settrial <days>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        days = int(context.args[0])
        if days < 1: raise ValueError
        db.set_trial_days(days)
        await update.message.reply_text(
            f"✅ Trial period set to *{days} days* globally.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")


async def cmd_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/setprice <amount>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        price = int(context.args[0])
        db.set_price(price)
        await update.message.reply_text(f"✅ Price set to *₹{price}*", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.")


async def cmd_setupi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/setupi <upi_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    upi = context.args[0]
    db.set_upi(upi)
    await update.message.reply_text(f"✅ UPI ID updated: `{upi}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_globalstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    s = db.global_stats()
    await update.message.reply_text(
        f"📊 *Global Stats — ForceHub*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users:    `{s['total_users']}`\n"
        f"🎨 Total Creators: `{s['total_creators']}`\n"
        f"🎯 Total Campaigns:`{s['total_campaigns']}`\n"
        f"📦 Total Materials:`{s['total_materials']}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆕 Today Joins:    `{s['today_joins']}`\n"
        f"🔓 Today Unlocks:  `{s['today_unlocks']}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now_str()}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_addcreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/addcreator <user_id> [name]`", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        creator_id = int(context.args[0])
        name       = " ".join(context.args[1:]) if len(context.args) > 1 else f"Creator_{creator_id}"
        trial_days = db.settings.get("trial_days", 90)
        if db.get_creator(creator_id):
            db.renew_creator(creator_id)
            await update.message.reply_text(
                f"✅ Creator `{creator_id}` renewed for *{trial_days} days*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            db.create_creator(creator_id, "", name)
            await update.message.reply_text(
                f"✅ Creator *{name}* (`{creator_id}`) added — *{trial_days}-day trial*.",
                parse_mode=ParseMode.MARKDOWN,
            )
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")


async def cmd_bancreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/bancreator <creator_id>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        cid     = str(int(context.args[0]))
        creator = db.creators.get(cid)
        if not creator:
            await update.message.reply_text("❌ Creator not found."); return
        creator["trial_start"] = "2000-01-01T00:00:00"
        creator["trial_days"]  = 0
        db.save(force=True)
        await update.message.reply_text(
            f"🚫 Creator `{cid}` has been *banned/expired*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(
        "📣 *Super Admin Broadcast*\n\nSelect target:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_broadcast_target(),
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    import tempfile
    try:
        payload = {
            "exported_at": datetime.now().isoformat(),
            "stats":       db.global_stats(),
            "users":       db.users,
            "creators":    db.creators,
            "campaigns":   db.campaigns,
            "analytics":   db.analytics,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            tmp = f.name
        with open(tmp, "rb") as f:
            await update.message.reply_document(
                f,
                filename=f"forcehub_export_{today_str()}.json",
                caption=f"📤 ForceHub Export — {now_str()}",
            )
        os.unlink(tmp)
    except Exception as exc:
        logger.error("Export failed: %s", exc)
        await update.message.reply_text("❌ Export failed.")


# ─────────────────────────────────────────────────────────────────
# CREATOR COMMANDS
# ─────────────────────────────────────────────────────────────────

async def cmd_renewpanel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    price = db.settings.get("price", 199)
    upi   = db.settings.get("upi_id", "N/A")
    days  = db.creator_days_left(uid)

    if not is_creator(uid):
        await update.message.reply_text(
            "❌ Not a registered creator. Contact admin to get access."
        )
        return

    status = "✅ Active" if db.is_creator_active(uid) else "❌ Expired"
    await update.message.reply_text(
        f"🔄 *Renew Creator Panel*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Status: {status} | ⏳ `{days}` days left\n"
        f"💰 Renewal Price: *₹{price}*\n"
        f"💳 UPI ID: `{upi}`\n\n"
        f"*How to Renew:*\n"
        f"1️⃣ Pay ₹{price} to `{upi}`\n"
        f"2️⃣ Screenshot the payment\n"
        f"3️⃣ Contact admin with screenshot + your ID: `{uid}`\n\n"
        f"Panel activated within 24 hours.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_mycampaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_creator(uid):
        await update.message.reply_text("❌ Not a creator."); return
    cr    = db.get_creator(uid)
    camps = cr.get("campaigns", [])
    if not camps:
        await update.message.reply_text("📭 No campaigns. Use /setup to create one."); return

    bot_me = await context.bot.get_me()
    text   = "🎯 *Your Campaigns:*\n\n"
    for cid in camps[-10:]:
        c   = db.campaigns.get(cid, {})
        mat = db.materials.get(c.get("material_id", ""), {})
        link    = f"https://t.me/{bot_me.username}?start={cid}"
        status  = "✅" if c.get("is_active") else "❌"
        clicks  = db.analytics.get("campaign_clicks", {}).get(cid, 0)
        unlocks = db.analytics.get("unlock_success", {}).get(cid, 0)
        text += (
            f"{status} *{mat.get('title', 'Untitled')}*\n"
            f"   ID: `{cid}` | 👆{clicks} | 🔓{unlocks}\n"
            f"   🔗 `{link}`\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_creator(uid):
        await update.message.reply_text("❌ Not a creator."); return
    if not db.is_creator_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel"); return

    cr      = db.get_creator(uid)
    mat_ids = cr.get("materials", [])
    text    = "📦 *Your Materials:*\n\n"
    if mat_ids:
        for mid in mat_ids[-10:]:
            mat  = db.materials.get(mid, {})
            text += f"• `{mid}` — *{mat.get('title', 'Untitled')}* ({mat.get('file_type','?')})\n"
    else:
        text += "No materials. Use /setup to add one."
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_creator(uid):
        await update.message.reply_text("❌ Not a creator."); return
    if not db.is_creator_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel"); return

    cr       = db.get_creator(uid)
    channels = cr.get("channels", [])
    text     = "📢 *Your Channels:*\n\n"
    text    += ("\n".join(f"• `{ch}`" for ch in channels)
                if channels else "No channels yet.\nUse /setup to add channels.")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_broadcast_my_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_creator(uid):
        await update.message.reply_text("❌ Not a creator."); return
    if not db.is_creator_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel"); return

    context.user_data["cbcast_step"] = "content"
    await update.message.reply_text(
        "📣 *Broadcast to Your Users*\n\n"
        "Send your message (text / photo / video / document):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="c_dash")]]
        ),
    )


# ─────────────────────────────────────────────────────────────────
# BOT STARTUP
# ─────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    """Register commands and start background tasks."""
    commands = [
        # User
        BotCommand("start",              "Main menu"),
        # Creator
        BotCommand("setup",              "Create a new campaign"),
        BotCommand("mycampaigns",        "View your campaigns"),
        BotCommand("materials",          "Manage materials"),
        BotCommand("channels",           "Manage channels"),
        BotCommand("broadcast_my_users", "Broadcast to your users"),
        BotCommand("renewpanel",         "Renew creator subscription"),
        # Admin
        BotCommand("globalstats",        "Global analytics"),
        BotCommand("broadcast",          "Super admin broadcast"),
        BotCommand("addcreator",         "Add / renew a creator"),
        BotCommand("bancreator",         "Ban a creator"),
        BotCommand("settrial",           "Set global trial days"),
        BotCommand("setprice",           "Set renewal price"),
        BotCommand("setupi",             "Set UPI ID"),
        BotCommand("export",             "Export data JSON"),
    ]
    await app.bot.set_my_commands(commands)
    asyncio.create_task(db.periodic_save())
    logger.info("🚀 ForceHub Bot started — %s", now_str())


def main():
    if not BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN is not set! Check your .env file.")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Setup Conversation ─────────────────────────────────────────
    setup_conv = ConversationHandler(
        entry_points=[
            CommandHandler("setup", cmd_setup),
        ],
        states={
            SETUP_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_recv_channels)
            ],
            SETUP_MATERIAL_TYPE: [
                CallbackQueryHandler(setup_recv_material_type, pattern=r"^mtype_")
            ],
            SETUP_MATERIAL_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_recv_title)
            ],
            SETUP_MATERIAL_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
                    & ~filters.COMMAND,
                    setup_recv_content,
                )
            ],
            SETUP_REFERRAL_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_recv_referral)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", setup_cancel),
            CallbackQueryHandler(setup_cancel, pattern=r"^setup_cancel$"),
        ],
        allow_reentry=True,
    )

    # ── Register handlers (order matters) ─────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(setup_conv)

    # Admin commands
    app.add_handler(CommandHandler("settrial",   cmd_settrial))
    app.add_handler(CommandHandler("setprice",   cmd_setprice))
    app.add_handler(CommandHandler("setupi",     cmd_setupi))
    app.add_handler(CommandHandler("globalstats",cmd_globalstats))
    app.add_handler(CommandHandler("addcreator", cmd_addcreator))
    app.add_handler(CommandHandler("bancreator", cmd_bancreator))
    app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    app.add_handler(CommandHandler("export",     cmd_export))

    # Creator commands
    app.add_handler(CommandHandler("materials",          cmd_materials))
    app.add_handler(CommandHandler("channels",           cmd_channels))
    app.add_handler(CommandHandler("mycampaigns",        cmd_mycampaigns))
    app.add_handler(CommandHandler("broadcast_my_users", cmd_broadcast_my_users))
    app.add_handler(CommandHandler("renewpanel",         cmd_renewpanel))

    # Specific callback before general router
    app.add_handler(CallbackQueryHandler(callback_router))

    # General message handler (broadcast inputs)
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            & ~filters.COMMAND,
            general_message_handler,
        )
    )

    logger.info("📡 Polling started…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
