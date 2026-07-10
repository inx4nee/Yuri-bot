import io
import re
import logging
import datetime
import random
import asyncio
from typing import Optional

import pytz
import aiohttp
from PIL import Image
from duckduckgo_search import DDGS
import discord

# --- Module-level logger ---
log = logging.getLogger(__name__)

MAX_IMAGE_BYTES  = 8 * 1024 * 1024   # 8 MB — download size cap for images
MAX_IMAGE_PIXELS = 5_000 * 5_000     # decompression bomb guard (25 MP)
CHUNK_SIZE       = 1_900             # Discord message length limit with headroom
HISTORY_MSG_MAX  = 400               # truncate (don't drop) long messages in history recap

# --- UTC helper (Py 3.12+ deprecates datetime.utcnow()) ---
UTC = datetime.timezone.utc


def utcnow() -> datetime.datetime:
    """Timezone-aware UTC now. Use everywhere instead of datetime.utcnow()."""
    return datetime.datetime.now(UTC)


# --- Shared aiohttp session (reused across all HTTP calls) ---
_session: Optional[aiohttp.ClientSession] = None


def get_session() -> aiohttp.ClientSession:
    """Return a process-wide shared aiohttp ClientSession.

    Created lazily on first use. Caller must NOT close it — closed automatically
    on bot shutdown via close_session().
    """
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "YuriBot/1.0"},
        )
    return _session


async def close_session() -> None:
    """Close the shared aiohttp session. Call on bot shutdown."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


_UNICODE_BRACKET_TABLE = str.maketrans({
    "\u3010": "[",   # [
    "\u3011": "]",   # ]
    "\u3014": "[",   # [
    "\u3015": "]",   # ]
    "\u300A": "<",   # <
    "\u300B": ">",   # >
    "\u300C": "[",   # [
    "\u300D": "]",   # ]
    "\uFF3B": "[",   # [ (fullwidth)
    "\uFF3D": "]",   # ] (fullwidth)
    "\uFF1C": "<",   # < (fullwidth)
    "\uFF1E": ">",   # > (fullwidth)
})

# Patterns that are only sent by someone trying to manipulate the model,
# never in normal conversation.
_INJECTION_RE = re.compile(
    r"<\s*(system|prompt|inst|assistant|user)\b[^>]*>"   # XML-style tags
    r"|"
    r"\[\s*/?\s*(SYSTEM|INST|PROMPT|ASSISTANT|USER)\s*\]"  # bracket-style tags
    r"|"
    r"\bignore\s+(previous|all|your)\s+instructions?\b",   # natural-language reset
    re.IGNORECASE,
)


# Matches Discord mentions that can ping: @everyone, @here, user/role/channel pings.
# We break the @ symbol with a zero-width space so Discord won't render them as pings.
_MENTION_RE = re.compile(
    r"@(everyone|here)\b"                       # @everyone / @here
    r"|"
    r"<@!?\d+>"                                # <@123> / <@!123>  (user)
    r"|"
    r"<@&\d+>"                                 # <@&123>           (role)
    r"|"
    r"<#\d+>"                                  # <#123>            (channel)
)


def sanitize_for_discord(text: str) -> str:
    """Make *text* safe to embed in a Discord message or embed.

    Neutralises every form of Discord mention so user-supplied content can never
    ping @everyone, a role, or an arbitrary user when surfaced through the bot.
    Use this for any embed description / message body that interpolates raw user
    input (e.g. /confess, /hotornot, /poll).
    """
    if not text:
        return ""
    text = str(text)

    def _break(match: re.Match) -> str:
        token = match.group(0)
        # Replace the leading @ or < with a version that contains a zero-width
        # space — Discord will display it but will NOT trigger a notification.
        if token.startswith("@"):
            return "@\u200b" + token[1:]
        return "<\u200b" + token[1:]

    return _MENTION_RE.sub(_break, text)


def sanitize_for_prompt(text: str) -> str:
    """Escape user input before interpolating it into a model prompt.

    Returns a cleaned string safe for embedding inside [USER_INPUT]...[/USER_INPUT]
    wrappers, or a sentinel string if a prompt-injection attempt is detected.
    """
    if not text:
        return ""

    text = str(text)

    # Step 1: normalise Unicode lookalikes - ASCII so pattern matching works
    text = text.translate(_UNICODE_BRACKET_TABLE)

    # Step 2: detect injection before escaping
    if _INJECTION_RE.search(text):
        log.warning("Prompt injection attempt detected and blocked.")
        return "[message removed: injection attempt detected]"

    # Step 3: escape remaining structural characters
    text = text.replace("[", r"\[").replace("]", r"\]")
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    return text


# --- Image helpers ---

async def get_image_from_url(url: str) -> Optional[Image.Image]:
    """Download an image from *url* with a hard size cap.

    Returns a PIL Image on success, None if the download fails, the URL
    returns a non-200 status, or the payload exceeds MAX_IMAGE_BYTES.
    Uses the process-wide shared aiohttp session.
    """
    try:
        session = get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                return None

            # Reject oversized payloads before streaming
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                log.warning("Image rejected: Content-Length %s exceeds limit.", content_length)
                return None

            data = bytearray()
            async for chunk in resp.content.iter_chunked(1024):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    log.warning("Image rejected: streamed size exceeded %d bytes.", MAX_IMAGE_BYTES)
                    return None

            return Image.open(io.BytesIO(data))

    except Exception as e:
        log.warning("Image download error from %s: %s", url, e)
        return None


def stitch_images(img1_data: Image.Image, img2_data: Image.Image) -> Optional[Image.Image]:
    """Combine two PIL images side-by-side at a standard height of 512 px.

    Returns the stitched PIL Image, or None on any error.
    """
    try:
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

        # Decompression bomb guard - reject suspiciously large source images
        if (
            img1_data.width > 5_000 or img1_data.height > 5_000
            or img2_data.width > 5_000 or img2_data.height > 5_000
        ):
            log.warning("stitch_images: one or both source images exceed the size limit.")
            return None

        base_height = 512

        ratio1 = base_height / float(img1_data.size[1])
        w1     = int(float(img1_data.size[0]) * ratio1)
        img1   = img1_data.resize((w1, base_height), Image.Resampling.BICUBIC)

        ratio2 = base_height / float(img2_data.size[1])
        w2     = int(float(img2_data.size[0]) * ratio2)
        img2   = img2_data.resize((w2, base_height), Image.Resampling.BICUBIC)

        result = Image.new("RGB", (w1 + w2, base_height))
        result.paste(img1, (0,  0))
        result.paste(img2, (w1, 0))
        return result

    except Exception as e:
        log.error("stitch_images error: %s", e)
        return None


# --- Time & search helpers ---

def get_smart_time(text_input: str) -> str:
    """Return a localised time string inferred from the language of *text_input*."""
    utc_now = datetime.datetime.now(pytz.utc)

    # Hindi / Hinglish / Bengali script or common Hinglish words -> IST
    if (
        re.search(r"[\u0900-\u097F]", text_input)   # Devanagari
        or re.search(r"[\u0980-\u09FF]", text_input) # Bengali
        or any(
            word in text_input.lower()
            for word in ["kya", "kab", "hai", "bhai", "samay", "baj", "baje"]
        )
    ):
        local = utc_now.astimezone(pytz.timezone("Asia/Kolkata"))
        return f"{local.strftime('%I:%M %p')} (IST)"

    # Japanese script -> JST
    if re.search(r"[\u3040-\u309F\u30A0-\u30FF]", text_input):
        local = utc_now.astimezone(pytz.timezone("Asia/Tokyo"))
        return f"{local.strftime('%I:%M %p')} (JST)"

    # Default -> IST with full date
    local = utc_now.astimezone(pytz.timezone("Asia/Kolkata"))
    return f"{local.strftime('%A, %B %d, %I:%M %p')} (IST)"


async def search_web(query: str) -> Optional[str]:
    """Run a DuckDuckGo text search and return a formatted context block.

    Returns None if the search yields no results or raises an exception.
    """
    try:
        results = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=2))
        )
        if not results:
            return None

        context = "\n[SYSTEM: WEB SEARCH RESULTS]\n"
        for res in results:
            context += (
                f"- Title: {sanitize_for_prompt(res['title'])}\n"
                f"  Snippet: {sanitize_for_prompt(res['body'])}\n"
            )
        return context

    except Exception as e:
        log.warning("Web search error for query '%s': %s", query, e)
        return None


async def search_gif_ddg(query: str) -> Optional[str]:
    """Search DuckDuckGo Images for a GIF and return a random result URL."""
    try:
        results = await asyncio.to_thread(
            lambda: list(DDGS().images(keywords=query, type_image="gif", max_results=8))
        )
        if results:
            return random.choice(results)["image"]
    except Exception as e:
        log.warning("GIF search error for query '%s': %s", query, e)
    return None


async def process_gif_tags(text: str) -> tuple[str, Optional[str]]:
    """Extract a [GIF: …] tag from *text*, search for the GIF, and strip the tag.

    Returns (cleaned_text, gif_url). gif_url is None when no tag is present or
    the search returns no results.
    """
    match = re.search(r"\[GIF:\s*(.*?)\]", text, re.IGNORECASE)
    gif_url: Optional[str] = None
    if match:
        query   = match.group(1).strip()
        gif_url = await search_gif_ddg(query)
        text    = text.replace(match.group(0), "").strip()
    return text, gif_url


async def fetch_channel_messages(
    channel: discord.abc.Messageable,
    *,
    fetch_limit: int = 100,
    keep_limit:  int = 20,
    timeout_secs: float = 8.0,
) -> list[str]:
    """Return up to *keep_limit* recent non-bot messages from *channel*.

    Each entry is a sanitised "DisplayName: message" string ready for prompt
    injection. Messages are returned in chronological order (oldest first).
    Raises nothing — on timeout or any error the partial list is returned.
    """
    messages: list[str] = []
    try:
        async with asyncio.timeout(timeout_secs):
            async for msg in channel.history(limit=fetch_limit):
                if msg.author.bot:
                    continue
                safe = sanitize_for_prompt(msg.content)
                if safe.strip():
                    messages.append(f"{msg.author.display_name}: {safe}")
                if len(messages) >= keep_limit:
                    break
    except asyncio.TimeoutError:
        log.warning(
            "fetch_channel_messages timed out after %.1fs in channel %s — "
            "returning %d message(s) collected so far.",
            timeout_secs, getattr(channel, "id", "?"), len(messages),
        )
    except Exception as e:
        log.warning("fetch_channel_messages error in channel %s: %s",
                    getattr(channel, "id", "?"), e)

    messages.reverse()  # chronological order: oldest -> newest
    return messages


# --- Discord helpers ---

async def send_chunked_reply(
    destination,
    text: str,
    *,
    mention_user: bool = False,
) -> None:
    """Send *text* to *destination*, splitting into CHUNK_SIZE chunks if needed.

    *destination* may be a discord.Message (uses .reply on the first chunk),
    a discord.Interaction (uses .followup.send), or any object with .send().
    """
    if not text:
        return

    chunks = [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]

    for i, chunk in enumerate(chunks):
        try:
            if hasattr(destination, "reply") and i == 0:
                await destination.reply(chunk, mention_author=mention_user)
            elif hasattr(destination, "followup"):
                await destination.followup.send(chunk)
            elif hasattr(destination, "send"):
                await destination.send(chunk)
            else:
                await destination.channel.send(chunk)
        except Exception as e:
            log.warning("send_chunked_reply failed on chunk %d: %s", i, e)


def get_user_dossier(member: discord.Member, include_presence: bool = True) -> str:
    """Build a short text profile of *member* for use in AI prompts.

    When *include_presence* is False, rich-presence details (Spotify, games,
    custom status) are omitted — used when a user has opted out via /privacy.
    """
    now         = utcnow()
    # member.created_at is timezone-aware (UTC) from discord.py
    created_at  = member.created_at if member.created_at.tzinfo else member.created_at.replace(tzinfo=UTC)
    age_days    = (now - created_at).days
    years       = age_days // 365

    roles     = [r.name for r in member.roles if r.name != "@everyone"]
    roles_str = sanitize_for_prompt(", ".join(roles) if roles else "No Roles")

    if not include_presence:
        return (
            f"METADATA (Use ONLY if funny):\n"
            f"- Name: {sanitize_for_prompt(member.display_name)}\n"
            f"- Account Age: {years} year(s), {age_days % 365} day(s) old.\n"
            f"- Roles: {roles_str}\n"
            f"- Status: (hidden by user privacy opt-out)\n"
        )

    status = str(member.status).upper()

    activity = "None"
    if member.activity:
        if isinstance(member.activity, discord.Spotify):
            activity = (
                f"Listening to {sanitize_for_prompt(member.activity.title)} "
                f"by {sanitize_for_prompt(member.activity.artist)}"
            )
        elif isinstance(member.activity, discord.Game):
            activity = f"Playing {sanitize_for_prompt(member.activity.name)}"
        elif isinstance(member.activity, discord.CustomActivity):
            activity = f"Custom Status: '{sanitize_for_prompt(str(member.activity.name))}'"

    return (
        f"METADATA (Use ONLY if funny):\n"
        f"- Name: {sanitize_for_prompt(member.display_name)}\n"
        f"- Account Age: {years} year(s), {age_days % 365} day(s) old.\n"
        f"- Roles: {roles_str}\n"
        f"- Status: {status} | Doing: {activity}\n"
    )


async def get_user_history_text(
    collection,
    user_id: int,
    *,
    limit: int = 15,
) -> str:
    """Fetch recent conversation from MongoDB and format it for an AI prompt.

    Fetches BOTH user messages and Yuri's replies so social commands like
    /roast, /rate, /ship, and /compatibility can see the full relationship
    dynamic — not just what the user said, but how Yuri responded to them.
    Each line is labelled "User:" or "Yuri:" for clarity.

    Returns a bullet-list string, or a fallback message if no history exists.
    """
    cursor = (
        collection
        .find({"user_id": user_id}, {"parts": 1, "role": 1, "_id": 0})
        .sort("timestamp", -1)
        .limit(limit)
    )
    messages: list[str] = []
    async for doc in cursor:
        content = doc.get("parts", [""])[0]
        role    = doc.get("role", "user")
        label   = "Yuri" if role == "model" else "User"
        if isinstance(content, str) and content.strip():
            # Truncate over-long messages instead of dropping them entirely —
            # a long message often carries the most context.
            if len(content) > HISTORY_MSG_MAX:
                content = content[:HISTORY_MSG_MAX] + "…"
            messages.append(f"{label}: {sanitize_for_prompt(content)}")

    if not messages:
        return "No recent chat history found."

    return "\n".join(f"- {m}" for m in reversed(messages))
