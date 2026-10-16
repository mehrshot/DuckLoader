#!/usr/bin/env bash
# Switches the local telegram-bot-api Docker container to --local mode, so
# the bot can receive files bigger than 20 MB (video -> audio).
#
# In local mode the server saves every file a user sends on disk and getFile
# returns its path inside the container (/var/lib/telegram-bot-api/...).
# This script moves that folder to a host directory the bot can read
# (LOCAL_BOT_API_DIR in the bot, default /home/mahshot/telegram-bot-api):
#
#   1. copies the current data volume there (nothing is lost),
#   2. re-creates the container with the same image, port, restart policy and
#      API credentials, plus TELEGRAM_LOCAL=1 and that folder mounted,
#   3. keeps the old container (stopped) as telegram-bot-api-old for rollback,
#   4. installs a cron job that deletes received videos after an hour.
#
# Usage:   sudo bash tools/enable_local_bot_api.sh [host_dir]
# Rollback (prints at the end too):
#   sudo docker stop telegram-bot-api && sudo docker rm telegram-bot-api
#   sudo docker rename telegram-bot-api-old telegram-bot-api && sudo docker start telegram-bot-api

set -euo pipefail

NAME=telegram-bot-api
OLD=telegram-bot-api-old
WORK_DIR=/var/lib/telegram-bot-api
HOST_DIR=${1:-/home/mahshot/telegram-bot-api}
BOT_USER=${SUDO_USER:-mahshot}
CRON_FILE=/etc/cron.d/duckloader-bot-api-cleanup

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: sudo bash $0" >&2
    exit 1
fi

docker inspect "$NAME" >/dev/null 2>&1 || { echo "Container $NAME not found." >&2; exit 1; }

if docker inspect "$OLD" >/dev/null 2>&1; then
    echo "$OLD already exists (an earlier run?). Remove it or roll back first." >&2
    exit 1
fi

ENV_LINES=$(docker inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}')
if echo "$ENV_LINES" | grep -qE '^TELEGRAM_LOCAL=.+'; then
    echo "$NAME is already in local mode; nothing to do."
    exit 0
fi

IMAGE=$(docker inspect "$NAME" --format '{{.Config.Image}}')
RESTART=$(docker inspect "$NAME" --format '{{.HostConfig.RestartPolicy.Name}}')
if [ -z "$RESTART" ] || [ "$RESTART" = "no" ]; then
    RESTART=unless-stopped
fi
NETWORK=$(docker inspect "$NAME" --format '{{.HostConfig.NetworkMode}}')
SOURCE=$(docker inspect "$NAME" --format "{{range .Mounts}}{{if eq .Destination \"$WORK_DIR\"}}{{.Source}}{{end}}{{end}}")

ENV_ARGS=()
while IFS= read -r line; do
    case "$line" in
        TELEGRAM_LOCAL=*|TELEGRAM_WORK_DIR=*|TELEGRAM_TEMP_DIR=*|"") ;;
        TELEGRAM_*) ENV_ARGS+=(-e "$line") ;;
    esac
done <<< "$ENV_LINES"

NET_ARGS=()
if [ "$NETWORK" = "host" ]; then
    NET_ARGS+=(--network host)
else
    while IFS= read -r line; do
        # "8081/tcp -> 127.0.0.1:8081"; the IPv6 twin of 0.0.0.0 is implied
        [ -z "$line" ] && continue
        case "$line" in *"[::]"*) continue ;; esac
        NET_ARGS+=(-p "${line##*-> }:${line%%/*}")
    done < <(docker port "$NAME")
fi

echo "Image:    $IMAGE"
echo "Restart:  $RESTART"
echo "Network:  ${NET_ARGS[*]:-none}"
echo "Data:     ${SOURCE:-none} -> $HOST_DIR"

# Any failure from here on: put the original container back and start it.
restore() {
    echo "Something failed; restoring the original container." >&2
    if docker inspect "$OLD" >/dev/null 2>&1; then
        docker rm -f "$NAME" >/dev/null 2>&1 || true
        docker rename "$OLD" "$NAME"
    fi
    docker start "$NAME" >/dev/null 2>&1 || true
}
trap restore ERR

mkdir -p "$HOST_DIR"
if [ -n "$SOURCE" ] && [ -d "$SOURCE" ]; then
    docker stop "$NAME" >/dev/null
    cp -a "$SOURCE/." "$HOST_DIR/"
    chown --reference="$SOURCE" "$HOST_DIR"
else
    docker stop "$NAME" >/dev/null
    chown 101:101 "$HOST_DIR"   # the image's telegram-bot-api user
fi

# The bot runs as $BOT_USER: let it read (and clean up) what the server saves.
if command -v setfacl >/dev/null 2>&1; then
    setfacl -R -m "u:$BOT_USER:rwX" "$HOST_DIR"
    setfacl -R -d -m "u:$BOT_USER:rwX" "$HOST_DIR"
else
    echo "setfacl not found (sudo apt install acl); relying on default permissions."
fi

docker rename "$NAME" "$OLD"
docker run -d --name "$NAME" --restart "$RESTART" "${NET_ARGS[@]}" "${ENV_ARGS[@]}" \
    -e TELEGRAM_LOCAL=1 \
    -e TELEGRAM_WORK_DIR="$WORK_DIR" \
    -e TELEGRAM_TEMP_DIR=/tmp/telegram-bot-api \
    -v "$HOST_DIR:$WORK_DIR" \
    "$IMAGE" >/dev/null

trap - ERR

sleep 3
if [ "$(docker inspect "$NAME" --format '{{.State.Running}}')" != "true" ]; then
    echo "The new container is not running. Last log lines:" >&2
    docker logs --tail 30 "$NAME" >&2 || true
    echo "Rolling back..." >&2
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rename "$OLD" "$NAME"
    docker start "$NAME" >/dev/null
    exit 1
fi

cat > "$CRON_FILE" <<CRON
# Delete files users sent to the bot (videos for video -> audio) after an hour.
*/30 * * * * root find $HOST_DIR -type f -mmin +60 \\( -path '*/videos/*' -o -path '*/video_notes/*' -o -path '*/documents/*' \\) -delete
CRON
chmod 644 "$CRON_FILE"

echo
echo "Done: $NAME now runs in local mode with its data in $HOST_DIR."
docker logs --tail 5 "$NAME" || true
echo
echo "Restart the bot:   sudo systemctl restart duckloader"
echo "Rollback:          sudo docker stop $NAME && sudo docker rm $NAME && sudo docker rename $OLD $NAME && sudo docker start $NAME"
echo "When all is well:  sudo docker rm $OLD"
