#!/bin/sh
# Asks for anything missing, builds the bot, connects Google, tests it, and starts it.
set -eu
cd "$(dirname "$0")"

if [ ! -f client_secret.json ]; then
    echo "client_secret.json is missing. Put it next to this script."
    exit 1
fi
chmod 644 client_secret.json

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
fi

ask() {
    if ! grep -q "^$1=.\+" .env; then
        printf '%s: ' "$2"
        read -r value
        sed -i "s|^$1=.*|$1=$value|" .env
    fi
}

if ! grep -q '^ENCRYPTION_KEY=.\+' .env; then
    key=$(openssl rand -base64 32 | tr '+/' '-_')
    sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$key|" .env
fi
ask DISCORD_TOKEN "Discord bot token"
ask DISCORD_GUILD_ID "Discord server ID"
ask DISCORD_OWNER_ID "Your Discord user ID"

docker compose build
docker compose run --rm bot auth
docker compose run --rm bot check
docker compose up -d
echo
echo "The bot is running. Use /archive status in Discord to watch progress."
