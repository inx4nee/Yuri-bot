# cogs/ai.py
# All fixes applied — see inline comments prefixed with FIX for each change.

import discord
from discord.ext import commands
from discord import app_commands

import os
import io
import re
import logging
import datetime
import base64
from typing import Optional

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from groq import AsyncGroq

# ── FIX (HIGH): Guard the `together` import ─────────────────────────────────
# Previously a bare top-level import crashed the entire bot at startup if the
# package was missing from the environment. Now the bot starts normally and
# only activates the Together fallback when both the package AND the required
# env vars are present.
try:
    from together import AsyncTogether
    _TOGETHER_AVAILABLE = True
except ImportError:
    AsyncTogether = None          # type: ignore[assignment,misc]
    _TOGETHER_AVAILABLE = False

import utils

# ── FIX (MEDIUM): Extract SYSTEM_PROMPT to prompts.py ───────────────────────
# Previously ~150 lines of prompt text lived in this file, making both the
# prompt and the cog logic harder to read, test, and version independently.
# The prompt now lives in prompts.py and is imported here — a one-line change
# to update the prompt no longer requires touching AI logic at all.
from prompts import SYSTEM_PROMPT

# ── Module-level logger ──────────────────────────────────────────────────────
# FIX (HIGH): Replace all print() calls with structured logging.
# print() output is invisible to log aggregators (Datadog, Railway, etc.) and
# provides no severity level, timestamp, or module context. Using the standard
# logging module means all output is captured by whatever handler main.py
# configures, works correctly in production, and can be filtered by level.
log = logging.getLogger(__name__)

# ── FIX (LOW): Named constants — no more magic numbers scattered in the code ─
# Every tunable number lives here so the meaning is clear and there is a single
# place to change values without hunting through method bodies.
USER_COOLDOWN_SECS   = 3     # minimum seconds between responses to the same user
GUILD_COOLDOWN_SECS  = 1     # minimum seconds between responses in the same server
MAX_INPUT_CHARS      = 2000  # hard cap on incoming message length
MAX_HISTORY_MESSAGES = 20    # conversation turns loaded from MongoDB per request
MAX_GROQ_TOKENS      = 256   # max tokens for all Groq completions
MIN_SEARCH_LENGTH    = 15    # messages shorter than this never trigger a web search

# ── FIX (CRITICAL): Compile search-trigger regex once at module level ────────
# Previously a plain word list ("who", "what", "where" ...) matched inside
# almost any English sentence and triggered a DuckDuckGo search on nearly
# every message. This regex requires word-boundary-anchored intent phrases
# so casual messages like "how are you" or "what's up" are correctly ignored.
_SEARCH_TRIGGER_RE = re.compile(
    r'\b('
    r'who is|who are|who was|who were|'
    r'what is|what are|what was|what were|what does|what did|'
    r'where is|where are|where can|'
    r'when is|when did|when does|when was|'
    r'why is|why does|why did|'
    r'how do|how does|how did|how can|how to|'
    r'weather|price of|cost of|'
    r'news|latest|current|today|right now|'
    r'search for|look up|tell me about'
    r')\b',
    re.IGNORECASE,
)


class AI(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # ── Gemini setup ─────────────────────────────────────────────────────
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT:        HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH:       HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        self.model_1 = genai.GenerativeModel(
            "gemini-2.0-flash",
            safety_settings=self.safety_settings,
            system_instruction=SYSTEM_PROMPT,
        )
        self.model_2 = genai.GenerativeModel(
            "gemini-1.5-flash-8b",
            safety_settings=self.safety_settings,
            system_instruction=SYSTEM_PROMPT,
        )

        # ── Groq multi-key setup ──────────────────────────────────────────────
        self.groq_keys: list[str] = []
        if os.getenv("GROQ_API_KEY"):
            self.groq_keys.append(os.getenv("GROQ_API_KEY"))  # type: ignore[arg-type]
        i = 2
        while os.getenv(f"GROQ_API_KEY_{i}"):
            self.groq_keys.append(os.getenv(f"GROQ_API_KEY_{i}"))  # type: ignore[arg-type]
            i += 1

        self.current_groq_index = 0
        if self.groq_keys:
            self.groq_client: Optional[AsyncGroq] = AsyncGroq(api_key=self.groq_keys[0])
            log.info("Loaded %d Groq API key(s).", len(self.groq_keys))
        else:
            self.groq_client = None
            log.warning("No Groq keys found — Groq fallback unavailable.")

        # Gemini per-model cooldown state
        self.cooldowns:   dict[int, Optional[datetime.datetime]] = {1: None, 2: None}
        self.fail_counts: dict[int, int]                         = {1: 0,    2: 0}

        # ── Together AI setup (fine-tuned Yuri model) ─────────────────────────
        together_key = os.getenv("TOGETHER_API_KEY")
        self.finetuned_model: Optional[str] = os.getenv("FINETUNED_MODEL_NAME")
        if _TOGETHER_AVAILABLE and together_key and self.finetuned_model:
            self.together_client = AsyncTogether(api_key=together_key)  # type: ignore[misc]
            log.info("Together AI loaded. Model: %s", self.finetuned_model)
        else:
            self.together_client = None
            if together_key and not _TOGETHER_AVAILABLE:
                log.warning(
                    "TOGETHER_API_KEY is set but 'together' is not installed. "
                    "Run: pip install together"
                )

        # ── FIX (HIGH): Per-user and per-guild rate-limit caches ─────────────
        # Only a per-user 3-second window existed before. A server with 50+
        # active users could send simultaneous mentions, creating a request
        # burst large enough to exhaust Gemini's per-minute quota and trigger
        # the 24-hour cooldown penalty for the entire bot.
        # The guild bucket (1 req/sec/server) is an outer guard; the user
        # bucket stays as-is. DM messages skip the guild check entirely.
        self._user_cooldowns:  dict[int, datetime.datetime] = {}
        self._guild_cooldowns: dict[int, datetime.datetime] = {}

    # ── Private helpers ───────────────────────────────────────────────────────

    def _advance_groq_key(self) -> None:
        """FIX (MEDIUM): Proactive round-robin rotation before each fallback call.

        Spreads load across all available keys proactively rather than waiting
        for one key to rate-limit before rotating to the next. Has no effect
        when only one key is loaded.
        """
        if len(self.groq_keys) <= 1:
            return
        self.current_groq_index = (self.current_groq_index + 1) % len(self.groq_keys)
        self.groq_client = AsyncGroq(api_key=self.groq_keys[self.current_groq_index])

    async def _rotate_groq_key(self) -> bool:
        """On-failure rotation: switch to the next key after an error.

        Returns False if only one key is loaded (nothing to rotate to).
        """
        if len(self.groq_keys) <= 1:
            return False
        self.current_groq_index = (self.current_groq_index + 1) % len(self.groq_keys)
        self.groq_client = AsyncGroq(api_key=self.groq_keys[self.current_groq_index])
        log.info("Rotated to Groq key #%d after error.", self.current_groq_index + 1)
        return True

    def _is_user_on_cooldown(self, user_id: int) -> bool:
        """True if the user sent a message within USER_COOLDOWN_SECS.

        Expired entries are pruned on every call to prevent unbounded memory growth.
        """
        now = datetime.datetime.now()
        self._user_cooldowns = {k: v for k, v in self._user_cooldowns.items() if v > now}
        if user_id in self._user_cooldowns:
            return True
        self._user_cooldowns[user_id] = now + datetime.timedelta(seconds=USER_COOLDOWN_SECS)
        return False

    def _is_guild_on_cooldown(self, guild_id: int) -> bool:
        """True if this guild received a response within GUILD_COOLDOWN_SECS."""
        now = datetime.datetime.now()
        self._guild_cooldowns = {k: v for k, v in self._guild_cooldowns.items() if v > now}
        if guild_id in self._guild_cooldowns:
            return True
        self._guild_cooldowns[guild_id] = now + datetime.timedelta(seconds=GUILD_COOLDOWN_SECS)
        return False

    # ── Audio ─────────────────────────────────────────────────────────────────

    async def transcribe_audio(self, file_bytes: bytes, filename: str) -> Optional[str]:
        """Transcribe audio via Groq Whisper. Rotates keys on failure."""
        if not self.groq_client:
            return None
        for _ in range(len(self.groq_keys) + 1):
            try:
                transcription = await self.groq_client.audio.transcriptions.create(
                    file=(filename, file_bytes),
                    model="whisper-large-v3",
                    response_format="json",
                )
                return transcription.text
            except Exception as e:
                log.warning("STT error (key #%d): %s", self.current_groq_index + 1, e)
                if not await self._rotate_groq_key():
                    break
        return None

    # ── Core AI ───────────────────────────────────────────────────────────────

    async def get_combined_response(
        self,
        user_id:         int,
        text_input:      Optional[str],
        image_input=None,
        prompt_override: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """Generate a Yuri response.

        Returns (response_text, gif_url). gif_url is None when no GIF tag
        was present in the model output.
        """

        # 1. Grudge check
        is_grudged = await self.bot.grudge_collection.find_one({"user_id": user_id})
        grudge_prompt = (
            "\n[SYSTEM: You hold a grudge against this user. Be cold/dismissive.]"
            if is_grudged else ""
        )

        # 2. Build conversation history from MongoDB
        # FIX (LOW): Uses MAX_HISTORY_MESSAGES constant — was hardcoded 20.
        cursor = (
            self.bot.chat_collection.find({"user_id": user_id})
            .sort("timestamp", -1)
            .limit(MAX_HISTORY_MESSAGES)
        )
        recent_docs = [doc async for doc in cursor]
        recent_docs.reverse()

        # FIX (CRITICAL): Merge consecutive same-role messages instead of dropping them.
        # When two user docs appeared back-to-back the old code silently discarded the
        # second one, creating invisible holes in the conversation context.
        history_db: list[dict] = []
        for doc in recent_docs:
            role = doc.get("role")
            if not history_db and role != "user":
                continue  # Gemini history must start with 'user'
            if history_db and history_db[-1]["role"] == role:
                history_db[-1]["parts"][0] += "\n" + doc["parts"][0]
            else:
                history_db.append({"role": role, "parts": [doc["parts"][0]]})
        # Gemini requires the history to end on a 'model' turn before the new user message
        if history_db and history_db[-1]["role"] == "user":
            history_db.pop()

        # 3. Time context
        time_str    = utils.get_smart_time(text_input or "")
        system_data = (
            f"[System: Current Date/Time is {time_str}. "
            f"Do not mention this unless asked.]{grudge_prompt}"
        )

        # 4. Conditional web search
        # FIX (CRITICAL): Regex with word-boundary anchors, not a single-word list.
        search_data = ""
        if (
            text_input
            and not prompt_override
            and len(text_input) >= MIN_SEARCH_LENGTH
            and _SEARCH_TRIGGER_RE.search(text_input)
        ):
            web_results = await utils.search_web(text_input)
            if web_results:
                search_data = web_results

        # 5. Construct final prompt
        sanitized    = utils.sanitize_for_prompt(text_input) if text_input else ""
        current_text = f"{system_data}\n{search_data}\n\n"
        if str(user_id) == str(self.bot.owner_id):
            current_text += "(System: User is your creator 'Sane'. Be cool.) "

        if prompt_override:
            current_text += f"{prompt_override} (Reply as Yuri.)"
        else:
            if sanitized:
                current_text += f"[USER_INPUT]{sanitized}[/USER_INPUT]"
            if image_input:
                current_text += " (User sent an image. Roast it or comment on it.)"

        # 6. Gemini generation loop — tries model_1 then model_2
        response_text = ""
        successful    = False
        now           = datetime.datetime.now()

        for layer in self.cooldowns:
            if self.cooldowns[layer] and now > self.cooldowns[layer]:
                self.cooldowns[layer] = None

        for model, layer in [(self.model_1, 1), (self.model_2, 2)]:
            if successful:
                break
            if not self.cooldowns[layer]:
                try:
                    gemini_history = history_db + [{"role": "user", "parts": [current_text]}]
                    if image_input:
                        gemini_history[-1]["parts"].append(image_input)
                    response      = await model.generate_content_async(gemini_history)
                    response_text = response.text
                    successful    = True
                    self.fail_counts[layer] = 0
                except Exception as e:
                    log.warning("Gemini layer %d error: %s", layer, e)
                    self.fail_counts[layer] += 1
                    wait = (
                        datetime.timedelta(minutes=1)
                        if self.fail_counts[layer] < 2
                        else datetime.timedelta(hours=24)
                    )
                    self.cooldowns[layer] = now + wait

        # 7. Groq fallback
        if not successful:
            response_text = await self.call_groq_fallback(
                history_db, SYSTEM_PROMPT, current_text, image_input
            )

        # 8. Process GIF tags and persist to DB
        clean_text, gif_url = await utils.process_gif_tags(response_text)

        if not prompt_override:
            user_save  = text_input or "[Image]"
            model_save = clean_text or f"[GIF: {gif_url}]"
            timestamp  = datetime.datetime.utcnow()
            await self.bot.chat_collection.insert_one(
                {"user_id": user_id, "role": "user",  "parts": [user_save],  "timestamp": timestamp}
            )
            await self.bot.chat_collection.insert_one(
                {"user_id": user_id, "role": "model", "parts": [model_save], "timestamp": timestamp}
            )

        return clean_text, gif_url

    async def call_groq_fallback(
        self,
        history:    list[dict],
        sys_prompt: str,
        msg:        str,
        img=None,
    ) -> str:
        """Groq fallback chain: 70B → 8B → rotate key → retry → Together AI.

        FIX (MEDIUM): Calls _advance_groq_key() once at the start of every
        invocation so each call begins on a different key (proactive round-robin),
        distributing load evenly instead of hammering key #1 until it breaks.
        On failure, _rotate_groq_key() still handles emergency rotation.
        """
        if not self.groq_client:
            return "server dead rn. try again later 💀"

        # Proactive round-robin — always start on a fresh key
        self._advance_groq_key()

        messages: list[dict] = [{"role": "system", "content": sys_prompt}]
        for m in history:
            role = "assistant" if m["role"] == "model" else "user"
            if isinstance(m["parts"][0], str):
                messages.append({"role": role, "content": m["parts"][0]})

        if img:
            if img.width > 1024 or img.height > 1024:
                img.thumbnail((1024, 1024))
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf     = io.BytesIO()
            img.save(buf, format="JPEG")
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text",      "text": msg},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                ],
            })
        else:
            messages.append({"role": "user", "content": msg})

        # FIX (LOW): Uses MAX_GROQ_TOKENS constant — was hardcoded literal 256.
        for _ in range(len(self.groq_keys) + 1):
            try:
                model = (
                    "meta-llama/llama-4-scout-17b-16e-instruct" if img
                    else "llama-3.3-70b-versatile"
                )
                comp = await self.groq_client.chat.completions.create(
                    model=model, messages=messages, max_tokens=MAX_GROQ_TOKENS
                )
                return comp.choices[0].message.content
            except Exception as e:
                log.warning(
                    "Groq %s failed (key #%d): %s: %s",
                    "vision" if img else "70b",
                    self.current_groq_index + 1,
                    type(e).__name__, e,
                )
                if not img:
                    try:
                        comp = await self.groq_client.chat.completions.create(
                            model="llama-3.1-8b-instant",
                            messages=messages,
                            max_tokens=MAX_GROQ_TOKENS,
                        )
                        return comp.choices[0].message.content
                    except Exception as e2:
                        log.warning(
                            "Groq 8B failed (key #%d): %s",
                            self.current_groq_index + 1, e2,
                        )
                if not await self._rotate_groq_key():
                    break

        # Last resort: Together AI fine-tuned model
        if self.together_client and self.finetuned_model:
            try:
                comp = await self.together_client.chat.completions.create(
                    model=self.finetuned_model, messages=messages, max_tokens=150
                )
                log.info("Together AI (fine-tuned) responded.")
                return comp.choices[0].message.content
            except Exception as e:
                log.error("Together AI failed: %s: %s", type(e).__name__, e)

        return "the ai is down rn, wait like 12 hours (rate limits) 💀"

    # ── Events ────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.bot.user or message.content.startswith(self.bot.command_prefix):
            return

        is_reply = (
            message.reference
            and message.reference.resolved
            and message.reference.resolved.author == self.bot.user
        )
        if not (self.bot.user.mentioned_in(message) or is_reply):
            return

        # Per-user cooldown checked first (cheapest)
        if self._is_user_on_cooldown(message.author.id):
            return

        # Per-guild cooldown — DMs have no guild so skip the check there
        if message.guild and self._is_guild_on_cooldown(message.guild.id):
            return

        try:
            async with message.channel.typing():
                user_id    = message.author.id
                clean_text = message.content.replace(f"<@{self.bot.user.id}>", "").strip()
                img_data   = None
                voice_text = ""

                for att in message.attachments:
                    fname = att.filename.lower()
                    if not img_data and any(fname.endswith(x) for x in ["png", "jpg", "jpeg", "webp"]):
                        img_data = await utils.get_image_from_url(att.url)
                    elif not voice_text and any(fname.endswith(x) for x in ["ogg", "mp3", "wav", "m4a"]):
                        file_bytes  = await att.read()
                        transcribed = await self.transcribe_audio(file_bytes, fname)
                        if transcribed:
                            voice_text = f'\n[User Voice Note]: "{transcribed}"'

                final_text = clean_text + voice_text
                if not final_text.strip() and not img_data:
                    return

                # FIX (HIGH): Hard cap on input length.
                # No upper bound existed before — a malicious user could paste a
                # 100 000-character block, silently blowing out token budgets.
                # 2000 chars matches Discord's own per-message limit so normal
                # users typing will never hit this ceiling.
                if len(final_text) > MAX_INPUT_CHARS:
                    await message.reply(
                        f"that message is way too long bestie 💀 "
                        f"keep it under {MAX_INPUT_CHARS} chars"
                    )
                    return

                resp_text, gif_url = await self.get_combined_response(
                    user_id, final_text, img_data
                )

                await utils.send_chunked_reply(message, resp_text, mention_user=True)
                if gif_url:
                    embed = discord.Embed(color=discord.Color.fromrgb(255, 105, 180))
                    embed.set_image(url=gif_url)
                    await message.channel.send(embed=embed)

        except Exception as e:
            log.exception("on_message error for user %s: %s", message.author.id, e)
            try:
                await message.reply("something broke rn try again")
            except Exception:
                pass

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="ask", description="Ask Yuri a Yes/No question.")
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        # FIX (MEDIUM): DM guard.
        # Without this check, /ask in a DM reaches guild-dependent logic in
        # get_combined_response (owner check, grudge lookup) with
        # interaction.guild == None, causing silent misbehaviour.
        if not interaction.guild:
            await interaction.response.send_message(
                "use this in a server bestie, not in my dms 💀", ephemeral=True
            )
            return

        await interaction.response.defer()
        safe_q = utils.sanitize_for_prompt(question)
        response, _ = await self.get_combined_response(
            interaction.user.id,
            None,
            prompt_override=f"Answer this yes/no question sassily: {safe_q}",
        )
        await utils.send_chunked_reply(interaction, f"**Q:** {question}\n**A:** {response}")

    @app_commands.command(name="rename", description="Give someone a chaotic nickname.")
    async def rename(self, interaction: discord.Interaction, member: discord.Member) -> None:
        # FIX (MEDIUM): DM guard.
        # /rename calls interaction.guild.me.top_role and member.edit(nick=...) —
        # both raise AttributeError when interaction.guild is None (DM context).
        if not interaction.guild:
            await interaction.response.send_message(
                "this only works in a server 💀", ephemeral=True
            )
            return

        await interaction.response.defer()

        if interaction.guild.me.top_role <= member.top_role:
            await interaction.followup.send("They are too powerful (Role Hierarchy).")
            return

        safe_name = utils.sanitize_for_prompt(member.display_name)
        raw, _ = await self.get_combined_response(
            interaction.user.id,
            None,
            prompt_override=f"Reply with ONLY a funny/mean nickname for {safe_name}. Max 2 words.",
        )
        new_nick = raw.replace('"', "").strip()[:32]
        try:
            await member.edit(nick=new_nick)
            await interaction.followup.send(f"You are now **{new_nick}** ✨")
        except discord.Forbidden:
            await interaction.followup.send(f"I chose **{new_nick}**, but Discord blocked me.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AI(bot))
