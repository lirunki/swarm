#!/usr/bin/env bash
# Start the local dashboard and connect it to the remote relay.
# For a self-signed relay certificate, run with:
#   SSL_CERT_FILE=/absolute/path/to/relay-cert.pem ./launch_swarm.sh
# Override SWARM_RELAY_NODE_ID / SWARM_RELAY_NAME for multiple controllers.
# Saved dashboard relay Settings take precedence over these CLI defaults.
set -euo pipefail

export SSL_CERT_FILE=/mnt/c/Users/luibriz/Documents/Git/swarm/cert.pem

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
swarm_bin="$script_dir/.venv/bin/swarm"
api_key_file="$script_dir/apikey.txt"

if [[ ! -x "$swarm_bin" ]]; then
  printf 'Swarm is not installed at %s. Create .venv and install this project first.\n' "$swarm_bin" >&2
  exit 1
fi
if [[ ! -f "$api_key_file" || ! -r "$api_key_file" ]]; then
  printf 'Missing or unreadable relay API key file: %s\n' "$api_key_file" >&2
  exit 1
fi

# Windows-mounted checkouts may ignore chmod. Keep a private runtime copy
# on the temporary filesystem without changing or printing the original key.
umask 077
private_dir="$(mktemp -d "${TMPDIR:-/tmp}/swarm-relay-key.XXXXXX")"
trap 'rm -f -- "$private_dir/relay.key"; rmdir -- "$private_dir"' EXIT
cp -- "$api_key_file" "$private_dir/relay.key"
chmod 600 -- "$private_dir/relay.key"

machine_name="$(hostname)"
node_slug="$(printf '%s' "$machine_name" | LC_ALL=C tr -cd 'A-Za-z0-9' | cut -c 1-48)"

"$swarm_bin" serve \
  --host 127.0.0.1 \
  --relay-url wss://achilipuvm.lirunki.com:9443/v1/connect \
  --relay-node-id "${SWARM_RELAY_NODE_ID:-swarm-${node_slug:-local}}" \
  --relay-name "${SWARM_RELAY_NAME:-$machine_name}" \
  --relay-api-key-file "$private_dir/relay.key" \
  "$@"
