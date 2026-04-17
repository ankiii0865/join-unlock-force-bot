#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║          ForceHub Bot  —  Force-Subscribe Platform           ║
║  Clean rewrite · HTML parse mode · Zero subscription wall   ║
║              python-telegram-bot v21  ·  async               ║
╚══════════════════════════════════════════════════════════════╝
Bug fixes in this version:
  ✅ HTML parse mode everywhere  →  no more _ * ` breaking entities
  ✅ h()  helper escapes all user strings  →  no more parse errors
  ✅ safe_edit()  →  silently ignores "message not modified" errors
  ✅ Null-guards on every user/creator/campaign lookup
  ✅ Channel names with underscores work perfectly
  ✅ Subscriptions removed — creators are always free
  ✅ Broadcast removed from creator panel — admin only
  ✅ Clean UI with consistent section dividers
"""

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from html import escape as _esc
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ForceHub")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Hardcoded super-admins — always full access
SUPER_ADMIN_IDS: List[int] = [5695957392]  # @chamgaadar  ANKIII YADAV

_env_admins: List[int] = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]
ADMIN_IDS: List[int] = list({*SUPER_ADMIN_IDS, *_env_admins})

DATA_DIR  = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_FILE = DATA_DIR / "forcehub_data.json"

# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATION STATES
# ─────────────────────────────────────────────────────────────────────────────
(
    SETUP_CHANNEL,
    SETUP_MAT_TYPE,
    SETUP_MAT_TITLE,
    SETUP_MAT_CONTENT,
    SETUP_REF_COUNT,
) = range(5)

ONBOARD_CHANNEL  = 10
CAMP_LINK        = 11
CAMP_CHANNELS    = 12

# ─────────────────────────────────────────────────────────────────────────────
# HTML HELPERS  — the root fix for all parse-entity bugs
# ─────────────────────────────────────────────────────────────────────────────
def h(text: Any) -> str:
    """Escape any string for safe use inside HTML Telegram messages."""
    return _esc(str(text) if text is not None else "")


def bold(text: Any) -> str:
    return f"<b>{h(text)}</b>"


def code(text: Any) -> str:
    return f"<code>{h(text)}</code>"


def line(char: str = "─", n: int = 30) -> str:
    return char * n


async def safe_edit(query, text: str, reply_markup=None, **kwargs):
    """
    Edit a message, silently ignoring 'Message is not modified' errors.
    Avoids the BadRequest crash when content hasn't changed.
    """
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            **kwargs,
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            pass  # Perfectly fine — content is already correct
        else:
            raise


async def safe_reply(message, text: str, reply_markup=None, **kwargs):
    """Send a reply with HTML parse mode."""
    return await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DATA MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class DataManager:
    SAVE_INTERVAL = 30

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._dirty     = False
        self._last_save = time.monotonic()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ── Skeleton ──────────────────────────────────────────────────────────────
    def _skeleton(self) -> Dict:
        return {
            "users":     {},
            "creators":  {},
            "materials": {},
            "campaigns": {},
            "analytics": {
                "campaign_clicks":      {},
                "verification_success": {},
                "unlock_success":       {},
                "referral_unlocks":     {},
                "daily":                {},
            },
            "settings": {
                "upi_id":    "yourname@upi",
                "price":     199,
                "admin_ids": list(ADMIN_IDS),
            },
            "channels_map": {},
        }

    # ── Load / save ───────────────────────────────────────────────────────────
    def _load(self):
        if DATA_FILE.exists():
            try:
                with open(DATA_FILE, encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(
                    "✅ Loaded — %d users, %d creators, %d campaigns",
                    len(self._data.get("users", {})),
                    len(self._data.get("creators", {})),
                    len(self._data.get("campaigns", {})),
                )
            except Exception as e:
                logger.error("Load failed (%s) — fresh start", e)
                self._data = self._skeleton()
        else:
            self._data = self._skeleton()
            self._flush()
            logger.info("📁 Created forcehub_data.json")

        for k, v in self._skeleton().items():
            self._data.setdefault(k, v)

    def _flush(self):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            self._dirty     = False
            self._last_save = time.monotonic()
        except Exception as e:
            logger.error("Flush failed: %s", e)

    def save(self, force: bool = False):
        self._dirty = True
        if force or (time.monotonic() - self._last_save >= self.SAVE_INTERVAL):
            self._flush()

    async def periodic_save(self):
        while True:
            await asyncio.sleep(self.SAVE_INTERVAL)
            if self._dirty:
                self._flush()

    # ── Accessors ─────────────────────────────────────────────────────────────
    @property
    def users(self)       -> Dict: return self._data["users"]
    @property
    def creators(self)    -> Dict: return self._data["creators"]
    @property
    def materials(self)   -> Dict: return self._data["materials"]
    @property
    def campaigns(self)   -> Dict: return self._data["campaigns"]
    @property
    def analytics(self)   -> Dict: return self._data["analytics"]
    @property
    def settings(self)    -> Dict: return self._data["settings"]
    @property
    def channels_map(self)-> Dict: return self._data.setdefault("channels_map", {})

    # ── Users ─────────────────────────────────────────────────────────────────
    def get_or_create_user(self, uid: int, username: str = "",
                           first_name: str = "") -> Dict:
        k = str(uid)
        if k not in self.users:
            self.users[k] = {
                "username":           username,
                "first_name":         first_name,
                "joined_at":          datetime.now().isoformat(),
                "unlocked_campaigns": [],
                "referral_count":     0,
                "referred_by":        None,
            }
            self._bump_daily("joins")
            self.save()
        else:
            if username:    self.users[k]["username"]   = username
            if first_name:  self.users[k]["first_name"] = first_name
        return self.users[k]

    def get_user(self, uid: int) -> Optional[Dict]:
        return self.users.get(str(uid))

    # ── Creators ──────────────────────────────────────────────────────────────
    def get_creator(self, uid: int) -> Optional[Dict]:
        return self.creators.get(str(uid))

    def register_creator(self, uid: int, username: str = "",
                         name: str = "", self_onboarded: bool = False) -> Dict:
        self.creators[str(uid)] = {
            "username":       username,
            "name":           name or f"Creator_{uid}",
            "channels":       [],
            "channel_id":     "",
            "materials":      [],
            "campaigns":      [],
            "joined_at":      datetime.now().isoformat(),
            "self_onboarded": self_onboarded,
        }
        self.save(force=True)
        return self.creators[str(uid)]

    def ensure_admin_creator(self, uid: int, username: str = "",
                             name: str = "") -> Dict:
        """Auto-register admin as creator (no trial, always free)."""
        if not self.get_creator(uid):
            self.creators[str(uid)] = {
                "username":       username,
                "name":           name or f"Admin_{uid}",
                "channels":       [],
                "channel_id":     "",
                "materials":      [],
                "campaigns":      [],
                "joined_at":      datetime.now().isoformat(),
                "self_onboarded": False,
            }
            self.save(force=True)
        return self.creators[str(uid)]

    # ── Campaigns ─────────────────────────────────────────────────────────────
    def new_campaign(self, creator_id: int, channels: List[str],
                     referral_required: int = 0,
                     material_id: Optional[str] = None,
                     unlock_link: Optional[str] = None) -> str:
        cid = str(uuid.uuid4())[:8].upper()
        while cid in self.campaigns:
            cid = str(uuid.uuid4())[:8].upper()
        self.campaigns[cid] = {
            "creator_id":        str(creator_id),
            "channels":          channels,
            "material_id":       material_id,
            "unlock_link":       unlock_link,
            "referral_required": referral_required,
            "is_active":         True,
            "campaign_type":     "url" if unlock_link else "material",
            "created_at":        datetime.now().isoformat(),
        }
        cr = self.get_creator(creator_id)
        if cr is not None:
            cr.setdefault("campaigns", []).append(cid)
        self.save(force=True)
        return cid

    # ── Analytics ─────────────────────────────────────────────────────────────
    def _bump_daily(self, field: str):
        today = datetime.now().strftime("%Y-%m-%d")
        day   = self.analytics["daily"].setdefault(today, {"joins": 0, "unlocks": 0})
        day[field] = day.get(field, 0) + 1

    def track(self, event: str, campaign_id: Optional[str] = None):
        if campaign_id:
            bucket = self.analytics.setdefault(event, {})
            bucket[campaign_id] = bucket.get(campaign_id, 0) + 1
        if event == "unlock_success":
            self._bump_daily("unlocks")
        self.save()

    def global_stats(self) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        td    = self.analytics.get("daily", {}).get(today, {})
        return {
            "total_users":     len(self.users),
            "total_creators":  len(self.creators),
            "total_campaigns": len(self.campaigns),
            "total_materials": len(self.materials),
            "today_joins":     td.get("joins",   0),
            "today_unlocks":   td.get("unlocks", 0),
        }

    # ── Settings shortcuts ────────────────────────────────────────────────────
    def set_upi(self,   v: str): self.settings["upi_id"] = v;  self.save(force=True)
    def set_price(self, v: int): self.settings["price"]  = v;  self.save(force=True)

    # ── Ban / remove creator ──────────────────────────────────────────────────
    def remove_creator(self, uid: int):
        self.creators.pop(str(uid), None)
        self.save(force=True)


db = DataManager()


# ─────────────────────────────────────────────────────────────────────────────
# ROLE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    if uid in SUPER_ADMIN_IDS:
        return True
    return uid in ADMIN_IDS or uid in db.settings.get("admin_ids", [])

def is_super_admin(uid: int) -> bool:
    return uid in SUPER_ADMIN_IDS

def is_creator(uid: int) -> bool:
    return str(uid) in db.creators

def can_create(uid: int) -> bool:
    """Admin or registered creator — no subscription wall."""
    return is_creator(uid) or is_admin(uid)


def now_str() -> str:
    return datetime.now().strftime("%d %b %Y  %H:%M IST")

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL / CAMPAIGN HELPERS
# ─────────────────────────────────────────────────────────────────────────────
async def check_membership(bot, uid: int, channels: List[str]) -> List[str]:
    """Return list of channels the user has NOT joined."""
    missing: List[str] = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch, uid)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except TelegramError:
            missing.append(ch)
    return missing


async def verify_channel(bot, channel: str) -> Dict:
    """
    Verify channel exists + bot is admin with correct permissions.
    Returns {"ok": bool, "reason": str, "chat_id": int, "title": str, "username": str}

    FIX: channel names with underscores (like @ani_wallpaperr) are handled
    correctly because we never pass them through Markdown formatting.
    """
    # Normalise channel input
    channel = channel.strip()
    if not channel.startswith("@") and not channel.startswith("-"):
        channel = "@" + channel

    try:
        chat = await bot.get_chat(channel)
    except Exception as e:
        return {
            "ok":     False,
            "reason": f"Channel {code(channel)} not found or inaccessible.\n{code(str(e))}",
        }

    try:
        me  = await bot.get_me()
        mem = await bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        return {"ok": False, "reason": f"Cannot check bot status: {code(str(e))}"}

    if mem.status not in ("administrator", "creator"):
        me2 = await bot.get_me()
        return {
            "ok":     False,
            "reason": (
                f"❌ Bot is <b>not admin</b> in {code(channel)}.\n\n"
                f"<b>Steps:</b>\n"
                f"1️⃣ Open your channel → <b>Administrators</b>\n"
                f"2️⃣ Add {code('@' + me2.username)} as admin\n"
                f"3️⃣ Enable <b>Post Messages</b> + <b>Invite Users</b>\n"
                f"4️⃣ Send the channel username again"
            ),
        }

    if mem.status == "administrator":
        missing_perms = []
        if not getattr(mem, "can_post_messages",  True):
            missing_perms.append("Post Messages")
        if not getattr(mem, "can_invite_users",   True):
            missing_perms.append("Invite Users")
        if missing_perms:
            return {
                "ok":     False,
                "reason": (
                    "⚠️ Bot is admin but missing permissions:\n"
                    + "".join(f"  • <b>{h(p)}</b>\n" for p in missing_perms)
                    + "\nGrant these in channel admin settings."
                ),
            }

    uname = f"@{chat.username}" if chat.username else str(chat.id)
    return {
        "ok":       True,
        "chat_id":  chat.id,
        "title":    chat.title or channel,
        "username": uname,
    }


async def deliver_campaign(bot, chat_id: int, campaign: Dict):
    """Send unlock content to the user."""
    unlock_link = campaign.get("unlock_link")
    if unlock_link:
        await bot.send_message(
            chat_id,
            f"🎉 <b>Unlocked! Here's your content:</b>\n\n🔗 {h(unlock_link)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🌐 Open Content", url=unlock_link)]]
            ),
        )
        return

    mid      = campaign.get("material_id")
    material = db.materials.get(mid or "")
    if not material:
        await bot.send_message(chat_id, "✅ Unlocked! Contact the creator for your content.")
        return

    ftype  = material.get("file_type", "text")
    title  = h(material.get("title", "Unlocked Content"))
    desc   = h(material.get("description", ""))
    file_id = material.get("file_id")
    header  = f"🎉 <b>{title}</b>\n\n"

    try:
        if ftype == "text":
            await bot.send_message(chat_id, header + desc, parse_mode=ParseMode.HTML)
        elif ftype == "photo":
            await bot.send_photo(chat_id, file_id,
                                 caption=header + desc, parse_mode=ParseMode.HTML)
        elif ftype == "video":
            await bot.send_video(chat_id, file_id,
                                 caption=header + desc, parse_mode=ParseMode.HTML)
        elif ftype == "document":
            await bot.send_document(chat_id, file_id,
                                    caption=header + desc, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("deliver_campaign: %s", e)
        await bot.send_message(chat_id,
                               "✅ Unlocked! Delivery failed — please contact the creator.")


async def batch_broadcast(
    app: Application,
    user_ids: List[int],
    ctype: str,
    content: Any,
    caption: str = "",
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Dict:
    sent = failed = 0
    for uid in user_ids:
        try:
            if ctype == "text":
                await app.bot.send_message(
                    uid, content,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            elif ctype == "photo":
                await app.bot.send_photo(
                    uid, content,
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            elif ctype == "video":
                await app.bot.send_video(
                    uid, content,
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            elif ctype == "document":
                await app.bot.send_document(
                    uid, content,
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            sent += 1
        except (Forbidden, BadRequest):
            failed += 1
        except Exception as e:
            logger.warning("Broadcast to %d: %s", uid, e)
            failed += 1
        await asyncio.sleep(0.05)
    return {"sent": sent, "failed": failed}


def parse_buttons(text: str) -> Optional[InlineKeyboardMarkup]:
    """Parse 'Label - https://url' lines into inline keyboard."""
    if not text or text.strip().lower() in ("skip", "no", "none"):
        return None
    rows = []
    for line_ in text.strip().splitlines():
        if " - " in line_:
            label, url = line_.split(" - ", 1)
            if label.strip() and url.strip().startswith("http"):
                rows.append([InlineKeyboardButton(label.strip(), url=url.strip())])
    return InlineKeyboardMarkup(rows) if rows else None


# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────
def kb_user() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Unlock Content",    callback_data="u_unlock")],
        [InlineKeyboardButton("📚 My Unlocks",        callback_data="u_unlocks"),
         InlineKeyboardButton("👥 Referral Link",     callback_data="u_referral")],
        [InlineKeyboardButton("🚀 Become Creator",    callback_data="u_become_creator")],
        [InlineKeyboardButton("❓ Help",              callback_data="u_help")],
    ])

def kb_creator() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard",      callback_data="c_dash")],
        [InlineKeyboardButton("➕ New Campaign",    callback_data="c_new"),
         InlineKeyboardButton("🔧 Advanced Setup", callback_data="c_adv_setup")],
        [InlineKeyboardButton("🎯 My Campaigns",   callback_data="c_campaigns"),
         InlineKeyboardButton("📈 Analytics",      callback_data="c_stats")],
        [InlineKeyboardButton("📢 My Channels",    callback_data="c_channels"),
         InlineKeyboardButton("📦 Materials",      callback_data="c_materials")],
        [InlineKeyboardButton("🔗 Share Links",    callback_data="c_links")],
        [InlineKeyboardButton("❓ Help",           callback_data="c_help")],
    ])

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",          callback_data="a_stats"),
         InlineKeyboardButton("📣 Broadcast",      callback_data="a_broadcast")],
        [InlineKeyboardButton("👥 All Users",      callback_data="a_users_0"),
         InlineKeyboardButton("🎨 All Creators",   callback_data="a_creators_0")],
        [InlineKeyboardButton("🎯 All Campaigns",  callback_data="a_campaigns_0"),
         InlineKeyboardButton("📦 All Materials",  callback_data="a_materials_0")],
        [InlineKeyboardButton("➕ Add Creator",    callback_data="a_addcreator"),
         InlineKeyboardButton("🚫 Remove Creator", callback_data="a_ban")],
        [InlineKeyboardButton("💬 DM User",        callback_data="a_dm"),
         InlineKeyboardButton("🗑 Del Campaign",   callback_data="a_delcamp")],
        [InlineKeyboardButton("⚙️ Settings",       callback_data="a_settings"),
         InlineKeyboardButton("📤 Export",         callback_data="a_export")],
    ])

def kb_admin_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Set Price",  callback_data="a_set_price"),
         InlineKeyboardButton("💳 Set UPI",    callback_data="a_set_upi")],
        [InlineKeyboardButton("👑 Add Admin",  callback_data="a_set_admin")],
        [InlineKeyboardButton("🔙 Back",       callback_data="a_panel")],
    ])

def kb_broadcast_target() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users",    callback_data="bcast_users")],
        [InlineKeyboardButton("🎨 All Creators", callback_data="bcast_creators")],
        [InlineKeyboardButton("📢 Everyone",     callback_data="bcast_everyone")],
        [InlineKeyboardButton("❌ Cancel",       callback_data="a_panel")],
    ])

def kb_back(to: str = "u_back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=to)]])

def kb_cancel(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=cb)]])


# ─────────────────────────────────────────────────────────────────────────────
# CORE UNLOCK FLOW  (/start deep links + verify)
# ─────────────────────────────────────────────────────────────────────────────
async def _process_campaign(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    campaign_id: str,
):
    user = update.effective_user
    if user is None:
        return
    uid = user.id
    msg = update.message

    campaign = db.campaigns.get(campaign_id)
    if not campaign or not campaign.get("is_active"):
        if msg:
            await safe_reply(msg, "❌ This campaign link is not active or doesn't exist.")
        return

    db.track("campaign_clicks", campaign_id)
    channels = campaign.get("channels", [])
    missing  = await check_membership(context.bot, uid, channels)

    if missing:
        btns = [
            [InlineKeyboardButton(
                f"📢 Join Channel {i + 1}",
                url=f"https://t.me/{c.lstrip('@')}",
            )]
            for i, c in enumerate(missing)
        ]
        btns.append([
            InlineKeyboardButton(
                "✅ I've Joined — Verify Now",
                callback_data=f"verify_{campaign_id}",
            )
        ])
        if msg:
            await safe_reply(
                msg,
                "🔐 <b>Content Locked</b>\n\n"
                "Join the channel(s) below to unlock:\n\n"
                + "\n".join(f"• {code(c)}" for c in missing)
                + "\n\nAfter joining tap <b>✅ Verify</b>",
                reply_markup=InlineKeyboardMarkup(btns),
            )
        return

    # Referral check
    ref_req   = campaign.get("referral_required", 0)
    u         = db.get_or_create_user(uid, user.username or "", user.first_name or "")
    user_refs = u.get("referral_count", 0)

    if ref_req > 0 and user_refs < ref_req:
        bot_me   = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
        if msg:
            await safe_reply(
                msg,
                f"👥 <b>{ref_req - user_refs} more referral(s) needed!</b>\n\n"
                f"Your referral link:\n{code(ref_link)}",
            )
        return

    if msg:
        await deliver_campaign(context.bot, msg.chat_id, campaign)
    db.track("unlock_success", campaign_id)
    u["unlocked_campaigns"] = list(
        set(u.get("unlocked_campaigns", []) + [campaign_id])
    )
    db.save()


async def cb_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles verify_<campaign_id> callback."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    campaign_id = query.data[7:]
    uid         = query.from_user.id if query.from_user else None
    if uid is None:
        await safe_edit(query, "❌ Could not identify user.")
        return

    campaign = db.campaigns.get(campaign_id)
    if not campaign:
        await safe_edit(query, "❌ Campaign not found.")
        return

    channels = campaign.get("channels", [])
    missing  = await check_membership(context.bot, uid, channels)

    if missing:
        btns = [
            [InlineKeyboardButton(
                f"📢 Join Channel {i + 1}",
                url=f"https://t.me/{c.lstrip('@')}",
            )]
            for i, c in enumerate(missing)
        ]
        btns.append([
            InlineKeyboardButton("✅ Verify Again", callback_data=f"verify_{campaign_id}")
        ])
        await safe_edit(
            query,
            "❌ <b>Still not joined!</b>\n\nMissing:\n"
            + "\n".join(f"• {code(c)}" for c in missing),
            reply_markup=InlineKeyboardMarkup(btns),
        )
        return

    db.track("verification_success", campaign_id)

    ref_req  = campaign.get("referral_required", 0)
    u        = db.get_or_create_user(uid)
    user_refs = u.get("referral_count", 0)

    if ref_req > 0 and user_refs < ref_req:
        bot_me   = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
        await safe_edit(
            query,
            f"👥 <b>{ref_req - user_refs} more referral(s) needed!</b>\n\n"
            f"Your link:\n{code(ref_link)}",
        )
        return

    await safe_edit(query, "🎁 <b>Unlocking your content…</b>")
    if query.message:
        await deliver_campaign(context.bot, query.message.chat_id, campaign)
    db.track("unlock_success", campaign_id)
    u["unlocked_campaigns"] = list(
        set(u.get("unlocked_campaigns", []) + [campaign_id])
    )
    db.save()


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or update.message is None:
        return
    uid = user.id
    db.get_or_create_user(uid, user.username or "", user.first_name or "")

    args = context.args or []
    if args:
        arg = args[0]

        # Referral link
        if arg.startswith("ref_"):
            referrer_id = arg[4:]
            u = db.get_user(uid)
            if u and not u.get("referred_by") and referrer_id != str(uid):
                u["referred_by"] = referrer_id
                referrer = db.users.get(referrer_id)
                if referrer:
                    referrer["referral_count"] = referrer.get("referral_count", 0) + 1
                    db.track("referral_unlocks", referrer_id)
                db.save()

        # Campaign deep-link: unlock_CAMPID or plain 8-char ID
        elif arg.startswith("unlock_") or (len(arg) == 8 and arg.isupper()):
            cid = arg[7:].upper() if arg.startswith("unlock_") else arg
            return await _process_campaign(update, context, cid)

    # ── Route to correct menu ─────────────────────────────────────────────────
    if is_admin(uid):
        s     = db.global_stats()
        badge = "👑 SUPER ADMIN" if is_super_admin(uid) else "🛡️ Admin"
        await safe_reply(
            update.message,
            f"{badge} — <b>ForceHub Control Center</b>\n"
            f"{line()}\n"
            f"👋 <b>{h(user.first_name)}</b>  🆔 {code(uid)}\n"
            f"{line()}\n"
            f"👥 <b>{s['total_users']}</b> users  🎨 <b>{s['total_creators']}</b> creators\n"
            f"🎯 <b>{s['total_campaigns']}</b> campaigns  📦 <b>{s['total_materials']}</b> materials\n"
            f"🆕 Today: <b>{s['today_joins']}</b> joins | <b>{s['today_unlocks']}</b> unlocks\n"
            f"{line()}\n"
            f"💰 ₹{h(db.settings.get('price',199))}  "
            f"💳 {code(db.settings.get('upi_id','Not set'))}\n"
            f"🕐 {h(now_str())}",
            reply_markup=kb_admin(),
        )

    elif is_creator(uid):
        cr    = db.get_creator(uid)
        camps = cr.get("campaigns", []) if cr else []
        total_u = sum(db.analytics.get("unlock_success", {}).get(c, 0) for c in camps)
        await safe_reply(
            update.message,
            f"🎨 <b>Creator Panel — ForceHub</b>\n"
            f"{line()}\n"
            f"👤 <b>{h(cr.get('name', user.first_name) if cr else user.first_name)}</b>  {code(uid)}\n"
            f"{line()}\n"
            f"🎯 <b>{len(camps)}</b> campaigns  🔓 <b>{total_u}</b> unlocks\n"
            f"📢 <b>{len(cr.get('channels', []) if cr else [])}</b> channels\n"
            f"🕐 {h(now_str())}",
            reply_markup=kb_creator(),
        )

    else:
        await safe_reply(
            update.message,
            f"🚀 <b>Welcome to ForceHub</b>, {h(user.first_name)}!\n\n"
            f"🔓 Unlock premium content by joining channels.\n"
            f"Get a campaign link from a creator and open it!\n\n"
            f"{line()}\n"
            f"🎨 <b>Are you a creator?</b>\n"
            f"Tap <b>🚀 Become Creator</b> to set up your own\n"
            f"unlock campaigns — completely <b>free!</b>\n\n"
            f"🆔 Your ID: {code(uid)}",
            reply_markup=kb_user(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# USER MENU CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────
async def cb_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data
    uid  = query.from_user.id if query.from_user else 0

    if data == "u_back":
        await safe_edit(query, "🚀 <b>ForceHub — Main Menu</b>", reply_markup=kb_user())

    elif data == "u_unlock":
        await safe_edit(
            query,
            "🔓 <b>Unlock Content</b>\n\n"
            "Get a campaign link from a creator and open it.\n\n"
            "Link format:\n"
            f"{code('t.me/BotName?start=CAMPAIGN_ID')}",
            reply_markup=kb_back("u_back"),
        )

    elif data == "u_unlocks":
        u        = db.get_user(uid)
        unlocked = (u.get("unlocked_campaigns", []) if u else [])[-10:]
        text     = "📚 <b>Your Unlocked Content</b>\n\n"
        if unlocked:
            for cid in unlocked:
                c   = db.campaigns.get(cid, {})
                lnk = c.get("unlock_link", "")
                mat = db.materials.get(c.get("material_id", ""), {})
                title = mat.get("title", "") or (lnk[:30] + "…" if lnk else cid)
                text += f"✅ {code(cid)} — {h(title)}\n"
        else:
            text += "📭 Nothing unlocked yet."
        await safe_edit(query, text, reply_markup=kb_back("u_back"))

    elif data == "u_referral":
        u        = db.get_or_create_user(uid)
        bot_me   = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
        await safe_edit(
            query,
            f"👥 <b>Your Referral Stats</b>\n\n"
            f"Total Referrals: <b>{u.get('referral_count', 0)}</b>\n\n"
            f"🔗 Your referral link:\n{code(ref_link)}\n\n"
            f"Share this link — when someone joins via it,\nyour referral count increases!",
            reply_markup=kb_back("u_back"),
        )

    elif data == "u_help":
        await safe_edit(
            query,
            "❓ <b>Help — ForceHub</b>\n\n"
            "🔓 <b>Unlock Content</b> — Open a campaign link from a creator\n"
            "📚 <b>My Unlocks</b> — View content you've already unlocked\n"
            "👥 <b>Referral Link</b> — Invite friends to earn bonus unlocks\n"
            "🚀 <b>Become Creator</b> — Set up your own unlock campaigns (free!)\n\n"
            "Need support? Contact the bot admin.",
            reply_markup=kb_back("u_back"),
        )

    elif data == "u_become_creator":
        if can_create(uid):
            if is_admin(uid):
                db.ensure_admin_creator(
                    uid,
                    query.from_user.username or "",
                    query.from_user.first_name or "",
                )
            cr    = db.get_creator(uid)
            camps = cr.get("campaigns", []) if cr else []
            total_u = sum(db.analytics.get("unlock_success", {}).get(c, 0) for c in camps)
            await safe_edit(
                query,
                f"🎨 <b>Creator Panel</b>\n"
                f"{line()}\n"
                f"👤 <b>{h(cr.get('name', query.from_user.first_name) if cr else query.from_user.first_name)}</b>\n"
                f"✅ Active — Free Forever\n"
                f"{line()}\n"
                f"🎯 <b>{len(camps)}</b> campaigns  🔓 <b>{total_u}</b> unlocks",
                reply_markup=kb_creator(),
            )
        else:
            bot_me = await context.bot.get_me()
            await safe_edit(
                query,
                f"🚀 <b>Become a Creator — ForceHub</b>\n"
                f"{line()}\n"
                f"Protect your content with force-subscribe campaigns.\n"
                f"Users must join your channel to unlock your content.\n\n"
                f"<b>Steps:</b>\n"
                f"1️⃣ Add {code('@' + bot_me.username)} as <b>Admin</b> in your channel\n"
                f"2️⃣ Grant <b>Post Messages</b> + <b>Invite Users</b> permissions\n"
                f"3️⃣ Tap <b>Connect My Channel</b> below\n\n"
                f"💡 <b>It's completely free!</b>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Connect My Channel",
                                          callback_data="onboard_start")],
                    [InlineKeyboardButton("🔙 Back", callback_data="u_back")],
                ]),
            )


# ─────────────────────────────────────────────────────────────────────────────
# CREATOR SELF-ONBOARDING CONVERSATION
# ─────────────────────────────────────────────────────────────────────────────
async def onboard_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id if update.effective_user else 0
    user = update.effective_user

    if can_create(uid):
        if is_admin(uid):
            db.ensure_admin_creator(
                uid,
                (user.username or "") if user else "",
                (user.first_name or "") if user else "",
            )
        text = "🎨 You already have creator access! Opening your panel…"
        if update.callback_query:
            await safe_edit(update.callback_query, text, reply_markup=kb_creator())
        elif update.message:
            await safe_reply(update.message, text, reply_markup=kb_creator())
        return ConversationHandler.END

    if user is None:
        return ConversationHandler.END

    bot_me = await context.bot.get_me()
    text   = (
        f"🚀 <b>Creator Onboarding</b>\n"
        f"{line()}\n\n"
        f"Protect your Telegram content with force-subscribe campaigns.\n"
        f"Users <b>must join</b> your channel before accessing your content.\n\n"
        f"<b>Before continuing:</b>\n"
        f"1️⃣ Add {code('@' + bot_me.username)} as <b>Admin</b> in your channel\n"
        f"2️⃣ Grant: <b>Post Messages</b> + <b>Invite Users</b>\n\n"
        f"{line()}\n"
        f"✏️ Now send your channel username:\n"
        f"Example: {code('@yourchannelname')}"
    )
    context.user_data.pop("admin_action", None)

    if update.callback_query:
        await safe_edit(update.callback_query, text, reply_markup=kb_cancel("onboard_cancel"))
    elif update.message:
        await safe_reply(update.message, text, reply_markup=kb_cancel("onboard_cancel"))

    return ONBOARD_CHANNEL


async def onboard_recv_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return ONBOARD_CHANNEL

    uid  = update.effective_user.id
    user = update.effective_user
    raw  = (update.message.text or "").strip()

    if not raw:
        await safe_reply(
            update.message,
            f"❌ Please send a channel username.\nExample: {code('@yourchannelname')}",
        )
        return ONBOARD_CHANNEL

    # Normalise — handles @ani_wallpaperr, ani_wallpaperr, -100123456789
    channel = raw if raw.startswith("@") or raw.startswith("-") else f"@{raw}"

    progress = await safe_reply(
        update.message,
        f"🔍 Verifying {code(channel)}…",
    )

    result = await verify_channel(context.bot, channel)

    if not result["ok"]:
        try:
            await progress.edit_text(
                f"<b>Verification Failed</b>\n\n{result['reason']}\n\n"
                f"Fix the issue, then send the channel username again:",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_cancel("onboard_cancel"),
            )
        except Exception:
            pass
        return ONBOARD_CHANNEL

    # ── Register creator ───────────────────────────────────────────────────────
    chan_uname = result["username"]
    chat_id    = result["chat_id"]
    chan_title = result["title"]

    cr = db.get_creator(uid)
    if cr is not None:
        if chan_uname not in cr.get("channels", []):
            cr.setdefault("channels", []).append(chan_uname)
        cr["channel_id"] = str(chat_id)
    else:
        db.register_creator(uid, user.username or "", user.first_name or "",
                            self_onboarded=True)
        cr = db.get_creator(uid)
        if cr is not None:
            cr["channels"]   = [chan_uname]
            cr["channel_id"] = str(chat_id)

    db.channels_map[str(chat_id)] = str(uid)
    db.save(force=True)
    logger.info("🎨 New creator self-onboarded: %s (%d)", user.first_name, uid)

    try:
        await progress.edit_text(
            f"✅ <b>Channel Connected Successfully!</b>\n"
            f"{line()}\n"
            f"📢 <b>{h(chan_title)}</b> ({code(chan_uname)})\n\n"
            f"🚀 <b>Creator Panel Activated!</b>\n"
            f"Use <b>➕ New Campaign</b> to create your first campaign!\n\n"
            f"<i>Note: Creating campaigns is completely free.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_creator(),
        )
    except Exception:
        pass

    return ConversationHandler.END


async def onboard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await safe_edit(
            update.callback_query,
            "❌ Onboarding cancelled.\n\nTap <b>🚀 Become Creator</b> anytime.",
            reply_markup=kb_user(),
        )
    elif update.message:
        await safe_reply(update.message, "❌ Cancelled.", reply_markup=kb_user())
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# CREATE CAMPAIGN CONVERSATION  (simple URL-based)
# ─────────────────────────────────────────────────────────────────────────────
async def createcamp_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id if update.effective_user else 0
    user = update.effective_user

    if not can_create(uid):
        msg = (
            "❌ <b>Creator access required</b>\n\n"
            "Tap <b>🚀 Become Creator</b> to get started — it's free!"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Become Creator", callback_data="u_become_creator")
        ]])
        if update.callback_query:
            await update.callback_query.answer("Creator access required!", show_alert=True)
        elif update.message:
            await safe_reply(update.message, msg, reply_markup=kb)
        return ConversationHandler.END

    if user and is_admin(uid):
        db.ensure_admin_creator(uid, user.username or "", user.first_name or "")

    context.user_data.pop("admin_action", None)
    context.user_data["camp_draft"] = {}

    text = (
        "➕ <b>Create Campaign — Step 1 / 2</b>\n"
        f"{line()}\n\n"
        "Send the <b>unlock content link</b> — this is what users\n"
        "receive after joining your channel(s).\n\n"
        "Supported: Drive, Telegram post, website, any URL\n\n"
        f"Example: {code('https://t.me/yourchannel/42')}"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query, text,
                        reply_markup=kb_cancel("createcamp_cancel"))
    elif update.message:
        await safe_reply(update.message, text,
                         reply_markup=kb_cancel("createcamp_cancel"))

    return CAMP_LINK


async def createcamp_recv_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return CAMP_LINK

    raw = (update.message.text or "").strip()
    if not raw.startswith("http"):
        await safe_reply(
            update.message,
            f"❌ Send a valid URL starting with {code('http')}",
        )
        return CAMP_LINK

    context.user_data["camp_draft"]["unlock_link"] = raw
    await safe_reply(
        update.message,
        "➕ <b>Create Campaign — Step 2 / 2</b>\n"
        f"{line()}\n\n"
        "Send the channel username(s) users <b>must join</b> to unlock.\n"
        "Separate multiple channels with spaces.\n\n"
        f"Example: {code('@channel1 @channel2')}\n\n"
        "⚠️ Bot must be <b>admin</b> in each channel!",
        reply_markup=kb_cancel("createcamp_cancel"),
    )
    return CAMP_CHANNELS


async def createcamp_recv_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return CAMP_CHANNELS

    uid  = update.effective_user.id
    raw  = (update.message.text or "").strip()
    parts = raw.split()
    channels = [
        p if p.startswith("@") or p.startswith("-") else f"@{p}"
        for p in parts if p
    ]

    if not channels:
        await safe_reply(
            update.message,
            f"❌ Send at least one channel username.\nExample: {code('@mychannel')}",
        )
        return CAMP_CHANNELS

    progress = await safe_reply(
        update.message,
        f"🔍 Verifying {len(channels)} channel(s)…",
    )

    valid, invalid = [], []
    for ch in channels:
        r = await verify_channel(context.bot, ch)
        (valid if r["ok"] else invalid).append(ch)

    if not valid:
        try:
            await progress.edit_text(
                "❌ Bot is not admin in <b>any</b> of those channels.\n\n"
                "Add the bot as admin first, then send the channel(s) again.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_cancel("createcamp_cancel"),
            )
        except Exception:
            pass
        return CAMP_CHANNELS

    link    = context.user_data.get("camp_draft", {}).get("unlock_link", "")
    camp_id = db.new_campaign(uid, valid, unlock_link=link)

    bot_me    = await context.bot.get_me()
    deep_link = f"https://t.me/{bot_me.username}?start=unlock_{camp_id}"
    warn_txt  = (
        f"\n⚠️ Skipped (bot not admin): {', '.join(h(c) for c in invalid)}"
        if invalid else ""
    )

    try:
        await progress.edit_text(
            f"✅ <b>Campaign Created Successfully!</b>\n"
            f"{line()}\n"
            f"🆔 Campaign ID: {code(camp_id)}\n"
            f"📢 Channels: {h(', '.join(valid))}{warn_txt}\n"
            f"🔗 Content: {h(link[:50])}{'…' if len(link)>50 else ''}\n"
            f"{line()}\n"
            f"📣 <b>Share this unlock link:</b>\n"
            f"{code(deep_link)}\n\n"
            f"Users tap → join channel → get content 🎉",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Link",    url=deep_link)],
                [InlineKeyboardButton("🎯 My Campaigns", callback_data="c_campaigns"),
                 InlineKeyboardButton("📊 Dashboard",   callback_data="c_dash")],
            ]),
        )
    except Exception:
        pass

    context.user_data.pop("camp_draft", None)
    return ConversationHandler.END


async def createcamp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("camp_draft", None)
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query, "❌ Campaign creation cancelled.",
                        reply_markup=kb_creator())
    elif update.message:
        await safe_reply(update.message, "❌ Cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# ADVANCED SETUP CONVERSATION  (file-based material campaigns)
# ─────────────────────────────────────────────────────────────────────────────
async def setup_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id if update.effective_user else 0
    user = update.effective_user

    if not can_create(uid):
        t = "❌ Creator access required. Tap 🚀 Become Creator first."
        if update.callback_query:
            await update.callback_query.answer(t, show_alert=True)
        elif update.message:
            await safe_reply(update.message, t)
        return ConversationHandler.END

    if user and is_admin(uid):
        db.ensure_admin_creator(uid, user.username or "", user.first_name or "")

    context.user_data.pop("admin_action", None)
    context.user_data["setup"] = {}

    text = (
        "🔧 <b>Advanced Campaign — Step 1 / 5</b>\n\n"
        "Send channel username(s) users must join.\n"
        f"Format: {code('@channel1 @channel2')}\n\n"
        "⚠️ Bot must be <b>admin</b> in those channels!"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query, text, reply_markup=kb_cancel("setup_cancel"))
    elif update.message:
        await safe_reply(update.message, text, reply_markup=kb_cancel("setup_cancel"))

    return SETUP_CHANNEL


async def setup_recv_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return SETUP_CHANNEL

    uid  = update.effective_user.id
    raw  = (update.message.text or "").strip()
    tokens = [
        t.strip() for t in raw.split()
        if t.startswith("@") or t.lstrip("-").isdigit()
    ]

    if not tokens:
        await safe_reply(
            update.message,
            f"❌ Invalid. Send: {code('@channel1 @channel2')}",
        )
        return SETUP_CHANNEL

    me = await context.bot.get_me()
    valid, invalid = [], []
    for ch in tokens:
        try:
            m = await context.bot.get_chat_member(ch, me.id)
            (valid if m.status in ("administrator", "creator") else invalid).append(ch)
        except Exception:
            invalid.append(ch)

    if not valid:
        await safe_reply(
            update.message,
            "❌ Bot is not admin in any channel you sent.\nAdd bot as admin first.",
        )
        return SETUP_CHANNEL

    if invalid:
        await safe_reply(
            update.message,
            f"⚠️ Skipped (not admin): {h(', '.join(invalid))}\n"
            f"✅ Using: {h(', '.join(valid))}",
        )

    context.user_data["setup"]["channels"] = valid
    cr = db.get_creator(uid)
    if cr is None and is_admin(uid):
        cr = db.ensure_admin_creator(uid)
    if cr:
        for ch in valid:
            if ch not in cr.get("channels", []):
                cr.setdefault("channels", []).append(ch)
        db.save()

    await safe_reply(
        update.message,
        f"✅ Channels: {h(', '.join(valid))}\n\n<b>Step 2 / 5:</b> Choose material type:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Text",     callback_data="mtype_text"),
             InlineKeyboardButton("🖼 Photo",    callback_data="mtype_photo")],
            [InlineKeyboardButton("🎥 Video",    callback_data="mtype_video"),
             InlineKeyboardButton("📄 Document", callback_data="mtype_document")],
            [InlineKeyboardButton("❌ Cancel",   callback_data="setup_cancel")],
        ]),
    )
    return SETUP_MAT_TYPE


async def setup_recv_mtype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return SETUP_MAT_TYPE
    await query.answer()
    mtype = query.data.split("_", 1)[1]
    context.user_data["setup"]["file_type"] = mtype
    await safe_edit(
        query,
        "<b>Step 3 / 5:</b> Send a title for this material.\n"
        f"Example: {code('CUET Notes 2026')}",
    )
    return SETUP_MAT_TITLE


async def setup_recv_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return SETUP_MAT_TITLE
    title = (update.message.text or "").strip()
    if len(title) < 3:
        await safe_reply(update.message, "❌ Title too short (min 3 chars).")
        return SETUP_MAT_TITLE
    context.user_data["setup"]["title"] = title
    ftype  = context.user_data["setup"]["file_type"]
    labels = {"text": "text message", "photo": "photo",
               "video": "video", "document": "document/PDF"}
    await safe_reply(
        update.message,
        f"<b>Step 4 / 5:</b> Send your {h(labels.get(ftype, ftype))}:",
    )
    return SETUP_MAT_CONTENT


async def setup_recv_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return SETUP_MAT_CONTENT
    s     = context.user_data["setup"]
    ftype = s["file_type"]
    msg   = update.message
    ok    = True

    if ftype == "text":
        if not msg.text:
            ok = False
        else:
            s["file_id"]     = None
            s["description"] = msg.text
    elif ftype == "photo":
        if not msg.photo:
            ok = False
        else:
            s["file_id"]     = msg.photo[-1].file_id
            s["description"] = msg.caption or ""
    elif ftype == "video":
        if not msg.video:
            ok = False
        else:
            s["file_id"]     = msg.video.file_id
            s["description"] = msg.caption or ""
    elif ftype == "document":
        if not msg.document:
            ok = False
        else:
            s["file_id"]     = msg.document.file_id
            s["description"] = msg.caption or ""

    if not ok:
        await safe_reply(msg, f"❌ Please send a {h(ftype)}.")
        return SETUP_MAT_CONTENT

    await safe_reply(
        msg,
        "<b>Step 5 / 5:</b> How many referrals required?\n"
        f"Send {code('0')} for no referral requirement.",
    )
    return SETUP_REF_COUNT


async def setup_recv_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return SETUP_REF_COUNT
    try:
        count = int((update.message.text or "").strip())
        if count < 0:
            raise ValueError
    except ValueError:
        await safe_reply(update.message, "❌ Send 0 or a positive number.")
        return SETUP_REF_COUNT

    s   = context.user_data["setup"]
    uid = update.effective_user.id

    mid = str(uuid.uuid4())[:10]
    db.materials[mid] = {
        "creator_id":  str(uid),
        "title":       s["title"],
        "description": s.get("description", ""),
        "file_id":     s.get("file_id"),
        "file_type":   s["file_type"],
        "created_at":  datetime.now().isoformat(),
    }
    cr = db.get_creator(uid)
    if cr is None and is_admin(uid):
        cr = db.ensure_admin_creator(uid)
    if cr:
        cr.setdefault("materials", []).append(mid)

    camp_id = db.new_campaign(uid, s["channels"], count, material_id=mid)
    bot_me  = await context.bot.get_me()
    link    = f"https://t.me/{bot_me.username}?start=unlock_{camp_id}"

    await safe_reply(
        update.message,
        f"🎉 <b>Campaign Created!</b>\n"
        f"{line()}\n"
        f"📦 Material: <b>{h(s['title'])}</b>\n"
        f"🆔 Campaign: {code(camp_id)}\n"
        f"📢 Channels: {h(', '.join(s['channels']))}\n"
        f"👥 Referrals: {code(count)}\n"
        f"{line()}\n"
        f"🔗 <b>Share Link:</b>\n{code(link)}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 My Campaigns", callback_data="c_campaigns"),
             InlineKeyboardButton("📊 Dashboard",   callback_data="c_dash")],
        ]),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("setup", None)
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(update.callback_query, "❌ Setup cancelled.", reply_markup=kb_creator())
    elif update.message:
        await safe_reply(update.message, "❌ Cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# CREATOR CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────
async def cb_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data
    uid  = query.from_user.id if query.from_user else 0

    def _guard() -> Optional[str]:
        if not can_create(uid):
            return "❌ Creator access required!"
        return None

    def _ensure_cr():
        if is_admin(uid):
            db.ensure_admin_creator(
                uid,
                query.from_user.username or "" if query.from_user else "",
                query.from_user.first_name or "" if query.from_user else "",
            )
        return db.get_creator(uid)

    # ── c_dash ────────────────────────────────────────────────────────────────
    if data == "c_dash":
        err = _guard()
        if err:
            await query.answer(err, show_alert=True); return
        cr    = _ensure_cr()
        camps = cr.get("campaigns", []) if cr else []
        total_u = sum(db.analytics.get("unlock_success",  {}).get(c, 0) for c in camps)
        total_c = sum(db.analytics.get("campaign_clicks", {}).get(c, 0) for c in camps)
        await safe_edit(
            query,
            f"📊 <b>Creator Dashboard</b>\n"
            f"{line()}\n"
            f"👤 <b>{h(cr.get('name', '?') if cr else '?')}</b>  {code(uid)}\n"
            f"✅ Active — Free Forever\n"
            f"{line()}\n"
            f"🎯 Campaigns: <b>{len(camps)}</b>\n"
            f"📦 Materials: <b>{len(cr.get('materials', []) if cr else [])}</b>\n"
            f"📢 Channels:  <b>{len(cr.get('channels', []) if cr else [])}</b>\n"
            f"👆 Clicks:    <b>{total_c}</b>  |  🔓 Unlocks: <b>{total_u}</b>",
            reply_markup=kb_creator(),
        )

    # ── c_campaigns ───────────────────────────────────────────────────────────
    elif data == "c_campaigns":
        err = _guard()
        if err:
            await query.answer(err, show_alert=True); return
        cr    = _ensure_cr()
        camps = cr.get("campaigns", []) if cr else []
        if not camps:
            await safe_edit(
                query,
                "📭 <b>No Campaigns Yet</b>\n\nCreate your first!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ New Campaign", callback_data="c_new")],
                    [InlineKeyboardButton("🔙 Back",        callback_data="c_dash")],
                ]),
            )
            return

        bot_me = await context.bot.get_me()
        text   = f"🎯 <b>Your Campaigns</b> ({len(camps)} total)\n\n"
        btns   = []
        for cid in camps[-12:]:
            c   = db.campaigns.get(cid, {})
            lnk = c.get("unlock_link", "")
            mat = db.materials.get(c.get("material_id", ""), {})
            title   = mat.get("title", "") or (lnk[:22] + "…" if lnk else "?")
            st      = "✅" if c.get("is_active") else "❌"
            clicks  = db.analytics.get("campaign_clicks",  {}).get(cid, 0)
            unlocks = db.analytics.get("unlock_success",   {}).get(cid, 0)
            text   += (
                f"{st} {code(cid)} <b>{h(title[:20])}</b>\n"
                f"   👆{clicks} | 🔓{unlocks}\n\n"
            )
            btns.append([
                InlineKeyboardButton(f"{st} Toggle", callback_data=f"c_toggle_{cid}"),
                InlineKeyboardButton("🔗 Link",      callback_data=f"c_link_{cid}"),
            ])
        btns.append([
            InlineKeyboardButton("➕ New Campaign", callback_data="c_new"),
            InlineKeyboardButton("🔙 Back",         callback_data="c_dash"),
        ])
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(btns))

    # ── c_toggle_<cid> ────────────────────────────────────────────────────────
    elif data.startswith("c_toggle_"):
        cid  = data[9:]
        camp = db.campaigns.get(cid)
        if not camp or (camp.get("creator_id") != str(uid) and not is_admin(uid)):
            await query.answer("❌ Not your campaign!", show_alert=True); return
        camp["is_active"] = not camp.get("is_active", True)
        db.save(force=True)
        st = "✅ Activated" if camp["is_active"] else "❌ Deactivated"
        await query.answer(f"{st}: {cid}", show_alert=True)
        # Refresh the campaigns view
        query.data = "c_campaigns"
        await cb_creator(update, context)

    # ── c_link_<cid> ──────────────────────────────────────────────────────────
    elif data.startswith("c_link_"):
        cid  = data[7:]
        camp = db.campaigns.get(cid)
        if not camp or (camp.get("creator_id") != str(uid) and not is_admin(uid)):
            await query.answer("❌ Not your campaign!", show_alert=True); return
        bot_me = await context.bot.get_me()
        link   = f"https://t.me/{bot_me.username}?start=unlock_{cid}"
        lnk    = camp.get("unlock_link", "")
        mat    = db.materials.get(camp.get("material_id", ""), {})
        title  = mat.get("title", "") or (lnk[:30] + "…" if lnk else cid)
        clk    = db.analytics.get("campaign_clicks",      {}).get(cid, 0)
        ver    = db.analytics.get("verification_success", {}).get(cid, 0)
        ulk    = db.analytics.get("unlock_success",       {}).get(cid, 0)
        await safe_edit(
            query,
            f"🔗 <b>Campaign Details</b>\n"
            f"{line()}\n"
            f"🆔 {code(cid)}  {'✅ Active' if camp.get('is_active') else '❌ Inactive'}\n"
            f"📦 Content: <b>{h(title)}</b>\n"
            f"📢 Channels: {h(', '.join(camp.get('channels', [])))}\n"
            f"👥 Referrals needed: {camp.get('referral_required', 0)}\n"
            f"{line()}\n"
            f"👆 Clicks: <b>{clk}</b>  ✅ Verified: <b>{ver}</b>  🔓 Unlocks: <b>{ulk}</b>\n"
            f"{line()}\n"
            f"📣 <b>Share Link:</b>\n{code(link)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Link", url=link)],
                [InlineKeyboardButton("🔙 Campaigns", callback_data="c_campaigns"),
                 InlineKeyboardButton("🏠 Dashboard", callback_data="c_dash")],
            ]),
        )

    # ── c_links ───────────────────────────────────────────────────────────────
    elif data == "c_links":
        err = _guard()
        if err:
            await query.answer(err, show_alert=True); return
        cr    = _ensure_cr()
        camps = cr.get("campaigns", []) if cr else []
        if not camps:
            await safe_edit(query, "📭 No campaigns yet. Create one first!",
                            reply_markup=kb_back("c_dash"))
            return
        bot_me = await context.bot.get_me()
        text   = "🔗 <b>Your Campaign Share Links</b>\n\n"
        for cid in camps[-10:]:
            c   = db.campaigns.get(cid, {})
            lnk = c.get("unlock_link", "")
            mat = db.materials.get(c.get("material_id", ""), {})
            title = mat.get("title", "") or (lnk[:22] + "…" if lnk else "?")
            st    = "✅" if c.get("is_active") else "❌"
            dl    = f"https://t.me/{bot_me.username}?start=unlock_{cid}"
            text += f"{st} <b>{h(title[:22])}</b>\n{code(dl)}\n\n"
        await safe_edit(query, text, reply_markup=kb_back("c_dash"))

    # ── c_stats ───────────────────────────────────────────────────────────────
    elif data == "c_stats":
        err = _guard()
        if err:
            await query.answer(err, show_alert=True); return
        cr    = _ensure_cr()
        camps = cr.get("campaigns", []) if cr else []
        text  = "📈 <b>Campaign Analytics</b>\n\n"
        for cid in camps[-10:]:
            c   = db.campaigns.get(cid, {})
            lnk = c.get("unlock_link", "")
            mat = db.materials.get(c.get("material_id", ""), {})
            title   = mat.get("title", "") or (lnk[:20] + "…" if lnk else cid)
            clk     = db.analytics.get("campaign_clicks",      {}).get(cid, 0)
            ver     = db.analytics.get("verification_success", {}).get(cid, 0)
            ulk     = db.analytics.get("unlock_success",       {}).get(cid, 0)
            st      = "✅" if c.get("is_active") else "❌"
            text   += (
                f"{st} <b>{h(title[:20])}</b> {code(cid)}\n"
                f"   👆{clk}  ✅{ver}  🔓{ulk}\n\n"
            )
        if not camps:
            text += "No campaigns yet."
        await safe_edit(query, text, reply_markup=kb_back("c_dash"))

    # ── c_channels ────────────────────────────────────────────────────────────
    elif data == "c_channels":
        err = _guard()
        if err:
            await query.answer(err, show_alert=True); return
        cr   = _ensure_cr()
        chns = cr.get("channels", []) if cr else []
        text = "📢 <b>Your Channels</b>\n\n"
        text += (
            "\n".join(f"• {code(ch)}" for ch in chns)
            if chns
            else "No channels connected.\nUse /becomecreator to connect one."
        )
        await safe_edit(query, text, reply_markup=kb_back("c_dash"))

    # ── c_materials ───────────────────────────────────────────────────────────
    elif data == "c_materials":
        err = _guard()
        if err:
            await query.answer(err, show_alert=True); return
        cr      = _ensure_cr()
        mat_ids = cr.get("materials", []) if cr else []
        text    = "📦 <b>Your Materials</b>\n\n"
        for mid in mat_ids[-10:]:
            m = db.materials.get(mid, {})
            text += f"• {code(mid)} — <b>{h(m.get('title', '?'))}</b> ({h(m.get('file_type', '?'))})\n"
        if not mat_ids:
            text += "No materials. Use <b>🔧 Advanced Setup</b> to create one."
        await safe_edit(query, text, reply_markup=kb_back("c_dash"))

    # ── c_help ────────────────────────────────────────────────────────────────
    elif data == "c_help":
        await safe_edit(
            query,
            "❓ <b>Creator Help Guide</b>\n"
            f"{line()}\n"
            f"<b>➕ New Campaign</b> — Create a URL-based unlock campaign (2-step wizard)\n\n"
            f"<b>🔧 Advanced Setup</b> — Deliver files/photos/videos as content\n\n"
            f"<b>🎯 My Campaigns</b> — Toggle on/off, get share links, view stats\n\n"
            f"<b>📈 Analytics</b> — Clicks, verifications, unlocks per campaign\n\n"
            f"<b>📢 My Channels</b> — Channels linked to your campaigns\n\n"
            f"<b>🔗 Share Links</b> — All campaign links in one place\n\n"
            f"{line()}\n"
            f"<b>Commands:</b>\n"
            f"{code('/creator')} — creator panel\n"
            f"{code('/createcampaign')} — new URL campaign\n"
            f"{code('/setup')} — advanced file campaign\n"
            f"{code('/dashboard')} — stats dashboard\n"
            f"{code('/mycampaigns')} — list all campaigns\n"
            f"{code('/becomecreator')} — connect a channel",
            reply_markup=kb_back("c_dash"),
        )

    # ── c_adv_setup ───────────────────────────────────────────────────────────
    elif data == "c_adv_setup":
        # Routed to ConversationHandler — this is just a fallback
        await query.answer("Opening advanced setup…")

    # ── c_new ─────────────────────────────────────────────────────────────────
    elif data == "c_new":
        # Routed to ConversationHandler via entry_point
        await query.answer("Opening campaign wizard…")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────
async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data
    uid  = query.from_user.id if query.from_user else 0

    if not is_admin(uid):
        await query.answer("❌ Admin access required!", show_alert=True)
        return

    # ── a_panel ───────────────────────────────────────────────────────────────
    if data == "a_panel":
        s     = db.global_stats()
        badge = "👑 SUPER ADMIN" if is_super_admin(uid) else "🛡️ Admin"
        await safe_edit(
            query,
            f"{badge} — <b>ForceHub Control Center</b>\n"
            f"{line()}\n"
            f"👥 <b>{s['total_users']}</b> users  🎨 <b>{s['total_creators']}</b> creators\n"
            f"🎯 <b>{s['total_campaigns']}</b> campaigns  📦 <b>{s['total_materials']}</b> materials\n"
            f"🆕 Today: <b>{s['today_joins']}</b> joins | <b>{s['today_unlocks']}</b> unlocks\n"
            f"{line()}\n"
            f"💰 ₹{h(db.settings.get('price', 199))}  "
            f"💳 {code(db.settings.get('upi_id', 'Not set'))}\n"
            f"🕐 {h(now_str())}",
            reply_markup=kb_admin(),
        )

    # ── a_stats ───────────────────────────────────────────────────────────────
    elif data == "a_stats":
        s  = db.global_stats()
        tc = sum(db.analytics.get("campaign_clicks",      {}).values())
        tv = sum(db.analytics.get("verification_success", {}).values())
        tu = sum(db.analytics.get("unlock_success",       {}).values())
        tr = sum(db.analytics.get("referral_unlocks",     {}).values())
        daily = db.analytics.get("daily", {})
        d_txt = ""
        for day in sorted(daily)[-7:]:
            dd    = daily[day]
            d_txt += (f"  {code(day)}: "
                      f"joins <b>{dd.get('joins',0)}</b> | "
                      f"unlocks <b>{dd.get('unlocks',0)}</b>\n")
        await safe_edit(
            query,
            f"📊 <b>Full Analytics — ForceHub</b>\n"
            f"{line()}\n"
            f"👥 Users: <b>{s['total_users']}</b>  "
            f"🎨 Creators: <b>{s['total_creators']}</b>\n"
            f"🎯 Campaigns: <b>{s['total_campaigns']}</b>  "
            f"📦 Materials: <b>{s['total_materials']}</b>\n"
            f"{line()}\n"
            f"👆 All-time Clicks:   <b>{tc}</b>\n"
            f"✅ Verified:          <b>{tv}</b>\n"
            f"🔓 Unlocks:           <b>{tu}</b>\n"
            f"👥 Referrals:         <b>{tr}</b>\n"
            f"{line()}\n"
            f"📅 <b>Last 7 Days:</b>\n{d_txt}"
            f"🕐 {h(now_str())}",
            reply_markup=kb_admin_back(),
        )

    # ── Paginated user list ───────────────────────────────────────────────────
    elif data.startswith("a_users_"):
        PAGE  = 10
        page  = int(data.split("_")[2])
        uids  = list(db.users.keys())
        total = len(uids)
        chunk = uids[page * PAGE:(page + 1) * PAGE]
        text  = f"👥 <b>All Users</b> — Page {page+1}/{max(1,(total-1)//PAGE+1)} (Total {total})\n\n"
        for k in chunk:
            u    = db.users[k]
            role = ("🎨" if is_creator(int(k)) else
                    "🛡️" if is_admin(int(k)) else "👤")
            name = f"@{u.get('username','')}" if u.get("username") else h(u.get("first_name","?"))
            text += f"{role} {code(k)} {name} | 🔓{len(u.get('unlocked_campaigns',[]))}\n"
        nav  = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"a_users_{page-1}"))
        if (page + 1) * PAGE < total:
            nav.append(InlineKeyboardButton("▶", callback_data=f"a_users_{page+1}"))
        btns = ([nav] if nav else []) + [
            [InlineKeyboardButton("🔍 View User",    callback_data="a_prompt_viewuser")],
            [InlineKeyboardButton("🔙 Back",         callback_data="a_panel")],
        ]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(btns))

    # ── Paginated creator list ────────────────────────────────────────────────
    elif data.startswith("a_creators_"):
        PAGE  = 8
        page  = int(data.split("_")[2])
        cids  = list(db.creators.keys())
        total = len(cids)
        chunk = cids[page * PAGE:(page + 1) * PAGE]
        text  = f"🎨 <b>All Creators</b> — Page {page+1}/{max(1,(total-1)//PAGE+1)} (Total {total})\n\n"
        for k in chunk:
            cr   = db.creators[k]
            name = h(cr.get("name", "?"))
            camps = len(cr.get("campaigns", []))
            text += f"✅ {code(k)} <b>{name}</b> 🎯{camps}\n"
        nav  = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"a_creators_{page-1}"))
        if (page + 1) * PAGE < total:
            nav.append(InlineKeyboardButton("▶", callback_data=f"a_creators_{page+1}"))
        btns = ([nav] if nav else []) + [
            [InlineKeyboardButton("🔍 View Creator", callback_data="a_prompt_viewcreator"),
             InlineKeyboardButton("➕ Add Creator",  callback_data="a_addcreator")],
            [InlineKeyboardButton("🔙 Back",         callback_data="a_panel")],
        ]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(btns))

    # ── Paginated campaign list ───────────────────────────────────────────────
    elif data.startswith("a_campaigns_"):
        PAGE  = 8
        page  = int(data.split("_")[2])
        camp_keys = list(db.campaigns.keys())
        total = len(camp_keys)
        chunk = camp_keys[page * PAGE:(page + 1) * PAGE]
        text  = f"🎯 <b>All Campaigns</b> — Page {page+1}/{max(1,(total-1)//PAGE+1)} (Total {total})\n\n"
        for cid in chunk:
            c   = db.campaigns[cid]
            lnk = c.get("unlock_link", "")
            mat = db.materials.get(c.get("material_id", ""), {})
            title   = mat.get("title", "") or (lnk[:18] + "…" if lnk else "?")
            st      = "✅" if c.get("is_active") else "❌"
            clk     = db.analytics.get("campaign_clicks",  {}).get(cid, 0)
            ulk     = db.analytics.get("unlock_success",   {}).get(cid, 0)
            text   += (
                f"{st} {code(cid)} <b>{h(title[:18])}</b>\n"
                f"   👆{clk} | 🔓{ulk} | creator:{code(c.get('creator_id','?'))}\n\n"
            )
        nav  = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"a_campaigns_{page-1}"))
        if (page + 1) * PAGE < total:
            nav.append(InlineKeyboardButton("▶", callback_data=f"a_campaigns_{page+1}"))
        btns = ([nav] if nav else []) + [
            [InlineKeyboardButton("🗑 Delete Campaign", callback_data="a_delcamp")],
            [InlineKeyboardButton("🔙 Back",            callback_data="a_panel")],
        ]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(btns))

    # ── Paginated materials list ──────────────────────────────────────────────
    elif data.startswith("a_materials_"):
        PAGE  = 10
        page  = int(data.split("_")[2])
        mids  = list(db.materials.keys())
        total = len(mids)
        chunk = mids[page * PAGE:(page + 1) * PAGE]
        text  = f"📦 <b>All Materials</b> — Page {page+1}/{max(1,(total-1)//PAGE+1)} (Total {total})\n\n"
        for mid in chunk:
            m = db.materials[mid]
            text += (
                f"• {code(mid)} <b>{h(m.get('title','?')[:20])}</b> "
                f"({h(m.get('file_type','?'))}) by {code(m.get('creator_id','?'))}\n"
            )
        nav  = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"a_materials_{page-1}"))
        if (page + 1) * PAGE < total:
            nav.append(InlineKeyboardButton("▶", callback_data=f"a_materials_{page+1}"))
        btns = ([nav] if nav else []) + [[InlineKeyboardButton("🔙 Back", callback_data="a_panel")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(btns))

    # ── Inline prompt starters ────────────────────────────────────────────────
    elif data in (
        "a_addcreator", "a_ban", "a_dm", "a_delcamp",
        "a_prompt_viewuser", "a_prompt_viewcreator",
    ):
        prompts = {
            "a_addcreator":        ("addcreator",  f"➕ Send: {code('<user_id> [Name]')}\nExample: {code('123456789 John')}"),
            "a_ban":               ("bancreator",   "🚫 Send the creator ID to remove:"),
            "a_dm":                ("dm_id",        "💬 Send the target user ID:"),
            "a_delcamp":           ("delcampaign",  "🗑 Send the campaign ID to deactivate:"),
            "a_prompt_viewuser":   ("viewuser",     "🔍 Send the user ID to view:"),
            "a_prompt_viewcreator":("viewcreator",  "🔍 Send the creator ID to view:"),
        }
        action, prompt = prompts[data]
        context.user_data["admin_action"] = action
        await safe_edit(
            query,
            f"<b>{prompt}</b>",
            reply_markup=kb_cancel("a_panel"),
        )

    # ── Confirm actions from inline buttons ───────────────────────────────────
    elif data.startswith("a_confirm_del_"):
        cid  = data[len("a_confirm_del_"):]
        camp = db.campaigns.get(cid)
        if camp:
            camp["is_active"] = False
            db.save(force=True)
            await query.answer(f"🗑 Campaign {cid} deactivated!", show_alert=True)
            await safe_edit(
                query,
                f"🗑 Campaign {code(cid)} has been <b>deactivated</b>.",
                reply_markup=kb_admin_back(),
            )

    elif data.startswith("a_confirm_ban_"):
        crid = int(data[len("a_confirm_ban_"):])
        db.remove_creator(crid)
        await query.answer(f"🚫 Creator {crid} removed!", show_alert=True)
        await safe_edit(
            query,
            f"🚫 Creator {code(crid)} has been <b>removed</b>.",
            reply_markup=kb_admin_back(),
        )

    elif data.startswith("a_renewcr_"):
        crid = int(data[len("a_renewcr_"):])
        cr   = db.get_creator(crid)
        if not cr:
            await query.answer("Creator not found!", show_alert=True); return
        # Ensure creator record is valid (no expiry concept in free mode)
        await query.answer(f"✅ Creator {crid} access confirmed!", show_alert=True)
        await safe_edit(
            query,
            f"✅ Creator {code(crid)} — <b>{h(cr.get('name','?'))}</b> — access verified.",
            reply_markup=kb_admin_back(),
        )

    # ── Broadcast ─────────────────────────────────────────────────────────────
    elif data == "a_broadcast":
        await safe_edit(
            query,
            "📣 <b>Admin Broadcast</b>\n\nSelect target audience:",
            reply_markup=kb_broadcast_target(),
        )

    elif data in ("bcast_users", "bcast_creators", "bcast_everyone"):
        context.user_data["bcast_target"] = data.split("_")[1]
        context.user_data["bcast_step"]   = "content"
        await safe_edit(
            query,
            f"📝 Target: <b>{h(context.user_data['bcast_target'].title())}</b>\n\n"
            f"Send your content (text / photo / video / document).\n"
            f"Captions supported for media.\n\n👇 Send now:",
            reply_markup=kb_cancel("a_panel"),
        )

    elif data == "bcast_skip_buttons":
        await _do_broadcast(update, context, reply_markup=None)

    # ── Settings ──────────────────────────────────────────────────────────────
    elif data == "a_settings":
        await safe_edit(
            query,
            f"⚙️ <b>Bot Settings</b>\n"
            f"{line()}\n"
            f"💰 Price:   {code('₹' + str(db.settings.get('price', 199)))}\n"
            f"💳 UPI ID:  {code(db.settings.get('upi_id', 'Not set'))}\n\n"
            f"Tap a button to change:",
            reply_markup=kb_admin_settings(),
        )

    elif data in ("a_set_price", "a_set_upi", "a_set_admin"):
        prompts_ = {
            "a_set_price": ("setprice", f"💰 Current: ₹{db.settings.get('price',199)}\nSend new price (₹):"),
            "a_set_upi":   ("setupi",   f"💳 Current: {code(db.settings.get('upi_id','Not set'))}\nSend new UPI ID:"),
            "a_set_admin": ("addadmin", "👑 Send user ID to grant admin access:"),
        }
        action_, prompt_ = prompts_[data]
        if data == "a_set_admin" and not is_super_admin(uid):
            await query.answer("👑 Super Admin only!", show_alert=True); return
        context.user_data["admin_action"] = action_
        await safe_edit(
            query,
            f"<b>{prompt_}</b>",
            reply_markup=kb_cancel("a_settings"),
        )

    elif data == "a_export":
        await safe_edit(
            query,
            f"📤 Use {code('/export')} command to download full JSON.",
            reply_markup=kb_admin_back(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# BROADCAST HELPER
# ─────────────────────────────────────────────────────────────────────────────
async def _do_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reply_markup: Optional[InlineKeyboardMarkup],
):
    target  = context.user_data.get("bcast_target", "users")
    ctype   = context.user_data.get("bcast_ctype", "text")
    content = context.user_data.get("bcast_content")
    caption = context.user_data.get("bcast_caption", "")

    if target == "users":
        ids = [int(k) for k in db.users]
    elif target == "creators":
        ids = [int(k) for k in db.creators]
    else:
        ids = list({int(k) for k in db.users} | {int(k) for k in db.creators})

    count = len(ids)
    prog_text = f"📣 Broadcasting to <b>{count}</b> recipients…"

    prog = None
    try:
        if update.callback_query and update.callback_query.message:
            prog = await update.callback_query.message.reply_text(
                prog_text, parse_mode=ParseMode.HTML)
        elif update.message:
            prog = await update.message.reply_text(
                prog_text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

    stats = await batch_broadcast(context.application, ids, ctype, content,
                                   caption, reply_markup)
    result = (
        f"✅ <b>Broadcast Complete!</b>\n\n"
        f"Target: <b>{h(target.title())}</b>  Total: <b>{count}</b>\n"
        f"✅ Sent: <b>{stats['sent']}</b>  ❌ Failed: <b>{stats['failed']}</b>\n"
        f"🕐 {h(now_str())}"
    )
    if prog:
        try:
            await prog.edit_text(result, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    for k in ("bcast_step", "bcast_target", "bcast_ctype", "bcast_content", "bcast_caption"):
        context.user_data.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
# GENERAL MESSAGE HANDLER  (admin prompts + broadcasts)
# ─────────────────────────────────────────────────────────────────────────────
async def general_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return

    uid = update.effective_user.id
    msg = update.message

    # ── Admin inline prompt handler ───────────────────────────────────────────
    # Only fires outside ConversationHandlers (checked via absence of draft keys)
    if (is_admin(uid)
            and context.user_data.get("admin_action")
            and not context.user_data.get("setup")
            and not context.user_data.get("camp_draft")):

        action = context.user_data.pop("admin_action")
        text   = (msg.text or "").strip()

        async def _parse_id() -> Optional[int]:
            try:
                return int(text)
            except ValueError:
                await safe_reply(msg, "❌ Invalid ID — must be a number.")
                return None

        if action == "viewuser":
            tid = await _parse_id()
            if tid is None: return
            u = db.get_user(tid)
            if not u:
                await safe_reply(msg, f"❌ User {code(tid)} not found."); return
            role  = "🎨 Creator" if is_creator(tid) else ("🛡️ Admin" if is_admin(tid) else "👤 User")
            uname = f"@{u['username']}" if u.get("username") else "—"
            await safe_reply(
                msg,
                f"👤 <b>User Profile</b>\n"
                f"{line()}\n"
                f"🆔 {code(tid)}  📛 <b>{h(u.get('first_name','?'))}</b>  {h(uname)}\n"
                f"🏷 Role: {role}\n"
                f"📅 Joined: {code(u.get('joined_at','?'))}\n"
                f"{line()}\n"
                f"🔓 Unlocked: <b>{len(u.get('unlocked_campaigns',[]))}</b> campaigns\n"
                f"👥 Referrals: <b>{u.get('referral_count',0)}</b>\n"
                f"👈 Referred by: {code(u.get('referred_by','None'))}",
                reply_markup=kb_admin_back(),
            )

        elif action == "viewcreator":
            tid = await _parse_id()
            if tid is None: return
            cr = db.get_creator(tid)
            if not cr:
                await safe_reply(msg, f"❌ Creator {code(tid)} not found."); return
            camps = cr.get("campaigns", [])
            tu    = sum(db.analytics.get("unlock_success",  {}).get(c, 0) for c in camps)
            tc    = sum(db.analytics.get("campaign_clicks", {}).get(c, 0) for c in camps)
            await safe_reply(
                msg,
                f"🎨 <b>Creator Profile</b>\n"
                f"{line()}\n"
                f"🆔 {code(tid)}  📛 <b>{h(cr.get('name','?'))}</b>\n"
                f"✅ Active — Free Forever\n"
                f"{line()}\n"
                f"🎯 Campaigns: <b>{len(camps)}</b>  👆 Clicks: <b>{tc}</b>  🔓 Unlocks: <b>{tu}</b>\n"
                f"📢 Channels: {h(', '.join(cr.get('channels',[])) or 'None')}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚫 Remove Creator",
                                          callback_data=f"a_confirm_ban_{tid}")],
                    [InlineKeyboardButton("🔙 Back", callback_data="a_panel")],
                ]),
            )

        elif action == "addcreator":
            parts = text.split(None, 1)
            if not parts:
                await safe_reply(msg, f"❌ Format: {code('<id> [Name]')}"); return
            try:
                crid = int(parts[0])
                name = parts[1].strip() if len(parts) > 1 else f"Creator_{crid}"
                if db.get_creator(crid):
                    await safe_reply(msg, f"ℹ️ Creator {code(crid)} already exists.",
                                     reply_markup=kb_admin_back())
                else:
                    db.register_creator(crid, "", name)
                    await safe_reply(
                        msg,
                        f"✅ Creator <b>{h(name)}</b> ({code(crid)}) added successfully!",
                        reply_markup=kb_admin_back(),
                    )
            except ValueError:
                await safe_reply(msg, "❌ Invalid user ID.")

        elif action == "bancreator":
            tid = await _parse_id()
            if tid is None: return
            cr = db.get_creator(tid)
            if not cr:
                await safe_reply(msg, f"❌ Creator {code(tid)} not found."); return
            await safe_reply(
                msg,
                f"🚫 Confirm removing creator {code(tid)} — <b>{h(cr.get('name','?'))}</b>?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, Remove", callback_data=f"a_confirm_ban_{tid}"),
                     InlineKeyboardButton("❌ Cancel",      callback_data="a_panel")],
                ]),
            )

        elif action == "dm_id":
            tid = await _parse_id()
            if tid is None: return
            context.user_data["admin_action"] = "dm_msg"
            context.user_data["dm_target"]    = tid
            u    = db.get_user(tid)
            name = f"@{u['username']}" if (u and u.get("username")) else str(tid)
            await safe_reply(
                msg,
                f"💬 <b>DM to {h(name)}</b>\n\nSend your message now:",
                reply_markup=kb_cancel("a_panel"),
            )

        elif action == "dm_msg":
            tid = context.user_data.pop("dm_target", None)
            if not tid:
                await safe_reply(msg, "❌ No target set. Start over."); return
            try:
                if msg.text:
                    await context.bot.send_message(
                        tid,
                        f"📩 <b>Message from Admin:</b>\n\n{h(msg.text)}",
                        parse_mode=ParseMode.HTML,
                    )
                elif msg.photo:
                    await context.bot.send_photo(
                        tid, msg.photo[-1].file_id,
                        caption=f"📩 <b>From Admin:</b> {h(msg.caption or '')}",
                        parse_mode=ParseMode.HTML,
                    )
                elif msg.video:
                    await context.bot.send_video(
                        tid, msg.video.file_id,
                        caption=f"📩 <b>From Admin:</b> {h(msg.caption or '')}",
                        parse_mode=ParseMode.HTML,
                    )
                elif msg.document:
                    await context.bot.send_document(
                        tid, msg.document.file_id,
                        caption=f"📩 <b>From Admin:</b> {h(msg.caption or '')}",
                        parse_mode=ParseMode.HTML,
                    )
                await safe_reply(msg, f"✅ Message delivered to {code(tid)}.",
                                 reply_markup=kb_admin_back())
            except Forbidden:
                await safe_reply(msg, f"❌ User {code(tid)} has blocked the bot.")
            except Exception as e:
                await safe_reply(msg, f"❌ Delivery failed: {code(str(e))}")

        elif action == "delcampaign":
            cid  = text.upper()
            camp = db.campaigns.get(cid)
            if not camp:
                await safe_reply(msg, f"❌ Campaign {code(cid)} not found."); return
            await safe_reply(
                msg,
                f"🗑 Confirm deactivate campaign {code(cid)}?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, Deactivate",
                                          callback_data=f"a_confirm_del_{cid}"),
                     InlineKeyboardButton("❌ Cancel", callback_data="a_panel")],
                ]),
            )

        elif action == "setprice":
            try:
                price = int(text)
                db.set_price(price)
                await safe_reply(msg, f"✅ Price set to <b>₹{price}</b>",
                                 reply_markup=kb_admin_back())
            except ValueError:
                await safe_reply(msg, "❌ Invalid number.")

        elif action == "setupi":
            db.set_upi(text)
            await safe_reply(msg, f"✅ UPI updated: {code(text)}",
                             reply_markup=kb_admin_back())

        elif action == "addadmin":
            if not is_super_admin(uid):
                await safe_reply(msg, "👑 Super Admin only."); return
            tid = await _parse_id()
            if tid is None: return
            admins = db.settings.setdefault("admin_ids", [])
            if tid not in admins:
                admins.append(tid)
                db.save(force=True)
                await safe_reply(msg, f"✅ {code(tid)} is now an Admin.",
                                 reply_markup=kb_admin_back())
            else:
                await safe_reply(msg, "ℹ️ Already an admin.")
        return

    # ── Admin broadcast — collect content ─────────────────────────────────────
    if is_admin(uid) and context.user_data.get("bcast_step") == "content":
        ctype = "text"; content = None; caption = msg.caption or ""
        if msg.text:
            ctype, content = "text",     msg.text
        elif msg.photo:
            ctype, content = "photo",    msg.photo[-1].file_id
        elif msg.video:
            ctype, content = "video",    msg.video.file_id
        elif msg.document:
            ctype, content = "document", msg.document.file_id
        else:
            await safe_reply(msg, "❌ Unsupported content type."); return

        context.user_data.update(
            bcast_ctype=ctype, bcast_content=content,
            bcast_caption=caption, bcast_step="buttons",
        )
        await safe_reply(
            msg,
            "🔘 <b>Add Inline Buttons?</b> (optional)\n\n"
            f"Format (one per line): {code('Label - https://url.com')}\n\n"
            "Or send <b>skip</b>:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip Buttons", callback_data="bcast_skip_buttons")
            ]]),
        )
        return

    if is_admin(uid) and context.user_data.get("bcast_step") == "buttons":
        rm = parse_buttons(msg.text or "skip")
        await _do_broadcast(update, context, rm)


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL DETECTION  (bot added as admin to a channel)
# ─────────────────────────────────────────────────────────────────────────────
async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    my_member = update.my_chat_member
    if my_member is None:
        return

    new_status = my_member.new_chat_member.status
    chat       = my_member.chat
    from_user  = my_member.from_user

    if chat.type not in ("channel", "supergroup"):
        return
    if new_status not in ("administrator", "creator"):
        return
    if from_user is None:
        return

    uid       = from_user.id
    chan_uname = f"@{chat.username}" if chat.username else str(chat.id)
    logger.info("🔔 Bot became admin in %s (by %d)", chat.title, uid)

    # Auto-add channel to creator record if they exist
    if can_create(uid):
        if is_admin(uid):
            db.ensure_admin_creator(uid)
        cr = db.get_creator(uid)
        if cr and chan_uname not in cr.get("channels", []):
            cr.setdefault("channels", []).append(chan_uname)
            cr["channel_id"] = str(chat.id)
            db.channels_map[str(chat.id)] = str(uid)
            db.save(force=True)

    try:
        bot_me = await context.bot.get_me()
        await context.bot.send_message(
            uid,
            f"🎉 <b>Channel Detected!</b>\n\n"
            f"Bot was added as admin in:\n"
            f"📢 <b>{h(chat.title)}</b> ({code(chan_uname)})\n\n"
            f"You can now create unlock campaigns!\n"
            f"Use /createcampaign or tap below 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Create Campaign", callback_data="c_new")],
                [InlineKeyboardButton("🔗 Connect Channel",  callback_data="onboard_start")],
            ]),
        )
    except Exception as e:
        logger.warning("Could not notify %d: %s", uid, e)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE COMMANDS
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid  = update.effective_user.id
    user = update.effective_user
    if not is_admin(uid):
        await safe_reply(update.message, "❌ Admin access required."); return
    s     = db.global_stats()
    badge = "👑 SUPER ADMIN" if is_super_admin(uid) else "🛡️ Admin"
    await safe_reply(
        update.message,
        f"{badge} — <b>ForceHub Control Center</b>\n"
        f"{line()}\n"
        f"👋 <b>{h(user.first_name)}</b>  🆔 {code(uid)}\n"
        f"👥 <b>{s['total_users']}</b> users  🎨 <b>{s['total_creators']}</b> creators\n"
        f"🎯 <b>{s['total_campaigns']}</b> campaigns  📦 <b>{s['total_materials']}</b> materials\n"
        f"🆕 Today: <b>{s['today_joins']}</b> joins | <b>{s['today_unlocks']}</b> unlocks\n"
        f"🕐 {h(now_str())}",
        reply_markup=kb_admin(),
    )


async def cmd_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid  = update.effective_user.id
    user = update.effective_user
    if not can_create(uid):
        await safe_reply(
            update.message,
            f"❌ You don't have creator access.\n\n"
            f"Use /becomecreator to get started — it's free!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Become Creator", callback_data="u_become_creator")
            ]]),
        )
        return
    if is_admin(uid):
        db.ensure_admin_creator(uid, user.username or "", user.first_name or "")
    cr    = db.get_creator(uid)
    camps = cr.get("campaigns", []) if cr else []
    total_u = sum(db.analytics.get("unlock_success",  {}).get(c, 0) for c in camps)
    total_c = sum(db.analytics.get("campaign_clicks", {}).get(c, 0) for c in camps)
    await safe_reply(
        update.message,
        f"🎨 <b>Creator Panel — ForceHub</b>\n"
        f"{line()}\n"
        f"👤 <b>{h(cr.get('name', user.first_name) if cr else user.first_name)}</b>  {code(uid)}\n"
        f"✅ Active — Free Forever\n"
        f"{line()}\n"
        f"🎯 Campaigns: <b>{len(camps)}</b>  🔓 Unlocks: <b>{total_u}</b>\n"
        f"👆 Clicks: <b>{total_c}</b>  📢 Channels: <b>{len(cr.get('channels', []) if cr else [])}</b>\n"
        f"🕐 {h(now_str())}",
        reply_markup=kb_creator(),
    )


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid  = update.effective_user.id
    user = update.effective_user
    if not can_create(uid):
        await safe_reply(update.message, "❌ Creator access required. Use /becomecreator"); return
    if is_admin(uid): db.ensure_admin_creator(uid, user.username or "", user.first_name or "")
    cr    = db.get_creator(uid)
    camps = cr.get("campaigns", []) if cr else []
    chans = cr.get("channels",   []) if cr else []
    total_c = sum(db.analytics.get("campaign_clicks",      {}).get(c, 0) for c in camps)
    total_v = sum(db.analytics.get("verification_success", {}).get(c, 0) for c in camps)
    total_u = sum(db.analytics.get("unlock_success",       {}).get(c, 0) for c in camps)
    unique_users = len({
        k for k, v in db.users.items()
        if any(c in set(camps) for c in v.get("unlocked_campaigns", []))
    })
    chan_status = h(", ".join(chans[:3])) if chans else "❌ Not connected — use /becomecreator"
    await safe_reply(
        update.message,
        f"📊 <b>Creator Dashboard</b>\n"
        f"{line()}\n"
        f"👤 <b>{h(cr.get('name', user.first_name) if cr else user.first_name)}</b>\n"
        f"🔗 Channel: {chan_status}\n"
        f"✅ Active — Free Forever\n"
        f"{line()}\n"
        f"🎯 Campaigns:        <b>{len(camps)}</b>\n"
        f"👆 Total Clicks:     <b>{total_c}</b>\n"
        f"✅ Total Verified:   <b>{total_v}</b>\n"
        f"🔓 Total Unlocks:    <b>{total_u}</b>\n"
        f"👥 Unique Unlockers: <b>{unique_users}</b>\n"
        f"{line()}\n"
        f"🕐 {h(now_str())}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ New Campaign",  callback_data="c_new"),
             InlineKeyboardButton("🎯 My Campaigns", callback_data="c_campaigns")],
            [InlineKeyboardButton("📈 Analytics",    callback_data="c_stats"),
             InlineKeyboardButton("🔗 My Links",     callback_data="c_links")],
        ]),
    )


async def cmd_mycampaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid  = update.effective_user.id
    user = update.effective_user
    if not can_create(uid):
        await safe_reply(update.message, "❌ Creator access required."); return
    if is_admin(uid): db.ensure_admin_creator(uid)
    cr    = db.get_creator(uid)
    camps = cr.get("campaigns", []) if cr else []
    if not camps:
        await safe_reply(update.message, "📭 No campaigns. Use /createcampaign"); return
    bot_me = await context.bot.get_me()
    text   = f"🎯 <b>Your Campaigns</b> ({len(camps)} total)\n\n"
    for cid in camps[-10:]:
        c       = db.campaigns.get(cid, {})
        lnk     = c.get("unlock_link", "")
        mat     = db.materials.get(c.get("material_id", ""), {})
        title   = mat.get("title", "") or (lnk[:22] + "…" if lnk else "?")
        st      = "✅" if c.get("is_active") else "❌"
        clicks  = db.analytics.get("campaign_clicks",  {}).get(cid, 0)
        unlocks = db.analytics.get("unlock_success",   {}).get(cid, 0)
        link    = f"https://t.me/{bot_me.username}?start=unlock_{cid}"
        text   += (
            f"{st} {code(cid)} <b>{h(title[:20])}</b>\n"
            f"   👆{clicks} | 🔓{unlocks}\n"
            f"   {code(link)}\n\n"
        )
    await safe_reply(update.message, text, reply_markup=kb_back("c_dash"))


async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid  = update.effective_user.id
    if not can_create(uid):
        await safe_reply(update.message, "❌ Creator access required."); return
    if is_admin(uid): db.ensure_admin_creator(uid)
    cr    = db.get_creator(uid)
    camps = cr.get("campaigns", []) if cr else []
    text  = "📈 <b>Your Analytics</b>\n\n"
    tc_t = tv_t = tu_t = 0
    for cid in camps:
        c   = db.campaigns.get(cid, {})
        lnk = c.get("unlock_link", "")
        mat = db.materials.get(c.get("material_id", ""), {})
        title   = mat.get("title", "") or (lnk[:20] + "…" if lnk else cid)
        clk     = db.analytics.get("campaign_clicks",      {}).get(cid, 0)
        ver     = db.analytics.get("verification_success", {}).get(cid, 0)
        ulk     = db.analytics.get("unlock_success",       {}).get(cid, 0)
        tc_t += clk; tv_t += ver; tu_t += ulk
        st = "✅" if c.get("is_active") else "❌"
        text += f"{st} <b>{h(title[:20])}</b>\n   👆{clk}  ✅{ver}  🔓{ulk}\n\n"
    text += (
        f"{line()}\n"
        f"<b>Totals:</b> 👆{tc_t}  ✅{tv_t}  🔓{tu_t}"
    )
    await safe_reply(update.message, text, reply_markup=kb_back("c_dash"))


async def cmd_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid = update.effective_user.id
    if not can_create(uid):
        await safe_reply(update.message, "❌ Creator access required."); return
    if is_admin(uid): db.ensure_admin_creator(uid)
    cr      = db.get_creator(uid)
    mat_ids = cr.get("materials", []) if cr else []
    text    = "📦 <b>Your Materials</b>\n\n"
    for mid in mat_ids[-10:]:
        m = db.materials.get(mid, {})
        text += f"• {code(mid)} — <b>{h(m.get('title','?'))}</b> ({h(m.get('file_type','?'))})\n"
    if not mat_ids: text += "No materials. Use /setup for file-based campaigns."
    await safe_reply(update.message, text, reply_markup=kb_back("c_dash"))


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid = update.effective_user.id
    if not can_create(uid):
        await safe_reply(update.message, "❌ Creator access required."); return
    if is_admin(uid): db.ensure_admin_creator(uid)
    cr   = db.get_creator(uid)
    chns = cr.get("channels", []) if cr else []
    text = "📢 <b>Your Channels</b>\n\n"
    text += "\n".join(f"• {code(ch)}" for ch in chns) if chns else "No channels. Use /becomecreator"
    await safe_reply(update.message, text, reply_markup=kb_back("c_dash"))


async def cmd_togglecampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid = update.effective_user.id
    if not can_create(uid):
        await safe_reply(update.message, "❌ Creator access required."); return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/togglecampaign <campaign_id>')}"); return
    cid  = context.args[0].upper()
    camp = db.campaigns.get(cid)
    if not camp:
        await safe_reply(update.message, f"❌ Campaign {code(cid)} not found."); return
    if not is_admin(uid) and camp.get("creator_id") != str(uid):
        await safe_reply(update.message, "❌ Not your campaign."); return
    camp["is_active"] = not camp.get("is_active", True)
    db.save(force=True)
    st = "✅ Activated" if camp["is_active"] else "❌ Deactivated"
    await safe_reply(update.message, f"{st} campaign {code(cid)}.")


async def cmd_globalstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    s  = db.global_stats()
    tc = sum(db.analytics.get("campaign_clicks",  {}).values())
    tu = sum(db.analytics.get("unlock_success",   {}).values())
    await safe_reply(
        update.message,
        f"📊 <b>Global Stats — ForceHub</b>\n"
        f"{line()}\n"
        f"👥 Users:     <b>{s['total_users']}</b>\n"
        f"🎨 Creators:  <b>{s['total_creators']}</b>\n"
        f"🎯 Campaigns: <b>{s['total_campaigns']}</b>\n"
        f"📦 Materials: <b>{s['total_materials']}</b>\n"
        f"{line()}\n"
        f"🆕 Today Joins:   <b>{s['today_joins']}</b>\n"
        f"🔓 Today Unlocks: <b>{s['today_unlocks']}</b>\n"
        f"👆 All-time Clicks: <b>{tc}</b>\n"
        f"🔓 All-time Unlocks: <b>{tu}</b>\n"
        f"🕐 {h(now_str())}",
    )


async def cmd_addcreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/addcreator <id> [name]')}"); return
    try:
        crid = int(context.args[0])
        name = " ".join(context.args[1:]) if len(context.args) > 1 else f"Creator_{crid}"
        if db.get_creator(crid):
            await safe_reply(update.message, f"ℹ️ Creator {code(crid)} already exists.")
        else:
            db.register_creator(crid, "", name)
            await safe_reply(
                update.message,
                f"✅ Creator <b>{h(name)}</b> ({code(crid)}) added — free access!",
            )
    except ValueError:
        await safe_reply(update.message, "❌ Invalid user ID.")


async def cmd_removecreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/removecreator <id>')}"); return
    try:
        crid = int(context.args[0])
        cr   = db.get_creator(crid)
        if not cr:
            await safe_reply(update.message, f"❌ Creator {code(crid)} not found."); return
        db.remove_creator(crid)
        await safe_reply(update.message, f"🚫 Creator {code(crid)} removed.")
    except ValueError:
        await safe_reply(update.message, "❌ Invalid ID.")


async def cmd_viewuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/viewuser <id>')}"); return
    try:
        tid   = int(context.args[0])
        u     = db.get_user(tid)
        if not u:
            await safe_reply(update.message, f"❌ User {code(tid)} not found."); return
        role  = "🎨 Creator" if is_creator(tid) else ("🛡️ Admin" if is_admin(tid) else "👤 User")
        uname = f"@{u['username']}" if u.get("username") else "—"
        await safe_reply(
            update.message,
            f"👤 <b>User Profile</b>\n"
            f"{line()}\n"
            f"🆔 {code(tid)}  📛 <b>{h(u.get('first_name','?'))}</b>  {h(uname)}\n"
            f"🏷 Role: {role}\n"
            f"📅 Joined: {code(u.get('joined_at','?'))}\n"
            f"{line()}\n"
            f"🔓 Unlocked: <b>{len(u.get('unlocked_campaigns',[]))}</b>\n"
            f"👥 Referrals: <b>{u.get('referral_count',0)}</b>\n"
            f"👈 Referred by: {code(u.get('referred_by','None'))}",
            reply_markup=kb_admin_back(),
        )
    except ValueError:
        await safe_reply(update.message, "❌ Invalid ID.")


async def cmd_viewcreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/viewcreator <id>')}"); return
    try:
        tid = int(context.args[0])
        cr  = db.get_creator(tid)
        if not cr:
            await safe_reply(update.message, f"❌ Creator {code(tid)} not found."); return
        camps = cr.get("campaigns", [])
        tu    = sum(db.analytics.get("unlock_success",  {}).get(c, 0) for c in camps)
        tc    = sum(db.analytics.get("campaign_clicks", {}).get(c, 0) for c in camps)
        await safe_reply(
            update.message,
            f"🎨 <b>Creator Profile</b>\n"
            f"{line()}\n"
            f"🆔 {code(tid)}  📛 <b>{h(cr.get('name','?'))}</b>\n"
            f"✅ Active — Free Forever\n"
            f"{line()}\n"
            f"🎯 Campaigns: <b>{len(camps)}</b>  👆 Clicks: <b>{tc}</b>  🔓 Unlocks: <b>{tu}</b>\n"
            f"📢 Channels: {h(', '.join(cr.get('channels',[])) or 'None')}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Remove", callback_data=f"a_confirm_ban_{tid}")],
                [InlineKeyboardButton("🔙 Back",   callback_data="a_panel")],
            ]),
        )
    except ValueError:
        await safe_reply(update.message, "❌ Invalid ID.")


async def cmd_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args or len(context.args) < 2:
        await safe_reply(update.message, f"Usage: {code('/dm <id> <message>')}"); return
    try:
        tid = int(context.args[0])
        msg = " ".join(context.args[1:])
        await context.bot.send_message(
            tid,
            f"📩 <b>Message from Admin:</b>\n\n{h(msg)}",
            parse_mode=ParseMode.HTML,
        )
        await safe_reply(update.message, f"✅ Delivered to {code(tid)}.")
    except Forbidden:
        await safe_reply(update.message, "❌ User has blocked the bot.")
    except ValueError:
        await safe_reply(update.message, "❌ Invalid ID.")
    except Exception as e:
        await safe_reply(update.message, f"❌ Error: {code(str(e))}")


async def cmd_delcampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/delcampaign <id>')}"); return
    cid  = context.args[0].upper()
    camp = db.campaigns.get(cid)
    if not camp:
        await safe_reply(update.message, f"❌ Campaign {code(cid)} not found."); return
    camp["is_active"] = False
    db.save(force=True)
    await safe_reply(update.message, f"🗑 Campaign {code(cid)} deactivated.")


async def cmd_listcreators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    cids = list(db.creators.keys())
    if not cids:
        await safe_reply(update.message, "📭 No creators registered."); return
    text = f"🎨 <b>All Creators</b> ({len(cids)} total)\n\n"
    for k in cids:
        cr   = db.creators[k]
        camps = len(cr.get("campaigns", []))
        text += f"✅ {code(k)} <b>{h(cr.get('name','?'))}</b> 🎯{camps}\n"
    await safe_reply(update.message, text, reply_markup=kb_admin_back())


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    try:
        page = int(context.args[0]) - 1 if context.args else 0
    except Exception:
        page = 0
    PAGE  = 15
    uids  = list(db.users.keys())
    total = len(uids)
    chunk = uids[page * PAGE:(page + 1) * PAGE]
    text  = f"👥 <b>Users</b> — Page {page+1}/{max(1,(total-1)//PAGE+1)} (Total {total})\n\n"
    for k in chunk:
        u    = db.users[k]
        role = "🎨" if is_creator(int(k)) else ("🛡️" if is_admin(int(k)) else "👤")
        name = f"@{u.get('username','')}" if u.get("username") else h(u.get("first_name", "?"))
        text += f"{role} {code(k)} {name} | 🔓{len(u.get('unlocked_campaigns',[]))}\n"
    if (page + 1) * PAGE < total:
        text += f"\n{code(f'/listusers {page+2}')} for next page"
    await safe_reply(update.message, text)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    try:
        payload = {
            "exported_at": datetime.now().isoformat(),
            "stats":       db.global_stats(),
            "users":       db.users,
            "creators":    db.creators,
            "campaigns":   db.campaigns,
            "analytics":   db.analytics,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            tmp = f.name
        with open(tmp, "rb") as f:
            await update.message.reply_document(
                f,
                filename=f"forcehub_{today_str()}.json",
                caption=f"📤 ForceHub Export — {now_str()}",
            )
        os.unlink(tmp)
    except Exception as e:
        logger.error("Export: %s", e)
        await safe_reply(update.message, "❌ Export failed.")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    await safe_reply(
        update.message,
        "📣 <b>Admin Broadcast</b>\n\nSelect target:",
        reply_markup=kb_broadcast_target(),
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    user = update.effective_user
    uid  = user.id
    role = (
        "👑 Super Admin" if is_super_admin(uid) else
        "🛡️ Admin"       if is_admin(uid)       else
        "🎨 Creator"     if is_creator(uid)     else "👤 User"
    )
    await safe_reply(
        update.message,
        f"🆔 <b>Your Telegram ID</b>\n\n"
        f"{code(uid)}\n\n"
        f"📛 <b>{h(user.first_name)}</b>  @{h(user.username or 'None')}\n"
        f"🏷 Role: {role}",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    uid = update.effective_user.id

    if is_admin(uid):
        text = (
            "🛡️ <b>Admin Help — ForceHub</b>\n"
            f"{line()}\n"
            f"{code('/admin')} — control center\n"
            f"{code('/broadcast')} — broadcast to all\n"
            f"{code('/globalstats')} — analytics\n"
            f"{code('/export')} — export JSON\n"
            f"{code('/addcreator <id> [name]')} — add creator\n"
            f"{code('/removecreator <id>')} — remove creator\n"
            f"{code('/viewuser <id>')} — user profile\n"
            f"{code('/viewcreator <id>')} — creator profile\n"
            f"{code('/listcreators')} — all creators\n"
            f"{code('/listusers [page]')} — all users\n"
            f"{code('/dm <id> <msg>')} — DM any user\n"
            f"{code('/delcampaign <id>')} — delete campaign\n"
            f"{code('/setprice <₹>')} — set price\n"
            f"{code('/setupi <upi>')} — set UPI\n"
            f"{code('/addadmin <id>')} — add admin\n"
            f"{code('/id')} — your Telegram ID\n"
        )
    elif is_creator(uid):
        text = (
            "🎨 <b>Creator Help — ForceHub</b>\n"
            f"{line()}\n"
            f"{code('/creator')} — creator panel\n"
            f"{code('/becomecreator')} — connect a channel\n"
            f"{code('/createcampaign')} — new URL campaign\n"
            f"{code('/setup')} — advanced file campaign\n"
            f"{code('/dashboard')} — stats dashboard\n"
            f"{code('/mycampaigns')} — list campaigns\n"
            f"{code('/mystats')} — analytics\n"
            f"{code('/materials')} — your materials\n"
            f"{code('/channels')} — your channels\n"
            f"{code('/togglecampaign <id>')} — on/off\n"
            f"{code('/id')} — your Telegram ID\n"
        )
    else:
        text = (
            "🚀 <b>ForceHub Help</b>\n"
            f"{line()}\n"
            "ForceHub is a content unlock platform.\n\n"
            "📢 Get a campaign link from a creator\n"
            "✅ Join the required channel(s)\n"
            "🔓 Unlock exclusive content!\n\n"
            f"{code('/start')} — main menu\n"
            f"{code('/id')} — your Telegram ID\n"
            f"{code('/becomecreator')} — become a creator (free!)\n"
        )
    await safe_reply(update.message, text)


async def cmd_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/setprice <amount>')}"); return
    try:
        db.set_price(int(context.args[0]))
        await safe_reply(update.message, f"✅ Price set to <b>₹{context.args[0]}</b>.")
    except ValueError:
        await safe_reply(update.message, "❌ Invalid amount.")


async def cmd_setupi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/setupi <upi_id>')}"); return
    db.set_upi(context.args[0])
    await safe_reply(update.message, f"✅ UPI updated: {code(context.args[0])}")


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None: return
    if not is_super_admin(update.effective_user.id):
        await safe_reply(update.message, "👑 Super Admin only."); return
    if not context.args:
        await safe_reply(update.message, f"Usage: {code('/addadmin <id>')}"); return
    try:
        nid    = int(context.args[0])
        admins = db.settings.setdefault("admin_ids", [])
        if nid not in admins:
            admins.append(nid)
            db.save(force=True)
            await safe_reply(update.message, f"✅ {code(nid)} is now an Admin.")
        else:
            await safe_reply(update.message, "ℹ️ Already an admin.")
    except ValueError:
        await safe_reply(update.message, "❌ Invalid ID.")


# ─────────────────────────────────────────────────────────────────────────────
# ERROR + UNKNOWN HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)
    for sa_id in SUPER_ADMIN_IDS:
        try:
            await context.bot.send_message(
                sa_id,
                f"⚠️ <b>Bot Error</b>\n{code(type(context.error).__name__)}: {code(str(context.error))}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_user is None:
        return
    uid = update.effective_user.id
    if is_admin(uid):
        await safe_reply(update.message, "❓ Unknown command. Use /help",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("🛡️ Admin Panel", callback_data="a_panel")
                         ]]))
    elif is_creator(uid):
        await safe_reply(update.message, "❓ Unknown command. Use /help",
                         reply_markup=InlineKeyboardMarkup([[
                             InlineKeyboardButton("🎨 Creator Panel", callback_data="c_dash")
                         ]]))
    else:
        await safe_reply(update.message, "❓ Unknown command. Use /start",
                         reply_markup=kb_user())


# ─────────────────────────────────────────────────────────────────────────────
# POST_INIT  +  MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    base_cmds = [
        BotCommand("start",         "🚀 Main menu"),
        BotCommand("id",            "🆔 Your Telegram ID"),
        BotCommand("help",          "❓ Help & commands"),
        BotCommand("becomecreator", "🎨 Become a creator (free)"),
    ]
    creator_cmds = base_cmds + [
        BotCommand("creator",         "🎨 Creator panel"),
        BotCommand("createcampaign",  "➕ New URL unlock campaign"),
        BotCommand("setup",           "🔧 Advanced campaign (files)"),
        BotCommand("dashboard",       "📊 Creator dashboard"),
        BotCommand("mycampaigns",     "🎯 My campaigns"),
        BotCommand("mystats",         "📈 My analytics"),
        BotCommand("materials",       "📦 My materials"),
        BotCommand("channels",        "📢 My channels"),
        BotCommand("togglecampaign",  "🔁 Toggle campaign on/off"),
    ]
    admin_cmds = creator_cmds + [
        BotCommand("admin",           "🛡️ Admin panel"),
        BotCommand("broadcast",       "📣 Broadcast to all"),
        BotCommand("globalstats",     "📊 Global analytics"),
        BotCommand("addcreator",      "➕ Add creator"),
        BotCommand("removecreator",   "🚫 Remove creator"),
        BotCommand("viewuser",        "👤 View user"),
        BotCommand("viewcreator",     "🎨 View creator"),
        BotCommand("listcreators",    "📋 List creators"),
        BotCommand("listusers",       "📋 List users"),
        BotCommand("dm",              "💬 DM any user"),
        BotCommand("delcampaign",     "🗑 Delete campaign"),
        BotCommand("setprice",        "💰 Set price"),
        BotCommand("setupi",          "💳 Set UPI ID"),
        BotCommand("addadmin",        "👑 Add admin"),
        BotCommand("export",          "📤 Export data"),
    ]

    await app.bot.set_my_commands(base_cmds)
    for aid in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            pass

    asyncio.create_task(db.periodic_save())
    logger.info("🚀 ForceHub Bot started — %s", now_str())


def main():
    if not BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN not set! Check .env")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ── Onboarding conversation ───────────────────────────────────────────────
    onboard_conv = ConversationHandler(
        entry_points=[
            CommandHandler("becomecreator", onboard_entry),
            CallbackQueryHandler(onboard_entry, pattern=r"^onboard_start$"),
        ],
        states={
            ONBOARD_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_recv_channel)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", onboard_cancel),
            CallbackQueryHandler(onboard_cancel, pattern=r"^onboard_cancel$"),
        ],
        per_message=False,
        allow_reentry=True,
    )

    # ── Create campaign conversation ──────────────────────────────────────────
    createcamp_conv = ConversationHandler(
        entry_points=[
            CommandHandler("createcampaign", createcamp_entry),
            CallbackQueryHandler(createcamp_entry, pattern=r"^c_new$"),
        ],
        states={
            CAMP_LINK:     [MessageHandler(filters.TEXT & ~filters.COMMAND, createcamp_recv_link)],
            CAMP_CHANNELS: [MessageHandler(filters.TEXT & ~filters.COMMAND, createcamp_recv_channels)],
        },
        fallbacks=[
            CommandHandler("cancel",         createcamp_cancel),
            CallbackQueryHandler(createcamp_cancel, pattern=r"^createcamp_cancel$"),
        ],
        per_message=False,
        allow_reentry=True,
    )

    # ── Advanced setup conversation ───────────────────────────────────────────
    setup_conv = ConversationHandler(
        entry_points=[
            CommandHandler("setup", setup_entry),
            CallbackQueryHandler(setup_entry, pattern=r"^c_adv_setup$"),
        ],
        states={
            SETUP_CHANNEL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_recv_channels)],
            SETUP_MAT_TYPE:   [CallbackQueryHandler(setup_recv_mtype, pattern=r"^mtype_")],
            SETUP_MAT_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_recv_title)],
            SETUP_MAT_CONTENT:[
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
                    & ~filters.COMMAND,
                    setup_recv_content,
                )
            ],
            SETUP_REF_COUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_recv_referral)],
        },
        fallbacks=[
            CommandHandler("cancel",       setup_cancel),
            CallbackQueryHandler(setup_cancel, pattern=r"^setup_cancel$"),
        ],
        per_message=False,
        allow_reentry=True,
    )

    # ── Register all handlers (order is critical) ─────────────────────────────
    # 1. ConversationHandlers first
    app.add_handler(onboard_conv)
    app.add_handler(createcamp_conv)
    app.add_handler(setup_conv)

    # 2. Core commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("id",      cmd_id))
    app.add_handler(CommandHandler("help",    cmd_help))

    # 3. Creator commands
    app.add_handler(CommandHandler("creator",         cmd_creator))
    app.add_handler(CommandHandler("dashboard",       cmd_dashboard))
    app.add_handler(CommandHandler("mycampaigns",     cmd_mycampaigns))
    app.add_handler(CommandHandler("mystats",         cmd_mystats))
    app.add_handler(CommandHandler("materials",       cmd_materials))
    app.add_handler(CommandHandler("channels",        cmd_channels))
    app.add_handler(CommandHandler("togglecampaign",  cmd_togglecampaign))

    # 4. Admin commands
    app.add_handler(CommandHandler("admin",          cmd_admin))
    app.add_handler(CommandHandler("broadcast",      cmd_broadcast))
    app.add_handler(CommandHandler("globalstats",    cmd_globalstats))
    app.add_handler(CommandHandler("addcreator",     cmd_addcreator))
    app.add_handler(CommandHandler("removecreator",  cmd_removecreator))
    app.add_handler(CommandHandler("viewuser",       cmd_viewuser))
    app.add_handler(CommandHandler("viewcreator",    cmd_viewcreator))
    app.add_handler(CommandHandler("listcreators",   cmd_listcreators))
    app.add_handler(CommandHandler("listusers",      cmd_listusers))
    app.add_handler(CommandHandler("dm",             cmd_dm))
    app.add_handler(CommandHandler("delcampaign",    cmd_delcampaign))
    app.add_handler(CommandHandler("setprice",       cmd_setprice))
    app.add_handler(CommandHandler("setupi",         cmd_setupi))
    app.add_handler(CommandHandler("addadmin",       cmd_addadmin))
    app.add_handler(CommandHandler("export",         cmd_export))

    # 5. Verify callback BEFORE generic routers
    app.add_handler(CallbackQueryHandler(cb_verify,  pattern=r"^verify_"))

    # 6. Section callback routers (pattern-scoped for clarity)
    app.add_handler(CallbackQueryHandler(cb_user,    pattern=r"^u_"))
    app.add_handler(CallbackQueryHandler(cb_creator, pattern=r"^c_"))
    app.add_handler(CallbackQueryHandler(cb_admin,   pattern=r"^(a_|bcast_)"))

    # 7. Channel detection
    app.add_handler(
        ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # 8. General message handler
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            & ~filters.COMMAND,
            general_message_handler,
        )
    )

    # 9. Unknown commands — must be last
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    # 10. Global error handler
    app.add_error_handler(error_handler)

    logger.info("📡 Polling started…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
