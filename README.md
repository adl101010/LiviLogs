# LiviLogs

A Discord bot for WoW guilds. When a Warcraft Logs link is posted after raid, it reads the whole
night and posts a report that tags the actual people: a headline card in the logs channel, and the
full report as a thread of cards under it.

Each card has a coloured edge, dividers between groups of callouts and small print explaining what
each one means. The headline card carries the boss's picture and a **View log on Warcraft Logs**
button:

```
┃ Raid report · The Venomous Abyss                                    [boss]
┃ Heroic · Sep 15, 2026 · 7 bosses down · 18 pulls · 2h 27m
┃ ─────────────────────────────────────────
┃ 🏆 Top DPS - @Zugzug 88.6
┃ 💚 Top healer - @Bubbleheart 95.9
┃ 🛡️ Top tank - @Tankenstein 81.1
┃ 📈 Ula'tek - 8 wipes · best P3 at 44%
┃ ─────────────────────────────────────────
┃ 🌟 90+ - @Bubbleheart healing 95.9
┃ 🗑️ Grey - @Totemtoss healing 20.3
┃ 💀 Floor inspector - @Facepull 11 deaths
┃ 🧵 Full report in the thread
┃ [ 👤 My night ]  [ View log on Warcraft Logs ↗ ]
```

**👤 My night** shows whoever presses it a card only they can see: their parse on every boss, their
deaths and what killed them, their consumables (with ⚠️ on anything the report called out, and a
dot per pull for potions: 🟢 one, 🟣 two or more, ⚫ none), their
interrupts, dispels and damage, and which callouts they got. It uses the characters an admin has
linked to them; anyone not linked yet picks their character from a menu. The last 8 nights are kept
in memory, so the button answers instantly; after a restart, or on an older report, the bot fetches
the log again first (a few seconds).

The cards use Discord's newer message layout ("components v2"), where @mentions still ping (unlike
embeds). If Discord ever refuses a card, the bot sends the same content as ordinary messages
instead, so a report is never lost.

The thread has six sections, each its own card, posted only if it has something to say:

| Section | What's in it |
|---|---|
| 🗺️ The night | Every boss: kills, pulls, best wipe. A boss-health bar per pull on long progression bosses, time on bosses, 💔 Heartbreaker (closest wipe before a kill), 🧱 Wall of the night, ☠️ Raid's nemesis, 🧨 Wipe starter |
| 📊 Parses | Everyone's night average by role, in WCL's colours, 👑 on top. On a night with no kills: raw DPS/HPS instead (wipes don't get parses) |
| 🌟 Highlights | 🩷 Pink parse, 🎵 Metronome, 🦶 Kick captain, 🧼 Dispel machine, 🪄 Necromancer, 💜 PI's favorite (who got Power Infusion from someone else the most), 🧍 Last one standing |
| 🤡 Lowlights | 🚽 Parse of shame, 🎢 Rollercoaster, ⚔️ Battle healer, 🧽 Damage sponge, 🛡️ Outdamaged by a tank |
| 💀 Deaths | 💀 Floor inspector, 🐤 Canary, 🎁 Couldn't wait for loot, 🎯 Nemesis, 🧲 Brez magnet, 👻 Ghost (most time spent dead, 3 minutes or more), ⏱️ Speedrunner |
| 🧪 Consumables | 🔮 Tryhards (Void-Touched rune), 🍺 Potion seller, 🫗 Mana chugger, 🍪 Cookie monster, 🧪 Potion hoarders, 🪦 Died with a healthstone in the bag, ⚗️ No flask, 🍗 Forgot to eat, 📜 No vantus. Retail only |
| 🛠️ Gear check | A chart of every raider's enchants and gems, then 🔧 Missing enchants, 🎁 Unwrapped loot (new piece worn unenchanted), 💎 Empty sockets. Retail only |

**Charts.** Four parts of the thread are pictures the bot draws:

- **Parses:** a grid of everyone's parse on every kill, in WCL's colours, with the night's average
  at the end. Replaces the leaderboard text.
- **Consumables:** one row per raider (flask, food, pulls potted, potions used, healthstones,
  vantus, rune). Yellow marks what would have been called out, by the same rules as the callouts.
  *Pulls potted* is pulls with at least one potion, out of pulls they were in; *potions used* is
  every potion drunk, so someone who drinks two on a long fight shows 18/18 and 23. For healers,
  mana potions count as potted pulls and show in the total ("3 + 22 mana"). Mana potions are
  found by name ("Mana Potion") in each log, so new expansions need no setting. Replaces the
  tryhard, hoarder, healthstone, flask, food and vantus callouts; the potion seller, mana chugger
  and cookie monster shoutouts stay as text.
- **Progress:** a boss's health at the end of each pull, coloured by phase, with the best pull
  marked. Replaces the `▇▅▅▄` bar strip, for bosses pulled 3 or more times.
- **Gear check:** one row per raider, a column each for helm, shoulders, chest, legs, boots, rings
  and weapons, plus gems. Every pull is checked, so it also catches a piece looted mid-raid and
  never enchanted ("new: 11 pulls"), and shows who enchanted partway through ("pull 4"). Rings, and
  weapons for dual-wielders, are split in two, so one bare ring shows. Off hands count only for
  dual-wield specs (or if someone enchanted theirs): shields and held items can't be enchanted.
  The callouts stay as text under the chart so the people who need to fix something get pinged.
  Empty sockets are found from the item's bonus ids (`SOCKET_BONUS_IDS`); WCL lists gems but not
  sockets, and sockets added by crafting don't show up, so those can't be checked.

A picture can't ping anyone, so a line of @mentions goes under the parse chart instead (unless
`THREAD_PING_EVERYONE=false`). If Discord refuses a card, the fallback text has the original lines.
Set `CHARTS=false` to go back to text everywhere.

How the numbers work:

- **Parses** are each player's average across the night's boss kills, to one decimal. WCL's site
  shows the same average with the decimals dropped, so 88.6 here is an 88 there. DPS and tanks are
  ranked on damage, healers on healing. Tanks are left out of the grey list (they nearly always parse grey on DPS).
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
- **Consumables** (retail): flask, food, augment rune and vantus come from the buffs WCL records on
  each player as every pull starts. Combat potions are counted per pull; healthstones and
  health/mana potions from casts. DPS and tanks are potion hoarders if they skipped a combat potion
  on more than half their pulls; healers only if they drank no potion of any kind (combat or mana)
  all night. "No vantus" only counts pulls where at least half the raid had one. "Died with a
  healthstone in the bag" is anyone who used no healthstone or health potion all night and died at
  least twice.
- **Retail and Classic.** The bot reads the WCL site from the link (`www.`, `classic.`, `fresh.`,
  `vanilla.`).
- Logs must be uploaded as **Public or Unlisted**. Private logs can't be read by bots.

**Pings:** the headline pings everyone it names. In the thread, each person is pinged once, on their
first mention, and Discord adds them to the thread. Set `THREAD_PING_EVERYONE=false` to stop the
parse leaderboard from pinging the whole raid; award winners are still pinged.

### Updating consumables for a new expansion

Healthstones, health and mana potions, flasks, food and vantus runes are recognised by name
pattern ("Healthstone", "Health Potion", "Healing Potion", "Mana Potion", "Flask of", "Phial of",
"Well Fed", "Vantus Rune"), so they carry over. Two lists don't follow a pattern and live in the
`.env`: `COMBAT_POTIONS` (Midnight: Potion of Recklessness, Light's Potential) and `TRYHARD_RUNES`
(Void-Touched). A health potion with an unusual name can be added with `EXTRA_HEALTH_ITEMS`. To find
new names, run the probe on a fresh log and look at the potions people actually used.

The gear check's enchantable slots are Midnight's. `SOCKET_BONUS_IDS` lists the item bonus ids that
mean "has a socket"; a new expansion may add new ones (look for bonus ids that only ever appear on
items with gems in them).

## How it decides when to post

Links often get posted while the log is still live. The bot reacts 👀, then waits until the log has
stopped growing for 20 minutes **and** WCL has finished calculating parses. A link posted after raid
is usually ready on the first check. It then replies to the link with the headline, opens the
thread, and swaps 👀 for ✅. It gives up waiting after 8 hours and posts whatever is there. Each log
is only posted once.

## Commands

| Command | What it does |
|---|---|
| `/link <member> <characters> [realm]` | **Admins.** Link one or more characters to a member, comma-separated: `Bob, Bobalt, Bobdruid-Argent Dawn`. A person can have any number of characters (alts); whichever one shows up in a log tags them. Linking a character that belongs to someone else moves it, and says so. The box autocompletes from logs the bot has seen |
| `/unlink <character> [realm]` | **Admins.** Remove a link |
| `/links [member]` | Anyone. Show someone's characters; with no member, list who from the last report isn't linked |
| `/recap <link>` | Post the report right now, without waiting |
| `/recap <link> record_only:True` | Add a past night to history without posting it, so "last raid's best" and streaks work from the first real post |

Unlinked characters still appear in the report, in bold, with a nudge at the end of the thread.

**Who counts as an admin:** Discord shows `/link` and `/unlink` only to members with the **Manage
Server** permission, and refuses them from anyone else. To let officers link people without giving
them Manage Server, go to Server Settings → Integrations → LiviLogs and allow their role on those two
commands.

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
every Classic site. A report costs roughly 20-45 of the 3,600 points WCL allows per hour.

### 3. IDs

In Discord: Settings → Advanced → **Developer Mode** on. Then right-click the logs channel → Copy
Channel ID (`WATCH_CHANNEL_IDS`) and your server icon → Copy Server ID (`DISCORD_GUILD_ID`).

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
| `bot/render.py` | Lines → cards: headline card, thread title, one card per section; plain-text version for fallback and the probe |
| `bot/charts.py` | The parse, consumables and progress pictures (Pillow) |
| `bot/mynight.py` | The private "My night" card |
| `bot/gear.py` | The gear check: enchants on every pull, new loot left bare, empty sockets |
| `bot/watch.py` | "Is this log finished?" |
| `bot/store.py` | SQLite: links, seen characters, report status, history |
| `bot/main.py` | Discord: channel watcher, slash commands, cards (components v2), threads, pings |

Pushing `dev` builds `ghcr.io/adl101010/livilogs:dev`; pushing `master` builds `:stable` and `:latest`.
