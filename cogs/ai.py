import discord
from discord.ext import commands
from discord import app_commands

import os
import io
import re
import logging
import asyncio
import datetime
import base64
from typing import Optional

from google import genai
from google.genai import types
from groq import AsyncGroq

try:
    from together import AsyncTogether
    _TOGETHER_AVAILABLE = True
except ImportError:
    AsyncTogether = None          # type: ignore[assignment,misc]
    _TOGETHER_AVAILABLE = False

import utils

from cogs.prompts import SYSTEM_PROMPT

log = logging.getLogger(__name__)

USER_COOLDOWN_SECS   = 3     # minimum seconds between responses to the same user
GUILD_COOLDOWN_SECS  = 1     # minimum seconds between responses in the same server
MAX_INPUT_CHARS      = 2000  # hard cap on incoming message length
MAX_HISTORY_MESSAGES = 40    # conversation turns loaded from MongoDB per request
MAX_GROQ_TOKENS      = 256   # max tokens for all Groq completions
MIN_SEARCH_LENGTH    = 15    # messages shorter than this never trigger a web search

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

        # --- Gemini setup (New google-genai SDK) ---
        self.gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.gemini_config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            safety_settings=[
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            ]
        )

        # --- Groq multi-key setup ---
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

        # --- Together AI setup (fine-tuned Yuri model) ---
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

        self._user_cooldowns:  dict[int, datetime.datetime] = {}
        self._guild_cooldowns: dict[int, datetime.datetime] = {}

    # --- Private helpers ---

    def _advance_groq_key(self) -> None:
        if len(self.groq_keys) <= 1:
            return
        self.current_groq_index = (self.current_groq_index + 1) % len(self.groq_keys)
        self.groq_client = AsyncGroq(api_key=self.groq_keys[self.current_groq_index])

    async def _rotate_groq_key(self) -> bool:
        if len(self.groq_keys) <= 1:
            return False
        self.current_groq_index = (self.current_groq_index + 1) % len(self.groq_keys)
        self.groq_client = AsyncGroq(api_key=self.groq_keys[self.current_groq_index])
        log.info("Rotated to Groq key #%d after error.", self.current_groq_index + 1)
        return True

    def _is_user_on_cooldown(self, user_id: int) -> bool:
        now = datetime.datetime.now()
        self._user_cooldowns = {k: v for k, v in self._user_cooldowns.items() if v > now}
        if user_id in self._user_cooldowns:
            return True
        self._user_cooldowns[user_id] = now + datetime.timedelta(seconds=USER_COOLDOWN_SECS)
        return False

    def _is_guild_on_cooldown(self, guild_id: int) -> bool:
        now = datetime.datetime.now()
        self._guild_cooldowns = {k: v for k, v in self._guild_cooldowns.items() if v > now}
        if guild_id in self._guild_cooldowns:
            return True
        self._guild_cooldowns[guild_id] = now + datetime.timedelta(seconds=GUILD_COOLDOWN_SECS)
        return False

    async def _safe_typing_task(self, channel: discord.abc.Messageable):
        """Runs the typing indicator safely in the background."""
        try:
            async with channel.typing():
                # Blocks infinitely until this task is explicitly cancelled by the main thread
                await asyncio.Event().wait() 
        except asyncio.CancelledError:
            pass # Normal exit when AI finishes generating
        except Exception:
            # Silently ignore 429 Too Many Requests or Forbidden errors
            pass

    # --- Audio ---

    async def transcribe_audio(self, file_bytes: bytes, filename: str) -> Optional[str]:
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

    # --- Core AI ---

    async def get_combined_response(
        self,
        user_id:         int,
        text_input:      Optional[str],
        image_input=None,
        prompt_override: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:

        is_grudged = await self.bot.grudge_collection.find_one({"user_id": user_id})
        grudge_prompt = (
            "\n[SYSTEM: You hold a grudge against this user. Be cold/dismissive.]"
            if is_grudged else ""
        )

        cursor = (
            self.bot.chat_collection.find({"user_id": user_id})
            .sort("timestamp", -1)
            .limit(MAX_HISTORY_MESSAGES)
        )
        recent_docs = [doc async for doc in cursor]
        recent_docs.reverse()

        history_db: list[dict] = []
        for doc in recent_docs:
            role = doc.get("role")
            if not history_db and role != "user":
                continue 
            if history_db and history_db[-1]["role"] == role:
                history_db[-1]["parts"][0] += "\n" + doc["parts"][0]
            else:
                history_db.append({"role": role, "parts": [doc["parts"][0]]})
        
        if history_db and history_db[-1]["role"] == "user":
            history_db.pop()

        time_str    = utils.get_smart_time(text_input or "")
        system_data = (
            f"[System: Current Date/Time is {time_str}. "
            f"Do not mention this unless asked.]{grudge_prompt}"
        )

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

        response_text = ""
        successful    = False
        now           = datetime.datetime.now()

        for layer in self.cooldowns:
            if self.cooldowns[layer] and now > self.cooldowns[layer]:
                self.cooldowns[layer] = None

        gemini_history = []
        for m in history_db:
            gemini_history.append(
                types.Content(role=m["role"], parts=[types.Part.from_text(text=m["parts"][0])])
            )

        new_parts = [types.Part.from_text(text=current_text)]
        if image_input:
            buf = io.BytesIO()
            if image_input.mode != "RGB":
                image_input = image_input.convert("RGB")
            image_input.save(buf, format="JPEG")
            new_parts.append(
                types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")
            )
        
        gemini_history.append(types.Content(role="user", parts=new_parts))

        for model_name, layer in [("gemini-2.0-flash", 1), ("gemini-1.5-flash-8b", 2)]:
            if successful:
                break
            if not self.cooldowns[layer]:
                try:
                    response = await self.gemini_client.aio.models.generate_content(
                        model=model_name,
                        contents=gemini_history,
                        config=self.gemini_config
                    )
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

        if not successful:
            response_text = await self.call_groq_fallback(
                history_db, SYSTEM_PROMPT, current_text, image_input
            )

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
        if not self.groq_client:
            return "server dead rn. try again later 💀"

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

    # --- Events ---

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

        if self._is_user_on_cooldown(message.author.id):
            return

        if message.guild and self._is_guild_on_cooldown(message.guild.id):
            return

        try:
            # Start the typing indicator as an independent background task
            typing_task = asyncio.create_task(self._safe_typing_task(message.channel))
            
            try:
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

                if len(final_text) > MAX_INPUT_CHARS:
                    await message.reply(
                        f"that message is way too long bestie 💀 "
                        f"keep it under {MAX_INPUT_CHARS} chars"
                    )
                    return

                resp_text, gif_url = await self.get_combined_response(
                    user_id, final_text, img_data
                )
            finally:
                # Guarantee the typing indicator stops when AI is done processing
                typing_task.cancel()

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

    # --- Slash commands ---

    @app_commands.command(name="ask", description="Ask Yuri a Yes/No question.")
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
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
        if not interaction.guild:
            await interaction.response.send_message(
                "this only works in a server", ephemeral=True
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
        n
