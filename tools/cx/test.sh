#!/usr/bin/env bash

set -euo pipefail

cx_test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cx_test_tmp="$(mktemp -d)"
cx_test_socket="cx-test-$$"
cx_real_tmux="$(command -v tmux)"

cx_test_cleanup() {
  "$cx_real_tmux" -L "$cx_test_socket" kill-server 2>/dev/null || true
  rm -rf -- "$cx_test_tmp"
}
trap cx_test_cleanup EXIT

mkdir -p "$cx_test_tmp/bin" "$cx_test_tmp/home" "$cx_test_tmp/repo"
git -C "$cx_test_tmp/repo" init -q

cat >"$cx_test_tmp/bin/tmux" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == 'attach-session' ]]; then
  exit 0
fi
exec "$cx_real_tmux" -L "$cx_test_socket" "\$@"
EOF

cat >"$cx_test_tmp/bin/codex" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$CX_TEST_OUTPUT/codex.args"
printf '%s\n' "${CX_TMUX_SESSION:-}" >"$CX_TEST_OUTPUT/codex.session"
sleep 30
EOF

cat >"$cx_test_tmp/bin/lazygit" <<'EOF'
#!/usr/bin/env bash
sleep 30
EOF

chmod +x "$cx_test_tmp/bin/tmux" "$cx_test_tmp/bin/codex" "$cx_test_tmp/bin/lazygit"

bash -n "$cx_test_root/bin/cx"
bash -n "$cx_test_root/bin/cx-repo"
bash -n "$cx_test_root/install.sh"

(
  cd "$cx_test_tmp/repo"
  unset TMUX TMUX_PANE CX_TMUX_SESSION
  PATH="$cx_test_tmp/bin:$PATH" \
    CX_TEST_OUTPUT="$cx_test_tmp" \
    "$cx_test_root/bin/cx" --yolo
)

cx_session="$("$cx_real_tmux" -L "$cx_test_socket" list-sessions -F '#{session_id}')"
[[ "$(<"$cx_test_tmp/codex.session")" == "$cx_session" ]]
grep -Fx -- '-c' "$cx_test_tmp/codex.args" >/dev/null
grep -Fx -- "shell_environment_policy.set.CX_TMUX_SESSION=\"$cx_session\"" \
  "$cx_test_tmp/codex.args" >/dev/null

cx_lazygit_pane="$(
  "$cx_real_tmux" -L "$cx_test_socket" list-panes -t "$cx_session" \
    -F '#{pane_id}|#{@cx_role}' |
    awk -F '|' '$2 == "lazygit" { print $1 }'
)"
cx_lazygit_pid_before="$(
  "$cx_real_tmux" -L "$cx_test_socket" display-message -p \
    -t "$cx_lazygit_pane" '#{pane_pid}'
)"

PATH="$cx_test_tmp/bin:$PATH" \
  CX_TMUX_SESSION="$cx_session" \
  "$cx_test_root/bin/cx-repo" "$cx_test_tmp/repo" >/dev/null

cx_lazygit_pid_after="$(
  "$cx_real_tmux" -L "$cx_test_socket" display-message -p \
    -t "$cx_lazygit_pane" '#{pane_pid}'
)"
[[ "$cx_lazygit_pid_before" != "$cx_lazygit_pid_after" ]]

if PATH="$cx_test_tmp/bin:$PATH" \
    CX_TMUX_SESSION='$999999' \
    "$cx_test_root/bin/cx-repo" "$cx_test_tmp/repo" >/dev/null 2>&1; then
  printf 'cx-repo accepted a stale tmux session\n' >&2
  exit 1
fi

CX_INSTALL_DIR="$cx_test_tmp/install-bin" \
  CODEX_HOME="$cx_test_tmp/codex-home" \
  HOME="$cx_test_tmp/home" \
  "$cx_test_root/install.sh" >/dev/null
CX_INSTALL_DIR="$cx_test_tmp/install-bin" \
  CODEX_HOME="$cx_test_tmp/codex-home" \
  HOME="$cx_test_tmp/home" \
  "$cx_test_root/install.sh" >/dev/null

[[ -x "$cx_test_tmp/install-bin/cx" ]]
[[ -x "$cx_test_tmp/install-bin/cx-repo" ]]
[[ "$(grep -Fc 'cx-repo <path-inside-that-repository>' \
  "$cx_test_tmp/codex-home/AGENTS.md")" == 1 ]]

printf 'cx integration tests passed\n'
