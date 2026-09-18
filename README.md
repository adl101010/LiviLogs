# LiviLogs

A Discord bot for WoW guilds. When a Warcraft Logs link is posted after raid, it reads the whole
night and posts a report that tags the actual people: a short headline in the logs channel, and the
full report in a thread under it.

```
📜 Raid report · The Venomous Abyss · Heroic · Sep 15 · 7 bosses down · 18 pulls · 2h 27m
📈 Ula'tek: 8 wipes, best P3 at 44%
🏆 Top DPS: @Zugzug 89.4 · Top healer: @Bubbleheart 96.0 · Top tank: @Tankenstein 82.0
🌟 90+: @Bubbleheart (healing 96.0)
🗑️ Grey: @Totemtoss (healing 20.9)
💀 Floor inspector: @Facepull (11 deaths)
🧵 Full report in the thread ↓
```

The thread has five sections, each posted only if it has something to say:

| Section | What's in it |
|---|---|
| 🗺️ The night | Every boss: kills, pulls, best wipe. A boss-health bar per pull on long progression bosses, time on bosses, 💔 Heartbreaker (closest wipe before a kill), 🧱 Wall of the night, ☠️ Raid's nemesis, 🧨 Wipe starter |
| 📊 Parses | Everyone's night average by role, in WCL's colours, 👑 on top. On a night with no kills: raw DPS/HPS instead (wipes don't get parses) |
| 🌟 Highlights | 🩷 Pink parse, 🎵 Metronome, 🦶 Kick captain, 🧼 Dispel machine, 🪄 Necromancer, 🧍 Last one standing |
| 🤡 Lowlights | 🚽 Parse of shame, 🎢 Rollercoaster, ⚔️ Battle healer, 🧽 Damage sponge, 🛡️ Outdamaged by a tank |
| 💀 Deaths | 💀 Floor inspector, 🐤 Canary, 🎁 Couldn't wait for loot, 🎯 Nemesis, 🧲 Brez magnet, ⏱️ Speedrunner |

How the numbers work:

- **Parses** are each player's average across the night's boss kills. DPS and tanks are ranked on
  damage, healers on healing. Tanks are left out of the grey list (they nearly always parse grey on DPS).
  They're WCL's numbers at the moment the report is posted. WCL re-ranks every log against everyone
  else's as the tier goes on, so the site's parses drift down a point or two over the following days.
- **Progression nights** (no kills) swap parses for raw DPS/HPS, divided by the time each person was
  actually in pulls; anyone who sat some out gets a "(6 pulls)" note instead of a bad number. The
  headline shows the best pull and how it compares with last raid's.
- **Deaths** count every boss pull, kills and wipes, but only the first 5 deaths of each pull: people
  who die after the wipe is called don't get blamed. Ties are included unless it's a pile-up.
- **History:** the bot remembers every night it posts, so it can say "last raid's best: P3 at 44%"
  and "(3 raids running)" when someone wins the same callout again.
- **Every callout checks whether the night gives it something worth saying** and stays silent
  otherwise, so the report is as long as the night was eventful.
- **Retail and Classic.** The bot reads the WCL site from the link (`www.`, `classic.`, `fresh.`,
  `vanilla.`).
- Logs must be uploaded as **Public or Unlisted**. Private logs can't be read by bots.

**Pings:** the headline pings everyone it names. In the thread, each person is pinged once, on their
first mention, and Discord adds them to the thread. Set `THREAD_PING_EVERYONE=false` to stop the
parse leaderboard from pinging the whole raid; award winners are still pinged.

## How it decides when to post

Links often get posted while the log is still live. The bot reacts 👀, then waits until the log has
stopped growing for 20 minutes **and** WCL has finished calculating parses. A link posted after raid
is usually ready on the first check. It then replies to the link with the headline, opens the
thread, and swaps 👀 for ✅. It gives up waiting after 8 hours and posts whatever is there. Each log
is only posted once.

## Commands

| Command | What it does |
|---|---|
| `/link <character> [realm] [member]` | Link a character to yourself. Officers can link for someone else. Alts are fine. The character box autocompletes from logs the bot has seen |
| `/unlink <character> [realm]` | Remove a link (your own, or anyone's if you're an officer) |
| `/links [member]` | Show someone's characters; with no member, list who from the last report isn't linked |
| `/recap <link>` | Post the report right now, without waiting |
| `/recap <link> record_only:True` | Add a past night to history without posting it, so "last raid's best" and streaks work from the first real post |

Unlinked characters still appear in the report, in bold, with a nudge to `/link` at the end of the thread.

## Setup

### 1. Discord bot

1. Go to <https://discord.com/developers/applications> and create a **New Application**.
2. **Bot** tab: **Reset Token** and copy it (that's `DISCORD_TOKEN`). Further down, turn on
   **Message Content Intent**. Without it the bot can't see links posted in chat.
3. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`. Bot permissions:
   View Channels, Send Messages, Read Message History, Add Reactions, **Create Public Threads**,
   **Send Messages in Threads**. Open the generated URL to invite the bot to your server.

Without the thread permissions the bot still works: it posts the report sections in the channel
under the headline instead.

### 2. Warcraft Logs API client

Go to <https://www.warcraftlogs.com/api/clients> and create a client. The redirect URL is required
but unused; put `http://localhost`. Copy the client ID and secret. One client works on retail and
every Classic site. A report costs about 15 of the 3,600 points WCL allows per hour.

### 3. IDs

In Discord: Settings → Advanced → **Developer Mode** on. Then right-click the logs channel → Copy
Channel ID (`WATCH_CHANNEL_IDS`), your server icon → Copy Server ID (`DISCORD_GUILD_ID`), and the
officer role → Copy Role ID (`OFFICER_ROLE_ID`).

### 4. Run it

In Dockge, create a stack from [`docker-compose.yml`](docker-compose.yml) and fill in the stack's `.env`
from [`.env.example`](.env.example). Set `TIMEZONE` to the guild's timezone: it's only used for the
date in thread titles, but a US raid that crosses midnight UTC would otherwise get tomorrow's date.
It needs no ports: it only makes outbound connections to Discord and Warcraft Logs. Data (links,
history, which logs were posted) lives in the `livilogs-data` volume.

## Trying it without Discord

`tools/probe.py` pulls real logs through the API and prints exactly what the bot would post. Give it
several links and it replays them oldest first, so later reports show history ("last raid's best",
"2 raids running").

```
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Linux/macOS: .venv/bin/pip
cp .env.example .env                                  # fill in WCL_CLIENT_ID / WCL_CLIENT_SECRET
.venv/Scripts/python -m tools.probe https://www.warcraftlogs.com/reports/<code> [more links]
```

Add `--compare` to also print each player's average under both WCL parse comparisons (Rankings:
against the best of the tier; Parses: against the last two weeks, usually higher). The bot uses
Rankings, which is what WCL's report pages show (checked against the site on 2026-09-18).

Raw JSON lands in `tools/probe-out/` (git-ignored, since it contains character names).
`python -m tools.make_fixture <probe json> tests/fixtures/<name>.json` turns one into a test fixture
with the names replaced.

## Development

```
.venv/Scripts/python -m pytest
```

| Path | What |
|---|---|
| `bot/wcl.py` | WCL API: login, the one big GraphQL query, link parsing |
| `bot/recap.py` | Raw WCL JSON → facts about the night. No network; all JSON parsing lives here |
| `bot/awards.py` | The night → headline, callouts and sections. Each callout decides if it has something to say |
| `bot/render.py` | Lines → Discord messages: headline, thread title, thread sections |
| `bot/watch.py` | "Is this log finished?" |
| `bot/store.py` | SQLite: links, seen characters, report status, history |
| `bot/main.py` | Discord: channel watcher, slash commands, posting, threads, pings |

Pushing `dev` builds `ghcr.io/adl101010/livilogs:dev`; pushing `master` builds `:stable` and `:latest`.
