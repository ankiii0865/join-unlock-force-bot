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
    ChatMemberHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from telegram import ChatMemberAdministrator
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

# ── HARDCODED SUPER ADMINS — these ALWAYS have full access ────────
# @chamgaadar | ANKIII YADAV | ID: 5695957392
SUPER_ADMIN_IDS: List[int] = [5695957392]

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
_env_admins: List[int] = [
    int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()
]
# Merge: super admins + env admins (deduplicated)
ADMIN_IDS: List[int] = list({*SUPER_ADMIN_IDS, *_env_admins})

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

# ── Creator onboarding + createcampaign conversation states ───────────────────
(
    ONBOARD_CHANNEL,           # 5 — waiting for channel username
    CREATECAMP_LINK,           # 6 — waiting for unlock content link
    CREATECAMP_CHANNELS,       # 7 — waiting for required channel(s)
) = range(5, 8)

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
    """Super admins hardcoded — ALWAYS returns True for them regardless of DB."""
    if user_id in SUPER_ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS or user_id in db.settings.get("admin_ids", [])


def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS


def can_use_creator_features(user_id: int) -> bool:
    """Admin can ALWAYS use creator features, even without being registered."""
    return is_creator(user_id) or is_admin(user_id)


def creator_is_active(user_id: int) -> bool:
    """Admin is always active. Creators check trial expiry."""
    if is_admin(user_id):
        return True
    return db.is_creator_active(user_id)


def ensure_admin_creator(user_id: int, username: str = "", name: str = "") -> dict:
    """Auto-register admin as creator with unlimited trial if not already one."""
    if not db.get_creator(user_id):
        db.creators[str(user_id)] = {
            "username": username,
            "name": name or f"Admin_{user_id}",
            "trial_start": "2000-01-01T00:00:00",
            "trial_days": 99999,
            "channels": [],
            "materials": [],
            "campaigns": [],
            "joined_at": datetime.now().isoformat(),
        }
        db.save(force=True)
    return db.get_creator(user_id)


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
        [InlineKeyboardButton("🚀 Become Creator",     callback_data="u_become_creator")],
        [InlineKeyboardButton("❓ Help",               callback_data="u_help")],
    ])


def kb_creator() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard",          callback_data="c_dash")],
        [InlineKeyboardButton("➕ New Campaign",        callback_data="c_setup"),
         InlineKeyboardButton("📦 Materials",          callback_data="c_materials")],
        [InlineKeyboardButton("🎯 My Campaigns",       callback_data="c_campaigns"),
         InlineKeyboardButton("📈 Analytics",          callback_data="c_stats")],
        [InlineKeyboardButton("📢 My Channels",        callback_data="c_channels"),
         InlineKeyboardButton("📣 Broadcast",          callback_data="c_broadcast")],
        [InlineKeyboardButton("🔗 Get Share Links",    callback_data="c_links"),
         InlineKeyboardButton("🔄 Renew Panel",        callback_data="c_renew")],
        [InlineKeyboardButton("❓ Help",               callback_data="c_help")],
    ])


def kb_admin() -> InlineKeyboardMarkup:
    """Main admin home panel."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats & Analytics",   callback_data="a_stats"),
         InlineKeyboardButton("📣 Broadcast",           callback_data="a_broadcast")],
        [InlineKeyboardButton("👥 All Users",           callback_data="a_users_0"),
         InlineKeyboardButton("🎨 All Creators",        callback_data="a_creators_0")],
        [InlineKeyboardButton("🎯 All Campaigns",       callback_data="a_campaigns_0"),
         InlineKeyboardButton("📦 All Materials",       callback_data="a_materials_0")],
        [InlineKeyboardButton("➕ Add Creator",         callback_data="a_addcreator_prompt"),
         InlineKeyboardButton("🚫 Ban Creator",         callback_data="a_ban_prompt")],
        [InlineKeyboardButton("💬 DM Any User",         callback_data="a_dm_prompt"),
         InlineKeyboardButton("🗑 Del Campaign",        callback_data="a_delcamp_prompt")],
        [InlineKeyboardButton("⚙️ Settings",            callback_data="a_settings"),
         InlineKeyboardButton("📤 Export JSON",         callback_data="a_export")],
    ])


def kb_admin_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏱ Set Trial Days",     callback_data="a_trial"),
         InlineKeyboardButton(f"💰 Set Price",          callback_data="a_price")],
        [InlineKeyboardButton(f"💳 Set UPI",            callback_data="a_upi"),
         InlineKeyboardButton(f"👑 Add Admin",          callback_data="a_addadmin_prompt")],
        [InlineKeyboardButton("🔙 Back",                callback_data="a_panel")],
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

    # ── Show appropriate menu — ADMIN CHECK ALWAYS FIRST ────────────
    if is_admin(uid):
        s = db.global_stats()
        badge = "👑 *SUPER ADMIN*" if is_super_admin(uid) else "🛡️ *Admin*"
        await update.message.reply_text(
            f"{badge} — ForceHub\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👋 Welcome back, *{user.first_name}*!\n"
            f"🆔 Your ID: `{uid}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Users:     `{s['total_users']}`  |  🎨 Creators: `{s['total_creators']}`\n"
            f"🎯 Campaigns: `{s['total_campaigns']}` |  📦 Materials: `{s['total_materials']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆕 Today Joins: `{s['today_joins']}` | 🔓 Unlocks: `{s['today_unlocks']}`\n"
            f"⏱ Trial: `{db.settings.get('trial_days',90)}d` | "
            f"💰 ₹`{db.settings.get('price',199)}` | "
            f"💳 `{db.settings.get('upi_id','Not set')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 `{now_str()}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin(),
        )

    elif is_creator(uid):
        cr     = db.get_creator(uid)
        days   = db.creator_days_left(uid)
        status = "✅ Active" if creator_is_active(uid) else "❌ Expired"
        camps  = cr.get("campaigns", []) if cr else []
        total_u = sum(db.analytics.get("unlock_success",{}).get(c,0) for c in camps)
        await update.message.reply_text(
            f"🎨 *Creator Panel — ForceHub*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👋 *{cr.get('name', user.first_name) if cr else user.first_name}*  |  `{uid}`\n"
            f"Status: {status}  |  ⏳ `{days}` days left\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Campaigns: `{len(camps)}`  |  🔓 Unlocks: `{total_u}`\n"
            f"📢 Channels: `{len(cr.get('channels',[]) if cr else [])}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 `{now_str()}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_creator(),
        )

    else:
        await update.message.reply_text(
            f"🚀 *Welcome to ForceHub*, {user.first_name}!\n\n"
            f"🔓 *Unlock premium content* by joining channels.\n"
            f"Get a campaign link from a creator and tap it!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎨 *Are you a creator?*\n"
            f"Tap *🚀 Become Creator* to protect your content\n"
            f"& build your own unlock campaigns — for free!\n\n"
            f"🆔 Your ID: `{uid}`",
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
            "❓ *Help — ForceHub*\n\n"
            "🔓 *Unlock Content:* Open a campaign link from a creator\n"
            "📚 *My Unlocks:* View content you\'ve already unlocked\n"
            "👥 *Referrals:* Invite friends to earn bonus unlocks\n"
            "🚀 *Become Creator:* Set up your own unlock campaigns\n\n"
            "Need support? Contact the creator or admin.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_user(),
        )

    elif data == "u_become_creator":
        if can_use_creator_features(uid):
            # Already a creator — go straight to creator dashboard
            ensure_admin_creator(uid, query.from_user.username or "", query.from_user.first_name or "")
            cr     = db.get_creator(uid)
            days   = db.creator_days_left(uid)
            status = "✅ Active" if creator_is_active(uid) else "❌ Expired"
            camps  = cr.get("campaigns", []) if cr else []
            total_u = sum(db.analytics.get("unlock_success",{}).get(c,0) for c in camps)
            await query.edit_message_text(
                f"🎨 *Creator Panel*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *{cr.get('name', query.from_user.first_name) if cr else query.from_user.first_name}*\n"
                f"Status: {status}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Campaigns: `{len(camps)}` | 🔓 Unlocks: `{total_u}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_creator(),
            )
        else:
            # Not yet a creator — show onboarding info + trigger via command
            bot_me = await context.bot.get_me()
            await query.edit_message_text(
                f"🚀 *Creator Mode — ForceHub*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Protect your content with force-subscribe campaigns.\n\n"
                f"*To get started:*\n"
                f"1️⃣ Add @{bot_me.username} as *Admin* in your channel\n"
                f"2️⃣ Grant: *Post Messages* + *Invite Users* permissions\n"
                f"3️⃣ Tap the button below to connect your channel\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 *It\'s completely free!*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Connect My Channel", callback_data="onboard_start")],
                    [InlineKeyboardButton("🔙 Back",               callback_data="u_back")],
                ]),
            )

    # ══════════════════════════════════════════
    #  CREATOR MENU CALLBACKS
    # ══════════════════════════════════════════
    elif data == "c_dash":
        if not can_use_creator_features(uid):
            await query.answer("❌ Creator access required!", show_alert=True); return
        if is_admin(uid):
            ensure_admin_creator(uid, query.from_user.username or "", query.from_user.first_name or "")
        cr     = db.get_creator(uid)
        days   = db.creator_days_left(uid)
        status = "✅ Active" if creator_is_active(uid) else "❌ Expired"
        camps  = cr.get("campaigns", []) if cr else []
        total_unlocks = sum(db.analytics.get("unlock_success",{}).get(cid,0) for cid in camps)
        total_clicks  = sum(db.analytics.get("campaign_clicks",{}).get(cid,0) for cid in camps)
        badge  = "👑 Admin+Creator" if is_admin(uid) else "🎨 Creator"
        expiry_line = "♾ No expiry (Admin)" if is_admin(uid) else f"⏳ {days} days left"
        await query.edit_message_text(
            f"{badge} *Dashboard*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *{cr.get('name','Creator') if cr else '?'}*  |  `{uid}`\n"
            f"Status: {status}  |  {expiry_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Materials: `{len(cr.get('materials',[]) if cr else [])}`\n"
            f"🎯 Campaigns: `{len(camps)}`\n"
            f"📢 Channels:  `{len(cr.get('channels',[]) if cr else [])}`\n"
            f"👆 Clicks:    `{total_clicks}`  |  🔓 Unlocks: `{total_unlocks}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_creator(),
        )

    elif data == "c_setup":
        # This is handled by the ConversationHandler entry point below
        # (CallbackQueryHandler pattern "^c_setup$" is in setup_conv entry_points)
        # Fallback in case it reaches here
        if not can_use_creator_features(uid):
            await query.answer("❌ Creator access required!", show_alert=True); return
        await query.answer()
        # Nothing — ConversationHandler handles it

    elif data == "c_channels":
        if not can_use_creator_features(uid):
            await query.answer("❌ Creator access required!", show_alert=True); return
        if not creator_is_active(uid):
            await query.answer("⏰ Subscription expired! Use /renewpanel", show_alert=True); return
        cr = db.get_creator(uid)
        if cr is None and is_admin(uid): cr = ensure_admin_creator(uid)
        ch_list = cr.get("channels", []) if cr else []
        text = "📢 *Your Channels*\n\n"
        text += ("\n".join(f"{i+1}. `{ch}`" for i, ch in enumerate(ch_list))
                 if ch_list else "No channels added yet.\nUse /setup to add channels.")
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_back_creator())

    elif data == "c_materials":
        if not can_use_creator_features(uid):
            await query.answer("❌ Creator access required!", show_alert=True); return
        if not creator_is_active(uid):
            await query.answer("⏰ Subscription expired! Use /renewpanel", show_alert=True); return
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
        if not can_use_creator_features(uid):
            await query.answer("❌ Creator access required!", show_alert=True); return
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
        if not can_use_creator_features(uid):
            await query.answer("❌ Creator access required!", show_alert=True); return
        if not creator_is_active(uid):
            await query.answer("⏰ Subscription expired! Use /renewpanel", show_alert=True); return
        context.user_data["cbcast_step"] = "content"
        await query.edit_message_text(
            "📣 *Broadcast to Your Users*\n\n"
            "Send your content (text / photo / video / document).\n"
            "Captions supported for media.\n\n"
            "👇 Send now:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="c_dash")]]
            ),
        )

    # ── NEW: My Campaigns list ────────────────────────────────────
    elif data == "c_campaigns":
        if not can_use_creator_features(uid): await query.answer("❌ Creator access required!", show_alert=True); return
        cr      = db.get_creator(uid)
        camps   = cr.get("campaigns", [])
        if not camps:
            await query.edit_message_text(
                "📭 *No Campaigns Yet*\n\nUse *New Campaign* to create your first one!",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ New Campaign", callback_data="c_setup")],
                    [InlineKeyboardButton("🔙 Back",        callback_data="c_dash")],
                ]),
            ); return
        bot_me = await context.bot.get_me()
        text   = f"🎯 *Your Campaigns* (`{len(camps)}` total)\n\n"
        btns   = []
        for cid in camps[-12:]:
            c       = db.campaigns.get(cid, {})
            mat     = db.materials.get(c.get("material_id",""), {})
            active  = "✅" if c.get("is_active") else "❌"
            clicks  = db.analytics.get("campaign_clicks",{}).get(cid,0)
            unlocks = db.analytics.get("unlock_success",{}).get(cid,0)
            text   += (f"{active} `{cid}` — *{mat.get('title','?')[:18]}*\n"
                       f"   👆{clicks} clicks | 🔓{unlocks} unlocks\n\n")
            btns.append([
                InlineKeyboardButton(f"{'✅' if c.get('is_active') else '❌'} {cid[:8]}",
                                     callback_data=f"c_togglecamp_{cid}"),
                InlineKeyboardButton("🔗 Link", callback_data=f"c_camplink_{cid}"),
            ])
        btns.append([InlineKeyboardButton("➕ New Campaign", callback_data="c_setup"),
                     InlineKeyboardButton("🔙 Back",         callback_data="c_dash")])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(btns))

    # ── Toggle campaign active/inactive ──────────────────────────
    elif data.startswith("c_togglecamp_"):
        if not can_use_creator_features(uid): await query.answer("❌ Creator access required!", show_alert=True); return
        cid  = data[len("c_togglecamp_"):]
        camp = db.campaigns.get(cid)
        if not camp or camp.get("creator_id") != str(uid):
            await query.answer("❌ Not your campaign!", show_alert=True); return
        camp["is_active"] = not camp.get("is_active", True)
        db.save(force=True)
        status = "✅ Activated" if camp["is_active"] else "❌ Deactivated"
        await query.answer(f"{status} campaign {cid}", show_alert=True)
        # Refresh campaigns view
        cr      = db.get_creator(uid)
        camps   = cr.get("campaigns", [])
        bot_me  = await context.bot.get_me()
        text    = f"🎯 *Your Campaigns* (`{len(camps)}` total)\n\n"
        btns    = []
        for c_id in camps[-12:]:
            c       = db.campaigns.get(c_id, {})
            mat     = db.materials.get(c.get("material_id",""), {})
            active  = "✅" if c.get("is_active") else "❌"
            clicks  = db.analytics.get("campaign_clicks",{}).get(c_id,0)
            unlocks = db.analytics.get("unlock_success",{}).get(c_id,0)
            text   += (f"{active} `{c_id}` — *{mat.get('title','?')[:18]}*\n"
                       f"   👆{clicks} | 🔓{unlocks}\n\n")
            btns.append([
                InlineKeyboardButton(f"{'✅' if c.get('is_active') else '❌'} {c_id[:8]}",
                                     callback_data=f"c_togglecamp_{c_id}"),
                InlineKeyboardButton("🔗 Link", callback_data=f"c_camplink_{c_id}"),
            ])
        btns.append([InlineKeyboardButton("➕ New Campaign", callback_data="c_setup"),
                     InlineKeyboardButton("🔙 Back",         callback_data="c_dash")])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(btns))

    # ── Get share link for specific campaign ──────────────────────
    elif data.startswith("c_camplink_"):
        if not can_use_creator_features(uid): await query.answer("❌ Creator access required!", show_alert=True); return
        cid    = data[len("c_camplink_"):]
        camp   = db.campaigns.get(cid)
        if not camp or camp.get("creator_id") != str(uid):
            await query.answer("❌ Not your campaign!", show_alert=True); return
        mat    = db.materials.get(camp.get("material_id",""), {})
        bot_me = await context.bot.get_me()
        link   = f"https://t.me/{bot_me.username}?start={cid}"
        clicks  = db.analytics.get("campaign_clicks",{}).get(cid,0)
        verif   = db.analytics.get("verification_success",{}).get(cid,0)
        unlocks = db.analytics.get("unlock_success",{}).get(cid,0)
        await query.edit_message_text(
            f"🔗 *Campaign Details*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID:       `{cid}`\n"
            f"📦 Material: *{mat.get('title','?')}*\n"
            f"📢 Channels: {', '.join(camp.get('channels',[]))}\n"
            f"👥 Referrals needed: `{camp.get('referral_required',0)}`\n"
            f"Status: {'✅ Active' if camp.get('is_active') else '❌ Inactive'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👆 Clicks:  `{clicks}`\n"
            f"✅ Verified:`{verif}`\n"
            f"🔓 Unlocks: `{unlocks}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 *Share Link:*\n`{link}`\n\n"
            f"Tap link to copy 👆",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Link",   url=link)],
                [InlineKeyboardButton("🔙 My Campaigns", callback_data="c_campaigns"),
                 InlineKeyboardButton("🏠 Dashboard",   callback_data="c_dash")],
            ]),
        )

    # ── All share links at once ───────────────────────────────────
    elif data == "c_links":
        if not can_use_creator_features(uid): await query.answer("❌ Creator access required!", show_alert=True); return
        cr     = db.get_creator(uid)
        camps  = cr.get("campaigns", [])
        bot_me = await context.bot.get_me()
        if not camps:
            await query.edit_message_text(
                "📭 No campaigns. Use *New Campaign* to create one.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_creator()); return
        text = "🔗 *Your Campaign Share Links*\n\n"
        for cid in camps[-10:]:
            c   = db.campaigns.get(cid, {})
            mat = db.materials.get(c.get("material_id",""), {})
            st  = "✅" if c.get("is_active") else "❌"
            link = f"https://t.me/{bot_me.username}?start={cid}"
            text += f"{st} *{mat.get('title','?')[:20]}*\n`{link}`\n\n"
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb_back_creator())

    # ── Creator help ──────────────────────────────────────────────
    elif data == "c_help":
        await query.edit_message_text(
            "❓ *Creator Help Guide*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "➕ *New Campaign* — 5-step wizard to create a force-subscribe campaign\n\n"
            "📦 *Materials* — all your uploaded content files\n\n"
            "🎯 *My Campaigns* — toggle on/off, get share links, view stats\n\n"
            "📈 *Analytics* — clicks, verifications, unlocks per campaign\n\n"
            "📢 *My Channels* — channels linked to your campaigns\n\n"
            "📣 *Broadcast* — send a message to all users who unlocked your content\n\n"
            "🔗 *Share Links* — all campaign links in one place\n\n"
            "🔄 *Renew Panel* — payment info for renewing your subscription\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Commands:*\n"
            "`/creator` — open creator panel\n"
            "`/setup` — new campaign\n"
            "`/mycampaigns` — list campaigns\n"
            "`/mystats` — your analytics\n"
            "`/broadcast_my_users` — broadcast",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="c_dash")]
            ]),
        )

    # ══════════════════════════════════════════
    #  ONBOARDING CALLBACKS  (non-conv fallback)
    # ══════════════════════════════════════════
    elif data == "onboard_start":
        # Route into the onboarding ConversationHandler via answer + instruction
        bot_me = await context.bot.get_me()
        await query.edit_message_text(
            f"🚀 *Creator Onboarding — Step 1/2*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Make sure @{bot_me.username} is *Admin* in your channel\n"
            f"with *Post Messages* and *Invite Users* permissions.\n\n"
            f"Then send your channel username:\n"
            f"Example: `@yourchannelname`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="u_back")]
            ]),
        )
        context.user_data["onboard_from_callback"] = True

    # ══════════════════════════════════════════
    #  ADMIN MENU CALLBACKS  (full access)
    # ══════════════════════════════════════════

    elif data == "a_panel":
        if not is_admin(uid): await query.answer("❌ Not admin!", show_alert=True); return
        s     = db.global_stats()
        badge = "👑 SUPER ADMIN" if is_super_admin(uid) else "🛡️ Admin"
        await query.edit_message_text(
            f"{badge} — *ForceHub Control Center*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Users:     `{s['total_users']}`  |  🎨 Creators: `{s['total_creators']}`\n"
            f"🎯 Campaigns: `{s['total_campaigns']}` |  📦 Materials: `{s['total_materials']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆕 Today Joins: `{s['today_joins']}` | 🔓 Unlocks: `{s['today_unlocks']}`\n"
            f"⏱ Trial: `{db.settings.get('trial_days',90)}d` | "
            f"💰 ₹`{db.settings.get('price',199)}` | "
            f"💳 `{db.settings.get('upi_id','Not set')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 `{now_str()}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin(),
        )

    elif data == "a_stats":
        if not is_admin(uid): return
        s = db.global_stats()
        # Per-campaign totals
        total_clicks  = sum(db.analytics.get("campaign_clicks",{}).values())
        total_verif   = sum(db.analytics.get("verification_success",{}).values())
        total_unlocks = sum(db.analytics.get("unlock_success",{}).values())
        total_refs    = sum(db.analytics.get("referral_unlocks",{}).values())
        # Daily history last 7 days
        daily_text = ""
        daily = db.analytics.get("daily", {})
        for d in sorted(daily.keys())[-7:]:
            dd = daily[d]
            daily_text += f"  `{d}` — joins: `{dd.get('joins',0)}` | unlocks: `{dd.get('unlocks',0)}`\n"
        await query.edit_message_text(
            f"📊 *Full Analytics — ForceHub*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users:    `{s['total_users']}`\n"
            f"🎨 Total Creators: `{s['total_creators']}`\n"
            f"🎯 Total Campaigns:`{s['total_campaigns']}`\n"
            f"📦 Total Materials:`{s['total_materials']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👆 Total Clicks:   `{total_clicks}`\n"
            f"✅ Total Verified: `{total_verif}`\n"
            f"🔓 Total Unlocks:  `{total_unlocks}`\n"
            f"👥 Total Referrals:`{total_refs}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 *Last 7 Days:*\n{daily_text}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 `{now_str()}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data == "a_settings":
        if not is_admin(uid): return
        await query.edit_message_text(
            f"⚙️ *Bot Settings*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Trial Days: `{db.settings.get('trial_days',90)}`\n"
            f"💰 Price:     `₹{db.settings.get('price',199)}`\n"
            f"💳 UPI ID:    `{db.settings.get('upi_id','Not set')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Commands:\n"
            f"`/settrial <days>` | `/setprice <₹>` | `/setupi <upi>`\n"
            f"`/addadmin <id>` | `/addcreator <id> [name]`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_settings(),
        )

    # ── PAGINATED USER LIST ───────────────────────────────────────
    elif data.startswith("a_users_"):
        if not is_admin(uid): return
        PAGE_SIZE = 10
        try: page = int(data.split("_")[2])
        except: page = 0
        all_uids  = list(db.users.keys())
        total     = len(all_uids)
        start     = page * PAGE_SIZE
        chunk     = all_uids[start:start + PAGE_SIZE]
        text      = f"👥 *All Users* — Page {page+1} / {max(1,(total-1)//PAGE_SIZE+1)} (Total: `{total}`)\n\n"
        for u_id in chunk:
            u_obj = db.users[u_id]
            is_cr = "🎨" if is_creator(int(u_id)) else ("🛡️" if is_admin(int(u_id)) else "👤")
            uname = f"@{u_obj.get('username','?')}" if u_obj.get("username") else u_obj.get("first_name","?")
            unlocks = len(u_obj.get("unlocked_campaigns", []))
            text += f"{is_cr} `{u_id}` — {uname} | 🔓{unlocks} | 👥{u_obj.get('referral_count',0)}\n"
        # Navigation
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"a_users_{page-1}"))
        if start + PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"a_users_{page+1}"))
        buttons = []
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton("🔍 View User Details", callback_data="a_viewuser_prompt")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="a_panel")])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    # ── PAGINATED CREATOR LIST ────────────────────────────────────
    elif data.startswith("a_creators_"):
        if not is_admin(uid): return
        PAGE_SIZE = 8
        try: page = int(data.split("_")[2])
        except: page = 0
        all_cids  = list(db.creators.keys())
        total     = len(all_cids)
        start     = page * PAGE_SIZE
        chunk     = all_cids[start:start + PAGE_SIZE]
        text      = f"🎨 *All Creators* — Page {page+1} / {max(1,(total-1)//PAGE_SIZE+1)} (Total: `{total}`)\n\n"
        for c_id in chunk:
            cr_obj  = db.creators[c_id]
            days    = db.creator_days_left(int(c_id))
            active  = "✅" if db.is_creator_active(int(c_id)) else "❌"
            camps   = len(cr_obj.get("campaigns",[]))
            total_u = sum(db.analytics.get("unlock_success",{}).get(cid,0) for cid in cr_obj.get("campaigns",[]))
            text += (f"{active} `{c_id}` — *{cr_obj.get('name','?')[:15]}* | "
                     f"⏳{days}d | 🎯{camps} | 🔓{total_u}\n")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"a_creators_{page-1}"))
        if start + PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"a_creators_{page+1}"))
        buttons = []
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton("🔍 View Creator Details", callback_data="a_viewcreator_prompt"),
                        InlineKeyboardButton("➕ Add Creator",           callback_data="a_addcreator_prompt")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="a_panel")])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    # ── PAGINATED CAMPAIGN LIST ───────────────────────────────────
    elif data.startswith("a_campaigns_"):
        if not is_admin(uid): return
        PAGE_SIZE = 8
        try: page = int(data.split("_")[2])
        except: page = 0
        all_camps = list(db.campaigns.keys())
        total     = len(all_camps)
        start     = page * PAGE_SIZE
        chunk     = all_camps[start:start + PAGE_SIZE]
        bot_me    = await context.bot.get_me()
        text      = f"🎯 *All Campaigns* — Page {page+1}/{max(1,(total-1)//PAGE_SIZE+1)} (Total: `{total}`)\n\n"
        for cid in chunk:
            c   = db.campaigns[cid]
            mat = db.materials.get(c.get("material_id",""), {})
            st  = "✅" if c.get("is_active") else "❌"
            clk = db.analytics.get("campaign_clicks",{}).get(cid,0)
            ulk = db.analytics.get("unlock_success",{}).get(cid,0)
            text += (f"{st} `{cid}` — *{mat.get('title','?')[:16]}*\n"
                     f"   creator:`{c.get('creator_id','?')}` | 👆{clk} | 🔓{ulk} | "
                     f"ref:{c.get('referral_required',0)}\n")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"a_campaigns_{page-1}"))
        if start + PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"a_campaigns_{page+1}"))
        buttons = []
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton("🗑 Delete Campaign", callback_data="a_delcamp_prompt")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="a_panel")])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    # ── PAGINATED MATERIALS LIST ──────────────────────────────────
    elif data.startswith("a_materials_"):
        if not is_admin(uid): return
        PAGE_SIZE = 10
        try: page = int(data.split("_")[2])
        except: page = 0
        all_mids = list(db.materials.keys())
        total    = len(all_mids)
        start    = page * PAGE_SIZE
        chunk    = all_mids[start:start + PAGE_SIZE]
        text     = f"📦 *All Materials* — Page {page+1}/{max(1,(total-1)//PAGE_SIZE+1)} (Total: `{total}`)\n\n"
        for mid in chunk:
            m = db.materials[mid]
            text += (f"• `{mid}` — *{m.get('title','?')[:20]}* "
                     f"({m.get('file_type','?')})"
                     f" by `{m.get('creator_id','?')}`\n")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"a_materials_{page-1}"))
        if start + PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"a_materials_{page+1}"))
        buttons = []
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="a_panel")])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))

    # ── PROMPT HANDLERS (inline text input flow) ──────────────────
    elif data == "a_viewuser_prompt":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "viewuser"
        await query.edit_message_text(
            "🔍 *View User Details*\n\nSend the user\'s Telegram ID:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]),
        )

    elif data == "a_viewcreator_prompt":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "viewcreator"
        await query.edit_message_text(
            "🔍 *View Creator Details*\n\nSend the creator\'s Telegram ID:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]),
        )

    elif data == "a_addcreator_prompt":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "addcreator"
        await query.edit_message_text(
            "➕ *Add / Renew Creator*\n\nSend: `<user_id> <name>`\n\nExample:\n`5695957392 John Doe`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]),
        )

    elif data == "a_ban_prompt":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "bancreator"
        await query.edit_message_text(
            "🚫 *Ban Creator*\n\nSend the creator\'s Telegram ID to ban/expire:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]),
        )

    elif data == "a_dm_prompt":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "dm_id"
        await query.edit_message_text(
            "💬 *Direct Message Any User*\n\nStep 1: Send the user\'s Telegram ID:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]),
        )

    elif data == "a_delcamp_prompt":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "delcampaign"
        await query.edit_message_text(
            "🗑 *Delete Campaign*\n\nSend the Campaign ID to delete/deactivate:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]),
        )

    elif data == "a_addadmin_prompt":
        if not is_super_admin(uid):
            await query.answer("👑 Only Super Admin can add admins!", show_alert=True); return
        context.user_data["admin_action"] = "addadmin"
        await query.edit_message_text(
            "👑 *Add Admin*\n\nSend the Telegram ID to grant admin access:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]),
        )

    # ── Confirm creator renew from view ──────────────────────────
    elif data.startswith("a_renewcr_"):
        if not is_admin(uid): return
        cr_id = int(data.split("_")[2])
        db.renew_creator(cr_id)
        days = db.settings.get("trial_days", 90)
        await query.answer(f"✅ Creator {cr_id} renewed for {days} days!", show_alert=True)
        await query.edit_message_text(
            f"✅ Creator `{cr_id}` renewed for *{days} days*.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data.startswith("a_bancr_"):
        if not is_admin(uid): return
        cr_id = int(data.split("_")[2])
        creator = db.creators.get(str(cr_id))
        if creator:
            creator["trial_start"] = "2000-01-01T00:00:00"
            creator["trial_days"]  = 0
            db.save(force=True)
            await query.answer(f"🚫 Creator {cr_id} banned!", show_alert=True)
            await query.edit_message_text(
                f"🚫 Creator `{cr_id}` has been *banned*.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
            )

    elif data.startswith("a_delcamp_"):
        if not is_admin(uid): return
        camp_id = data[len("a_delcamp_"):]
        camp = db.campaigns.get(camp_id)
        if camp:
            camp["is_active"] = False
            db.save(force=True)
            await query.answer(f"🗑 Campaign {camp_id} deactivated!", show_alert=True)
            await query.edit_message_text(
                f"🗑 Campaign `{camp_id}` has been *deactivated*.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
            )

    # ── Standard admin settings ───────────────────────────────────
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
            "📤 Exporting data…\n\nUse /export for the full JSON file.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
        )

    elif data == "a_price":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "setprice"
        await query.edit_message_text(
            f"💰 *Set Price*\n\nCurrent: ₹{db.settings.get('price', 199)}\n\nSend new price (numbers only):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_settings")]]),
        )

    elif data == "a_trial":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "settrial"
        await query.edit_message_text(
            f"⏱ *Set Trial Days*\n\nCurrent: {db.settings.get('trial_days', 90)} days\n\nSend new trial days:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_settings")]]),
        )

    elif data == "a_upi":
        if not is_admin(uid): return
        context.user_data["admin_action"] = "setupi"
        await query.edit_message_text(
            f"💳 *Set UPI ID*\n\nCurrent: `{db.settings.get('upi_id', 'Not set')}`\n\nSend new UPI ID:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="a_settings")]]),
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
    if update.effective_user is None:
        return
    uid = update.effective_user.id
    msg = update.message

    # ══════════════════════════════════════════════════════════════
    #  ADMIN INLINE ACTION HANDLER  (processes text inputs for admin prompts)
    #  NOTE: Only fires when NOT inside the setup ConversationHandler
    # ══════════════════════════════════════════════════════════════
    if is_admin(uid) and context.user_data.get("admin_action") and not context.user_data.get("setup"):
        action = context.user_data.pop("admin_action")
        text   = (msg.text or "").strip()

        # ── View User ───────────────────────────────────────────────
        if action == "viewuser":
            try:
                target_id = int(text)
                u_obj = db.get_user(target_id)
                if not u_obj:
                    await msg.reply_text(f"❌ User `{target_id}` not found.", parse_mode=ParseMode.MARKDOWN)
                    return
                is_cr_flag  = "🎨 Yes" if is_creator(target_id) else "No"
                is_adm_flag = "🛡️ Yes" if is_admin(target_id) else "No"
                unlocked    = u_obj.get("unlocked_campaigns", [])
                ref_by      = u_obj.get("referred_by", "None")
                uname       = f"@{u_obj['username']}" if u_obj.get("username") else "—"
                await msg.reply_text(
                    f"👤 *User Profile*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🆔 ID:        `{target_id}`\n"
                    f"📛 Name:      *{u_obj.get('first_name','?')}*\n"
                    f"🔗 Username:  {uname}\n"
                    f"📅 Joined:    `{u_obj.get('joined_at','?')}` \n"
                    f"🎨 Creator:   {is_cr_flag}\n"
                    f"🛡️ Admin:     {is_adm_flag}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔓 Unlocked:  `{len(unlocked)}` campaigns\n"
                    f"👥 Referrals: `{u_obj.get('referral_count',0)}`\n"
                    f"👈 Referred by: `{ref_by}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    + ("\n".join(f"  • `{cid}`" for cid in unlocked[-5:]) if unlocked else "  No unlocks yet."),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 DM This User",    callback_data=f"a_panel"),
                         InlineKeyboardButton("👁 View Users List", callback_data="a_users_0")],
                        [InlineKeyboardButton("🔙 Admin Panel",     callback_data="a_panel")],
                    ]),
                )
            except (ValueError, Exception) as e:
                await msg.reply_text(f"❌ Error: {e}", parse_mode=ParseMode.MARKDOWN)

        # ── View Creator ────────────────────────────────────────────
        elif action == "viewcreator":
            try:
                target_id = int(text)
                cr_obj = db.get_creator(target_id)
                if not cr_obj:
                    await msg.reply_text(f"❌ Creator `{target_id}` not found.", parse_mode=ParseMode.MARKDOWN)
                    return
                days    = db.creator_days_left(target_id)
                active  = "✅ Active" if db.is_creator_active(target_id) else "❌ Expired"
                camps   = cr_obj.get("campaigns", [])
                mats    = cr_obj.get("materials", [])
                chans   = cr_obj.get("channels", [])
                total_unlocks = sum(db.analytics.get("unlock_success",{}).get(cid,0) for cid in camps)
                total_clicks  = sum(db.analytics.get("campaign_clicks",{}).get(cid,0) for cid in camps)
                await msg.reply_text(
                    f"🎨 *Creator Profile*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🆔 ID:        `{target_id}`\n"
                    f"📛 Name:      *{cr_obj.get('name','?')}*\n"
                    f"🔗 Username:  @{cr_obj.get('username','—')}\n"
                    f"📅 Joined:    `{cr_obj.get('joined_at','?')}` \n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Status:       {active}\n"
                    f"⏳ Days Left: `{days}`\n"
                    f"Trial Start:  `{cr_obj.get('trial_start','?')}` \n"
                    f"Trial Days:   `{cr_obj.get('trial_days',90)}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 Campaigns: `{len(camps)}` | 📦 Materials: `{len(mats)}`\n"
                    f"📢 Channels:  {len(chans)}\n"
                    f"👆 Clicks:    `{total_clicks}` | 🔓 Unlocks: `{total_unlocks}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Channels: {', '.join(chans) or 'None'}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Renew Creator", callback_data=f"a_renewcr_{target_id}"),
                         InlineKeyboardButton("🚫 Ban Creator",  callback_data=f"a_bancr_{target_id}")],
                        [InlineKeyboardButton("🔙 Admin Panel",  callback_data="a_panel")],
                    ]),
                )
            except (ValueError, Exception) as e:
                await msg.reply_text(f"❌ Error: {e}", parse_mode=ParseMode.MARKDOWN)

        # ── Add / Renew Creator ──────────────────────────────────────
        elif action == "addcreator":
            parts = text.split(None, 1)
            if not parts:
                await msg.reply_text("❌ Format: `<user_id> [Name]`", parse_mode=ParseMode.MARKDOWN)
                return
            try:
                cr_id  = int(parts[0])
                name   = parts[1] if len(parts) > 1 else f"Creator_{cr_id}"
                days   = db.settings.get("trial_days", 90)
                if db.get_creator(cr_id):
                    db.renew_creator(cr_id)
                    await msg.reply_text(
                        f"✅ Creator `{cr_id}` renewed for *{days} days*.",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
                    )
                else:
                    db.create_creator(cr_id, "", name)
                    await msg.reply_text(
                        f"✅ Creator *{name}* (`{cr_id}`) added — *{days}-day trial*.",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
                    )
            except ValueError:
                await msg.reply_text("❌ Invalid user ID.", parse_mode=ParseMode.MARKDOWN)

        # ── Ban Creator ──────────────────────────────────────────────
        elif action == "bancreator":
            try:
                cr_id   = str(int(text))
                creator = db.creators.get(cr_id)
                if not creator:
                    await msg.reply_text("❌ Creator not found."); return
                creator["trial_start"] = "2000-01-01T00:00:00"
                creator["trial_days"]  = 0
                db.save(force=True)
                await msg.reply_text(
                    f"🚫 Creator `{cr_id}` has been *banned/expired*.",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
                )
            except ValueError:
                await msg.reply_text("❌ Invalid ID.")

        # ── DM — collect target ID ───────────────────────────────────
        elif action == "dm_id":
            try:
                dm_target = int(text)
                context.user_data["admin_action"] = "dm_msg"
                context.user_data["dm_target_id"] = dm_target
                u_obj = db.get_user(dm_target)
                uname = f"@{u_obj['username']}" if (u_obj and u_obj.get("username")) else str(dm_target)
                await msg.reply_text(
                    f"💬 *DM to {uname}*\n\nNow send the message to deliver:",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("❌ Cancel", callback_data="a_panel")]]
                    ),
                )
            except ValueError:
                await msg.reply_text("❌ Invalid user ID.")

        # ── DM — send message ────────────────────────────────────────
        elif action == "dm_msg":
            dm_target = context.user_data.pop("dm_target_id", None)
            if not dm_target:
                await msg.reply_text("❌ No target. Start over."); return
            try:
                caption = msg.caption or ""
                if msg.text:
                    await context.bot.send_message(
                        dm_target, f"📩 *Message from Admin:*\n\n{msg.text}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                elif msg.photo:
                    await context.bot.send_photo(
                        dm_target, msg.photo[-1].file_id,
                        caption=f"📩 *From Admin:* {caption}", parse_mode=ParseMode.MARKDOWN,
                    )
                elif msg.video:
                    await context.bot.send_video(
                        dm_target, msg.video.file_id,
                        caption=f"📩 *From Admin:* {caption}", parse_mode=ParseMode.MARKDOWN,
                    )
                elif msg.document:
                    await context.bot.send_document(
                        dm_target, msg.document.file_id,
                        caption=f"📩 *From Admin:* {caption}", parse_mode=ParseMode.MARKDOWN,
                    )
                await msg.reply_text(
                    f"✅ Message delivered to `{dm_target}`!",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
                )
            except Forbidden:
                await msg.reply_text(f"❌ User `{dm_target}` blocked the bot.")
            except Exception as e:
                await msg.reply_text(f"❌ Delivery failed: {e}")

        # ── Delete Campaign ──────────────────────────────────────────
        elif action == "delcampaign":
            camp_id = text.upper()
            camp    = db.campaigns.get(camp_id)
            if not camp:
                await msg.reply_text(f"❌ Campaign `{camp_id}` not found.", parse_mode=ParseMode.MARKDOWN)
                return
            await msg.reply_text(
                f"🗑 *Confirm Delete Campaign `{camp_id}`?*\n\n"
                f"This will deactivate it — users won\'t be able to access it.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, Deactivate", callback_data=f"a_delcamp_{camp_id}"),
                     InlineKeyboardButton("❌ Cancel",          callback_data="a_panel")],
                ]),
            )

        # ── Add Admin ────────────────────────────────────────────────
        elif action == "addadmin":
            if not is_super_admin(uid):
                await msg.reply_text("👑 Only Super Admin can add admins!"); return
            try:
                new_admin_id = int(text)
                admins = db.settings.setdefault("admin_ids", [])
                if new_admin_id not in admins:
                    admins.append(new_admin_id)
                    db.save(force=True)
                    await msg.reply_text(
                        f"✅ User `{new_admin_id}` added as *Admin*.",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin(),
                    )
                else:
                    await msg.reply_text(f"ℹ️ Already an admin.")
            except ValueError:
                await msg.reply_text("❌ Invalid user ID.")

        # ── Inline settings changes ──────────────────────────────────
        elif action == "setprice":
            try:
                price = int(text)
                db.set_price(price)
                await msg.reply_text(f"✅ Price set to *₹{price}*",
                                     parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin())
            except ValueError:
                await msg.reply_text("❌ Invalid amount.")

        elif action == "settrial":
            try:
                days = int(text)
                db.set_trial_days(days)
                await msg.reply_text(f"✅ Trial set to *{days} days*",
                                     parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin())
            except ValueError:
                await msg.reply_text("❌ Invalid number.")

        elif action == "setupi":
            db.set_upi(text)
            await msg.reply_text(f"✅ UPI updated: `{text}`",
                                 parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_admin())
        return  # ← Don't fall through to broadcast handlers

    # ── Callback-triggered onboarding: catch channel username ─────
    if context.user_data.get("onboard_from_callback") and not context.user_data.get("setup"):
        await onboard_text_handler(update, context)
        return

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
    if can_use_creator_features(uid) and context.user_data.get("cbcast_step") == "content":
        if not creator_is_active(uid):
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

async def _send_setup_step1(update: Update) -> None:
    """Send Step 1 message — works for both message and callback query updates."""
    text = (
        "🔧 *Create New Campaign — Step 1 / 5*\n\n"
        "Send the channel username(s) users must join.\n"
        "Format: `@channel1 @channel2`\n\n"
        "⚠️ Bot must be *admin* in those channels!"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="setup_cancel")]])
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = update.effective_user

    if not can_use_creator_features(uid):
        msg = "❌ You need creator access to set up campaigns.\n\nContact admin to get registered."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    if not creator_is_active(uid):
        msg = "⏰ Your creator subscription has expired! Use /renewpanel to renew."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    # Auto-register admin as creator if needed
    if is_admin(uid):
        ensure_admin_creator(uid, user.username or "", user.first_name or "")

    context.user_data.pop("admin_action", None)  # Clear any stale admin action
    context.user_data["setup"] = {}

    await _send_setup_step1(update)
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

    # Persist to creator (admin auto-creates their record)
    creator_uid = update.effective_user.id
    cr = db.get_creator(creator_uid)
    if cr is None and is_admin(creator_uid):
        cr = ensure_admin_creator(creator_uid)
    if cr is not None:
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
    if cr is None and is_admin(uid):
        cr = ensure_admin_creator(uid)
    if cr:
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

    if not can_use_creator_features(uid):
        await update.message.reply_text(
            "❌ You don't have creator access.\n"
            f"Ask admin to run: `/addcreator {uid}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if is_admin(uid):
        await update.message.reply_text(
            "👑 *You are Super Admin — no renewal needed!*\n"
            "Your creator access never expires.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status = "✅ Active" if creator_is_active(uid) else "❌ Expired"
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
    if not can_use_creator_features(uid):
        await update.message.reply_text("❌ Creator access required."); return
    if is_admin(uid): ensure_admin_creator(uid)
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
    if not can_use_creator_features(uid):
        await update.message.reply_text("❌ Creator access required."); return
    if not creator_is_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel"); return
    if is_admin(uid): ensure_admin_creator(uid)
    cr      = db.get_creator(uid)
    mat_ids = cr.get("materials", []) if cr else []
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
    if not can_use_creator_features(uid):
        await update.message.reply_text("❌ Creator access required."); return
    if not creator_is_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel"); return
    if is_admin(uid): ensure_admin_creator(uid)
    cr       = db.get_creator(uid)
    channels = cr.get("channels", []) if cr else []
    text     = "📢 *Your Channels:*\n\n"
    text    += ("\n".join(f"• `{ch}`" for ch in channels)
                if channels else "No channels yet.\nUse /setup to add channels.")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_broadcast_my_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_use_creator_features(uid):
        await update.message.reply_text("❌ Creator access required."); return
    if is_admin(uid): ensure_admin_creator(uid)
    if not creator_is_active(uid):
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
# NEW COMMANDS — smooth shortcuts
# ─────────────────────────────────────────────────────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin — dedicated admin panel shortcut"""
    uid  = update.effective_user.id
    user = update.effective_user
    if not is_admin(uid):
        await update.message.reply_text("❌ You don't have admin access.")
        return
    s     = db.global_stats()
    badge = "👑 SUPER ADMIN" if is_super_admin(uid) else "🛡️ Admin"
    await update.message.reply_text(
        f"{badge} — *ForceHub Control Center*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 *{user.first_name}* | 🆔 `{uid}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users:     `{s['total_users']}`  |  🎨 Creators: `{s['total_creators']}`\n"
        f"🎯 Campaigns: `{s['total_campaigns']}` |  📦 Materials: `{s['total_materials']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆕 Today Joins: `{s['today_joins']}` | 🔓 Unlocks: `{s['today_unlocks']}`\n"
        f"⏱ Trial: `{db.settings.get('trial_days',90)}d` | "
        f"💰 ₹`{db.settings.get('price',199)}` | "
        f"💳 `{db.settings.get('upi_id','Not set')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now_str()}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_admin(),
    )


async def cmd_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/creator — dedicated creator panel shortcut"""
    uid  = update.effective_user.id
    user = update.effective_user
    if not can_use_creator_features(uid):
        await update.message.reply_text(
            "❌ You don't have creator access yet.\n\n"
            "Ask admin to run: `/addcreator {uid}`".format(uid=uid),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    # Auto-register admin as creator if they're not already
    if is_admin(uid):
        ensure_admin_creator(uid, user.username or "", user.first_name or "")
    cr     = db.get_creator(uid)
    days   = db.creator_days_left(uid)
    status = "✅ Active" if creator_is_active(uid) else "❌ Expired"
    camps  = cr.get("campaigns", []) if cr else []
    total_unlocks = sum(db.analytics.get("unlock_success",{}).get(cid,0) for cid in camps)
    total_clicks  = sum(db.analytics.get("campaign_clicks",{}).get(cid,0) for cid in camps)
    badge  = "👑 Admin+Creator" if is_admin(uid) else "🎨 Creator"
    expiry = "♾ No expiry (Admin)" if is_admin(uid) else f"⏳ {days} days left"
    await update.message.reply_text(
        f"{badge} *Panel — ForceHub*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *{cr.get('name', user.first_name) if cr else user.first_name}*  |  `{uid}`\n"
        f"Status: {status}  |  {expiry}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Campaigns: `{len(camps)}`  |  📦 Materials: `{len(cr.get('materials',[]) if cr else [])}`\n"
        f"📢 Channels:  `{len(cr.get('channels',[]) if cr else [])}`\n"
        f"👆 Total Clicks: `{total_clicks}`  |  🔓 Unlocks: `{total_unlocks}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now_str()}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_creator(),
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/id — returns the user's Telegram ID (useful for admin setup)"""
    user = update.effective_user
    role = "👑 Super Admin" if is_super_admin(user.id) else (
           "🛡️ Admin" if is_admin(user.id) else (
           "🎨 Creator" if is_creator(user.id) else "👤 User"))
    await update.message.reply_text(
        f"🆔 *Your Telegram ID*\n\n"
        f"`{user.id}`\n\n"
        f"👤 Name: *{user.first_name}*\n"
        f"🔗 Username: @{user.username or 'None'}\n"
        f"🏷 Role: {role}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — context-aware help"""
    uid  = update.effective_user.id
    user = update.effective_user

    if is_admin(uid):
        text = (
            "🛡️ *Admin Help — ForceHub*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Panel Commands:*\n"
            "`/admin` — open admin control center\n"
            "`/broadcast` — broadcast to users/creators/everyone\n"
            "`/globalstats` — full analytics\n"
            "`/export` — export all data as JSON\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Creator Management:*\n"
            "`/addcreator <id> [name]` — add or renew creator\n"
            "`/bancreator <id>` — ban/expire creator\n"
            "`/viewuser <id>` — view user profile\n"
            "`/viewcreator <id>` — view creator profile\n"
            "`/dm <id> <msg>` — direct message any user\n"
            "`/renewcreator <id>` — renew creator subscription\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Settings:*\n"
            "`/settrial <days>` — set trial period\n"
            "`/setprice <₹>` — set renewal price\n"
            "`/setupi <upi>` — set UPI ID\n"
            "`/addadmin <id>` — add new admin\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Campaigns:*\n"
            "`/delcampaign <id>` — deactivate campaign\n"
            "`/togglecampaign <id>` — toggle campaign on/off\n"
            "`/id` — get your Telegram ID"
        )
    elif is_creator(uid):
        text = (
            "🎨 *Creator Help — ForceHub*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "`/creator` — open creator panel\n"
            "`/setup` — create new campaign (wizard)\n"
            "`/mycampaigns` — list all your campaigns\n"
            "`/mystats` — your analytics\n"
            "`/materials` — manage your materials\n"
            "`/channels` — manage your channels\n"
            "`/broadcast_my_users` — broadcast to your audience\n"
            "`/renewpanel` — renew subscription info\n"
            "`/togglecampaign <id>` — activate/deactivate campaign\n"
            "`/id` — get your Telegram ID"
        )
    else:
        text = (
            "🚀 *ForceHub Help*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "ForceHub is a content unlock platform.\n\n"
            "📢 Get a campaign link from a creator\n"
            "✅ Join the required channel(s)\n"
            "🔓 Unlock exclusive content!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "`/start` — main menu\n"
            "`/id` — your Telegram ID\n"
            "`/help` — this message"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mystats — creator personal analytics"""
    uid = update.effective_user.id
    if not can_use_creator_features(uid):
        await update.message.reply_text("❌ Creator access required."); return
    if is_admin(uid): ensure_admin_creator(uid)
    cr    = db.get_creator(uid)
    camps = cr.get("campaigns", []) if cr else []
    text  = "📈 *Your Analytics — ForceHub*\n\n"
    total_clicks = total_verif = total_unlocks = 0
    for cid in camps:
        c   = db.campaigns.get(cid, {})
        mat = db.materials.get(c.get("material_id",""), {})
        clk = db.analytics.get("campaign_clicks",{}).get(cid,0)
        ver = db.analytics.get("verification_success",{}).get(cid,0)
        ulk = db.analytics.get("unlock_success",{}).get(cid,0)
        total_clicks  += clk
        total_verif   += ver
        total_unlocks += ulk
        st = "✅" if c.get("is_active") else "❌"
        text += (f"{st} *{mat.get('title','?')[:20]}* — `{cid}`\n"
                 f"   👆 {clk}  ✅ {ver}  🔓 {ulk}\n\n")
    text += (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
             f"*Totals:* 👆`{total_clicks}` ✅`{total_verif}` 🔓`{total_unlocks}`\n"
             f"📢 Channels: `{len(cr.get('channels',[]))}` | "
             f"📦 Materials: `{len(cr.get('materials',[]))}`")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb_back_creator())


async def cmd_viewuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/viewuser <id> — admin: view detailed user profile"""
    uid = update.effective_user.id
    if not is_admin(uid): return
    if not context.args:
        await update.message.reply_text("Usage: `/viewuser <user_id>`",
                                        parse_mode=ParseMode.MARKDOWN); return
    try:
        target_id = int(context.args[0])
        u_obj = db.get_user(target_id)
        if not u_obj:
            await update.message.reply_text(f"❌ User `{target_id}` not found.",
                                            parse_mode=ParseMode.MARKDOWN); return
        is_cr_flag  = "🎨 Yes" if is_creator(target_id) else "No"
        is_adm_flag = "🛡️ Yes" if is_admin(target_id) else "No"
        unlocked    = u_obj.get("unlocked_campaigns", [])
        uname       = f"@{u_obj['username']}" if u_obj.get("username") else "—"
        await update.message.reply_text(
            f"👤 *User Profile*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID:        `{target_id}`\n"
            f"📛 Name:      *{u_obj.get('first_name','?')}*\n"
            f"🔗 Username:  {uname}\n"
            f"📅 Joined:    `{u_obj.get('joined_at','?')}`\n"
            f"🎨 Creator:   {is_cr_flag}  |  🛡️ Admin: {is_adm_flag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔓 Unlocked:  `{len(unlocked)}` campaigns\n"
            f"👥 Referrals: `{u_obj.get('referral_count',0)}`\n"
            f"👈 Referred by: `{u_obj.get('referred_by','None')}`\n"
            + ("\n*Unlocked campaigns:*\n" + "\n".join(f"  • `{c}`" for c in unlocked[-5:])
               if unlocked else ""),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Admin Panel", callback_data="a_panel")]
            ]),
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")


async def cmd_viewcreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/viewcreator <id> — admin: view detailed creator profile"""
    uid = update.effective_user.id
    if not is_admin(uid): return
    if not context.args:
        await update.message.reply_text("Usage: `/viewcreator <creator_id>`",
                                        parse_mode=ParseMode.MARKDOWN); return
    try:
        target_id = int(context.args[0])
        cr_obj = db.get_creator(target_id)
        if not cr_obj:
            await update.message.reply_text(f"❌ Creator `{target_id}` not found.",
                                            parse_mode=ParseMode.MARKDOWN); return
        days   = db.creator_days_left(target_id)
        active = "✅ Active" if db.is_creator_active(target_id) else "❌ Expired"
        camps  = cr_obj.get("campaigns", [])
        total_unlocks = sum(db.analytics.get("unlock_success",{}).get(cid,0) for cid in camps)
        total_clicks  = sum(db.analytics.get("campaign_clicks",{}).get(cid,0) for cid in camps)
        await update.message.reply_text(
            f"🎨 *Creator Profile*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: `{target_id}` | 📛 *{cr_obj.get('name','?')}*\n"
            f"Status: {active} | ⏳ `{days}` days left\n"
            f"📅 Joined: `{cr_obj.get('joined_at','?')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Campaigns: `{len(camps)}` | 📦 Materials: `{len(cr_obj.get('materials',[]))}`\n"
            f"👆 Clicks: `{total_clicks}` | 🔓 Unlocks: `{total_unlocks}`\n"
            f"📢 Channels: {', '.join(cr_obj.get('channels',[])) or 'None'}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Renew", callback_data=f"a_renewcr_{target_id}"),
                 InlineKeyboardButton("🚫 Ban",   callback_data=f"a_bancr_{target_id}")],
                [InlineKeyboardButton("🔙 Admin Panel", callback_data="a_panel")],
            ]),
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid creator ID.")


async def cmd_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dm <user_id> <message> — admin: DM any user directly"""
    uid = update.effective_user.id
    if not is_admin(uid): return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/dm <user_id> <message>`\n\nExample:\n`/dm 123456789 Hello!`",
            parse_mode=ParseMode.MARKDOWN); return
    try:
        target_id = int(context.args[0])
        message   = " ".join(context.args[1:])
        await context.bot.send_message(
            target_id,
            f"📩 *Message from Admin:*\n\n{message}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(
            f"✅ Message delivered to `{target_id}`.", parse_mode=ParseMode.MARKDOWN
        )
    except Forbidden:
        await update.message.reply_text(f"❌ User blocked the bot.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_delcampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delcampaign <id> — admin: deactivate a campaign"""
    uid = update.effective_user.id
    if not is_admin(uid): return
    if not context.args:
        await update.message.reply_text("Usage: `/delcampaign <campaign_id>`",
                                        parse_mode=ParseMode.MARKDOWN); return
    camp_id = context.args[0].upper()
    camp    = db.campaigns.get(camp_id)
    if not camp:
        await update.message.reply_text(f"❌ Campaign `{camp_id}` not found.",
                                        parse_mode=ParseMode.MARKDOWN); return
    camp["is_active"] = False
    db.save(force=True)
    await update.message.reply_text(
        f"🗑 Campaign `{camp_id}` has been *deactivated*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_togglecampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/togglecampaign <id> — admin or creator: toggle campaign active/inactive"""
    uid  = update.effective_user.id
    if not can_use_creator_features(uid):
        await update.message.reply_text("❌ No access."); return
    if not context.args:
        await update.message.reply_text("Usage: `/togglecampaign <campaign_id>`",
                                        parse_mode=ParseMode.MARKDOWN); return
    camp_id = context.args[0].upper()
    camp    = db.campaigns.get(camp_id)
    if not camp:
        await update.message.reply_text(f"❌ Campaign `{camp_id}` not found.",
                                        parse_mode=ParseMode.MARKDOWN); return
    # Creators can only toggle their own campaigns
    if not is_admin(uid) and camp.get("creator_id") != str(uid):
        await update.message.reply_text("❌ Not your campaign!"); return
    camp["is_active"] = not camp.get("is_active", True)
    db.save(force=True)
    status = "✅ Activated" if camp["is_active"] else "❌ Deactivated"
    await update.message.reply_text(
        f"{status} campaign `{camp_id}`.", parse_mode=ParseMode.MARKDOWN
    )


async def cmd_renewcreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/renewcreator <id> [days] — admin: renew creator subscription"""
    uid = update.effective_user.id
    if not is_admin(uid): return
    if not context.args:
        await update.message.reply_text("Usage: `/renewcreator <id> [days]`",
                                        parse_mode=ParseMode.MARKDOWN); return
    try:
        cr_id = int(context.args[0])
        days  = int(context.args[1]) if len(context.args) > 1 else None
        cr    = db.get_creator(cr_id)
        if not cr:
            await update.message.reply_text(f"❌ Creator `{cr_id}` not found.",
                                            parse_mode=ParseMode.MARKDOWN); return
        db.renew_creator(cr_id, days)
        final_days = days or db.settings.get("trial_days", 90)
        await update.message.reply_text(
            f"✅ Creator `{cr_id}` (*{cr.get('name','?')}*) renewed for *{final_days} days*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid ID or days.")


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addadmin <id> — super admin only: grant admin access"""
    uid = update.effective_user.id
    if not is_super_admin(uid):
        await update.message.reply_text("👑 Only Super Admin can use this."); return
    if not context.args:
        await update.message.reply_text("Usage: `/addadmin <user_id>`",
                                        parse_mode=ParseMode.MARKDOWN); return
    try:
        new_id = int(context.args[0])
        admins = db.settings.setdefault("admin_ids", [])
        if new_id not in admins:
            admins.append(new_id)
            db.save(force=True)
            await update.message.reply_text(
                f"✅ User `{new_id}` is now an *Admin*.", parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(f"ℹ️ Already an admin.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")


async def cmd_listcreators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/listcreators — admin: quick text list of all creators"""
    uid = update.effective_user.id
    if not is_admin(uid): return
    cids = list(db.creators.keys())
    if not cids:
        await update.message.reply_text("📭 No creators registered yet."); return
    text = f"🎨 *All Creators* (`{len(cids)}` total)\n\n"
    for cid in cids:
        cr     = db.creators[cid]
        days   = db.creator_days_left(int(cid))
        active = "✅" if db.is_creator_active(int(cid)) else "❌"
        text  += f"{active} `{cid}` — *{cr.get('name','?')}* | ⏳{days}d\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb_back_admin())


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/listusers [page] — admin: paginated user list"""
    uid = update.effective_user.id
    if not is_admin(uid): return
    try: page = int(context.args[0]) - 1 if context.args else 0
    except: page = 0
    PAGE_SIZE = 15
    all_uids  = list(db.users.keys())
    total     = len(all_uids)
    start     = page * PAGE_SIZE
    chunk     = all_uids[start:start + PAGE_SIZE]
    text      = (f"👥 *Users* — Page {page+1}/{max(1,(total-1)//PAGE_SIZE+1)} "
                 f"(Total: `{total}`)\n\n")
    for u_id in chunk:
        u_obj = db.users[u_id]
        role  = "🎨" if is_creator(int(u_id)) else ("🛡️" if is_admin(int(u_id)) else "👤")
        uname = f"@{u_obj.get('username','')}" if u_obj.get("username") else u_obj.get("first_name","?")
        text += f"{role} `{u_id}` — {uname} | 🔓{len(u_obj.get('unlocked_campaigns',[]))}\n"
    if start + PAGE_SIZE < total:
        text += f"\nUse `/listusers {page+2}` for next page"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — logs errors silently, notifies super admin."""
    logger.error("Update caused error: %s", context.error, exc_info=context.error)
    # Notify super admin
    for sa_id in SUPER_ADMIN_IDS:
        try:
            await context.bot.send_message(
                sa_id,
                f"⚠️ *Bot Error*\n`{type(context.error).__name__}: {context.error}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unknown commands gracefully."""
    uid = update.effective_user.id
    if is_admin(uid):
        await update.message.reply_text(
            "❓ Unknown command. Use /help for admin commands.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛡️ Admin Panel", callback_data="a_panel")]]),
        )
    elif is_creator(uid):
        await update.message.reply_text(
            "❓ Unknown command. Use /help for creator commands.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎨 Creator Panel", callback_data="c_dash")]]),
        )
    else:
        await update.message.reply_text(
            "❓ Unknown command. Use /start to begin.",
            reply_markup=kb_user(),
        )


# ─────────────────────────────────────────────────────────────────
# BOT STARTUP
# ─────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    """Register commands and start background tasks."""
    # ── All users ──────────────────────────────────────────────────
    user_commands = [
        BotCommand("start",          "🚀 Main menu"),
        BotCommand("id",             "🆔 Your Telegram ID"),
        BotCommand("help",           "❓ Help & commands"),
        BotCommand("becomecreator",  "🎨 Become a creator"),
    ]
    # ── Creator only ───────────────────────────────────────────────
    creator_commands = user_commands + [
        BotCommand("creator",            "🎨 Creator panel"),
        BotCommand("createcampaign",     "➕ Create unlock campaign"),
        BotCommand("dashboard",          "📊 Creator dashboard"),
        BotCommand("broadcastusers",     "📣 Broadcast to your users"),
        BotCommand("setup",              "🔧 Advanced campaign setup"),
        BotCommand("mycampaigns",        "🎯 Your campaigns"),
        BotCommand("mystats",            "📈 Your analytics"),
        BotCommand("materials",          "📦 Manage materials"),
        BotCommand("channels",           "📢 Manage channels"),
        BotCommand("broadcast_my_users", "📣 Broadcast (alias)"),
        BotCommand("togglecampaign",     "🔁 Toggle campaign on/off"),
        BotCommand("renewpanel",         "🔄 Renew subscription"),
    ]
    # ── Admin ──────────────────────────────────────────────────────
    admin_commands = creator_commands + [
        BotCommand("admin",           "🛡️ Admin control center"),
        BotCommand("broadcast",       "📣 Broadcast to all"),
        BotCommand("globalstats",     "📊 Global analytics"),
        BotCommand("addcreator",      "➕ Add/renew creator"),
        BotCommand("bancreator",      "🚫 Ban creator"),
        BotCommand("renewcreator",    "🔄 Renew creator"),
        BotCommand("viewuser",        "👤 View user details"),
        BotCommand("viewcreator",     "🎨 View creator details"),
        BotCommand("listcreators",    "📋 List all creators"),
        BotCommand("listusers",       "📋 List all users"),
        BotCommand("dm",              "💬 DM any user"),
        BotCommand("delcampaign",     "🗑 Delete campaign"),
        BotCommand("togglecampaign",  "🔁 Toggle campaign"),
        BotCommand("settrial",        "⏱ Set trial days"),
        BotCommand("setprice",        "💰 Set price"),
        BotCommand("setupi",          "💳 Set UPI ID"),
        BotCommand("addadmin",        "👑 Add admin"),
        BotCommand("export",          "📤 Export JSON"),
    ]
    # Set scoped commands — Telegram shows relevant commands per user
    try:
        from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat
        await app.bot.set_my_commands(user_commands)
        # Set admin-specific commands for each super admin
        for sa_id in SUPER_ADMIN_IDS:
            try:
                await app.bot.set_my_commands(
                    admin_commands,
                    scope=BotCommandScopeChat(chat_id=sa_id)
                )
            except Exception:
                pass
        # Set for env admins too
        for a_id in ADMIN_IDS:
            try:
                await app.bot.set_my_commands(
                    admin_commands,
                    scope=BotCommandScopeChat(chat_id=a_id)
                )
            except Exception:
                pass
    except Exception:
        # Fallback: set global commands
        await app.bot.set_my_commands(admin_commands)

    asyncio.create_task(db.periodic_save())
    logger.info("🚀 ForceHub Bot started — %s", now_str())



# ───────────────────────────────────────────────────────────────────────────
# CREATOR ONBOARDING SYSTEM — defined before main()
# ───────────────────────────────────────────────────────────────────────────



# ═══════════════════════════════════════════════════════════════════════════════
#  CREATOR ONBOARDING SYSTEM
#  Self-service: any user can become a creator by connecting their channel
# ═══════════════════════════════════════════════════════════════════════════════

# ── Keyboards ──────────────────────────────────────────────────────────────────

def kb_onboard_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="onboard_cancel")]])


def kb_creator_simple() -> InlineKeyboardMarkup:
    """Minimal inline creator menu for newly onboarded creators."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard",       callback_data="c_dash")],
        [InlineKeyboardButton("➕ Create Campaign", callback_data="c_setup"),
         InlineKeyboardButton("🔗 My Links",        callback_data="c_links")],
        [InlineKeyboardButton("📣 Broadcast",       callback_data="c_broadcast"),
         InlineKeyboardButton("⚙️ Settings",        callback_data="c_help")],
    ])


# ── Channel verification helper ────────────────────────────────────────────────

async def verify_channel_and_permissions(bot, channel: str) -> dict:
    """
    Verify:
      1. Channel exists / bot can access it
      2. Bot is admin in the channel
      3. Bot has can_post_messages + can_invite_users
    Returns {ok: bool, reason: str, chat_id: int|None, title: str}
    """
    try:
        chat = await bot.get_chat(channel)
    except Exception as e:
        return {"ok": False, "reason": f"Channel `{channel}` not found or inaccessible.\n_{e}_"}

    try:
        me     = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        return {"ok": False, "reason": f"Could not check bot status: `{e}`"}

    if member.status not in ("administrator", "creator"):
        return {
            "ok": False,
            "reason": (
                f"❌ Bot is *not admin* in `{channel}`.\n\n"
                f"Please:\n"
                f"1️⃣ Open your channel settings\n"
                f"2️⃣ Go to *Administrators* → *Add Admin*\n"
                f"3️⃣ Search `@{(await bot.get_me()).username}` and add\n"
                f"4️⃣ Enable *Post Messages* + *Invite Users*\n"
                f"5️⃣ Then send the channel username again"
            )
        }

    # Check permissions (only for ChatMemberAdministrator)
    if member.status == "administrator":
        can_post   = getattr(member, "can_post_messages",   True)
        can_invite = getattr(member, "can_invite_users",    True)
        missing = []
        if not can_post:   missing.append("*Post Messages*")
        if not can_invite: missing.append("*Invite Users*")
        if missing:
            return {
                "ok": False,
                "reason": (
                    f"⚠️ Bot is admin but missing permissions:\n"
                    + "\n".join(f"  • {p}" for p in missing)
                    + "\n\nPlease grant these in the channel admin settings."
                )
            }

    return {
        "ok":      True,
        "chat_id": chat.id,
        "title":   chat.title or channel,
        "username": f"@{chat.username}" if chat.username else str(chat.id),
        "reason":  "OK"
    }


# ── ONBOARDING CONVERSATION ────────────────────────────────────────────────────

async def cmd_become_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /becomecreator or button → start onboarding."""
    uid  = update.effective_user.id
    user = update.effective_user

    # Already a creator → go to dashboard
    if can_use_creator_features(uid):
        ensure_admin_creator(uid, user.username or "", user.first_name or "")
        reply_fn = (update.callback_query.edit_message_text
                    if update.callback_query
                    else update.message.reply_text)
        await reply_fn(
            "🎨 You already have creator access!\nOpening your dashboard…",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_creator(),
        )
        return ConversationHandler.END

    bot_me = await context.bot.get_me()
    text   = (
        f"🚀 *Creator Onboarding — Step 1 / 2*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"You can protect your Telegram content using\n"
        f"*unlock campaigns* — users must join your\n"
        f"channel before accessing your content.\n\n"
        f"*Before you continue:*\n"
        f"1️⃣ Add @{bot_me.username} as *Admin* in your channel\n"
        f"2️⃣ Grant: *Post Messages* + *Invite Users* permissions\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✏️ Now send your channel username:\n"
        f"Example: `@yourchannelname`"
    )
    kb = kb_onboard_cancel()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    context.user_data.pop("admin_action", None)
    context.user_data["onboarding"] = True
    return ONBOARD_CHANNEL


async def onboard_recv_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: receive channel username, verify, register creator."""
    uid  = update.effective_user.id
    user = update.effective_user
    msg  = update.message

    raw  = msg.text.strip() if msg.text else ""
    if not raw:
        await msg.reply_text("❌ Please send a valid channel username (e.g. `@mychannel`)",
                             parse_mode=ParseMode.MARKDOWN)
        return ONBOARD_CHANNEL

    # Normalize: ensure starts with @
    channel = raw if raw.startswith("@") or raw.startswith("-") else f"@{raw}"

    # Show "verifying…" message
    progress = await msg.reply_text(
        f"🔍 Verifying `{channel}`…",
        parse_mode=ParseMode.MARKDOWN,
    )

    result = await verify_channel_and_permissions(context.bot, channel)

    if not result["ok"]:
        await progress.edit_text(
            f"*Verification Failed*\n\n{result['reason']}\n\n"
            f"Fix the issue and send the channel username again:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_onboard_cancel(),
        )
        return ONBOARD_CHANNEL  # Stay in state, let them retry

    # ── All checks passed — register as creator ────────────────────────────────
    chat_id    = result["chat_id"]
    chan_title  = result["title"]
    chan_uname  = result["username"]
    trial_days = db.settings.get("trial_days", 90)

    # Create creator record (self-service — no admin approval needed)
    if db.get_creator(uid):
        # Already exists (e.g. re-onboarding) — add channel if not present
        cr = db.get_creator(uid)
        if chan_uname not in cr.get("channels", []):
            cr.setdefault("channels", []).append(chan_uname)
        cr["channel_id"] = str(chat_id)
        db.save(force=True)
    else:
        db.creators[str(uid)] = {
            "username":    user.username or "",
            "name":        user.first_name or f"Creator_{uid}",
            "trial_start": datetime.now().isoformat(),
            "trial_days":  trial_days,
            "channels":    [chan_uname],
            "channel_id":  str(chat_id),
            "materials":   [],
            "campaigns":   [],
            "joined_at":   datetime.now().isoformat(),
            "self_onboarded": True,
        }
        db.save(force=True)
        logger.info("🎨 New creator self-onboarded: %s (%d)", user.first_name, uid)

    # Update channel in channels_map for quick lookup
    db._data.setdefault("channels_map", {})[str(chat_id)] = str(uid)
    db.save(force=True)

    await progress.edit_text(
        f"✅ *Channel Connected Successfully!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 Channel: *{chan_title}* (`{chan_uname}`)\n"
        f"🆔 Your ID: `{uid}`\n\n"
        f"🚀 *Creator Panel Activated!*\n\n"
        f"You can now create unlock campaigns.\n"
        f"Use *➕ Create Campaign* to get started!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_creator(),
    )

    context.user_data.pop("onboarding", None)
    return ConversationHandler.END


async def onboard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel onboarding at any point."""
    context.user_data.pop("onboarding", None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Onboarding cancelled. You can tap *🚀 Become Creator* anytime.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_user(),
        )
    else:
        await update.message.reply_text(
            "❌ Onboarding cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="u_back")]]),
        )
    return ConversationHandler.END


# ── CREATE CAMPAIGN CONVERSATION ───────────────────────────────────────────────

async def cmd_createcampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: /createcampaign — starts simple URL-based campaign creation."""
    uid  = update.effective_user.id
    user = update.effective_user

    if not can_use_creator_features(uid):
        text = (
            "❌ *Creator access required*\n\n"
            "Tap *🚀 Become Creator* first to create campaigns."
        )
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        else:
            await update.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Become Creator", callback_data="u_become_creator")]
                ]),
            )
        return ConversationHandler.END

    if not creator_is_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel")
        return ConversationHandler.END

    if is_admin(uid):
        ensure_admin_creator(uid, user.username or "", user.first_name or "")

    context.user_data.pop("admin_action", None)
    context.user_data["campaign_draft"] = {}

    text = (
        "➕ *Create Campaign — Step 1 / 2*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send the *unlock content link* — this is what users\n"
        "receive after joining your channel(s).\n\n"
        "Supported: Drive, Telegram post, website, PDF, any URL\n\n"
        "Example:\n`https://t.me/yourchannel/42`"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="createcamp_cancel")]])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    return CREATECAMP_LINK


async def createcamp_recv_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 of createcampaign: receive unlock link."""
    raw = (update.message.text or "").strip()
    if not raw.startswith("http"):
        await update.message.reply_text(
            "❌ Please send a valid URL starting with `http://` or `https://`\n\n"
            "Example: `https://t.me/yourchannel/42`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return CREATECAMP_LINK

    context.user_data["campaign_draft"]["unlock_link"] = raw

    await update.message.reply_text(
        "➕ *Create Campaign — Step 2 / 2*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send the channel username(s) users *must join* to unlock.\n"
        "Separate multiple channels with spaces.\n\n"
        "Example:\n`@channel1 @channel2`\n\n"
        "⚠️ Bot must be *admin* in each channel listed!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="createcamp_cancel")]
        ]),
    )
    return CREATECAMP_CHANNELS


async def createcamp_recv_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: receive required channels, validate, save campaign."""
    uid   = update.effective_user.id
    raw   = (update.message.text or "").strip()
    parts = raw.split()
    channels = [p if p.startswith("@") or p.startswith("-") else f"@{p}" for p in parts if p]

    if not channels:
        await update.message.reply_text(
            "❌ Send at least one channel username. Example: `@mychannel`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return CREATECAMP_CHANNELS

    # Validate bot is admin in each channel
    progress = await update.message.reply_text(
        f"🔍 Verifying {len(channels)} channel(s)…",
        parse_mode=ParseMode.MARKDOWN,
    )
    valid, invalid = [], []
    for ch in channels:
        r = await verify_channel_and_permissions(context.bot, ch)
        if r["ok"]:
            valid.append(ch)
        else:
            invalid.append(ch)

    if not valid:
        await progress.edit_text(
            "❌ Bot is not admin in *any* of those channels.\n\n"
            "Add bot as admin first, then send the channel(s) again.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="createcamp_cancel")]
            ]),
        )
        return CREATECAMP_CHANNELS

    # ── Save campaign ──────────────────────────────────────────────────────────
    draft   = context.user_data.get("campaign_draft", {})
    link    = draft.get("unlock_link", "")

    # Generate unique campaign ID
    camp_id = str(uuid.uuid4())[:8].upper()
    while camp_id in db.campaigns:
        camp_id = str(uuid.uuid4())[:8].upper()

    db.campaigns[camp_id] = {
        "creator_id":         str(uid),
        "required_channels":  valid,
        "channels":           valid,           # keep compatibility with existing verify flow
        "unlock_link":        link,
        "material_id":        None,            # URL-based, no material
        "referral_required":  0,
        "is_active":          True,
        "campaign_type":      "url_unlock",
        "created_at":         datetime.now().isoformat(),
    }

    # Link campaign to creator record
    cr = db.get_creator(uid)
    if cr is None and is_admin(uid):
        cr = ensure_admin_creator(uid)
    if cr:
        cr.setdefault("campaigns", []).append(camp_id)
    db.save(force=True)

    db.track("campaign_clicks", camp_id)  # initialise counter

    bot_me     = await context.bot.get_me()
    deep_link  = f"https://t.me/{bot_me.username}?start=unlock_{camp_id}"
    warn_skipped = ""
    if invalid:
        warn_skipped = f"\n⚠️ Skipped (bot not admin): {', '.join(invalid)}"

    await progress.edit_text(
        f"✅ *Campaign Created Successfully!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Campaign ID: `{camp_id}`\n"
        f"📢 Channels: {', '.join(valid)}\n"
        f"🔗 Unlock Link: `{link}`{warn_skipped}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📣 *Share this unlock link:*\n"
        f"`{deep_link}`\n\n"
        f"Users tap this → join your channel → get the link! 🎉",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Open Link",      url=deep_link)],
            [InlineKeyboardButton("🎯 My Campaigns",   callback_data="c_campaigns"),
             InlineKeyboardButton("📊 Dashboard",     callback_data="c_dash")],
        ]),
    )

    context.user_data.pop("campaign_draft", None)
    return ConversationHandler.END


async def createcamp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("campaign_draft", None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Campaign creation cancelled.",
            reply_markup=kb_creator(),
        )
    else:
        await update.message.reply_text("❌ Campaign creation cancelled.",
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("🏠 Creator Panel", callback_data="c_dash")]
                                        ]))
    return ConversationHandler.END


# ── FIX: Handle unlock_<id> deep links in /start ──────────────────────────────

_ORIG_handle_campaign = _handle_campaign  # preserve original


async def _handle_campaign_extended(update: Update, context: ContextTypes.DEFAULT_TYPE, campaign_id: str):
    """
    Extended campaign handler that supports both:
    - Original material-based campaigns (file delivery)
    - New URL-based campaigns (unlock_link delivery)
    """
    user = update.effective_user
    uid  = user.id

    campaign = db.campaigns.get(campaign_id)
    if not campaign or not campaign.get("is_active"):
        await update.message.reply_text(
            "❌ This campaign link is not active or doesn't exist."
        )
        return

    db.track("campaign_clicks", campaign_id)

    channels   = campaign.get("channels", campaign.get("required_channels", []))
    not_joined = await check_channel_membership(context.bot, uid, channels)

    if not_joined:
        buttons = [
            [InlineKeyboardButton(f"📢 Join Channel {i+1}",
                                  url=f"https://t.me/{c.lstrip('@')}")]
            for i, c in enumerate(not_joined)
        ]
        buttons.append(
            [InlineKeyboardButton("✅ I've Joined — Verify",
                                  callback_data=f"verify_{campaign_id}")]
        )
        await update.message.reply_text(
            f"🔐 *Content Locked*\n\n"
            f"Join the channel(s) below to unlock:\n\n"
            + "\n".join(f"• `{c}`" for c in not_joined)
            + "\n\nAfter joining, tap *✅ Verify*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Referral check
    ref_req   = campaign.get("referral_required", 0)
    u         = db.get_or_create_user(uid)
    user_refs = u.get("referral_count", 0)

    if ref_req > 0 and user_refs < ref_req:
        bot_me   = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
        needed   = ref_req - user_refs
        await update.message.reply_text(
            f"👥 *{needed} more referral(s) needed!*\n\n"
            f"Your link:\n`{ref_link}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Deliver ────────────────────────────────────────────────────────────────
    unlock_link = campaign.get("unlock_link")
    if unlock_link:
        # URL-based campaign
        await update.message.reply_text(
            f"🎉 *Unlocked! Here's your content:*\n\n"
            f"🔗 {unlock_link}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Content", url=unlock_link)]
            ]),
        )
    else:
        # Material-based (original flow)
        await deliver_material(context.bot, update.message.chat_id, campaign)

    db.track("unlock_success", campaign_id)
    u["unlocked_campaigns"] = list(set(u.get("unlocked_campaigns", []) + [campaign_id]))
    db.save()


# ── PATCH /start to handle unlock_ prefix ─────────────────────────────────────

async def cmd_start_patched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Patched /start that intercepts unlock_<id> deep links before
    calling original cmd_start.
    """
    args = context.args or []
    if args:
        arg = args[0]
        # New format: unlock_CAMPID
        if arg.startswith("unlock_"):
            camp_id = arg[7:].upper()
            db.get_or_create_user(
                update.effective_user.id,
                username=update.effective_user.username or "",
                first_name=update.effective_user.first_name or "",
            )
            return await _handle_campaign_extended(update, context, camp_id)
        # Original format: CAMPID (8 chars upper)
        if len(arg) == 8 and arg.isupper():
            db.get_or_create_user(
                update.effective_user.id,
                username=update.effective_user.username or "",
                first_name=update.effective_user.first_name or "",
            )
            return await _handle_campaign_extended(update, context, arg)
    # Fall through to original
    return await cmd_start(update, context)


# ── VERIFY callback extended ───────────────────────────────────────────────────

async def verify_callback_extended(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Extended verify callback that supports both url-based and material campaigns.
    Registered BEFORE callback_router so it intercepts verify_ patterns.
    """
    query      = update.callback_query
    await query.answer()
    data       = query.data
    campaign_id = data[7:]
    uid        = query.from_user.id

    campaign = db.campaigns.get(campaign_id)
    if not campaign:
        await query.edit_message_text("❌ Campaign not found.")
        return

    channels   = campaign.get("channels", campaign.get("required_channels", []))
    not_joined = await check_channel_membership(context.bot, uid, channels)

    if not_joined:
        buttons = [
            [InlineKeyboardButton(f"📢 Join Channel {i+1}",
                                  url=f"https://t.me/{c.lstrip('@')}")]
            for i, c in enumerate(not_joined)
        ]
        buttons.append([InlineKeyboardButton("✅ Verify Again",
                                             callback_data=f"verify_{campaign_id}")])
        await query.edit_message_text(
            "❌ *Still not joined!*\n\nMissing:\n"
            + "\n".join(f"• `{c}`" for c in not_joined),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    db.track("verification_success", campaign_id)

    ref_req   = campaign.get("referral_required", 0)
    u         = db.get_or_create_user(uid)
    user_refs = u.get("referral_count", 0)

    if ref_req > 0 and user_refs < ref_req:
        bot_me   = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
        needed   = ref_req - user_refs
        await query.edit_message_text(
            f"👥 *{needed} more referral(s) needed!*\n\n`{ref_link}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await query.edit_message_text("🎁 *Unlocking…*", parse_mode=ParseMode.MARKDOWN)

    unlock_link = campaign.get("unlock_link")
    if unlock_link:
        await context.bot.send_message(
            query.message.chat_id,
            f"🎉 *Unlocked! Here's your content:*\n\n🔗 {unlock_link}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Content", url=unlock_link)]
            ]),
        )
    else:
        await deliver_material(context.bot, query.message.chat_id, campaign)

    db.track("unlock_success", campaign_id)
    u["unlocked_campaigns"] = list(set(u.get("unlocked_campaigns", []) + [campaign_id]))
    db.save()


# ── CREATOR DASHBOARD command ──────────────────────────────────────────────────

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dashboard — show creator dashboard with stats."""
    uid  = update.effective_user.id
    user = update.effective_user

    if not can_use_creator_features(uid):
        await update.message.reply_text(
            "❌ *Creator access required*\n\nTap 🚀 Become Creator first.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Become Creator", callback_data="u_become_creator")]
            ]),
        )
        return

    if is_admin(uid):
        ensure_admin_creator(uid, user.username or "", user.first_name or "")

    cr    = db.get_creator(uid)
    camps = cr.get("campaigns", []) if cr else []
    chans = cr.get("channels",   []) if cr else []

    total_clicks  = sum(db.analytics.get("campaign_clicks",       {}).get(c, 0) for c in camps)
    total_verif   = sum(db.analytics.get("verification_success",  {}).get(c, 0) for c in camps)
    total_unlocks = sum(db.analytics.get("unlock_success",        {}).get(c, 0) for c in camps)

    # Unique users who unlocked this creator's campaigns
    unlock_users = set()
    for uid_str, u_data in db.users.items():
        if any(c in set(camps) for c in u_data.get("unlocked_campaigns", [])):
            unlock_users.add(uid_str)

    chan_status = (
        f"✅ {', '.join(chans[:3])}" if chans
        else "❌ Not connected — use /becomecreator"
    )
    days  = db.creator_days_left(uid)
    badge = "♾ No expiry" if is_admin(uid) else f"⏳ {days} days left"

    await update.message.reply_text(
        f"📊 *Creator Dashboard*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *{cr.get('name', user.first_name) if cr else user.first_name}*\n"
        f"🔗 Channel: {chan_status}\n"
        f"⏳ Status: {badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Campaigns:       `{len(camps)}`\n"
        f"👆 Total Clicks:    `{total_clicks}`\n"
        f"✅ Verified:        `{total_verif}`\n"
        f"🔓 Total Unlocks:   `{total_unlocks}`\n"
        f"👥 Unique Unlockers:`{len(unlock_users)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now_str()}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Create Campaign", callback_data="c_setup"),
             InlineKeyboardButton("🎯 My Campaigns",   callback_data="c_campaigns")],
            [InlineKeyboardButton("📣 Broadcast",      callback_data="c_broadcast"),
             InlineKeyboardButton("🔗 My Links",       callback_data="c_links")],
        ]),
    )


# ── BROADCAST USERS command ────────────────────────────────────────────────────

async def cmd_broadcastusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcastusers — creator sends message to all their unlockers."""
    uid = update.effective_user.id

    if not can_use_creator_features(uid):
        await update.message.reply_text(
            "❌ *Creator access required*\n\nTap 🚀 Become Creator first.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not creator_is_active(uid):
        await update.message.reply_text("⏰ Subscription expired! Use /renewpanel")
        return

    if is_admin(uid):
        ensure_admin_creator(uid)

    cr      = db.get_creator(uid)
    camp_ids = set(cr.get("campaigns", []) if cr else [])

    # Find users who have unlocked any of this creator's campaigns
    target_ids = [
        int(u_id)
        for u_id, u_data in db.users.items()
        if any(c in camp_ids for c in u_data.get("unlocked_campaigns", []))
    ]

    if not target_ids:
        await update.message.reply_text(
            "📭 *No audience yet!*\n\n"
            "Nobody has unlocked your content yet.\n"
            "Share your campaign links to grow your audience!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 My Links", callback_data="c_links")]
            ]),
        )
        return

    context.user_data["cbcast_step"]       = "content"
    context.user_data["cbcast_targets"]    = target_ids
    await update.message.reply_text(
        f"📣 *Broadcast to Your Audience*\n\n"
        f"👥 Audience size: *{len(target_ids)}* users\n\n"
        f"Send your message now.\n"
        f"Supports: text, photo, video, document (+ caption)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="c_dash")]
        ]),
    )


# ── AUTO-DETECT bot added to channel ──────────────────────────────────────────

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fires when the bot's admin status changes in any chat.
    If bot becomes admin in a channel, try to find the owner and notify them.
    """
    my_member = update.my_chat_member
    if not my_member:
        return

    new_status = my_member.new_chat_member.status
    chat       = my_member.chat
    from_user  = my_member.from_user  # who made the change

    # Only care about channels (not groups)
    if chat.type not in ("channel", "supergroup"):
        return

    if new_status not in ("administrator", "creator"):
        return  # bot was removed or demoted — ignore

    # Bot just became admin — notify the user who added it
    if not from_user:
        return

    uid = from_user.id
    logger.info("🔔 Bot added as admin in %s by %d", chat.title, uid)

    chan_uname = f"@{chat.username}" if chat.username else str(chat.id)

    try:
        await context.bot.send_message(
            uid,
            f"🎉 *Channel Detected!*\n\n"
            f"Bot was added as admin in:\n"
            f"📢 *{chat.title}* (`{chan_uname}`)\n\n"
            f"You can now:\n"
            f"• Create unlock campaigns for this channel\n"
            f"• Use /createcampaign to get started\n\n"
            f"Or tap the button below 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Create Campaign", callback_data="c_setup")],
                [InlineKeyboardButton("🚀 Connect Channel", callback_data="onboard_start")],
            ]),
        )
    except Exception as e:
        logger.warning("Could not notify user %d about channel: %s", uid, e)

    # Auto-register if they're already in creators
    if can_use_creator_features(uid):
        cr = db.get_creator(uid)
        if cr and chan_uname not in cr.get("channels", []):
            cr.setdefault("channels", []).append(chan_uname)
            cr["channel_id"] = str(chat.id)
            db._data.setdefault("channels_map", {})[str(chat.id)] = str(uid)
            db.save(force=True)


# ── ONBOARDING TEXT MESSAGE handler (catch channel input from callback flow) ───

async def onboard_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Catches channel username messages when user started onboarding
    via the inline button (not /becomecreator command) — outside ConversationHandler.
    """
    uid = update.effective_user.id
    if not context.user_data.get("onboard_from_callback"):
        return  # Not in callback-triggered onboarding — ignore

    raw     = (update.message.text or "").strip()
    channel = raw if raw.startswith("@") or raw.startswith("-") else f"@{raw}"

    progress = await update.message.reply_text(
        f"🔍 Verifying `{channel}`…", parse_mode=ParseMode.MARKDOWN
    )
    result = await verify_channel_and_permissions(context.bot, channel)

    if not result["ok"]:
        await progress.edit_text(
            f"*Verification Failed*\n\n{result['reason']}\n\nSend channel username again:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_onboard_cancel(),
        )
        return  # Stay waiting

    # Register creator
    chan_uname  = result["username"]
    chat_id     = result["chat_id"]
    chan_title  = result["title"]
    user        = update.effective_user
    trial_days  = db.settings.get("trial_days", 90)

    if db.get_creator(uid):
        cr = db.get_creator(uid)
        if chan_uname not in cr.get("channels", []):
            cr.setdefault("channels", []).append(chan_uname)
        cr["channel_id"] = str(chat_id)
    else:
        db.creators[str(uid)] = {
            "username":       user.username or "",
            "name":           user.first_name or f"Creator_{uid}",
            "trial_start":    datetime.now().isoformat(),
            "trial_days":     trial_days,
            "channels":       [chan_uname],
            "channel_id":     str(chat_id),
            "materials":      [],
            "campaigns":      [],
            "joined_at":      datetime.now().isoformat(),
            "self_onboarded": True,
        }

    db._data.setdefault("channels_map", {})[str(chat_id)] = str(uid)
    db.save(force=True)
    context.user_data.pop("onboard_from_callback", None)

    await progress.edit_text(
        f"✅ *Channel Connected Successfully!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 *{chan_title}* (`{chan_uname}`)\n\n"
        f"🚀 *Creator Panel Activated!*\n"
        f"Use *➕ Create Campaign* to get started!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_creator(),
    )



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
            CallbackQueryHandler(cmd_setup, pattern=r"^c_setup$"),  # button → same handler
        ],
        per_message=False,
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

    # ── Creator onboarding ConversationHandler ────────────────────
    onboard_conv = ConversationHandler(
        entry_points=[
            CommandHandler("becomecreator", cmd_become_creator),
            CallbackQueryHandler(cmd_become_creator, pattern=r"^onboard_start$"),
        ],
        per_message=False,
        states={
            ONBOARD_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_recv_channel)
            ],
        },
        fallbacks=[
            CommandHandler("cancel",       onboard_cancel),
            CallbackQueryHandler(onboard_cancel, pattern=r"^onboard_cancel$"),
        ],
        allow_reentry=True,
        name="onboard_conv",
    )

    # ── Create campaign ConversationHandler ────────────────────────
    createcamp_conv = ConversationHandler(
        entry_points=[
            CommandHandler("createcampaign", cmd_createcampaign),
            CallbackQueryHandler(cmd_createcampaign, pattern=r"^createcamp_new$"),
        ],
        per_message=False,
        states={
            CREATECAMP_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, createcamp_recv_link)
            ],
            CREATECAMP_CHANNELS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, createcamp_recv_channels)
            ],
        },
        fallbacks=[
            CommandHandler("cancel",         createcamp_cancel),
            CallbackQueryHandler(createcamp_cancel, pattern=r"^createcamp_cancel$"),
        ],
        allow_reentry=True,
        name="createcamp_conv",
    )

    # ── Register handlers (order matters) ─────────────────────────
    # Patched /start handles unlock_ deep links
    app.add_handler(CommandHandler("start",   cmd_start_patched))
    # Onboarding BEFORE setup (lower group number = higher priority)
    app.add_handler(onboard_conv)
    app.add_handler(createcamp_conv)
    app.add_handler(setup_conv)

    # ── Universal commands ─────────────────────────────────────────
    app.add_handler(CommandHandler("id",     cmd_id))
    app.add_handler(CommandHandler("help",   cmd_help))

    # ── Admin commands ─────────────────────────────────────────────
    app.add_handler(CommandHandler("admin",          cmd_admin))
    app.add_handler(CommandHandler("broadcast",      cmd_broadcast))
    app.add_handler(CommandHandler("globalstats",    cmd_globalstats))
    app.add_handler(CommandHandler("settrial",       cmd_settrial))
    app.add_handler(CommandHandler("setprice",       cmd_setprice))
    app.add_handler(CommandHandler("setupi",         cmd_setupi))
    app.add_handler(CommandHandler("addcreator",     cmd_addcreator))
    app.add_handler(CommandHandler("bancreator",     cmd_bancreator))
    app.add_handler(CommandHandler("renewcreator",   cmd_renewcreator))
    app.add_handler(CommandHandler("viewuser",       cmd_viewuser))
    app.add_handler(CommandHandler("viewcreator",    cmd_viewcreator))
    app.add_handler(CommandHandler("listcreators",   cmd_listcreators))
    app.add_handler(CommandHandler("listusers",      cmd_listusers))
    app.add_handler(CommandHandler("dm",             cmd_dm))
    app.add_handler(CommandHandler("delcampaign",    cmd_delcampaign))
    app.add_handler(CommandHandler("addadmin",       cmd_addadmin))
    app.add_handler(CommandHandler("export",         cmd_export))

    # ── Creator commands ───────────────────────────────────────────
    app.add_handler(CommandHandler("creator",            cmd_creator))
    app.add_handler(CommandHandler("mycampaigns",        cmd_mycampaigns))
    app.add_handler(CommandHandler("mystats",            cmd_mystats))
    app.add_handler(CommandHandler("materials",          cmd_materials))
    app.add_handler(CommandHandler("channels",           cmd_channels))
    app.add_handler(CommandHandler("broadcast_my_users", cmd_broadcast_my_users))
    app.add_handler(CommandHandler("renewpanel",         cmd_renewpanel))
    app.add_handler(CommandHandler("togglecampaign",     cmd_togglecampaign))

    # ── New commands ──────────────────────────────────────────────
    app.add_handler(CommandHandler("becomecreator",   cmd_become_creator))
    app.add_handler(CommandHandler("createcampaign",  cmd_createcampaign))
    app.add_handler(CommandHandler("dashboard",       cmd_dashboard))
    app.add_handler(CommandHandler("broadcastusers",  cmd_broadcastusers))

    # ── Channel admin detection ────────────────────────────────────
    app.add_handler(ChatMemberHandler(handle_my_chat_member,
                                      ChatMemberHandler.MY_CHAT_MEMBER))

    # ── Verify callback (extended — must come BEFORE callback_router) ──
    app.add_handler(CallbackQueryHandler(verify_callback_extended, pattern=r"^verify_"))

    # ── Callback + message handlers ────────────────────────────────
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            & ~filters.COMMAND,
            general_message_handler,
        )
    )
    # Unknown command handler (must be last)
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    # ── Error handler ──────────────────────────────────────────────
    app.add_error_handler(error_handler)

    logger.info("📡 Polling started…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
