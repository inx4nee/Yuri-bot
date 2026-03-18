# 🌸 Yuri (Discord Bot)

![Python Version](https://img.shields.io/badge/Python-3.11-blue)
![Discord.py](https://img.shields.io/badge/discord.py-v2.0+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

Yuri is a highly interactive, chaotic, and dramatic Gen Z AI Discord bot. Unlike standard, polite AI assistants, Yuri is designed with a specific, opinionated teenage persona. She will roast you, judge your vibe, gossip, and dramatically deny being an AI if accused. 

Beneath her chaotic personality is a robust, multi-model AI architecture powered by Google Gemini and Groq, featuring conversation memory, image vision, voice note transcription, and dynamic social commands.

---

## ✨ Core Features

* **🧠 Multi-Model AI Brain:** Uses Google Gemini (1.5-flash & 2.0-flash) as the primary engine, with an automatic fallback to a rotating key system of Groq Llama 3 models to bypass rate limits and ensure maximum uptime.
* **👁️ Vision & Audio Processing:** Can "see" image attachments using Llama-3.2-11b-vision and transcribe voice notes using Groq's Whisper model.
* **💾 Persistent Memory:** Utilizes MongoDB to remember the last 30 days of conversational context for every user.
* **🔍 Web Aware:** Integrates DuckDuckGo to automatically search the web when asked about current events, news, or prices.
* **🎭 Social & Drama Systems:** Built-in systems for anonymous confessions, secret crush matching, and personalized roasts based on a user's Discord profile and chat history.
* **🛡️ Defensive Prompting:** Built-in prompt-injection defenses. If users try to "reprogram" her using system tags, she will actively mock them instead of complying.

---

## 📜 Command Menu

### 💬 Chatting
Yuri doesn't need commands to chat! Just `@mention` her or reply to one of her messages to talk. Send images for her to judge, or voice notes for her to transcribe and reply to.

### 👀 Judgment & Social
* `/roast @user` - Yuri analyzes the user's profile and chat history to absolutely destroy their ego.
* `/rate @user` - Judges a user's vibe (0-100%) based on their recent chat logs.
* `/ship @user1 [@user2]` - Checks the compatibility between two users, complete with a stitched image of their avatars.
* `/rename @user` - Gives someone a cursed, chaotic nickname.

### 🔥 Drama & Chaos
* `/confess [message]` - Sends a completely anonymous confession to a designated server channel.
* `/crush @user` - Secretly matches with someone. If they run the command on you too, Yuri DMs both of you!
* `/truth` - Get a spicy, chaotic teenage Truth question.
* `/dare` - Get a chaotic Dare.

### 🧠 Utility
* `/ask [question]` - Ask Yuri a direct yes/no question for a sassy answer.
* `/wipe` - **(Admin Only)** Erase a user's conversational history from the database.
* `/feedback` - Report a bug or suggest a feature directly to the developer.

### 🪽 Admin Only
* `/setup [channel]` - Sets the channel where anonymous confessions are posted.
* `/grudge @user` - Forces Yuri to hold a permanent grudge against a user, making her cold and dismissive toward them.
* `/ungrudge @user` - Forgives a user.
* `/health` - Checks the bot's ping, MongoDB connection status, and Groq API availability.

---

## 🛠️ Tech Stack

* **Language:** Python 3.11
* **Discord Library:** `discord.py`
* **Database:** MongoDB (`motor` for async)
* **AI APIs:** * `google-generativeai` (Gemini)
  * `groq` (Llama 3 & Whisper)
* **Image Processing:** `Pillow` (PIL)
* **Web Search:** `duckduckgo-search`

---

## 🚀 Setup & Installation

### 1. Prerequisites
* Python 3.11+
* A MongoDB cluster (e.g., MongoDB Atlas)
* A Discord Bot Token
* API Keys for Google Gemini and Groq

### 2. Clone and Install
```bash
git clone [https://github.com/yourusername/yuri-bot.git](https://github.com/yourusername/yuri-bot.git)
cd yuri-bot
pip install -r requirements.txt

```
3. Environment Variables

Create a .env file in the root directory and add the following required variables:

DISCORD_TOKEN=your_discord_bot_token_here
MONGO_URL=your_mongodb_connection_string
OWNER_ID=your_personal_discord_user_id
GEMINI_API_KEY=your_google_gemini_api_key
GROQ_API_KEY=your_primary_groq_api_key

Optional:

Add backup Groq keys for automatic rotation if rate-limited
GROQ_API_KEY_2=your_second_groq_api_key
GROQ_API_KEY_3=your_third_groq_api_key

### 4. Run the Bot
python main.py


__Note__: If deploying to a platform like Heroku, a Procfile is already included.
