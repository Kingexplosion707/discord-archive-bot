"""The bot's small web server: the personal Stremio addons and the word-by-word lyrics page."""
from __future__ import annotations

import html
import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from aiohttp import web

if TYPE_CHECKING:
    from .discord_app import ArchiveClient

log = logging.getLogger(__name__)

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
}


@web.middleware
async def cors(request: web.Request, handler):  # noqa: ANN001, ANN201
    if request.method == "OPTIONS":
        return web.Response(headers=CORS)
    response = await handler(request)
    response.headers.update(CORS)
    return response


def _json(data: object, status: int = 200, cache: int = 0) -> web.Response:
    headers = {"Cache-Control": f"max-age={cache}" if cache else "no-store"}
    return web.json_response(data, status=status, headers=headers)


def build_app(client: "ArchiveClient") -> web.Application:
    app = web.Application(middlewares=[cors])

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def stremio(request: web.Request) -> web.Response:
        tracker = client.tracker
        token = request.match_info["token"]
        tail = request.match_info["tail"]
        if tail == "manifest.json":
            if await tracker.user_for_token(token) is None:
                return _json({"error": "unknown link"}, 404)
            return _json(tracker.manifest(token), cache=3600)
        if tail == "install":
            return await install_page(token)
        parts = [unquote(p) for p in tail.removesuffix(".json").split("/")]
        if len(parts) < 3:
            return _json({"error": "not found"}, 404)
        resource, kind, item_id = parts[0], parts[1], parts[2]
        if resource == "subtitles":
            # Stremio asks for subtitles the moment a video starts playing.
            known = await tracker.handle_play(token, kind, item_id)
            return _json({"subtitles": []}) if known else _json({"error": "unknown link"}, 404)
        if resource == "catalog":
            metas = await tracker.catalog(token, kind, item_id)
            return _json({"metas": metas or []}, status=200 if metas is not None else 404)
        return _json({"error": "not found"}, 404)

    async def install_page(token: str) -> web.Response:
        tracker = client.tracker
        if await tracker.user_for_token(token) is None:
            return web.Response(text="This addon link isn't valid anymore. Run /tracker link in Discord.", status=404)
        manifest = tracker.addon_url(token)
        deep = "stremio://" + manifest.split("://", 1)[1]
        web_install = "https://web.stremio.com/#/addons?addon=" + quote(manifest, safe="")
        page = INSTALL_HTML.replace("{{DEEP}}", html.escape(deep)).replace("{{WEB}}", html.escape(web_install)).replace(
            "{{MANIFEST}}", html.escape(manifest)
        )
        return web.Response(text=page, content_type="text/html")

    async def lyrics_page(request: web.Request) -> web.Response:
        if client.lyrics.state(request.match_info["token"]) is None:
            return web.Response(text="This lyrics link has ended. Open the newest one from Discord.", status=404)
        return web.Response(text=LYRICS_HTML, content_type="text/html")

    async def lyrics_state(request: web.Request) -> web.Response:
        state = client.lyrics.state(request.match_info["token"])
        return _json(state if state is not None else {"ended": True}, status=200 if state else 404)

    app.router.add_get("/", health)
    app.router.add_get("/stremio/{token}/{tail:.*}", stremio)
    app.router.add_get("/lyrics/{token}", lyrics_page)
    app.router.add_get("/lyrics/{token}/state", lyrics_state)
    app.router.add_route("OPTIONS", "/{tail:.*}", health)
    return app


INSTALL_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Install the watch tracker</title>
<style>
:root{color-scheme:dark}body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#0f0b1a;color:#eee;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
main{max-width:460px;width:100%}h1{font-size:1.5rem;margin:0 0 .5rem}p{color:#bbb}
a.btn{display:block;text-align:center;padding:14px;border-radius:12px;background:#8b5cf6;color:#fff;font-weight:600;text-decoration:none;margin:12px 0}
a.alt{background:#2a2340}code{display:block;word-break:break-all;background:#1c1630;padding:12px;border-radius:10px;font-size:.85rem}
</style></head><body><main>
<h1>Discord Watch Tracker</h1>
<p>This addon tells your Discord server what you press play on, and puts your watchlist and friends' recommendations on Stremio's home screen.</p>
<a class="btn" href="{{DEEP}}">Open in the Stremio app</a>
<a class="btn alt" href="{{WEB}}">Install on Stremio Web</a>
<p>Or paste this link into the search bar on Stremio's Addons page:</p><code>{{MANIFEST}}</code>
<p>Keep this link to yourself. It's how the bot knows it's you.</p>
</main></body></html>"""


LYRICS_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lyrics</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#000;color:#fff;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;overflow:hidden;height:100vh}
#bg{position:fixed;inset:-10%;background-size:cover;background-position:center;filter:blur(60px) saturate(1.6) brightness(.55);transform:scale(1.2);transition:background-image 1s}
#shade{position:fixed;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.35),rgba(0,0,0,.6))}
header{position:fixed;top:0;left:0;right:0;display:flex;gap:14px;align-items:center;padding:16px;z-index:2;background:linear-gradient(rgba(0,0,0,.5),transparent)}
header img{width:56px;height:56px;border-radius:8px;box-shadow:0 4px 20px rgba(0,0,0,.5)}
#meta b{display:block;font-size:1.05rem}#meta span{opacity:.75;font-size:.9rem}
#bar{position:fixed;bottom:0;left:0;height:3px;background:#fff;opacity:.7;z-index:2;width:0}
#lines{position:relative;z-index:1;height:100vh;overflow-y:auto;padding:45vh 6vw;scrollbar-width:none;mask-image:linear-gradient(transparent,#000 22%,#000 78%,transparent)}
#lines::-webkit-scrollbar{display:none}
.line{font-weight:800;font-size:clamp(1.6rem,4.6vw,3rem);line-height:1.2;margin:0 0 .55em;opacity:.35;transition:opacity .3s,transform .3s,filter .3s;transform-origin:left;filter:blur(1.2px)}
.line.past{opacity:.45;filter:none}.line.active{opacity:1;filter:none;transform:scale(1.03)}
.w{background:linear-gradient(90deg,#fff var(--p,0%),rgba(255,255,255,.4) var(--p,0%));-webkit-background-clip:text;background-clip:text;color:transparent}
.line.past .w{--p:100%}
.static .line{opacity:.9;filter:none;font-size:clamp(1.2rem,3.4vw,2rem)}
#msg{position:fixed;inset:0;display:grid;place-items:center;z-index:3;font-size:1.2rem;opacity:.8;pointer-events:none}
footer{position:fixed;bottom:10px;right:14px;font-size:.75rem;opacity:.5;z-index:2}
</style></head><body>
<div id="bg"></div><div id="shade"></div>
<header><img id="cover" alt=""><div id="meta"><b id="title"></b><span id="artist"></span></div></header>
<div id="lines"></div><div id="bar"></div><div id="msg"></div><footer id="src"></footer>
<script>
const stateUrl = location.pathname.replace(/\/$/, "") + "/state";
let S = null, offset = 0, trackId = null, lineEls = [], activeIdx = -2;
const $ = id => document.getElementById(id);
function esc(t){return t.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function build(){
  const box = $("lines"); box.innerHTML = ""; lineEls = []; activeIdx = -2;
  box.className = S.synced ? "" : "static";
  if (!S.lines.length){ $("msg").textContent = "No lyrics for this song"; return; }
  $("msg").textContent = "";
  S.lines.forEach((l) => {
    const d = document.createElement("div"); d.className = "line";
    if (S.synced && l.w.length) d.innerHTML = l.w.map(w => `<span class="w">${esc(w[2])}</span>`).join(" ");
    else d.textContent = l.t;
    box.appendChild(d); lineEls.push(d);
  });
}
async function poll(){
  try{
    const r = await fetch(stateUrl, {cache:"no-store"});
    if (r.status === 404){ $("msg").textContent = "This session ended"; S = null; return; }
    const s = await r.json(); offset = s.now_ms - Date.now();
    const changed = !S || s.track.id !== trackId || s.lines.length !== S.lines.length;
    S = s;
    if (changed){
      trackId = s.track.id; $("title").textContent = s.track.title; $("artist").textContent = s.track.artist;
      if (s.track.cover){ $("cover").src = s.track.cover; $("bg").style.backgroundImage = `url("${s.track.cover}")`; }
      $("src").textContent = s.source ? "Lyrics: " + s.source : ""; document.title = s.track.title + " — lyrics";
      build();
    }
    $("msg").textContent = S.lines.length ? (S.playing ? "" : "Paused") : "No lyrics for this song";
  }catch(e){}
}
function frame(){
  if (S && S.synced && lineEls.length){
    const now = S.playing ? Date.now() + offset : (S.stopped_ms || Date.now() + offset);
    const pos = (now - S.start_ms) / 1000;
    $("bar").style.width = Math.min(100, 100 * pos * 1000 / (S.track.duration_ms || 1)) + "%";
    let idx = -1; for (let i = 0; i < S.lines.length; i++){ if (S.lines[i].s <= pos) idx = i; else break; }
    if (idx !== activeIdx){
      lineEls.forEach((el, i) => { el.classList.toggle("active", i === idx); el.classList.toggle("past", i < idx); });
      if (idx >= 0) lineEls[idx].scrollIntoView({behavior:"smooth", block:"center"});
      activeIdx = idx;
    }
    if (idx >= 0){
      const words = S.lines[idx].w, spans = lineEls[idx].children;
      for (let i = 0; i < words.length && i < spans.length; i++){
        const [a, b] = words[i]; const p = pos <= a ? 0 : pos >= b ? 100 : 100 * (pos - a) / Math.max(.05, b - a);
        spans[i].style.setProperty("--p", p.toFixed(1) + "%");
      }
    }
  }
  requestAnimationFrame(frame);
}
poll(); setInterval(poll, 3000); requestAnimationFrame(frame);
</script></body></html>"""
