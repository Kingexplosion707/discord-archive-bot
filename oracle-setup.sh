#!/bin/sh
# Run on the Oracle server (move-to-oracle.sh does this for you). Installs Docker, restores
# the bot's data, asks for the public web address, and starts everything.
set -eu
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Installing Docker..."
    sudo apt-get update -q
    sudo apt-get install -y -q docker.io docker-compose-v2
    sudo usermod -aG docker "$USER"
fi
DOCKER="sudo docker"

set_env() {
    if grep -q "^$1=" .env; then
        sed -i "s|^$1=.*|$1=$2|" .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

echo
echo "The Stremio addons and the lyrics page need a public https address."
echo "Make a Cloudflare Tunnel (see the README), point its hostname at http://bot:8080, then:"
printf 'Public address (like https://bot.yourdomain.com): '
read -r PUBLIC
printf 'Cloudflare tunnel token: '
read -r TOKEN
set_env PUBLIC_URL "$(echo "$PUBLIC" | sed 's#/*$##')"
set_env TUNNEL_TOKEN "$TOKEN"
chmod 600 .env

echo "Building the bot (a few minutes the first time)..."
$DOCKER compose build

if [ -f bot-data.tgz ]; then
    echo "Restoring its memory, so nothing gets uploaded twice..."
    $DOCKER volume create face-archive-bot_bot-data >/dev/null
    $DOCKER run --rm -v face-archive-bot_bot-data:/data -v "$PWD":/in --user 0 --entrypoint tar face-archive-bot \
        xzpf /in/bot-data.tgz -C /data
    rm bot-data.tgz
fi

echo "Testing every connection..."
$DOCKER compose run --rm bot check || {
    echo "Fix the FAIL lines above, then run: cd ~/face-archive-bot && sudo docker compose up -d"
    exit 1
}
$DOCKER compose up -d
echo
echo "Running on Oracle. Logs: cd ~/face-archive-bot && sudo docker compose logs -f bot"
