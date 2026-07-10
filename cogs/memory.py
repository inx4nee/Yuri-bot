"""Long-term memory summarization cog.

When chat history ages out (30-day TTL), this cog summarizes it into a
permanent "dossier" stored in a separate collection. Yuri remembers users
forever without unbounded DB growth.

How it works:
  - A periodic task (every 6 hours) scans for users whose chat history is
    about to expire (older than 25 days).
  - For each such user, it summarizes their oldest messages into a short
    dossier paragraph using Gemini.
  - The dossier is stored in `memory_dossiers` (keyed by user_id) and
    appended to over time as more conversations age out.
  - When building prompts, the AI cog can include the dossier as context.

The dossier is a concise, model-generated summary — NOT the raw messages.
This keeps storage O(1) per user regardless of how many conversations
they've had.
"""
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types

import os
import datetime
import logging
from typing import Optional

import utils

log = logging.getLogger(__name__)


# How old must messages be before we summarize them? (Must be < 30 day TTL)
SUMMARIZE_AGE_DAYS = 25
# How often to run the sweep
SWEEP_INTERVAL_HOURS = 6
# Max messages to summarize in one batch
MAX_MESSAGES_PER_SUMMARY = 60
# Max dossier length (chars) — append-based, so we cap total growth
MAX_DOSSIER_CHARS = 2000
# Gemini model for summarization
SUMMARY_MODEL = "gemini-2.0-flash"


class MemorySummarizer(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Lazily create the Gemini client (shared with AI cog's key)
        self._gemini_client: Optional[genai.Client] = None
        self._sweep.start()

    def cog_unload(self) -> None:
        self._sweep.cancel()

    @property
    def gemini_client(self) -> Optional[genai.Client]:
        if self._gemini_client is None:
            key = os.getenv("GEMINI_API_KEY")
            if key:
                self._gemini_client = genai.Client(api_key=key)
        return self._gemini_client

    # ------------------------------------------------------------------
    # Public API — used by the AI cog to fetch a user's dossier
    # ------------------------------------------------------------------

    async def get_user_dossier_text(self, user_id: int) -> str:
        """Return the stored long-term dossier for *user_id*, or empty string.

        The AI cog can prepend this to the conversation context so Yuri
        "remembers" users even after their raw chat history has expired.
        """
        doc = await self.bot.memory_dossiers_col.find_one(
            {"user_id": user_id}, {"dossier": 1, "_id": 0}
        )
        if doc is None:
            return ""
        return doc.get("dossier", "")

    # ------------------------------------------------------------------
    # Sweep loop
    # ------------------------------------------------------------------

    @tasks.loop(hours=SWEEP_INTERVAL_HOURS)
    async def _sweep(self) -> None:
        """Summarize soon-to-expire chat history into permanent dossiers."""
        try:
            await self._run_summarization_pass()
        except Exception as e:
            log.warning("memory summarization sweep error: %s", e)

    @_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_summarization_pass(self) -> None:
        """Find users with old messages and summarize them.

        Strategy: find messages older than SUMMARIZE_AGE_DAYS that haven't
        been marked as "summarized" yet. Group by user, summarize each
        batch, append to the user's dossier, then mark them summarized.
        """
        cutoff = utils.utcnow() - datetime.timedelta(days=SUMMARIZE_AGE_DAYS)

        # Find distinct users with unsummarized old messages
        pipeline = [
            {"$match": {
                "timestamp": {"$lt": cutoff},
                "summarized": {"$ne": True},
            }},
            {"$group": {"_id": "$user_id"}},
            {"$limit": 50},  # cap work per sweep pass
        ]
        cursor = self.bot.chat_collection.aggregate(pipeline)
        user_ids = [doc["_id"] async for doc in cursor]

        if not user_ids:
            return

        log.info("memory summarization: processing %d user(s)", len(user_ids))

        for user_id in user_ids:
            try:
                await self._summarize_user_history(user_id, cutoff)
            except Exception as e:
                log.warning("memory summarization failed for user %s: %s", user_id, e)

    async def _summarize_user_history(self, user_id: int, cutoff: datetime.datetime) -> None:
        """Summarize old messages for a single user and append to their dossier."""
        # Fetch the oldest unsummarized messages for this user
        cursor = (
            self.bot.chat_collection
            .find({
                "user_id": user_id,
                "timestamp": {"$lt": cutoff},
                "summarized": {"$ne": True},
            }, {"parts": 1, "role": 1, "timestamp": 1, "_id": 1})
            .sort("timestamp", 1)
            .limit(MAX_MESSAGES_PER_SUMMARY)
        )
        docs = [doc async for doc in cursor]
        if not docs:
            return

        # Build a transcript for the summarizer
        transcript_lines = []
        doc_ids = []
        for doc in docs:
            role = "Yuri" if doc.get("role") == "model" else "User"
            content = doc.get("parts", [""])[0]
            if isinstance(content, str) and content.strip():
                transcript_lines.append(f"{role}: {content[:300]}")
            doc_ids.append(doc["_id"])

        if not transcript_lines:
            # All messages were empty — just mark them summarized
            await self.bot.chat_collection.update_many(
                {"_id": {"$in": doc_ids}},
                {"$set": {"summarized": True}},
            )
            return

        transcript = "\n".join(transcript_lines)

        # Fetch the existing dossier so we can append to it
        existing_dossier = await self.get_user_dossier_text(user_id)

        # Generate the summary
        new_summary = await self._generate_summary(transcript, existing_dossier)
        if not new_summary:
            # Summarization failed — don't mark as summarized, retry next pass
            return

        # Append + cap
        if existing_dossier:
            updated_dossier = (existing_dossier + "\n\n" + new_summary)[:MAX_DOSSIER_CHARS]
        else:
            updated_dossier = new_summary[:MAX_DOSSIER_CHARS]

        # Upsert the dossier
        await self.bot.memory_dossiers_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "dossier": updated_dossier,
                "updated_at": utils.utcnow(),
            }},
            upsert=True,
        )

        # Mark the summarized messages so we don't re-process them
        await self.bot.chat_collection.update_many(
            {"_id": {"$in": doc_ids}},
            {"$set": {"summarized": True}},
        )

        log.info("memory summarization: updated dossier for user %s (%d messages summarized)",
                 user_id, len(doc_ids))

    async def _generate_summary(self, transcript: str, existing_dossier: str) -> Optional[str]:
        """Use Gemini to summarize a conversation transcript into a dossier entry.

        The summary is written from Yuri's perspective — her impression of
        the user, their relationship dynamic, memorable topics, etc.
        """
        client = self.gemini_client
        if client is None:
            return None

        prompt = (
            "You are Yuri, a chaotic gen-z Discord bot. Below is a transcript of "
            "your recent conversation with a user. Summarize it into a short "
            "(3-5 sentence) 'dossier' entry written from your perspective — your "
            "impression of them, the vibe, memorable things they said, how you "
            "two get along. Keep it lowercase, gen-z, in-character. This dossier "
            "will be used as long-term memory so you can remember them later.\n\n"
        )
        if existing_dossier:
            prompt += (
                "EXISTING DOSSIER (build on this, don't repeat):\n"
                f"{existing_dossier}\n\n"
            )
        prompt += f"NEW CONVERSATION TRANSCRIPT:\n{transcript}\n\n"
        prompt += "DOSSIER ENTRY:"

        try:
            config = types.GenerateContentConfig(
                max_output_tokens=300,
                temperature=0.7,
            )
            response = await client.aio.models.generate_content(
                model=SUMMARY_MODEL,
                contents=prompt,
                config=config,
            )
            text = response.text.strip()
            return text if text else None
        except Exception as e:
            log.warning("memory summarization: Gemini call failed: %s", e)
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemorySummarizer(bot))
