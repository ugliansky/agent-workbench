#!/usr/bin/env bash

set -euo pipefail

dispatch_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
test_tmp="$(mktemp -d)"

cleanup() {
  rm -rf -- "$test_tmp"
}
trap cleanup EXIT

mkdir -p "$test_tmp/bin" "$test_tmp/repo" "$test_tmp/state"
git init -q -b main "$test_tmp/repo"
git -C "$test_tmp/repo" config user.name 'Agent Dispatch Test'
git -C "$test_tmp/repo" config user.email 'agent-dispatch@example.invalid'
printf '# Codecrawler\n' >"$test_tmp/repo/README.md"
git -C "$test_tmp/repo" add README.md
git -C "$test_tmp/repo" commit -q -m 'Initial commit'

cat >"$test_tmp/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >>"$AGENT_DISPATCH_TEST_GH_LOG"
printf '\n' >>"$AGENT_DISPATCH_TEST_GH_LOG"

if [[ "${1:-} ${2:-}" == 'auth status' ]]; then
  exit 0
elif [[ "${1:-} ${2:-}" == 'repo view' ]]; then
  printf '%s\n' '{"nameWithOwner":"ugliansky/codecrawler"}'
elif [[ "${1:-} ${2:-}" == 'issue list' && " $* " == *' --search '* ]]; then
  if [[ -e "$AGENT_DISPATCH_TEST_DISABLE_FILE" ]]; then
    printf '%s\n' '[{"number":99,"title":"[DISABLE DAEMON]","author":{"login":"ugliansky"},"url":"https://github.com/ugliansky/codecrawler/issues/99"}]'
  else
    printf '%s\n' '[]'
  fi
elif [[ "${1:-} ${2:-}" == 'issue list' ]]; then
  printf '%s\n' '[{"number":7,"title":"[agent] Fix movement","body":"## Task\\n\\nFix movement.","author":{"login":"ugliansky"},"url":"https://github.com/ugliansky/codecrawler/issues/7"}]'
elif [[ "${1:-}" == 'api' && " $* " == *' --method POST '* ]]; then
  counter_file="$AGENT_DISPATCH_TEST_COMMENT_COUNTER"
  comment_id="$(cat "$counter_file")"
  printf '%s\n' "$((comment_id + 1))" >"$counter_file"
  printf '%s\n' "$comment_id"
elif [[ "${1:-}" == 'api' && " $* " == *'/events?per_page=100'* ]]; then
  printf '%s\n' '[[{"id":50,"event":"labeled","label":{"name":"agent:ready"},"actor":{"login":"ugliansky"}}]]'
elif [[ "${1:-}" == 'api' && " $* " == *' --paginate '* ]]; then
  printf '%s\n' '[[{"id":101,"body":"<!-- agent-dispatch -->\\nАгенту нужна помощь","user":{"login":"ugliansky"},"html_url":"https://example.test/worker"},{"id":200,"body":"/agent continue Используй SQLite и продолжай.","user":{"login":"ugliansky"},"html_url":"https://example.test/human"}]]'
fi
EOF

cat >"$test_tmp/bin/codex" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%q ' "$@" >>"$AGENT_DISPATCH_TEST_CODEX_LOG"
printf '\n' >>"$AGENT_DISPATCH_TEST_CODEX_LOG"

result_file=''
previous=''
for argument in "$@"; do
  if [[ "$previous" == '--output-last-message' ]]; then
    result_file="$argument"
    break
  fi
  previous="$argument"
done
[[ -n "$result_file" ]]

if [[ " $* " == *' exec resume '* ]]; then
  printf '%s\n' '{"state":"completed","message":"SQLite выбран, работа завершена."}' >"$result_file"
else
  printf '%s\n' '{"state":"needs_human","message":"Выбрать JSON или SQLite?"}' >"$result_file"
fi
printf '%s\n' '{"type":"thread.started","thread_id":"thread-7"}'
printf '%s\n' '{"type":"turn.completed"}'
EOF

chmod +x "$test_tmp/bin/gh" "$test_tmp/bin/codex"
printf '100\n' >"$test_tmp/comment-counter"
: >"$test_tmp/gh.log"
: >"$test_tmp/codex.log"

cat >"$test_tmp/config.toml" <<EOF
[worker]
poll_interval_seconds = 30
state_db = "$test_tmp/state/state.db"

[[projects]]
name = "codecrawler"
github_repo = "ugliansky/codecrawler"
local_path = "$test_tmp/repo"
allowed_authors = ["ugliansky"]
trigger_label = "agent:ready"
close_issue_on_complete = false
sandbox = "workspace-write"
approve_for_me = true

[projects.messages]
start_prompt = "Выполни таск номер \$issue_number."
resume_prompt = "Ответ человека: \$comment_body"
EOF

run_dispatch() {
  PATH="$test_tmp/bin:$PATH" \
    AGENT_DISPATCH_TEST_GH_LOG="$test_tmp/gh.log" \
    AGENT_DISPATCH_TEST_CODEX_LOG="$test_tmp/codex.log" \
    AGENT_DISPATCH_TEST_COMMENT_COUNTER="$test_tmp/comment-counter" \
    AGENT_DISPATCH_TEST_DISABLE_FILE="$test_tmp/disable" \
    python3 "$dispatch_root/src/agent_dispatch.py" \
      --config "$test_tmp/config.toml" "$@"
}

run_dispatch check >/dev/null
run_dispatch run-once

first_status="$(run_dispatch status)"
grep -F $'codecrawler#7\twaiting\tthread=thread-7' <<<"$first_status" >/dev/null
grep -F -- '--sandbox workspace-write' "$test_tmp/codex.log" >/dev/null
grep -F -- '--approve-for-me' "$test_tmp/codex.log" >/dev/null
if grep -F -- 'danger-full-access' "$test_tmp/codex.log" >/dev/null; then
  printf 'integration test found danger-full-access\n' >&2
  exit 1
fi

run_dispatch run-once

second_status="$(run_dispatch status)"
grep -F $'codecrawler#7\tcompleted\tthread=thread-7' <<<"$second_status" >/dev/null
grep -F -- 'exec resume' "$test_tmp/codex.log" >/dev/null
grep -F -- 'thread-7' "$test_tmp/codex.log" >/dev/null
grep -F -- 'Используй SQLite и продолжай.' "$test_tmp/codex.log" >/dev/null
grep -F -- '--remove-label agent:waiting --add-label agent:running' \
  "$test_tmp/gh.log" >/dev/null
grep -F -- '--remove-label agent:running --add-label agent:done' \
  "$test_tmp/gh.log" >/dev/null

codex_calls_before_pause="$(wc -l <"$test_tmp/codex.log")"
touch "$test_tmp/disable"
run_dispatch run-once >/dev/null
codex_calls_after_pause="$(wc -l <"$test_tmp/codex.log")"
[[ "$codex_calls_before_pause" == "$codex_calls_after_pause" ]]
grep -F -- 'agent-dispatch:disable-ack' "$test_tmp/gh.log" >/dev/null
rm "$test_tmp/disable"

printf 'agent-dispatch bridge integration test passed\n'
