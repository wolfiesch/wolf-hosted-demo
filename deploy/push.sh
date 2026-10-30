#!/usr/bin/env sh
# Copy the source to a Docker host over SSH and build the image there. With --restart, also
# install the systemd unit and restart the service. A reverse proxy on the host's Docker network
# (not in this repository) serves the public route.
#
# Settings come from the environment or from deploy/.env (gitignored):
#   HOST     SSH destination of the Docker host
#   DEST     directory on the host that receives the source (default /opt/wolf-demo)
#   NETWORK  Docker network the reverse proxy shares with the container
set -eu
cd "$(dirname "$0")/.."
[ -f deploy/.env ] && . deploy/.env
: "${HOST:?set HOST (environment or deploy/.env) to the SSH destination}"
: "${NETWORK:?set NETWORK (environment or deploy/.env) to the reverse proxy Docker network}"
DEST="${DEST:-/opt/wolf-demo}"

WOLF_SHA256=$(awk '$2 == "wolf-x86_64-unknown-linux-musl" {print $1}' bin/SHA256SUMS)
[ -n "$WOLF_SHA256" ] || { echo "bin/SHA256SUMS has no linux binary entry" >&2; exit 1; }

rsync -az --delete --exclude .venv --exclude .git --exclude __pycache__ --exclude bin/wolf \
  --exclude deploy/.env ./ "$HOST:$DEST/"
ssh "$HOST" "cd $DEST && docker build --build-arg WOLF_SHA256=$WOLF_SHA256 -t wolf-demo:latest . && docker image prune -f --filter label=app=wolf-demo >/dev/null"

if [ "${1:-}" = "--restart" ]; then
  ssh "$HOST" "sed 's|@NETWORK@|$NETWORK|' $DEST/deploy/wolf-demo.service | sudo tee /etc/systemd/system/wolf-demo.service >/dev/null \
    && sudo systemctl daemon-reload && sudo systemctl enable --now wolf-demo && sudo systemctl restart wolf-demo"
fi
