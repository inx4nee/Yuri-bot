"""Function-calling tools for Gemini.

Defines a set of tools the model can call during generation:
  - web_search: replaces the regex-based _SEARCH_TRIGGER_RE heuristic
  - get_time_in_timezone: returns current time in a named timezone
  - calculate: evaluates a math expression safely

Each tool has:
  1. A FunctionDeclaration (sent to Gemini so it knows the tool exists)
  2. A Python handler that executes the tool call

The AI cog passes these tool declarations to Gemini via GenerateContentConfig.tools.
When Gemini responds with a FunctionCall, the cog dispatches it to the matching handler.
"""
import asyncio
import logging
import math
import re
from typing import Any, Optional

import pytz

import utils

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Tool: web_search
# ------------------------------------------------------------------

WEB_SEARCH_DECL = {
    "name": "web_search",
    "description": (
        "Search the web for up-to-date information (news, prices, facts, current events). "
        "Use this when the user asks about something that may have changed recently or "
        "that you don't have reliable knowledge about. Returns 2 short snippets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query — be specific and concise.",
            },
        },
        "required": ["query"],
    },
}


async def web_search(query: str) -> str:
    """Run a DuckDuckGo text search and return formatted results."""
    result = await utils.search_web(query)
    return result or "No web results found."


# ------------------------------------------------------------------
# Tool: get_time_in_timezone
# ------------------------------------------------------------------

_TIMEZONE_DECL = {
    "name": "get_time_in_timezone",
    "description": (
        "Get the current date and time in a specific timezone. Use this when the user "
        "asks what time it is somewhere, or needs time-related context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": (
                    "A timezone name like 'Asia/Kolkata', 'America/New_York', 'Europe/London', "
                    "'Asia/Tokyo'. If unsure, use 'UTC'."
                ),
            },
        },
        "required": ["timezone"],
    },
}


def get_time_in_timezone(timezone: str) -> str:
    """Return the current time in the named timezone."""
    import datetime
    try:
        tz = pytz.timezone(timezone)
    except pytz.UnknownTimeZoneError:
        # Try a fuzzy match — common city names
        common = {
            "india": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
            "ist": "Asia/Kolkata",
            "japan": "Asia/Tokyo", "tokyo": "Asia/Tokyo", "jst": "Asia/Tokyo",
            "us": "America/New_York", "new york": "America/New_York", "est": "America/New_York",
            "pst": "America/Los_Angeles", "los angeles": "America/Los_Angeles",
            "uk": "Europe/London", "london": "Europe/London", "gmt": "Europe/London",
            "paris": "Europe/Paris", "cet": "Europe/Paris",
            "dubai": "Asia/Dubai", "gst": "Asia/Dubai",
            "australia": "Australia/Sydney", "sydney": "Australia/Sydney",
        }
        tz_name = common.get(timezone.lower().strip())
        if tz_name is None:
            return f"Unknown timezone '{timezone}'. Try 'Asia/Kolkata' or 'America/New_York'."
        tz = pytz.timezone(tz_name)
    now = datetime.datetime.now(tz)
    return now.strftime("%A, %B %d, %Y — %I:%M %p")


# ------------------------------------------------------------------
# Tool: calculate
# ------------------------------------------------------------------

# Safe math: only allow numbers, operators, parentheses, decimal points, and
# a whitelist of math functions. No names, no imports, no attribute access.
_ALLOWED_MATH = re.compile(
    r"^[\d\s\+\-\*\/\(\)\.\,]"
    r"*(?:sqrt|sin|cos|tan|log|log10|exp|pi|e|abs|round|floor|ceil|pow|min|max)?"
    r"[\d\s\+\-\*\/\(\)\.\,]*$",
    re.IGNORECASE,
)

_CALC_DECL = {
    "name": "calculate",
    "description": (
        "Evaluate a math expression and return the result. Supports +, -, *, /, "
        "parentheses, and functions: sqrt, sin, cos, tan, log, exp, pow, abs, round, "
        "floor, ceil, min, max. Constants: pi, e."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "The math expression to evaluate, e.g. '2 + 3 * 4' or 'sqrt(144)'.",
            },
        },
        "required": ["expression"],
    },
}


def calculate(expression: str) -> str:
    """Safely evaluate a math expression.

    Uses a restricted eval with a whitelist of math functions. This is NOT
    a general eval — names, imports, and attribute access are blocked by the
    regex pre-filter and the limited globals.
    """
    expr = expression.strip()
    if not expr:
        return "Error: empty expression"

    # Pre-filter: reject anything that doesn't look like a math expression
    if not _ALLOWED_MATH.match(expr.replace("sqrt", "").replace("sin", "")
                               .replace("cos", "").replace("tan", "")
                               .replace("log", "").replace("exp", "")
                               .replace("abs", "").replace("round", "")
                               .replace("floor", "").replace("ceil", "")
                               .replace("pow", "").replace("min", "")
                               .replace("max", "").replace("pi", "")
                               .replace("e", "")):
        return f"Error: expression contains disallowed characters"

    safe_globals = {
        "__builtins__": {},
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log10": math.log10, "exp": math.exp,
        "pi": math.pi, "e": math.e,
        "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
        "pow": pow, "min": min, "max": max,
    }
    try:
        result = eval(expr, safe_globals, {})  # noqa: S307 — restricted eval
        return f"{result}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

# Maps tool name → (declaration dict, handler)
TOOL_REGISTRY = {
    "web_search":             (WEB_SEARCH_DECL,    web_search),
    "get_time_in_timezone":   (_TIMEZONE_DECL,     get_time_in_timezone),
    "calculate":              (_CALC_DECL,         calculate),
}


async def dispatch_tool(name: str, args: dict) -> str:
    """Dispatch a tool call by name. Returns the tool's string result.

    Async handlers are awaited; sync handlers are run in a thread to avoid
    blocking the event loop.
    """
    if name not in TOOL_REGISTRY:
        return f"Error: unknown tool '{name}'"
    _, handler = TOOL_REGISTRY[name]
    try:
        if asyncio.iscoroutinefunction(handler):
            result = await handler(**args)
        else:
            result = await asyncio.to_thread(handler, **args)
        return str(result)
    except Exception as e:
        log.warning("tool '%s' failed: %s: %s", name, type(e).__name__, e)
        return f"Error: {type(e).__name__}: {e}"


def get_tool_declarations() -> list[dict]:
    """Return the list of FunctionDeclaration dicts for Gemini."""
    return [decl for decl, _ in TOOL_REGISTRY.values()]
