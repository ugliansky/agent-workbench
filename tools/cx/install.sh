#!/usr/bin/env bash

set -euo pipefail

cx_install_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cx_install_dir="${CX_INSTALL_DIR:-$HOME/.local/bin}"
cx_codex_home="${CODEX_HOME:-$HOME/.codex}"
cx_agents_file="$cx_codex_home/AGENTS.md"
cx_agents_source="$cx_install_root/codex/AGENTS.md"

mkdir -p "$cx_install_dir" "$cx_codex_home"
install -m 0755 "$cx_install_root/bin/cx" "$cx_install_dir/cx"
install -m 0755 "$cx_install_root/bin/cx-repo" "$cx_install_dir/cx-repo"

if [[ ! -f "$cx_agents_file" ]] ||
    ! grep -Fq 'cx-repo <path-inside-that-repository>' "$cx_agents_file"; then
  if [[ -s "$cx_agents_file" ]]; then
    printf '\n' >>"$cx_agents_file"
  fi
  cat "$cx_agents_source" >>"$cx_agents_file"
fi

printf 'Installed cx and cx-repo in %s\n' "$cx_install_dir"
printf 'Updated Codex instructions in %s\n' "$cx_agents_file"
