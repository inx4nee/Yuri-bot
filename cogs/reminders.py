"""Reminders cog — `/remind` slash command + periodic delivery sweep.

Reminders are persisted in MongoDB so they survive bot restarts (unlike the
previous in-memory hotornot approach). A 30-second sweep loop checks for
due reminders and DMs the user, falling back to a channel ping if DMs are
disabled.
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands

import re
import logging
import datetime
from typing import Optional

import utils

log = logging.getLogger(__name__)


# --- Time parser ---
# Matches things like: "10m", "2h", "1d", "30s", "1h30m", "2d4h", "90 minutes"
# Also accepts the long forms: min, mins, hr, hrs, sec, secs, day, days
_TIME_RE = re.compile(
    r"(?:(\d+)\s*(?:d|days?))?"
    r"(?:(\d+)\s*(?:h|hrs?|hours?))?"
    r"(?:(\d+)\s*(?:m|mins?|minutes?))?"
    r"(?:(\d+)\s*(?:s|secs?|seconds?))?",
    re.IGNORECASE,
)

MAX_REMINDER_DELAY_SECS  = 30 * 24 * 3600   # 30 days cap (matches chat history TTL)
MAX_REMINDER_MSG_CHARS   = 800
REMINDER_SWEEP_SECS      = 15


def parse_time_to_seconds(text: str) -> Optional[int]:
    """Parse a human-friendly duration string like '1h30m' into seconds.

    Returns None if the input is empty, unparseable, or zero.
    """
    if not text:
        return None
    text = text.strip().lower()
    # Allow bare integers to be treated as minutes ("30" → 30 min)
    if text.isdigit():
        mins = int(text)
        return mins * 60 if mins > 0 else None

    match = _TIME_RE.fullmatch(text)
    if not match:
        return None
    d, h, m, s = (int(g) if g else 0 for g in match.groups())
    total = d * 86400 + h * 3600 + m * 60 + s
    if total <= 0:
        return None
    return total


class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._sweep.start()

    def cog_unload(self) -> None:
        self._sweep.cancel()

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="remind",
        description="Set a reminder. I'll DM you when it's time.",
    )
    @app_commands.describe(
        time="When to remind you (e.g. '30m', '2h', '1d', '1h30m', or bare minutes)",
        message="What to remind you about (max 800 chars).",
    )
    @app_commands.checks.cooldown(1, 10.0)  # max 1 reminder per 10s per user
    async def remind(
        self,
        interaction: discord.Interaction,
        time: str,
        message: str,
    ) -> None:
        """Set a reminder. Delivery is via DM (falls back to channel ping)."""
        await interaction.response.defer(ephemeral=True)

        secs = parse_time_to_seconds(time)
        if secs is None:
            await interaction.followup.send(
                "i couldn't parse that time bestie 💀 try `30m`, `2h`, `1d`, or `1h30m`.",
                ephemeral=True,
            )
            return

        if secs > MAX_REMINDER_DELAY_SECS:
            await interaction.followup.send(
                f"that's too far in the future 💀 max is 30 days. "
                f"(you asked for {secs // 86400} days)",
                ephemeral=True,
            )
            return

        if len(message) > MAX_REMINDER_MSG_CHARS:
            await interaction.followup.send(
                f"keep the message under {MAX_REMINDER_MSG_CHARS} chars bestie 💀",
                ephemeral=True,
            )
            return

        deliver_at = utils.utcnow() + datetime.timedelta(seconds=secs)

        await self.bot.reminders_collection.insert_one({
            "user_id":     interaction.user.id,
            "username":    interaction.user.name,
            "channel_id":  interaction.channel_id,
            "guild_id":    interaction.guild_id,
            "message":     message,
            "deliver_at":  deliver_at,
            "created_at":  utils.utcnow(),
        })

        # Build a human-readable summary of the delay
        parts = []
        days, rem = divmod(secs, 86400)
        hrs, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        if days: parts.append(f"{days}d")
        if hrs:  parts.append(f"{hrs}h")
        if mins: parts.append(f"{mins}m")
        if secs: parts.append(f"{secs}s")
        delay_str = " ".join(parts) or f"{secs}s"

        await interaction.followup.send(
            f"⏰ ok i'll remind you in **{delay_str}**. "
            f"make sure your DMs are open or i'll ping you here instead.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Sweep loop
    # ------------------------------------------------------------------

    @tasks.loop(seconds=REMINDER_SWEEP_SECS)
    async def _sweep(self) -> None:
        """Deliver any reminders whose deliver_at has passed."""
        try:
            now = utils.utcnow()
            cursor = self.bot.reminders_collection.find({"deliver_at": {"$lte": now}})
            async for doc in cursor:
                await self._deliver_reminder(doc)
                await self.bot.reminders_collection.delete_one({"_id": doc["_id"]})
        except Exception as e:
            log.warning("reminder sweep error: %s", e)

    @_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver_reminder(self, doc: dict) -> None:
        """DM the user; fall back to pinging them in the original channel."""
        user_id = doc["user_id"]
        message = doc.get("message", "")
        # Sanitize the stored message before echoing — defensive against any
        # mention-shaped text the user themselves may have included at create time
        safe_msg = utils.sanitize_for_discord(message)
        created_at = doc.get("created_at")

        embed = discord.Embed(
            title="⏰ REMINDER",
            description=safe_msg or "(no message)",
            color=discord.Color.from_rgb(255, 105, 180),
            timestamp=utils.utcnow(),
        )
        if created_at:
            embed.set_footer(text=f"set {created_at.strftime('%b %d, %H:%M')} UTC")

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.NotFound:
                log.warning("reminder: user %s no longer exists.", user_id)
                return

        # Try DM first
        try:
            await user.send(embed=embed)
            return
        except discord.Forbidden:
            pass  # DMs disabled — fall through to channel ping
        except Exception as e:
            log.warning("reminder DM failed for user %s: %s", user_id, e)

        # Fallback: ping in the original channel
        channel_id = doc.get("channel_id")
        if channel_id is None:
            log.warning("reminder: no channel fallback for user %s.", user_id)
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                log.warning("reminder: channel %s no longer accessible.", channel_id)
                return

        try:
            await channel.send(
                content=f"hey <@{user_id}>, you asked me to remind you:",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.Forbidden:
            log.warning("reminder: no permission to post in channel %s.", channel_id)
        except Exception as e:
            log.warning("reminder channel fallback failed: %s", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reminders(bot))
