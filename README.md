# 🚀 ForceHub Bot

**Force Subscribe + Content Unlock Platform for Telegram**

> Built with `python-telegram-bot v21` · Async · Railway-ready

---

## ✨ Features

| System | Description |
|---|---|
| 🔐 Force Subscribe | Users must join channel(s) to unlock content |
| 📦 Material Types | Text, Photo, Video, Document/PDF |
| 👥 Referral System | Optional referral count requirement per campaign |
| 📣 Admin Broadcast | Broadcast to Users / Creators / Everyone with media + buttons |
| 📣 Creator Broadcast | Creators message only their own audience |
| ⏱ Trial System | Configurable free trial for creators (default 90 days) |
| 📊 Analytics | Track clicks, verifications, unlocks, referrals per campaign |
| 🛡 Admin Panel | Full control: add/ban creators, set price/UPI/trial |
| 💾 Auto Storage | JSON files auto-created in `/app/data/` |

---

## 📁 File Structure

```
forcehub/
├── bot.py              # Main bot (all systems)
├── requirements.txt    # Dependencies
├── .env.example        # Environment variable template
├── .env                # Your actual config (don't commit!)
└── README.md           # This file
```

Data files (auto-created):
```
/app/data/
├── forcehub_data.json    # All users, creators, campaigns, analytics
└── forcehub_config.json  # Bot version/config metadata
```

---

## ⚙️ Setup

### 1. Clone & Install

```bash
git clone <your-repo>
cd forcehub
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
nano .env
```

Fill in:
- `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `ADMIN_IDS` — your Telegram user ID (get from [@userinfobot](https://t.me/userinfobot))
- `DATA_DIR` — where to store JSON data (default: `/app/data`)

### 3. Run Locally

```bash
python bot.py
```

For local development, set `DATA_DIR=./data` in your `.env`.

---

## 🚂 Deploy on Railway

1. Push code to GitHub
2. Create new Railway project → **Deploy from GitHub**
3. Set environment variables in Railway dashboard:
   - `BOT_TOKEN`
   - `ADMIN_IDS`
   - `DATA_DIR` = `/app/data`
4. Add a **Volume** mounted at `/app/data` (so data persists across deploys)
5. Railway will auto-detect Python and run `python bot.py`

> ⚠️ **Important:** Without a mounted volume, data resets on every deploy!

---

## 🤖 Bot Commands

### 👤 User Commands
| Command | Description |
|---|---|
| `/start` | Open main menu |

### 🎨 Creator Commands
| Command | Description |
|---|---|
| `/setup` | Create a new campaign (5-step wizard) |
| `/mycampaigns` | View all your campaigns + stats |
| `/materials` | List your materials |
| `/channels` | List your channels |
| `/broadcast_my_users` | Broadcast to users who unlocked your content |
| `/renewpanel` | Renew expired creator subscription |

### 🛡️ Admin Commands
| Command | Description |
|---|---|
| `/globalstats` | View total users, creators, campaigns, today's activity |
| `/broadcast` | Broadcast to Users / Creators / Everyone |
| `/addcreator <id> [name]` | Add a creator or renew existing |
| `/bancreator <id>` | Ban/expire a creator |
| `/settrial <days>` | Set global trial days (e.g. `/settrial 90`) |
| `/setprice <amount>` | Set renewal price in ₹ |
| `/setupi <upi_id>` | Set UPI ID for payments |
| `/export` | Export full data as JSON |

---

## 🔗 Campaign Link Format

```
https://t.me/<BotUsername>?start=<CAMPAIGN_ID>
```

Example:
```
https://t.me/ForceHubBot?start=AB12CD34
```

### Referral Link Format
```
https://t.me/<BotUsername>?start=ref_<UserID>
```

---

## 📊 Database Structure (JSON)

```json
{
  "users": {
    "<user_id>": {
      "username": "",
      "first_name": "",
      "joined_at": "ISO timestamp",
      "unlocked_campaigns": ["CAMP_ID"],
      "referral_count": 0,
      "referred_by": null,
      "banned": false
    }
  },
  "creators": {
    "<creator_id>": {
      "name": "",
      "trial_start": "ISO timestamp",
      "trial_days": 90,
      "channels": ["@channel"],
      "materials": ["material_id"],
      "campaigns": ["CAMP_ID"]
    }
  },
  "materials": {
    "<material_id>": {
      "creator_id": "",
      "title": "",
      "description": "",
      "file_id": null,
      "file_type": "text|photo|video|document"
    }
  },
  "campaigns": {
    "<CAMP_ID>": {
      "creator_id": "",
      "material_id": "",
      "channels": ["@channel"],
      "referral_required": 0,
      "is_active": true
    }
  },
  "analytics": {
    "campaign_clicks": { "<CAMP_ID>": 42 },
    "verification_success": {},
    "unlock_success": {},
    "referral_unlocks": {},
    "daily": {
      "2026-04-14": { "joins": 10, "unlocks": 5 }
    }
  },
  "settings": {
    "trial_days": 90,
    "upi_id": "yourname@upi",
    "price": 199,
    "admin_ids": [123456789]
  }
}
```

---

## 🔐 Security Features

- ✅ Bot validates it's admin in the channel before accepting it
- ✅ Duplicate campaign ID prevention (UUID-based)
- ✅ Fake referral loop prevention (can't refer yourself)
- ✅ Expired creator access blocked on `/setup`, `/materials`, `/channels`
- ✅ Broadcast permission strictly checked (admin-only / creator-own-users)
- ✅ Content delivery only after verified channel membership

---

## 📣 Broadcast — Supported Content

| Type | Supported |
|---|---|
| Text | ✅ |
| Photo + Caption | ✅ |
| Video + Caption | ✅ |
| Document/PDF + Caption | ✅ |
| Inline Buttons | ✅ |
| Delivery Stats | ✅ |

**Button format:**
```
Button Name - https://url.com
Another Button - https://url2.com
```

---

## 🛠 Performance Notes

- JSON loaded **once** on startup, cached in memory
- Dirty writes saved to disk every **30 seconds** (background task)
- Broadcast uses `asyncio.sleep(0.05)` between sends to respect Telegram limits
- All handlers are fully **async**

---

## 📞 Support

Built for Railway deployment with persistent `/app/data` volume.

For issues, check logs with:
```bash
railway logs
```
