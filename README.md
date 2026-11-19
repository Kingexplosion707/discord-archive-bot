# Discord Photo Archive Bot

Copies every photo and video in your Discord server to one Google Photos album, then keeps watching for new posts and does a full check every day at 7 PM. Google Photos handles sorting from there.

It also tracks what everyone watches through a personal Stremio addon, and shows live, synced lyrics for whatever people are playing on Spotify.

## Setup

You'll need Docker on the server.

### 1. Discord

1. Go to the [Developer Portal](https://discord.com/developers/applications) and create an application.
2. On the **Bot** page, click **Reset Token** and copy it.
3. On the same page, turn on **Message Content Intent**, **Server Members Intent** and **Presence Intent** (the last two let the lyrics see what people play on Spotify).
4. Invite the bot with this link, swapping in your Application ID from **General Information**:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=17179954176
   ```

5. In Discord, turn on Developer Mode (Settings > Advanced). Right-click the server for its ID and your name for your user ID.
6. For any private channel you want included, add the bot's role to it. `/archive status` lists the channels it can't read.

### 2. Google Photos

1. In [Google Cloud](https://console.cloud.google.com/), create a project and enable **Photos Library API** and **Google Drive API**.
2. Open **Google Auth Platform**, click **Get started**, and choose **External**.
3. Under **Data Access**, add the scope `https://www.googleapis.com/auth/photoslibrary.appendonly`.
4. Under **Audience**, click **Publish app**. If you leave it in Testing, the login stops working after 7 days.
5. Under **Clients**, create a **Desktop app** client and download the JSON. Save it as `client_secret.json` next to `install.sh`.

### 3. Install

Run `sh install.sh`. It asks for the Discord values, then builds, tests and starts the bot.

It prints a Google link partway through. After you allow access, your browser lands on an error page at `localhost`. That's expected: copy the whole address and paste it into the terminal.

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/archive status` | Anyone | Shows progress |
| `/archive scan` | Owner | Checks the server for new media now and retries anything that failed |
| `/archive google` | Owner | Signs the bot in to Google (works from your phone) |
| `/tracker setup` | Owner | Picks the channel where watch activity, episode alerts and recaps go |
| `/tracker link` | Anyone | DMs you your personal Stremio addon link (install it once in Stremio) |
| `/tracker status` | Anyone | Shows whether your plays are reaching the bot, and what's missing if not |
| `/watch library` | Anyone | Your Stremio library (continue watching with progress, and saved titles), or a friend's |
| `/tracker progress` | Anyone | Connects your Stremio account so posts show where you are (e.g. 23:41 / 47:12) and when you finish |
| `/tracker privacy` `pings` `unlink` `progress-off` | Anyone | Hide your activity, turn pings off, or reset your addon link |
| `/watch info` `rate` `add` `remove` | Anyone | Look up a title with poster and details, rate it, add it to your watchlist |
| `/watch recommend` | Anyone | Recommends a title to a friend and pings them; it shows up in their Stremio |
| `/watch now` `history` `stats` `watchlist` `recs` | Anyone | Who's watching what, your history, totals, lists |
| `/watch match` | Anyone | Shows how similar your taste is to a friend's |
| `/watch movienight` | Anyone | Starts a vote on what to watch from everyone's watchlists; the winner is announced when it closes |
| `/list create` `add` | Anyone | Ranked lists (1st to last): paste many titles at once, one per line; numbers, years and IMDb links are understood |
| `/list move` `remove` `show` `all` `delete` | Anyone | Reorder, trim, show yours or a friend's lists |
| `/watch recap` | Anyone | The week's watching (also posted automatically Sunday evening) |
| `/lyrics setup` | Owner | Picks the music channel, and optionally adds a Spicy Lyrics key |
| `/lyrics on` / `off` / `now` | Anyone | Opt in or out of live lyrics for your Spotify; `now` sends you the full lyrics privately |

## Watch tracker

Each person runs `/tracker link`, opens the link, and clicks **Install** in Stremio. From then on, whenever they start something in Stremio the bot posts it in the tracker channel with the poster, episode still, description, IMDb rating, cast, who else has seen it, and buttons to add, rate or recommend it. Followed shows get a ping when a new episode comes out. The addon also adds rows to Stremio's home screen: your watchlist, what friends recommended you, and what friends are watching. Details come from Stremio's own Cinemeta (anime from the Kitsu addon), so no API key is needed.

The addon alone only knows when playback starts. Run `/tracker progress` and sign in to Stremio once (the password isn't saved, only Stremio's sign-in key, encrypted) and your post also shows a live progress bar with the exact time, then switches to Finished at 90%. Connected accounts also get `/watch add` and `/watch remove` mirrored into their Stremio library. Posts don't ping anyone; pings are for recommendations and new episodes of shows you follow.

## Live lyrics

People who run `/lyrics on` get one message in the music channel that follows their Spotify: it's edited line by line as the song plays, so everyone in the channel can read along. The **Word by word** button opens a page that highlights each word in time, Spicy Lyrics style. The bot reads the song from people's Discord status (show Spotify on your profile), so nobody needs to connect a Spotify account.

Lyrics come from the [Spicy Lyrics API](https://developers.spicylyrics.org) if you add a key (`sl_pk_...`) in `/lyrics setup`, with word timings. Without one, or when Spicy has nothing, it uses LRCLIB, which is free and keyless. Discord limits message edits, so the live message moves about once a second; the web page is smooth.

## Run it free on Oracle Cloud (from your phone)

Oracle's Always Free tier gives you a small server that never turns off.

1. **Sign up** at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/). It asks for a card to confirm you're real; Always Free resources aren't charged.
2. **Get the values** the setup script asks for:
   - Discord token: Developer Portal > your app > **Bot** > **Reset Token**. While you're there, make sure all three Privileged Gateway Intents are on.
   - Google client ID and secret: Google Cloud > **Google Auth Platform** > **Clients** > your Desktop client. Copy the Client ID, then **Add secret** and copy the new secret right away.
3. **Fill in** the quotes at the top of `oracle-cloud-init.sh`.
4. **Create the server:** Compute > Instances > **Create instance**. Image: **Ubuntu 24.04**. Shape: **Ampere VM.Standard.A1.Flex**, 2 OCPUs and 12 GB (if it says out of capacity, use **VM.Standard.E2.1.Micro** under AMD instead; the script adds swap so 1 GB is enough). Open **Show advanced options** > **Management** > **Paste cloud-init script** and paste the whole file. Create.
5. **Wait about 10 minutes.** The bot comes online in Discord by itself and DMs you that it isn't signed in to Google.
6. **Run `/archive google`** in your server, tap **Sign in**, allow access, copy the address of the page that won't load, tap **Paste address**, paste it. Done.

The server and owner are worked out automatically (the only server the bot is in, and whoever owns the bot app). Set `DISCORD_GUILD_ID` and `DISCORD_OWNER_ID` in `.env` if you ever need to choose.

**Fresh start:** a new server doesn't know what the old one uploaded, so `START_FRESH=true` makes its first pass record everything already in Discord without uploading it, and only new posts go to Google Photos. They go into a new album named "Discord Archive", because the new server has no way to look up the old album (the old one stays as it is). The message log starts a new "Discord Server Log.txt" with the full history.

The server pulls bot updates from GitHub every hour, so you don't need to log in to it.

Moving from a machine that's already running the bot instead? `sh move-to-oracle.sh ORACLE_IP` carries everything over, so nothing starts fresh.

Updating: `docker compose build && docker compose up -d` (with `sudo` on Oracle). Your data is kept in the `bot-data` Docker volume.

## TV Tracker (optional)

A second bot, forked from AniTracker and changed to track movies and TV through Simkl, runs beside this one.

1. Create a Discord application for it, invite it to your server, and copy its token and Client ID.
2. On the server: `cp tvtracker.env.example tvtracker.env`, fill it in (token, Client ID, `SIMKL_CLIENT_ID`, dashboard password and secret), then `chmod 600 tvtracker.env`.
3. Add `tv` to `COMPOSE_PROFILES` in `.env`, then run `docker compose up -d --build`.

Without the `tv` profile, the service doesn't start, and the archive bot is unaffected.
