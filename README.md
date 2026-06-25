# Model UN Discord Bot

An AI Game Master for a persistent, text-based Model UN simulation on Discord. Each
player is assigned a country. Players post in-character actions; an LLM reviews them
for plausibility, narrates their immediate effects, and schedules realistic delayed
consequences. The world also moves forward on its own: random global events occur,
daily summaries are compiled, and once a week the simulation advances one full
in-game year — complete with an AI-written "Year in Review" and updated national
statistics.

**1 real-world week = 1 in-game year.**

## How it works

### The action pipeline
When a player posts in the submissions channel:

1. **Logged** — the raw message is recorded to the chat logs immediately.
2. **Reviewed** — the AI checks the action against `admin/admin_rules.txt` and the
   country's current stats. Implausible or rule-breaking actions are rejected with a
   reason, and the pipeline stops there.
3. **Summarized** — roleplay flourish is stripped out, leaving the core action.
4. **Resolved** — the AI narrates the immediate outcome and may schedule one or more
   **Response Events**: delayed consequences (e.g. "China lodges a protest — 2 days
   IRL") that will mature and play out automatically later, taking into account
   whatever the country did in the meantime.
5. **Broadcast** — the result is posted to the actions channel and logged.

### The world clock
A background loop (configurable interval) continuously:

- Fires any **Random Events** or **Response Events** whose scheduled time has arrived,
  posting the AI-generated narrative to the events channel.
- Once a day: compiles a **Daily Summary** from the day's chat/event activity, then
  clears the "active" logs (they represent "since the last daily summary").
- Once every half-week: generates a one-line **seed** for a future random event and
  schedules it to fire at a random point in the next half-week.
- Once a week: writes the **Weekly Summary** ("Year in Review"), recalculates every
  country's **national statistics**, posts both to the log channel, advances the
  in-game year, and (optionally) pauses new submissions for up to an hour while this
  happens (`weekly_downtime`).

All of this is driven by the prompts and toggles already defined in `config.json` —
`bot.py` doesn't hardcode any simulation text; it just assembles the right context for
each call and applies the structured outputs.

## Repository layout

```
admin/
  admin_rules.txt            Hard constraints the AI must enforce (you write this)
  admin_reminders.txt        Soft "keep in mind" notes for the AI (you write this)
chats/
  active_chat_log.csv        Messages since the last daily summary (auto-managed)
  full_chat_log.csv          Full message history (auto-managed)
countries/
  assigned_nations.csv       Country_Name,Discord_User_ID (you maintain this)
  stats/
    current_national_statistics.csv   Latest stats per country (you input intial statistics)
    full_national_statistics.csv      Full stats history (auto-managed)
events/
  log/
    active_event_log.csv     Events since the last daily summary (auto-managed)
    full_event_log.csv       Full event history (auto-managed)
  upcoming/
    future_random_events.csv     Scheduled random events (auto-managed)
    future_reponse_events.csv    Scheduled response events (auto-managed)
summaries/
  daily_summaries.txt        Appended daily, cleared weekly (auto-managed)
  weekly_summaries.txt       Appended weekly, never cleared (auto-managed)
.env                          API Keys & Channel IDs (you fill this in)
bot.py                        The code that runs the bot
config.json                   Feature toggles, loop timing, AI settings, AI prompts
sim_tracker.json              Persisted loop timestamps + current in-game year
requirements.txt              Python dependencies
```

## Setup

### 1. Discord application
In the [Discord Developer Portal](https://discord.com/developers/applications), create
an application and bot user. Under **Bot**, enable the **Message Content Intent**
(the bot reads raw message text). Invite it to your server with permission to
**Send Messages**, **Read Message History**, and **Add Reactions**.

### 2. Fill in `.env`
The file already has the right keys — just add your values:

```
DISCORD_TOKEN=your-bot-token
LLM_API_KEY=your-llm-api-key
CHANNEL_SUBMIT_ID=...   # where players post actions
CHANNEL_ACTIONS_ID=...  # where the bot posts action results
CHANNEL_EVENT_ID=...    # where the bot posts random/response events
CHANNEL_LOG_ID=...      # where the bot posts weekly summaries + stats
```

### 3. Fill in `config.json` → `"AI settings"`
`bot.py` calls an **OpenAI-compatible Chat Completions endpoint**
(`POST {base_url}/chat/completions`), which covers OpenAI itself, Azure OpenAI,
OpenRouter, and most local runners (Ollama, LM Studio, vLLM, text-generation-webui).
Replace the placeholders:

- `"model"` → a real model name (e.g. `"gpt-4.1-mini"`, `"llama3.1"`, etc.)
- `"base_url"` → leave as `"null"` to default to `https://api.openai.com/v1`, or point
  it at your local server's OpenAI-compatible URL.
- `"max_tokens"` → leave as `-1` to omit a cap entirely (the provider's own default applies), or set it to a positive integer to limit how many tokens each AI response can generate. Some of the longer outputs (the weekly "Year in Review" narrative, the national statistics JSON) can run long, so if you do set a cap, give it enough headroom — too low a value will cut off the response mid-sentence or mid-JSON, which `bot.py` can't parse.

Everything else in `config.json` (feature toggles, loop timing, the prompts
themselves) can be left as-is and edited freely later — **it's re-read on every use,
so you don't need to restart the bot after changing it.**

### 4. Add your players
Edit `countries/assigned_nations.csv`:

```
Country_Name,Discord_User_ID
United States,123456789012345678
China,234567890123456789
```

### 5. Seed starting national statistics — required
`countries/stats/current_national_statistics.csv` starts empty. The bot only ever
*adjusts* existing rows (percentage/point shifts each week) — it can't invent a
country's starting numbers. Add one row per country before the first weekly update
runs:

```
Year,Country,Population,GDP,Budget,Quality_of_Life,Stability
2026,United States,341784857,32383920000000,12305890000000,93.8,51.9
2026,China,1404890000,20851590000000,7089540000000,79.7,63.6
```

### 6. (Recommended) Write your admin rules & reminders
`admin/admin_rules.txt` and `admin/admin_reminders.txt` start empty. The Prompt Review
step uses `admin_rules.txt` as the hard constraints actions can't violate (e.g. "no
metagaming," "no inventing weapons of mass destruction without a multi-week tech
program"); `admin_reminders.txt` is loaded into nearly every AI call as softer
ongoing context (e.g. "The Untied States and China are currently at a ceasefire").

### 7. Install & run

```
pip install -r requirements.txt
python bot.py
```

## Files you need to edit yourself

These already exist in the repo with the right structure — `bot.py` reads from them
but doesn't (and shouldn't) generate their initial content:

| File | What to do |
|---|---|
| `.env` | Add your Discord token, LLM API key, and the four channel IDs. |
| `config.json` | Set `"AI settings" → "model"` (required) and `"base_url"` (optional) — both are currently the placeholder string `"null"`. |
| `countries/assigned_nations.csv` | Add `Country_Name,Discord_User_ID` for every player. |
| `countries/stats/current_national_statistics.csv` | Add one starting-stats row per country (see step 5 above) — required before the first weekly update. |
| `admin/admin_rules.txt`, `admin/admin_reminders.txt` | Optional but recommended — give the AI real constraints/context. They're currently empty, so the simulation will run with no admin guardrails until you do. |
| `requirements.txt` | No new dependency is strictly required — `bot.py` only uses `discord.py` (which already pulls in `aiohttp`, used here for the LLM calls) and `python-dotenv`, both already listed. If you'd like `aiohttp` pinned explicitly rather than relying on it being a transitive dependency, add `aiohttp>=3.9` yourself. |

`sim_tracker.json` needs no manual edits — the bot initializes its null fields to
sensible defaults (year 1, "now") the first time it runs. You can add your own timestamps before you run if you want the bot to summarize during inactive times, or if you want to choose a specific year.

## Design notes / assumptions

- **`active_chat_log.csv` / `active_event_log.csv` are cleared after each daily
  summary** (their header comments say "since the last daily summary"), and
  **`daily_summaries.txt` is cleared after each weekly summary** (its 7 entries are
  what the weekly summary distills). `weekly_summaries.txt` and `full_*` files are
  append-only and never cleared.
- **Country mentions** in event/effect text are detected by case-insensitive
  substring match against the names in `assigned_nations.csv`. This is simple and
  works well for real country names; it can over- or under-match on short/ambiguous
  names, so use full names in that file where possible.
- **Response Event trigger delays** (e.g. "2 days IRL", "1 week IRL") are parsed with
  a regex for minutes/hours/days/weeks. Unparseable delays default to 1 day, with a
  warning logged.
- **Weekly downtime** holds new submissions (with a polite "try again later" reply)
  for the full configured `downtime_minutes`, even if the AI work finishes early, so
  the maintenance window is predictable.
- **Message_ID** in every log is the real Discord message ID, which is how Response
  Events later look up the original message and everything a country said since then.