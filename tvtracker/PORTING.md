# Bringing AniTracker updates into TV Tracker

TV Tracker follows [sxpehr/AniTracker](https://github.com/sxpehr/AniTracker), but tracks
**movies and TV shows through Simkl** instead of anime through AniList. When AniTracker
ships something new, port it here with these rules. `UPSTREAM.md` holds the last
AniTracker commit that was ported.

## How the port works
- Most AniTracker files are copied as they are. Simkl stands in for AniList in two places:
  - `catalog.js` offers the same functions as AniTracker's `anilist.js` (getRecentFeed,
    getMediaById, getMediaDetails, getEntriesForMedia, getMediaTags, …), answered from Simkl.
  - `features/media.js` offers the same functions as AniTracker's `features/anilistSocial.js`.
  - Feature files must `require('../catalog')` / `require('./media')`, never `anilist` or
    `anilistSocial`. If upstream adds a new AniList function, add a Simkl version to
    `catalog.js` or `media.js` with the same name and data shape (or a safe empty answer).
- Data shape: media look like AniList Media. `type` is always `'ANIME'` (it means
  "something you watch"); `format` is `'MOVIE'` or `'TV'`; `kind` is `'movie'` or `'show'`.
  A media id is `simklId * 2` for a movie and `simklId * 2 + 1` for a show.
- People: a person's Simkl account id is stored as `anilistId` on their linked account.
  Simkl only shares a history with its owner's sign-in, so only signed-in people can be
  tracked. Keep the field names (`anilistId`, `anilistName`) so upstream code works.
- Simkl has no activity stream: `getRecentFeed` returns `{ activities: [], entries }` and
  `features/feed.js` turns list changes into posts. Keep that file's synthetic-event logic.

## Keep these TV Tracker parts when copying upstream files over
- `index.js`: Simkl device-code `/signin` (`handleSignIn` + `watchDeviceLogin`), no web OAuth
  router; `/track` takes a Discord **user** option; `missingChannelPermsFresh` (the bot
  doesn't get its own role updates); `migrateTvTracker`; `catalog.init({ loadData, saveData })`;
  restart role from `PING_ROLE_NAME` (default `tv`); `/mute` handler; category checks in the
  poller (step 2b and the posting `skip`).
- `catalog.js`, `simkl.js`, `features/media.js`, `features/categories.js`, `storage.js`'s
  `TV_DATA_FILE` folder, `features/activityPost.js` (watch time line; no episode bar for movies),
  `moreInfo.js` (movie runtime, IMDb button), `features/statsEngine.js` (110/42 minute defaults),
  `dashboard/routes.js` (never send sign-in tokens), `changelog.js` (TV Tracker wording).
- `commands.js`: `/find` (not `/anime`; no `airing`, no `vibe`; movie/TV genre choices),
  `/mostrecommended` (not `/recommendanimes`), no `/favorites`, option `title` (not `anime`),
  `/track user:`, `/mute`.

## Wording
Everything a person sees says movies and TV, Simkl and TV Tracker — never anime, AniList,
AniTracker, manga, AnimeThemes or openings. Anime openings become **trailers** (Simkl's YouTube
trailers via `getOpenings`). Leave internal names alone (`anilistId`, `animeTitle`, the
`'ANIME'` type, `/banner/v1/anime/` paths, stats keys).

Drop features that need AniList-only data (airing schedules, AniList favourites, anime tags)
or replace them with a Simkl equivalent (genres for tags, trending lists, trailers).

## Before merging
- `cd tvtracker && npm ci && npm run check` (syntax + Node tests) passes.
- `python -m pytest` in the repo root passes (except `test_check_command` when exiftool is missing).
- No `anilist.co` links or AniList/anime wording in visible text: `grep -rniE "anilist|anime|anitracker" --include=*.js tvtracker | grep -v node_modules` and check each hit is internal.
- Update `UPSTREAM.md` to the AniTracker commit you ported.
