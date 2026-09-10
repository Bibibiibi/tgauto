#!/bin/sh
set -eu

if [ ! -f "${TG_CONFIG}" ] && [ -f "/app/bots.json" ]; then
  mkdir -p "$(dirname "${TG_CONFIG}")"
  cp /app/bots.json "${TG_CONFIG}"
fi

MIHOMO_CONFIG_PATH="${MIHOMO_CONFIG_PATH:-/app/data/mihomo/config.yaml}"
if [ ! -f "${MIHOMO_CONFIG_PATH}" ] && [ -f "/app/mihomo/config.yaml" ]; then
  mkdir -p "$(dirname "${MIHOMO_CONFIG_PATH}")"
  cp /app/mihomo/config.yaml "${MIHOMO_CONFIG_PATH}"
fi

exec "$@"
