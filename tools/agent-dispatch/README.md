# `agent-dispatch`

`agent-dispatch` connects GitHub Issues to persistent local Codex threads. It
polls configured repositories for issues with a trigger label, starts Codex in
the matching local checkout, publishes the result as an issue comment, and
resumes the same thread after an allowed user replies.

The worker makes outbound GitHub requests only. It does not expose a webhook or
local port.

## Workflow

1. A configured repository gets an issue labeled `agent:ready`.
2. The worker claims it with `agent:running` and starts Codex in the configured
   local path.
3. Codex fetches and handles the issue through the repository's existing
   instructions, skills, and GitHub access.
4. The final turn returns one structured state:
   `completed`, `needs_human`, or `failed`.
5. For `needs_human`, the worker posts the question, applies `agent:waiting`,
   and stores the Codex thread ID and comment watermark in SQLite.
6. A new comment by an allowed author beginning with `/agent continue`
   resumes that same thread. The cycle continues until completion or failure.

An open issue whose title is exactly `[DISABLE DAEMON]`, created by an allowed
author, pauses that repository. The worker acknowledges the control issue and
keeps polling without claiming or resuming tasks. Closing the issue restores
normal operation. A Codex turn already running in the foreground is not
force-stopped; the pause takes effect before the next polling action.

The dispatcher deliberately does not create branches, worktrees, commits, or
pull requests. Those remain part of the agent's repository-specific workflow.

## Safety model

- Repository paths, GitHub repositories, labels, authors, sandbox mode, and
  message templates come only from local configuration.
- Issue text and comments cannot supply paths or Codex CLI flags.
- Only comments from configured authors resume a waiting thread.
- The worker verifies that the trigger label was most recently applied by a
  configured author; issue authorship alone does not authorize execution.
- Worker comments contain a marker and are never mistaken for human replies,
  even when the worker uses the same GitHub account.
- Only one active issue is allowed per configured local repository.
- A dirty local checkout is not used. The worker asks for human action and
  waits instead.
- `danger-full-access` is intentionally unsupported by the MVP. Codex can run
  read-only or with `workspace-write`; automatic approval review is optional.
- The worker never merges or closes an issue unless explicitly configured to
  close completed issues.

Unattended execution still processes untrusted repository content. A dedicated
OS account or container provides stronger isolation than a sandbox alone.

## Install

Requirements:

- Python 3.11 or newer
- Codex CLI
- Git
- GitHub CLI authenticated for every configured repository

```bash
./tools/agent-dispatch/install.sh
mkdir -p ~/.config/agent-dispatch
cp tools/agent-dispatch/config.example.toml \
  ~/.config/agent-dispatch/config.toml
```

Edit the configuration, then validate it without changing issues:

```bash
agent-dispatch check
agent-dispatch status
agent-dispatch run-once --dry-run
```

Create or update workflow labels in every configured repository:

```bash
agent-dispatch bootstrap
```

Process one polling cycle manually:

```bash
agent-dispatch run-once
```

After testing a real issue, enable the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-dispatch.service
```

## Configuration

Each `[[projects]]` entry connects one GitHub repository to one local checkout.
Add as many projects as needed.

```toml
[worker]
poll_interval_seconds = 30
state_db = "~/.local/state/agent-dispatch/state.db"

[[projects]]
name = "codecrawler"
github_repo = "ugliansky/codecrawler"
local_path = "/home/me/src/codecrawler"
allowed_authors = ["ugliansky"]
trigger_label = "agent:ready"
close_issue_on_complete = false
sandbox = "workspace-write"
approve_for_me = true
resume_command = "/agent continue"
turn_timeout_seconds = 3600
completion_requires_closed_issue = false
disable_issue_title = "[DISABLE DAEMON]"
```

`sandbox` may be `read-only` or `workspace-write`. The tool does not accept
`danger-full-access` because turns originate from remote input. Codex CLI's
`--approve-for-me` already selects the reviewed workspace-write path, so the
worker deliberately omits the mutually exclusive explicit `--sandbox` flag
when automatic approval is enabled. Set `approve_for_me = false` for an
explicit read-only or non-auto-approved workspace-write run.

`completion_requires_closed_issue = true` keeps a project in `agent:waiting`
when Codex reports `completed` before GitHub shows the issue closed with reason
`completed`. This is useful for repositories where merge and tracker cleanup
are part of the definition of done.

To retry an `agent:failed` or `agent:rejected` issue remotely, remove its
terminal state label and reapply the trigger label. The new label event must be
created by an allowed author. The worker resets the persisted thread and starts
a fresh Codex session.

### Message templates

Templates use `string.Template` syntax such as `$issue_number`. Project message
templates can customize both directions of the bridge:

```toml
[projects.messages]
start_prompt = "Выполни таск номер $issue_number."
resume_prompt = """
В issue #$issue_number человек ответил:

$comment_body

Продолжи работу.
"""
waiting_comment = "Агенту нужна помощь:\n\n$message"
completed_comment = "Агент закончил работу:\n\n$message"
failed_comment = "Агент завершился с ошибкой:\n\n$message"
paused_comment = "Worker приостановлен. Закройте этот issue для продолжения."
```

Available values include `$project_name`, `$github_repo`, `$issue_number`,
`$issue_title`, `$issue_url`, `$thread_id`, `$message`, `$comment_author`,
`$comment_body`, `$comment_url`, and `$resume_command`.

## States

- `agent:ready`: queued on GitHub.
- `agent:running`: currently executing a Codex turn.
- `agent:waiting`: waiting for an allowed user's comment.
- `agent:done`: completed.
- `agent:failed`: failed or interrupted.
- `agent:rejected`: invalid author or unsafe task claim.

SQLite stores the issue state, Codex thread ID, and last processed GitHub
comment ID so daemon restarts do not lose the conversation.

## Test

```bash
./tools/agent-dispatch/test.sh
```
