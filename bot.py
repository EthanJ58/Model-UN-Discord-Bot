"""
Model UN Discord Bot
=====================
An AI-driven Discord bot that runs a persistent, turn-based "Model UN" geopolitical
simulation. Players are each assigned a country. They post in-character actions in a
"submission" channel; an LLM reviews, summarizes, and resolves those actions; the world
moves forward on a daily / half-weekly / weekly loop (1 IRL week = 1 in-game year).

This file implements everything described in the project's architecture document:
  - Message pipeline:  Review -> Summarize -> Effect -> Broadcast + Log
  - Event pipeline:     Random Events & scheduled Response Events
  - Simulation loop:    Daily summaries, half-week event seeding, weekly summary + stats

See README.md for setup instructions and for the list of files you must fill in
yourself (.env, config.json AI settings, countries/assigned_nations.csv, and an
initial countries/stats/current_national_statistics.csv seed row per country).

Nothing in this file should need to change for normal use -- behavior is driven by
config.json (hot-reloaded on every use) and the data files under admin/, countries/,
chats/, events/, and summaries/.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# ============================================================================
# PATHS & CONSTANTS
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent

ADMIN_DIR = BASE_DIR / "admin"
ADMIN_RULES_PATH = ADMIN_DIR / "admin_rules.txt"
ADMIN_REMINDERS_PATH = ADMIN_DIR / "admin_reminders.txt"

CHATS_DIR = BASE_DIR / "chats"
ACTIVE_CHAT_LOG = CHATS_DIR / "active_chat_log.csv"
FULL_CHAT_LOG = CHATS_DIR / "full_chat_log.csv"

COUNTRIES_DIR = BASE_DIR / "countries"
ASSIGNED_NATIONS = COUNTRIES_DIR / "assigned_nations.csv"
STATS_DIR = COUNTRIES_DIR / "stats"
CURRENT_STATS = STATS_DIR / "current_national_statistics.csv"
FULL_STATS = STATS_DIR / "full_national_statistics.csv"

EVENTS_DIR = BASE_DIR / "events"
EVENTS_LOG_DIR = EVENTS_DIR / "log"
ACTIVE_EVENT_LOG = EVENTS_LOG_DIR / "active_event_log.csv"
FULL_EVENT_LOG = EVENTS_LOG_DIR / "full_event_log.csv"
EVENTS_UPCOMING_DIR = EVENTS_DIR / "upcoming"
FUTURE_RANDOM_EVENTS = EVENTS_UPCOMING_DIR / "future_random_events.csv"
# NOTE: filename intentionally matches the repo's existing typo ("reponse").
FUTURE_RESPONSE_EVENTS = EVENTS_UPCOMING_DIR / "future_reponse_events.csv"

SUMMARIES_DIR = BASE_DIR / "summaries"
DAILY_SUMMARIES_PATH = SUMMARIES_DIR / "daily_summaries.txt"
WEEKLY_SUMMARIES_PATH = SUMMARIES_DIR / "weekly_summaries.txt"

CONFIG_PATH = BASE_DIR / "config.json"
SIM_TRACKER_PATH = BASE_DIR / "sim_tracker.json"

CHAT_LOG_FIELDS = ["Timestamp", "Message_ID", "Country", "Message"]
EVENT_LOG_FIELDS = ["Timestamp", "Message_ID", "Country/Region/World", "Message"]
FUTURE_EVENT_FIELDS = ["Trigger_Date", "Message_ID", "Country/Region/World", "Category", "Description", "Active"]
STATS_FIELDS = ["Year", "Country", "Population", "GDP", "Budget", "Quality_of_Life", "Stability"]

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DISCORD_MESSAGE_LIMIT = 1900  # leave headroom under Discord's 2000 char cap

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("model_un_bot")

# Mutable runtime state (not persisted -- resets on restart, which is fine since
# downtime is only ever active *during* a live weekly-task run).
DOWNTIME = {"active": False}
_background_loop_started = False

# ============================================================================
# ENV / DISCORD SETUP
# ============================================================================

load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LLM_API_KEY = os.getenv("LLM_API_KEY")
CHANNEL_SUBMIT_ID = int(os.getenv("CHANNEL_SUBMIT_ID") or 0)
CHANNEL_ACTIONS_ID = int(os.getenv("CHANNEL_ACTIONS_ID") or 0)
CHANNEL_EVENT_ID = int(os.getenv("CHANNEL_EVENT_ID") or 0)
CHANNEL_LOG_ID = int(os.getenv("CHANNEL_LOG_ID") or 0)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================================
# FILE I/O HELPERS
# ============================================================================


def read_csv_rows(path: Path) -> list[dict]:
    """Read a CSV into a list of dicts using whatever header the file already has."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv_row(path: Path, fieldnames: list[str], row: dict) -> None:
    file_has_header = path.exists() and path.stat().st_size > 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_has_header:
            writer.writeheader()
        writer.writerow(row)


def rewrite_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def clear_csv(path: Path, fieldnames: list[str]) -> None:
    rewrite_csv(path, fieldnames, [])


def csv_rows_to_text(rows: list[dict], fieldnames: list[str]) -> str:
    """Render rows back to CSV text for embedding directly into an AI prompt."""
    if not rows:
        return "(none)"
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().strip()


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as f:
        if existing:
            f.write("\n\n")
        f.write(content.strip() + "\n")


def overwrite_text(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_config() -> dict:
    """Re-read config.json on every call so admin edits apply without a restart."""
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_tracker() -> dict:
    with SIM_TRACKER_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    now_iso = datetime.now(timezone.utc).isoformat()
    changed = False
    if not data.get("current_in_game_year"):
        data["current_in_game_year"] = 1
        changed = True
    for key in ("last_daily_summary_utc", "last_half_week_event_utc", "last_weekly_summary_and_stats_utc"):
        if not data.get(key):
            data[key] = now_iso
            changed = True
    if changed:
        save_tracker(data)
    return data


def save_tracker(data: dict) -> None:
    with SIM_TRACKER_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ============================================================================
# AI / LLM CLIENT  (OpenAI-compatible Chat Completions -- works with OpenAI,
# OpenRouter, Azure OpenAI, and local servers like Ollama/LM Studio/vLLM via base_url)
# ============================================================================


async def call_llm(system_prompt: str, user_prompt: str) -> str:
    ai_cfg = load_config()["AI settings"]

    base_url = str(ai_cfg.get("base_url") or "").strip()
    if not base_url or base_url.lower() == "null":
        base_url = DEFAULT_OPENAI_BASE_URL

    model = ai_cfg.get("model")
    if not model or str(model).strip().lower() == "null":
        raise RuntimeError(
            "config.json -> 'AI settings' -> 'model' is not set. "
            "Set it to a real model name before the bot can make AI calls."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": ai_cfg.get("temperature", 0.7),
        "top_p": ai_cfg.get("top_p", 0.9),
        "frequency_penalty": ai_cfg.get("frequency_penalty", 0),
        "presence_penalty": ai_cfg.get("presence_penalty", 0),
    }
    max_tokens = ai_cfg.get("max_tokens", -1)
    if isinstance(max_tokens, (int, float)) and max_tokens > 0:
        payload["max_tokens"] = int(max_tokens)

    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    timeout = aiohttp.ClientTimeout(total=ai_cfg.get("timeout_seconds", 3600))
    max_retries = max(1, int(ai_cfg.get("max_retries", 3)))
    url = f"{base_url.rstrip('/')}/chat/completions"

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"LLM API returned HTTP {resp.status}: {body[:500]}")
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001 -- we deliberately catch broadly to retry
            last_error = exc
            log.warning("LLM call attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                await asyncio.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"LLM call failed after {max_retries} attempts: {last_error}")


def parse_json_response(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    return json.loads(cleaned)


# ============================================================================
# COUNTRY & STATISTICS HELPERS
# ============================================================================


def get_assigned_country(discord_user_id: int) -> str | None:
    for row in read_csv_rows(ASSIGNED_NATIONS):
        if str(row.get("Discord_User_ID", "")).strip() == str(discord_user_id):
            name = row.get("Country_Name", "").strip()
            return name or None
    return None


def get_all_country_names() -> list[str]:
    return [r["Country_Name"].strip() for r in read_csv_rows(ASSIGNED_NATIONS) if r.get("Country_Name", "").strip()]


def find_mentioned_countries(text: str, exclude: str | None = None) -> list[str]:
    if not text:
        return []
    lower_text = text.lower()
    found = []
    for name in get_all_country_names():
        if name == exclude:
            continue
        if name.lower() in lower_text:
            found.append(name)
    return found


def get_latest_stats_for(countries: list[str] | None) -> list[dict]:
    """current_national_statistics.csv holds at most one (current) row per country,
    so 'latest' is just 'the row for that country'. None/empty -> every country."""
    rows = read_csv_rows(CURRENT_STATS)
    if not countries:
        return rows
    wanted = {c.lower() for c in countries}
    return [r for r in rows if r.get("Country", "").lower() in wanted]


def get_full_chat_log_since(since_iso: str) -> list[dict]:
    try:
        since_dt = datetime.fromisoformat(since_iso)
    except ValueError:
        return read_csv_rows(FULL_CHAT_LOG)
    result = []
    for row in read_csv_rows(FULL_CHAT_LOG):
        try:
            ts = datetime.fromisoformat(row["Timestamp"])
        except (KeyError, ValueError):
            continue
        if ts >= since_dt:
            result.append(row)
    return result


def find_chat_message_by_id(message_id: str) -> dict | None:
    for row in read_csv_rows(FULL_CHAT_LOG):
        if row.get("Message_ID") == message_id:
            return row
    return None


# ============================================================================
# CONTEXT BUILDERS
# (Each AI task gets the inputs the architecture doc specifies. Admin Rules and
# Admin Reminders are always bundled together -- see README "Design Notes".)
# ============================================================================


def admin_context_text() -> str:
    rules = read_text(ADMIN_RULES_PATH) or "(none set)"
    reminders = read_text(ADMIN_REMINDERS_PATH) or "(none set)"
    return f"Admin Rules (must not be violated):\n{rules}\n\nAdmin Reminders:\n{reminders}"


def sim_info_text(tracker: dict) -> str:
    return (
        f"In-Game Year: {tracker.get('current_in_game_year', 1)}\n"
        f"Real-World UTC Timestamp: {datetime.now(timezone.utc).isoformat()}"
    )


def build_shared_context(
    prompter_country: str | None,
    message_text: str,
    extra_countries: list[str] | None = None,
    scope_world: bool = False,
) -> str:
    """Admin Reminders + Active Chat Log + Active Event Log + Daily/Weekly Summaries
    + Relevant Current National Statistics, per the architecture doc's instruction
    blocks for Prompt Review / Prompt Effect / Response Events / Random Events."""
    tracker = load_tracker()
    mentioned = find_mentioned_countries(message_text, exclude=prompter_country)
    if extra_countries:
        mentioned = list(dict.fromkeys(mentioned + extra_countries))

    if scope_world:
        relevant_countries = None
    else:
        relevant_countries = list(dict.fromkeys(([prompter_country] if prompter_country else []) + mentioned))

    stats_rows = get_latest_stats_for(relevant_countries)

    parts = [
        f"## Current Simulation Info\n{sim_info_text(tracker)}",
        f"## Admin Rules & Reminders\n{admin_context_text()}",
        f"## Active Chat Log\n{csv_rows_to_text(read_csv_rows(ACTIVE_CHAT_LOG), CHAT_LOG_FIELDS)}",
        f"## Active Event Log\n{csv_rows_to_text(read_csv_rows(ACTIVE_EVENT_LOG), EVENT_LOG_FIELDS)}",
        f"## Daily Summaries\n{read_text(DAILY_SUMMARIES_PATH) or '(none yet)'}",
        f"## Weekly Summaries\n{read_text(WEEKLY_SUMMARIES_PATH) or '(none yet)'}",
        f"## Relevant Current National Statistics\n{csv_rows_to_text(stats_rows, STATS_FIELDS)}",
    ]
    return "\n\n".join(parts)


def build_review_user_prompt(raw_prompt: str, shared_context: str) -> str:
    return f"## Given Prompt\n{raw_prompt}\n\n{shared_context}"


def build_effect_user_prompt(prompt_summary: str, shared_context: str) -> str:
    return f"## Prompt Summary\n{prompt_summary}\n\n{shared_context}"


# ============================================================================
# OUTPUT PARSERS
# ============================================================================


def parse_review_output(raw: str) -> tuple[bool, str]:
    try:
        data = parse_json_response(raw)
        return bool(data.get("valid", False)), str(data.get("reason", "")).strip()
    except Exception:
        log.warning("Could not parse Prompt Review JSON, defaulting to invalid. Raw: %s", raw[:300])
        return False, "The review response could not be parsed, so this action was not approved. Please try resubmitting."


def parse_effect_output(raw: str) -> tuple[str, list[dict]]:
    """Parses the 'Result: ... / Scheduled Response Events: ...' format defined in
    config.json's prompt_effect instructions."""
    result_match = re.search(r"Result:\s*(.*?)(?=\n\s*Scheduled Response Events:|\Z)", raw, re.DOTALL | re.IGNORECASE)
    result_text = result_match.group(1).strip() if result_match else raw.strip()

    events: list[dict] = []
    events_match = re.search(r"Scheduled Response Events:\s*(.*)", raw, re.DOTALL | re.IGNORECASE)
    if events_match:
        block = events_match.group(1).strip()
        if block and "none" not in block.lower()[:10]:
            chunks = re.findall(
                r"-\s*Event:\s*(.*?)\s*-\s*Trigger Delay:\s*(.*?)(?=(?:\n\s*-\s*Event:)|\Z)",
                block,
                re.DOTALL | re.IGNORECASE,
            )
            for desc, delay in chunks:
                events.append({"description": desc.strip(), "delay_text": delay.strip()})
    return result_text, events


def resolve_trigger_delay(delay_text: str) -> datetime:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(minute|hour|day|week)s?", delay_text, re.IGNORECASE)
    now = datetime.now(timezone.utc)
    if not match:
        log.warning("Could not parse trigger delay '%s'; defaulting to 1 day.", delay_text)
        return now + timedelta(days=1)
    qty = float(match.group(1))
    unit_map = {"minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}
    return now + timedelta(**{unit_map[match.group(2).lower()]: qty})


def _parse_signed_number(value, default: float = 0.0) -> float:
    if value is None:
        return default
    s = str(value).strip().replace("%", "")
    if s.startswith("+"):
        s = s[1:]
    try:
        return float(s)
    except ValueError:
        return default


def apply_percentage(current_value, baseline_pct, deviation_pct) -> str:
    base = _parse_signed_number(current_value, 0.0)
    pct = _parse_signed_number(baseline_pct, 0.0) + _parse_signed_number(deviation_pct, 0.0)
    return f"{base * (1 + pct / 100.0):.2f}"


def apply_points(current_value, baseline_pts, deviation_pts) -> str:
    base = _parse_signed_number(current_value, 0.0)
    delta = _parse_signed_number(baseline_pts, 0.0) + _parse_signed_number(deviation_pts, 0.0)
    return f"{base + delta:.0f}"


# ============================================================================
# DISCORD OUTPUT HELPERS
# ============================================================================


def chunk_text(text: str, size: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


async def send_to_channel(channel_id: int, content: str) -> None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:
            log.error("Could not find/access channel %s: %s", channel_id, exc)
            return
    for chunk in chunk_text(content):
        await channel.send(chunk)


async def safe_reply(message: discord.Message, content: str) -> None:
    for chunk in chunk_text(content):
        try:
            await message.reply(chunk, mention_author=False)
        except Exception as exc:
            log.error("Could not reply to message %s: %s", message.id, exc)


async def safe_react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except Exception:
        pass  # missing permissions etc. are non-fatal


def log_event(message_id: str, scope: str, message_text: str) -> None:
    row = {
        "Timestamp": datetime.now(timezone.utc).isoformat(),
        "Message_ID": message_id,
        "Country/Region/World": scope,
        "Message": message_text,
    }
    append_csv_row(ACTIVE_EVENT_LOG, EVENT_LOG_FIELDS, row)
    append_csv_row(FULL_EVENT_LOG, EVENT_LOG_FIELDS, row)


# ============================================================================
# MESSAGE PIPELINE: Prompt Sent -> Reviewed -> Summarized -> Effect -> Logged
# ============================================================================


async def process_submission(message: discord.Message) -> None:
    try:
        await _process_submission_inner(message)
    except Exception:
        log.exception("Error processing submission %s", message.id)
        await safe_reply(message, "⚠️ An internal error occurred while processing this action. Please notify an admin.")


async def _process_submission_inner(message: discord.Message) -> None:
    raw_prompt = (message.content or "").strip()
    if not raw_prompt:
        return  # ignore attachment-only / empty messages

    if DOWNTIME["active"]:
        await safe_reply(message, "⏳ The simulation is currently running its weekly update. Please resend this action once downtime ends.")
        return

    country = get_assigned_country(message.author.id)
    if not country:
        await safe_reply(
            message,
            "⚠️ You are not assigned to a country yet. Ask an admin to add you to `countries/assigned_nations.csv`.",
        )
        return

    await safe_react(message, "👀")

    cfg = load_config()
    features = cfg["features"]
    instructions = cfg["instructions"]

    timestamp = datetime.now(timezone.utc).isoformat()
    message_id = str(message.id)

    # Step 1: log the raw prompt unconditionally (chat history is independent of validity)
    chat_row = {"Timestamp": timestamp, "Message_ID": message_id, "Country": country, "Message": raw_prompt}
    append_csv_row(ACTIVE_CHAT_LOG, CHAT_LOG_FIELDS, chat_row)
    append_csv_row(FULL_CHAT_LOG, CHAT_LOG_FIELDS, chat_row)

    shared_context = build_shared_context(prompter_country=country, message_text=raw_prompt)

    # Step 2: Prompt Review
    if features.get("prompt_review", True):
        review_raw = await call_llm(instructions["prompt_review"], build_review_user_prompt(raw_prompt, shared_context))
        valid, reason = parse_review_output(review_raw)
        if not valid:
            await safe_react(message, "❌")
            await safe_reply(message, f"❌ **Action Rejected:** {reason}")
            return
        await safe_react(message, "✅")

    # Step 3: Prompt Summary (kept deliberately narrow -- just the raw text, per
    # config.json's prompt_summary instructions, which only ask it to strip fluff)
    if features.get("prompt_summary", True):
        prompt_summary = await call_llm(instructions["prompt_summary"], raw_prompt)
    else:
        prompt_summary = raw_prompt

    # Step 4: Prompt Effect
    if not features.get("prompt_effect", True):
        return

    effect_raw = await call_llm(instructions["prompt_effect"], build_effect_user_prompt(prompt_summary, shared_context))
    result_text, scheduled_events = parse_effect_output(effect_raw)

    # Step 5: schedule any future Response Events the effect step called for
    for event in scheduled_events:
        trigger_dt = resolve_trigger_delay(event["delay_text"])
        row = {
            "Trigger_Date": trigger_dt.isoformat(),
            "Message_ID": message_id,
            "Country/Region/World": country,
            "Category": "Response",
            "Description": event["description"],
            "Active": "True",
        }
        append_csv_row(FUTURE_RESPONSE_EVENTS, FUTURE_EVENT_FIELDS, row)

    # Step 6: broadcast the assessment + log it as an event
    await send_to_channel(CHANNEL_ACTIONS_ID, f"**{country} — Action Result**\n{result_text}")
    log_event(message_id, country, result_text)


# ============================================================================
# EVENT PROCESSING: Random Events & scheduled Response Events
# ============================================================================


async def process_due_events(features: dict, instructions: dict) -> None:
    now = datetime.now(timezone.utc)

    if features.get("random_events", True):
        rows = read_csv_rows(FUTURE_RANDOM_EVENTS)
        changed = False
        for row in rows:
            if row.get("Active", "True").strip().lower() != "true":
                continue
            try:
                trigger = datetime.fromisoformat(row["Trigger_Date"])
            except (KeyError, ValueError):
                continue
            if trigger <= now:
                await fire_random_event(row, instructions)
                row["Active"] = "False"
                changed = True
        if changed:
            rewrite_csv(FUTURE_RANDOM_EVENTS, FUTURE_EVENT_FIELDS, rows)

    if features.get("response_events", True):
        rows = read_csv_rows(FUTURE_RESPONSE_EVENTS)
        changed = False
        for row in rows:
            if row.get("Active", "True").strip().lower() != "true":
                continue
            try:
                trigger = datetime.fromisoformat(row["Trigger_Date"])
            except (KeyError, ValueError):
                continue
            if trigger <= now:
                await fire_response_event(row, instructions)
                row["Active"] = "False"
                changed = True
        if changed:
            rewrite_csv(FUTURE_RESPONSE_EVENTS, FUTURE_EVENT_FIELDS, rows)


async def fire_random_event(row: dict, instructions: dict) -> None:
    scope = row.get("Country/Region/World", "World") or "World"
    is_world = scope.strip().lower() == "world"
    target_countries = [] if is_world else (find_mentioned_countries(scope) or [scope])

    shared_context = build_shared_context(
        prompter_country=None, message_text=scope, extra_countries=target_countries, scope_world=is_world
    )
    user_prompt = f"## Event Info\n{row.get('Description', '')}\n\n{shared_context}"
    narrative = await call_llm(instructions["random_events"], user_prompt)
    await send_to_channel(CHANNEL_EVENT_ID, narrative)
    log_event(row.get("Message_ID", ""), scope, narrative)


async def fire_response_event(row: dict, instructions: dict) -> None:
    country = row.get("Country/Region/World", "")
    message_id = row.get("Message_ID", "")

    original = find_chat_message_by_id(message_id)
    original_text = original["Message"] if original else "(original message not found)"
    original_ts = original.get("Timestamp") if original else None

    since_rows = []
    if original_ts:
        for r in read_csv_rows(FULL_CHAT_LOG):
            if r.get("Country") == country and r.get("Timestamp", "") > original_ts:
                since_rows.append(r)
    since_text = csv_rows_to_text(since_rows, CHAT_LOG_FIELDS)

    shared_context = build_shared_context(prompter_country=country, message_text=row.get("Description", ""))
    user_prompt = (
        f"## Event Info\n{row.get('Description', '')}\n\n"
        f"## Original Message\n{original_text}\n\n"
        f"## {country}'s Messages Since Original Message\n{since_text}\n\n"
        f"{shared_context}"
    )
    narrative = await call_llm(instructions["response_events"], user_prompt)
    await send_to_channel(CHANNEL_EVENT_ID, narrative)
    log_event(message_id, country, narrative)


# ============================================================================
# SIMULATION LOOP: Daily Summary / Half-Week Event Seed / Weekly Summary + Stats
# ============================================================================


async def run_daily_summary(instructions: dict) -> None:
    tracker = load_tracker()
    user_prompt = (
        f"## Current Simulation Info\n{sim_info_text(tracker)}\n\n"
        f"## Admin Rules & Reminders\n{admin_context_text()}\n\n"
        f"## Active Chat Log\n{csv_rows_to_text(read_csv_rows(ACTIVE_CHAT_LOG), CHAT_LOG_FIELDS)}\n\n"
        f"## Active Event Log\n{csv_rows_to_text(read_csv_rows(ACTIVE_EVENT_LOG), EVENT_LOG_FIELDS)}\n\n"
        f"## Daily Summaries\n{read_text(DAILY_SUMMARIES_PATH) or '(none yet)'}\n\n"
        f"## Weekly Summaries\n{read_text(WEEKLY_SUMMARIES_PATH) or '(none yet)'}"
    )
    summary = await call_llm(instructions["daily_summary"], user_prompt)
    append_text(DAILY_SUMMARIES_PATH, summary)
    # "active_*_log.csv" = "since last daily summary" -> reset once consumed
    clear_csv(ACTIVE_CHAT_LOG, CHAT_LOG_FIELDS)
    clear_csv(ACTIVE_EVENT_LOG, EVENT_LOG_FIELDS)


async def run_half_week_event_seed(instructions: dict, loop_cfg: dict) -> None:
    tracker = load_tracker()
    user_prompt = (
        f"## Current Simulation Info\n{sim_info_text(tracker)}\n\n"
        f"## Daily Summaries\n{read_text(DAILY_SUMMARIES_PATH) or '(none yet)'}\n\n"
        f"## Weekly Summaries\n{read_text(WEEKLY_SUMMARIES_PATH) or '(none yet)'}\n\n"
        f"## Active Chat Log\n{csv_rows_to_text(read_csv_rows(ACTIVE_CHAT_LOG), CHAT_LOG_FIELDS)}\n\n"
        f"## Active Event Log\n{csv_rows_to_text(read_csv_rows(ACTIVE_EVENT_LOG), EVENT_LOG_FIELDS)}"
    )
    seed = await call_llm(instructions["random_event_seed"], user_prompt)
    half_week_seconds = loop_cfg.get("half_week_seconds", 302400)
    trigger = datetime.now(timezone.utc) + timedelta(seconds=random.uniform(0, half_week_seconds))
    row = {
        "Trigger_Date": trigger.isoformat(),
        "Message_ID": "",
        "Country/Region/World": "World",
        "Category": "Random",
        "Description": seed.strip(),
        "Active": "True",
    }
    append_csv_row(FUTURE_RANDOM_EVENTS, FUTURE_EVENT_FIELDS, row)


async def run_weekly_summary(instructions: dict) -> str:
    user_prompt = (
        f"## Admin Rules & Reminders\n{admin_context_text()}\n\n"
        f"## Daily Summaries\n{read_text(DAILY_SUMMARIES_PATH) or '(none)'}\n\n"
        f"## Weekly Summaries\n{read_text(WEEKLY_SUMMARIES_PATH) or '(none yet)'}"
    )
    summary = await call_llm(instructions["weekly_summary"], user_prompt)
    append_text(WEEKLY_SUMMARIES_PATH, summary)
    # Daily summaries collectively represent the week just consumed -- reset for next week
    overwrite_text(DAILY_SUMMARIES_PATH, "")
    return summary


def apply_statistics_update(data: dict) -> None:
    baseline = data.get("global_baseline_adjustments", {})
    deviations = data.get("country_deviations", {})

    rows = read_csv_rows(CURRENT_STATS)
    rows_by_country = {r["Country"]: r for r in rows if r.get("Country")}

    for name in get_all_country_names():
        if name not in rows_by_country:
            log.warning(
                "No current-stats row for '%s'. Seed countries/stats/current_national_statistics.csv "
                "with an initial row for every assigned country.",
                name,
            )

    updated_rows = []
    for country, row in rows_by_country.items():
        dev = deviations.get(country, {}).get("additional_metrics", {})
        new_row = dict(row)
        try:
            new_row["Year"] = str(int(float(row.get("Year", 1))) + 1)
        except ValueError:
            new_row["Year"] = row.get("Year", "1")
        for metric in ("Population", "GDP", "Budget"):
            new_row[metric] = apply_percentage(row.get(metric), baseline.get(metric), dev.get(metric))
        for metric in ("Quality_of_Life", "Stability"):
            new_row[metric] = apply_points(row.get(metric), baseline.get(metric), dev.get(metric))
        updated_rows.append(new_row)

    rewrite_csv(CURRENT_STATS, STATS_FIELDS, updated_rows)
    for r in updated_rows:
        append_csv_row(FULL_STATS, STATS_FIELDS, r)


async def run_national_statistics(instructions: dict, since_dt: datetime) -> None:
    chat_rows = get_full_chat_log_since(since_dt.isoformat())
    user_prompt = f"## Full Chat Log for Past Week\n{csv_rows_to_text(chat_rows, CHAT_LOG_FIELDS)}"
    raw = await call_llm(instructions["national_statistics"], user_prompt)
    try:
        data = parse_json_response(raw)
    except Exception:
        log.exception("Could not parse National Statistics JSON; skipping stats update this week. Raw: %s", raw[:500])
        return
    apply_statistics_update(data)


def render_stats_markdown_table() -> str:
    rows = read_csv_rows(CURRENT_STATS)
    if not rows:
        return "(no statistics on record yet)"
    header = "| " + " | ".join(STATS_FIELDS) + " |"
    sep = "| " + " | ".join(["---"] * len(STATS_FIELDS)) + " |"
    lines = [header, sep]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(f, "")) for f in STATS_FIELDS) + " |")
    return "\n".join(lines)


async def post_weekly_report(weekly_summary_text: str, new_year: int) -> None:
    message = (
        f"# 📅 Year {new_year} Weekly Report\n\n"
        f"{weekly_summary_text}\n\n"
        f"## 📊 Updated National Statistics\n{render_stats_markdown_table()}"
    )
    await send_to_channel(CHANNEL_LOG_ID, message)


async def run_weekly_tasks(cfg: dict, last_weekly_dt: datetime, new_year: int) -> None:
    instructions = cfg["instructions"]
    features = cfg["features"]
    loop_cfg = cfg["loop settings"]
    downtime_enabled = features.get("weekly_downtime", True)
    downtime_minutes = loop_cfg.get("downtime_minutes", 60)
    start = datetime.now(timezone.utc)

    if downtime_enabled:
        DOWNTIME["active"] = True
        await send_to_channel(
            CHANNEL_LOG_ID, "⏳ Weekly downtime has begun. Compiling the Year in Review and updating national statistics."
        )

    try:
        weekly_summary_text = ""
        if features.get("weekly_summary", True):
            weekly_summary_text = await run_weekly_summary(instructions)
        if features.get("national_statistics", True):
            await run_national_statistics(instructions, last_weekly_dt)
        await post_weekly_report(weekly_summary_text or "(weekly summary disabled)", new_year)
    finally:
        if downtime_enabled:
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            remaining = downtime_minutes * 60 - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
            DOWNTIME["active"] = False
            await send_to_channel(CHANNEL_LOG_ID, "✅ Weekly downtime has ended. Actions may now be submitted.")


async def run_periodic_checks() -> None:
    cfg = load_config()
    features = cfg["features"]
    loop_cfg = cfg["loop settings"]
    tracker = load_tracker()
    now = datetime.now(timezone.utc)

    await process_due_events(features, cfg["instructions"])

    last_daily = datetime.fromisoformat(tracker["last_daily_summary_utc"])
    if features.get("daily_summary", True) and (now - last_daily).total_seconds() >= loop_cfg.get("day_seconds", 86400):
        await run_daily_summary(cfg["instructions"])
        tracker = load_tracker()
        tracker["last_daily_summary_utc"] = now.isoformat()
        save_tracker(tracker)

    last_half_week = datetime.fromisoformat(tracker["last_half_week_event_utc"])
    if features.get("random_events", True) and (now - last_half_week).total_seconds() >= loop_cfg.get("half_week_seconds", 302400):
        await run_half_week_event_seed(cfg["instructions"], loop_cfg)
        tracker = load_tracker()
        tracker["last_half_week_event_utc"] = now.isoformat()
        save_tracker(tracker)

    last_weekly = datetime.fromisoformat(tracker["last_weekly_summary_and_stats_utc"])
    if (now - last_weekly).total_seconds() >= loop_cfg.get("week_seconds", 604800):
        new_year = int(tracker.get("current_in_game_year", 1)) + 1
        await run_weekly_tasks(cfg, last_weekly, new_year)
        tracker = load_tracker()
        tracker["last_weekly_summary_and_stats_utc"] = now.isoformat()
        tracker["current_in_game_year"] = new_year
        save_tracker(tracker)


async def background_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await run_periodic_checks()
        except Exception:
            log.exception("Error during periodic simulation checks")
        interval = load_config()["loop settings"].get("loop_check_interval_seconds", 60)
        await asyncio.sleep(interval)


# ============================================================================
# DISCORD EVENT HANDLERS & ENTRYPOINT
# ============================================================================


@bot.event
async def on_ready():
    global _background_loop_started
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
    if not _background_loop_started:
        asyncio.create_task(background_loop())
        _background_loop_started = True


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_SUBMIT_ID:
        return
    await process_submission(message)


def _validate_env() -> None:
    missing = [
        name
        for name, val in (
            ("DISCORD_TOKEN", DISCORD_TOKEN),
            ("CHANNEL_SUBMIT_ID", CHANNEL_SUBMIT_ID),
            ("CHANNEL_ACTIONS_ID", CHANNEL_ACTIONS_ID),
            ("CHANNEL_EVENT_ID", CHANNEL_EVENT_ID),
            ("CHANNEL_LOG_ID", CHANNEL_LOG_ID),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(f"Missing required .env values: {', '.join(missing)}. See README.md for setup steps.")


def main() -> None:
    _validate_env()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()