# wcl-bot

A Discord bot for WoW guilds. When a Warcraft Logs link is posted after raid, it reads the whole
night and posts a recap that tags the actual people:

```
📜 Raid recap · Liberation of Undermine · Heroic · September 16, 2026 · 7 kills, 12 wipes
🏆 90+ club: @Alex 97.4 · @Bob 92.1
⚪ Grey parses: @Carl 18.0
💀 Most deaths: @Dana 7 (first to die ×4) · @Eve 5 · @Finn 5
```

- **Parses** are each player's average across the night's boss kills. DPS are ranked on DPS, healers on
  healing. Tanks are left out of the grey list (they nearly always parse grey on DPS).
- **Deaths** count every boss pull, kills and wipes. Deaths after the 5th in a pull are ignored, so
  people who die after the wipe is called don't get blamed. Ties for last place are all included.
- **Retail and Classic.** The bot reads the WCL site from the link (`www.`, `classic.`, `fresh.`,
  `vanilla.`).
- Logs must be uploaded as **Public or Unlisted**. Private logs can't be read by bots.

## How it decides when to post

Links often get posted while the log is still live. The bot reacts 👀, then waits until the log has
stopped growing for 20 minutes **and** WCL has finished calculating parses. A link posted after raid
is usually ready on the first check. It then replies to the link with the recap and swaps 👀 for ✅.
It gives up waiting after 8 hours and posts whatever is there. Each log is only posted once.

## Commands

| Command | What it does |
|---|---|
| `/link <character> [realm] [member]` | Link a character to yourself. Officers can link for someone else. Alts are fine. The character box autocompletes from logs the bot has seen |
| `/unlink <character> [realm]` | Remove a link (your own, or anyone's if you're an officer) |
| `/links [member]` | Show someone's characters; with no member, list who from the last recap isn't linked |
| `/recap <link>` | Post a recap right now, without waiting |

Unlinked characters still appear in the recap, in bold, with a nudge to `/link`.

## Setup

### 1. Discord bot

1. Go to <https://discord.com/developers/applications> and create a **New Application**.
2. **Bot** tab: **Reset Token** and copy it (that's `DISCORD_TOKEN`). Further down, turn on
   **Message Content Intent**. Without it the bot can't see links posted in chat.
3. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`. Bot permissions:
   View Channels, Send Messages, Read Message History, Add Reactions. Open the generated URL
   to invite the bot to your server.

### 2. Warcraft Logs API client

Go to <https://www.warcraftlogs.com/api/clients> and create a client. The redirect URL is required
but unused; put `http://localhost`. Copy the client ID and secret.

### 3. IDs

In Discord: Settings → Advanced → **Developer Mode** on. Then right-click the logs channel → Copy
Channel ID (`WATCH_CHANNEL_IDS`), your server icon → Copy Server ID (`DISCORD_GUILD_ID`), and the
officer role → Copy Role ID (`OFFICER_ROLE_ID`).

### 4. Run it

In Dockge, create a stack from [`docker-compose.yml`](docker-compose.yml) and fill in the stack's `.env`
from [`.env.example`](.env.example). It needs no ports: it only makes outbound connections to Discord
and Warcraft Logs. Data (links, which logs were posted) lives in the `wcl-bot-data` volume.

## Checking the numbers against the site

`tools/probe.py` pulls a real log through the API and prints each player's night average under every
parse option WCL offers, side by side, plus the exact recap the bot would post. Use it to confirm
the numbers match what the report page shows, and set `WCL_COMPARE` / `WCL_TIMEFRAME` if the default
doesn't match.

```
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Linux/macOS: .venv/bin/pip
cp .env.example .env                                  # fill in WCL_CLIENT_ID / WCL_CLIENT_SECRET
.venv/Scripts/python -m tools.probe https://www.warcraftlogs.com/reports/<code>
```

Raw JSON lands in `tools/probe-out/` (git-ignored, since it contains character names).

## Development

```
.venv/Scripts/python -m pytest
```

| Path | What |
|---|---|
| `bot/wcl.py` | WCL API: login, GraphQL queries, link parsing |
| `bot/recap.py` | Raw WCL JSON → recap numbers. No network; all JSON parsing lives here |
| `bot/render.py` | Recap → message text |
| `bot/watch.py` | "Is this log finished?" |
| `bot/store.py` | SQLite: links, seen characters, report status |
| `bot/main.py` | Discord: channel watcher, slash commands, posting |

Pushing `dev` builds `ghcr.io/adl101010/wcl-bot:dev`; pushing `master` builds `:stable` and `:latest`.
