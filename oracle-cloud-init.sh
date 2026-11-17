#!/bin/bash
# Paste this whole file into Oracle when creating the server:
#   Create instance > Show advanced options > Management > Initialization script > Paste cloud-init script
# Fill in the values between the quotes first. The server sets itself up in about 10 minutes,
# then the bot comes online in Discord. Nothing to do over SSH.

DISCORD_TOKEN=""          # Developer Portal > your app > Bot > Reset Token
GOOGLE_CLIENT_ID=""       # Google Cloud > Google Auth Platform > Clients > your Desktop client
GOOGLE_CLIENT_SECRET=""   # same page: Add secret (copy it right away, Google only shows it once)
PUBLIC_URL=""             # optional: https://bot.yourdomain.com (for Stremio addons and the lyrics page)
TUNNEL_TOKEN=""           # optional: Cloudflare Zero Trust tunnel token for that address

# ---------------------------------------------------------------------------------------------
REPO="https://github.com/Kingexplosion707/discord-archive-bot.git"
DIR=/opt/discord-archive-bot
set -eux
exec > >(tee -a /var/log/discord-bot-setup.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git openssl
systemctl enable --now docker

if [ ! -d "$DIR/.git" ]; then
    git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

cat > client_secret.json <<EOF
{"installed": {"client_id": "$GOOGLE_CLIENT_ID", "client_secret": "$GOOGLE_CLIENT_SECRET",
 "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
 "redirect_uris": ["http://localhost"]}}
EOF
chmod 644 client_secret.json

if [ ! -f .env ]; then
    PROFILES=""
    [ -n "$TUNNEL_TOKEN" ] && PROFILES="tunnel"
    cat > .env <<EOF
DISCORD_TOKEN=$DISCORD_TOKEN
ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
START_FRESH=true
PUBLIC_URL=$PUBLIC_URL
TUNNEL_TOKEN=$TUNNEL_TOKEN
COMPOSE_PROFILES=$PROFILES
EOF
    chmod 600 .env
fi

docker compose up -d --build

# Pick up bot updates once a day.
cat > /etc/cron.daily/discord-archive-bot <<EOF
#!/bin/sh
cd $DIR && git pull -q --ff-only && docker compose up -d --build >/dev/null 2>&1
EOF
chmod 755 /etc/cron.daily/discord-archive-bot
echo "Discord archive bot is running."
