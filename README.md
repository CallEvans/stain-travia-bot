# 🎯 Stain Trivia Bot

A Telegram trivia bot that drops questions in groups every 2 hours. Admins register their groups via private chat. Points reset every 72 hours.

## Features

- 🎯 Auto-drops trivia questions every 2 hours in registered groups
- 🏆 First correct answer wins 10 points
- 📊 Leaderboard showing top 7 players
- ⏰ Points reset every 72 hours automatically
- 🔒 Join gate — users must join @stainprojectss
- 💾 Group permissions and scores saved to JSON file
- 4000+ questions from Open Trivia Database (no API key needed)

## Commands

### Private chat (admin)

|Command   |Description                     |
|----------|--------------------------------|
|`/start`  |Welcome message + how it works  |
|`/give`   |Register your group with the bot|
|`/ping`   |Check bot uptime                |
|`/support`|Contact the owner               |

### Group chat

|Command        |Description                       |
|---------------|----------------------------------|
|`/ans <answer>`|Answer the current trivia question|
|`/leaderboard` |Show top 7 players in the group   |

-----

## Deploy: GitHub → Render → UptimeRobot

### 1. Create your bot

1. Message **@BotFather** on Telegram → `/newbot`
1. Copy your **BOT_TOKEN**
1. Add bot as admin of `@stainprojectss` (so it can verify members)

### 2. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit: Stain Trivia Bot"
git remote add origin https://github.com/YOUR_USERNAME/stain-trivia-bot.git
git branch -M main
git push -u origin main
```

### 3. Deploy on Render

1. [render.com](https://render.com) → **New** → **Web Service**
1. Connect your repo
1. Settings:

|Field            |Value                            |
|-----------------|---------------------------------|
|**Environment**  |`Python 3`                       |
|**Build Command**|`pip install -r requirements.txt`|
|**Start Command**|`python bot.py`                  |
|**Instance Type**|`Free`                           |

1. Environment variable:

|Key        |Value                     |
|-----------|--------------------------|
|`BOT_TOKEN`|your token from @BotFather|

### 4. Set up UptimeRobot

Render’s free tier spins down after inactivity which would stop the 2-hour question scheduler.
UptimeRobot pings your service every 5 minutes to keep it alive.

1. Go to [uptimerobot.com](https://uptimerobot.com) → sign up free
1. **Add New Monitor**:
- Monitor Type: `HTTP(s)`
- Friendly Name: `Stain Trivia Bot`
- URL: `https://your-app.onrender.com`
- Monitoring Interval: `5 minutes`
1. Click **Create Monitor**

### 5. Add bot to your group

1. Add the bot to your group
1. Make it an admin (so it can send messages)
1. In private chat with the bot → send `/give`
1. Send your group ID when prompted

**Getting your group ID:** Forward any message from your group to [@userinfobot](https://t.me/userinfobot)

-----

## How It Works

```
Admin registers group via /give in private chat
    → Bot saves group ID to data.json

Scheduler runs every minute
    → If 2 hours passed since last question
    → Fetches fresh question from Open Trivia DB
    → Sends to group

User answers with /ans Paris
    → Bot checks against correct answer
    → If correct: awards 10 points, closes question
    → If wrong: tells them to try again

Every 72 hours
    → All scores in every group reset automatically
```

## File Structure

```
trivia_group_bot/
├── bot.py              # All bot logic
├── requirements.txt
├── Procfile
├── .python-version
├── .gitignore
├── README.md
└── data.json           # auto-created, not committed to git
```

-----

## Owner

Built and maintained by **Stain**.
🔗 https://linktr.ee/iamevanss
