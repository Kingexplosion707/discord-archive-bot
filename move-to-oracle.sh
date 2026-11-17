#!/bin/sh
# Run on the Optiplex. Moves the bot, its settings and everything it remembers to the
# Oracle server, starts it there, then switches the Optiplex copy off so it never runs twice.
#
#   sh move-to-oracle.sh ORACLE_IP
set -eu
cd "$(dirname "$0")"

IP="${1:-}"
KEY="$HOME/.ssh/oracle"
if [ -z "$IP" ]; then
    echo "Usage: sh move-to-oracle.sh ORACLE_IP"
    exit 1
fi
if [ ! -f "$KEY" ]; then
    echo "No key at $KEY. Make one with: ssh-keygen -t ed25519 -f ~/.ssh/oracle -N \"\""
    echo "and paste ~/.ssh/oracle.pub into Oracle when you create the server."
    exit 1
fi
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$IP"

echo "Checking the Oracle server answers..."
$SSH true

echo "Stopping the bot here so nothing changes while it moves..."
docker compose stop bot || true

echo "Packing its memory (uploads done, Google login, tracker data)..."
VOLUME="$(docker volume ls -q | grep -E '(^|_)bot-data$' | head -1)"
if [ -z "$VOLUME" ]; then
    echo "Couldn't find the bot's data volume. Is this the right folder?"
    exit 1
fi
docker run --rm -v "$VOLUME":/data -v "$PWD":/out --user 0 --entrypoint tar face-archive-bot \
    czpf /out/bot-data.tgz -C /data .

echo "Packing the bot and its settings..."
tar czf /tmp/face-archive-bot-move.tgz --exclude=./bot-data.tgz --exclude='./tests' --exclude='__pycache__' -C "$PWD" . \
    && mv /tmp/face-archive-bot-move.tgz ./move.tgz

echo "Copying to Oracle..."
$SSH "mkdir -p ~/face-archive-bot"
scp -i "$KEY" move.tgz bot-data.tgz "ubuntu@$IP:~/face-archive-bot/"
rm -f move.tgz

echo "Setting it up on Oracle..."
ssh -t -i "$KEY" "ubuntu@$IP" "cd ~/face-archive-bot && tar xzf move.tgz && rm move.tgz && sh oracle-setup.sh"

echo "Switching the Optiplex copy off for good (its data stays here as a backup)..."
docker compose down
mv bot-data.tgz "bot-data-backup-$(date +%Y%m%d).tgz"
echo
echo "Done. The bot now runs on Oracle. To check on it later: ssh -i ~/.ssh/oracle ubuntu@$IP"
