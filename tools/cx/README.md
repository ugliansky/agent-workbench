# `cx`

`cx` starts a dedicated tmux session with Codex on the left and `lazygit` on
the right. `cx-repo` lets Codex switch only its own companion pane when work
moves to another repository.

## Requirements

- Bash
- Codex CLI
- Git
- tmux
- lazygit

## Install

```bash
./tools/cx/install.sh
```

The installer copies `cx` and `cx-repo` to `${CX_INSTALL_DIR:-$HOME/.local/bin}`
and adds the instructions from [`codex/AGENTS.md`](codex/AGENTS.md) to
`${CODEX_HOME:-$HOME/.codex}/AGENTS.md` when they are not already present.

Make sure the installation directory is in `PATH`, then start Codex as usual:

```bash
cx
cx --yolo
cx resume --last
```

When launched outside tmux, `cx` creates a new session. When launched from an
existing tmux pane, it starts Codex directly and does not alter the current
layout.

## Repository selection

The initial `lazygit` directory is selected in this order:

1. the Git root containing `CX_GIT_DIR`, when set;
2. the Git root containing the current directory;
3. the only child Git repository of the current directory;
4. the current directory.

`cx` passes its tmux session ID through Codex's `shell_environment_policy.set`
configuration. This is necessary because shell commands may be executed by a
shared Codex app-server process rather than by the TUI process itself.

`cx-repo` fails closed when it cannot identify the owning session or when the
session ID is stale. It never falls back to whichever tmux session happens to
be active.

## Test

```bash
./tools/cx/test.sh
```

The test uses a separate tmux socket and temporary fake `codex` and `lazygit`
executables, so it does not touch interactive tmux sessions.
