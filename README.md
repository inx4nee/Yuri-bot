# 🌸 Yuri

![Python Version](https://img.shields.io/badge/Python-3.11-blue)
![Discord.py](https://img.shields.io/badge/discord.py-v2.0+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

Yuri is a highly interactive, chaotic, and dramatic Gen Z AI Discord bot. Unlike standard, polite AI assistants, Yuri is designed with a specific, opinionated teenage persona. She will roast you, judge your vibe, gossip, and dramatically deny being an AI if accused.

Beneath her chaotic personality is a robust, multi-model AI architecture powered by Google Gemini and Groq, featuring conversation memory, image vision, voice note transcription, and dynamic social commands.

---

## ✨ Core Features

* **🧠 Multi-Model AI Brain:** Uses Google Gemini (2.0-flash & 1.5-flash-8b) as the primary engine, with automatic fallback to a rotating key system of Groq Llama models. Optionally supports a **custom fine-tuned model** via Together AI as a final last-resort fallback — ideal for plugging in a personality-trained version of Yuri when all other providers are exhausted.
* **👁️ Vision & Audio Processing:** Can "see" image attachments using Llama-3.2-11b-vision and transcribe voice notes using Groq's Whisper model.
* **💾 Persistent Memory:** Utilizes MongoDB to remember the last 30 days of conversational context for every user.
* **🔍 Web Aware:** Integrates DuckDuckGo to automatically search the web when asked about current events, news, or prices.
* **🎭 Social & Drama Systems:** Built-in systems for anonymous confessions, secret crush matching, and personalized roasts based on a user's Discord profile and chat history.
* **🛡️ Defensive Prompting:** Built-in prompt-injection defenses. If users try to "reprogram" her using system tags, she will actively mock them instead of complying.

---

## 🤖 AI Fallback Chain

When one provider fails or hits a rate limit, Yuri automatically falls through to the next tier with no interruption:

```
1. Gemini 2.0 Flash          ← Primary
2. Gemini 1.5 Flash 8B       ← Secondary Gemini
3. Groq Llama 3.3 70B        ← Groq (round-robin across all loaded keys)
4. Groq Llama 3.1 8B         ← Groq lite
5. Together AI (fine-tuned)  ← Optional last resort
```

Groq keys rotate proactively on every call (not just on failure), so load is spread evenly across all keys you provide. The Together AI tier only activates if `TOGETHER_API_KEY` and `FINETUNED_MODEL_NAME` are set — otherwise it is skipped silently.

---

## 📜 Command Menu

### 💬 Chatting
Yuri doesn't need commands to chat! Just `@mention` her or reply to one of her messages to talk. Send images for her to judge, or voice notes for her to transcribe and reply to.

### 👀 Judgment & Social
* `/roast @user` - Yuri analyzes the user's profile and chat history to absolutely destroy their ego.
* `/rate @user` - Judges a user's vibe (0-100%) based on their recent chat logs.
* `/ship @user1 [@user2]` - Checks the compatibility between two users, complete with a stitched image of their avatars.
* `/compatibility @user1 [@user2]` - Deep compatibility analysis referencing actual message history.
* `/rename @user` - Gives someone a cursed, chaotic nickname.

### 🔥 Drama & Chaos
* `/confess [message]` - Sends a completely anonymous confession to a designated server channel.
* `/crush @user` - Secretly matches with someone. If they run the command on you too, Yuri DMs both of you!
* `/truth` - Get a spicy, chaotic teenage Truth question.
* `/dare` - Get a chaotic Dare.
* `/poll [question] [opt1] [opt2]` - Yuri hosts a poll and picks a side.
* `/hotornot [description]` - Anonymous submission. Server judges you — verdict posted after 15 minutes.
* `/summarize` - Yuri recaps the last 20 messages in the channel.

### 🧠 Utility
* `/ask [question]` - Ask Yuri a direct yes/no question for a sassy answer.
* `/clearhistory` - Wipe your own chat history with Yuri.
* `/feedback` - Report a bug or suggest a feature directly to the developer.

### 🪽 Admin Only
* `/setup [channel]` - Sets the channel where anonymous confessions are posted.
* `/grudge @user` - Forces Yuri to hold a permanent grudge against a user, making her cold and dismissive toward them.
* `/ungrudge @user` - Forgives a user.
* `/wipe @user` - *(Owner only)* Erase a user's conversational history from the database.
* `/health` - Checks the bot's ping, MongoDB connection status, and Groq API availability.

### ⚙️ Owner Commands (prefix `!`)
* `!sync` - Syncs slash commands globally after any schema change.
* `!stats` - Bot-wide usage statistics across all servers.
* `!inbox` - Lists all feedback submissions.
* `!reply <user_id> <message>` - DMs a formatted response to a feedback submission.
* `!fetchlog <user_id>` - Downloads a user's full conversation log as a `.txt` file.
* `!dailylog` - Downloads today's interaction log.
* `!wipeall` - Clears the entire chat history collection.
* `!usercount` - Count of unique users currently in the database.

---

## 🛠️ Tech Stack

* **Language:** Python 3.11
* **Discord Library:** `discord.py`
* **Database:** MongoDB (`motor` for async)
* **AI APIs:**
  * `google-generativeai` (Gemini)
  * `groq` (Llama 3 & Whisper)
  * `together` *(optional — fine-tuned model fallback)*
* **Image Processing:** `Pillow` (PIL)
* **Web Search:** `duckduckgo-search`

---

## 📁 Project Structure

```text
yuri-bot/
├── .gitignore
├── main.py                  # Bot init, MongoDB setup, cog loader, global error handler
├── utils.py                 # Shared helpers: image I/O, web search, GIF tags, sanitisation
├── Procfile                 # Deployment instructions for cloud hosting (e.g., Heroku)
├── requirements.txt
├── runtime.txt              # Python 3.11 pin for deployment platforms
├── LICENSE.txt
├── PRIVACY_POLICY.md
├── README.md
│
├── cogs/
│   ├── prompts.py           # Yuri's system prompt (versioned independently from ai.py)
│   ├── ai.py                # Inference pipeline, cooldown state, key rotation, on_message
│   ├── social.py            # /roast, /ship, /confess, /crush, /poll, /hotornot, /summarize
│   ├── admin.py             # Admin/owner diagnostics and configuration commands
│   └── general.py           # /help, /feedback, rotating status loop
│
└── tests/
    ├── test_ai.py           # Fallback chain, key rotation, search triggers, sanitisation
    ├── test_admin.py        # Health command, MongoDB interaction mocks
    └── test_utils.py        # Image download guards, stitch dimensions, smart time
```

---

## 🚀 Setup & Installation

### 1. Prerequisites
* Python 3.11+
* A MongoDB cluster (e.g., MongoDB Atlas)
* A Discord Bot Token with **Message Content** and **Server Members** intents enabled
* API keys for Google Gemini and Groq

### 2. Clone and Install
```bash
git clone https://github.com/Saineeee/Yuri-bot.git
cd yuri-bot
pip install -r requirements.txt
```

### 3. Environment Variables

Create a `.env` file in the root directory:

```dotenv
# Required
DISCORD_TOKEN=
MONGO_URL=
OWNER_ID=                    # Your Discord user ID (integer)
GEMINI_API_KEY=
GROQ_API_KEY=

# Optional — Groq key rotation
GROQ_API_KEY_2=              # Additional keys for round-robin load balancing
GROQ_API_KEY_3=              # Add as many as needed

# Optional — Fine-tuned model via Together AI
TOGETHER_API_KEY=            # Omit entirely to disable this fallback tier
FINETUNED_MODEL_NAME=        # e.g. your-org/yuri-llama3-ft
```

**Configuration reference:**

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token from the Discord Developer Portal |
| `MONGO_URL` | ✅ | MongoDB connection string; Atlas SRV format supported |
| `OWNER_ID` | ✅ | Discord user ID granted owner-only commands |
| `GEMINI_API_KEY` | ✅ | Google AI Studio key for Gemini access |
| `GROQ_API_KEY` | ✅ | Primary Groq key (inference, Whisper, vision) |
| `GROQ_API_KEY_2` … `_N` | ➖ | Extra Groq keys; scanned at startup, no upper limit |
| `TOGETHER_API_KEY` | ➖ | Together AI key; omitting disables the fine-tuned tier |
| `FINETUNED_MODEL_NAME` | ➖ | Together AI model ID — both vars must be set together |

### 4. Run the Bot
```bash
python main.py
```

After first startup, run `!sync` in any server channel (owner only) to register slash commands globally.

> **Deploying to Heroku/Railway?** A `Procfile` is already included. Set env vars in the platform's config vars dashboard instead of a `.env` file.

---

## 🧬 Fine-Tuning Your Own Model (Optional)

The Together AI slot lets you plug in a model trained on Yuri's own conversation data, giving the most on-character responses during provider outages.

### 1. Export Training Data
```python
import json, asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def export():
    col = AsyncIOMotorClient(MONGO_URL)["yuri_bot_db"]["chat_history"]
    async for doc in col.find({}).sort([("user_id", 1), ("timestamp", 1)]):
        role = "assistant" if doc["role"] == "model" else "user"
        print(json.dumps({"role": role, "content": doc["parts"][0]}))

asyncio.run(export())
```

Group adjacent messages into conversation pairs before uploading. Filter out any messages stored as `[message removed: injection attempt detected]`.

### 2. Fine-Tune on Together AI
1. Upload the JSONL dataset at [together.ai/fine-tuning](https://www.together.ai/fine-tuning).
2. Select a Llama 3 base model and set Yuri's system prompt as the training system message.
3. Start the job (typically 30–90 min for small datasets) and copy the resulting model name.

### 3. Activate
Add both vars to your `.env` and restart — no code changes needed:
```dotenv
TOGETHER_API_KEY=your_key
FINETUNED_MODEL_NAME=your-org/yuri-llama3-ft
```

---

## 🧪 Running Tests

No live credentials required — all external I/O is mocked.

```bash
python -m pytest tests/ -v
```

---

## 🩺 Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| Bot ignores `@` mentions | Missing privileged intents | Enable **Message Content** + **Server Members** in Discord Developer Portal → Bot |
| Slash commands not showing | Tree not synced | Run `!sync` as the bot owner |
| `FATAL: MongoDB Connection Failed` | Bad connection string or IP not allowlisted | Verify `MONGO_URL`; whitelist your host IP in Atlas → Network Access |
| All responses return rate-limit message | All provider tiers exhausted | Add more `GROQ_API_KEY_N` keys; wait for Gemini 24 h cooldown to reset |
| Together AI tier never activates | One or both env vars missing | Both `TOGETHER_API_KEY` and `FINETUNED_MODEL_NAME` must be set |
| `Role Hierarchy` error on `/rename` | Bot role below target's highest role | Drag Yuri's role above target roles in Server Settings → Roles |

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE.txt) file for details.

---

## 🔒 Privacy

Yuri processes message content, images, and voice notes only when explicitly mentioned or replied to. Conversational context is stored securely in MongoDB and automatically expires after 30 days. Read the full [Privacy Policy](PRIVACY_POLICY.md).
