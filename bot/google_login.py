"""`python -m bot auth`: connect a Google account.

The address-bar paste means this works over plain SSH, with no port forwarding.
"""
from __future__ import annotations

import os
from typing import Callable
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import Flow

from .config import Config
from .google_photos import SCOPES, save_login
from .vault import Vault

REDIRECT_URI = "http://localhost:8765/"


class LoginProblem(Exception):
    """Something the person can fix; the message says what."""


def start_login(config: Config) -> tuple[Flow, str, str]:
    """Returns the flow, the Google link to open, and the state to check the reply against."""
    if not config.client_secret_file.is_file():
        raise LoginProblem(
            f"Can't find the Google client file at {config.client_secret_file}. "
            "Download it from Google Cloud and save it as client_secret.json next to .env."
        )
    try:
        flow = Flow.from_client_secrets_file(
            str(config.client_secret_file), scopes=SCOPES, redirect_uri=REDIRECT_URI
        )
    except PermissionError as exc:
        raise LoginProblem("The bot can't read client_secret.json. Run: chmod 644 client_secret.json") from exc
    except ValueError as exc:
        raise LoginProblem(
            "client_secret.json isn't a Desktop app client. Create one and download it again."
        ) from exc
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return flow, url, state


def finish_login(config: Config, flow: Flow, state: str, pasted: str) -> None:
    """Swaps the pasted localhost address for a saved login. Blocking (talks to Google)."""
    query = parse_qs(urlparse(pasted.strip()).query)
    if "error" in query:
        raise LoginProblem(f"Google reported an error: {query['error'][0]}")
    if query.get("state", [""])[0] != state:
        raise LoginProblem("That address doesn't match this login attempt. Start again.")
    code = query.get("code", [""])[0]
    if not code:
        raise LoginProblem("That address has no login code in it. Copy the whole address and try again.")

    # Google may report scopes in a different form. The saved login still works.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # oauthlib raises many types; all mean "try again".
        raise LoginProblem(f"Google didn't accept that code ({exc}). Start again.") from exc
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise LoginProblem("Google didn't return a long-term login. Start again.")

    config.data_dir.mkdir(parents=True, exist_ok=True)
    save_login(
        Vault(config.encryption_key),
        config.token_file,
        {
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
        },
    )


def run_login(
    config: Config,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    try:
        flow, url, state = start_login(config)
    except LoginProblem as exc:
        say(str(exc))
        return 1

    say("")
    say("1. Open this link in a browser and sign in to the Google account for the archive:")
    say("")
    say(url)
    say("")
    say("2. Allow access. If it says Google hasn't verified the app, choose Advanced, then continue.")
    say("3. Your browser will end on an error page at localhost. That's expected.")
    say("   Copy the whole address from the address bar.")
    say("")
    pasted = ask("Paste the address here: ")
    try:
        finish_login(config, flow, state, pasted)
    except LoginProblem as exc:
        say(str(exc))
        return 1
    say("Google account connected.")
    return 0
