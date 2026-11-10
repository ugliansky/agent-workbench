#!/usr/bin/env bash

set -euo pipefail

dispatch_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
dispatch_bin_dir="${AGENT_DISPATCH_BIN_DIR:-$HOME/.local/bin}"
dispatch_systemd_dir="${AGENT_DISPATCH_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
dispatch_config_dir="${AGENT_DISPATCH_CONFIG_DIR:-$HOME/.config/agent-dispatch}"

mkdir -p "$dispatch_bin_dir" "$dispatch_systemd_dir" "$dispatch_config_dir"
install -m 0755 "$dispatch_root/src/agent_dispatch.py" \
  "$dispatch_bin_dir/agent-dispatch"
install -m 0644 "$dispatch_root/systemd/agent-dispatch.service" \
  "$dispatch_systemd_dir/agent-dispatch.service"
install -m 0600 "$dispatch_root/config.example.toml" \
  "$dispatch_config_dir/config.toml.example"

printf 'Installed agent-dispatch in %s\n' "$dispatch_bin_dir"
printf 'Installed user service in %s\n' "$dispatch_systemd_dir"
if [[ -e "$dispatch_config_dir/config.toml" ]]; then
  printf 'Preserved existing configuration in %s/config.toml\n' \
    "$dispatch_config_dir"
else
  printf 'Copy and review %s/config.toml.example before enabling the service\n' \
    "$dispatch_config_dir"
fi
