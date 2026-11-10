# Local tmux integration

- A tmux session started with `cx` has a companion `lazygit` pane. Before the first write to a Git repository, run `cx-repo <path-inside-that-repository>` so the companion pane shows the repository being modified.
- Run `cx-repo` again before writing to a different repository. Do not switch the pane for read-only inspection of another repository.
- A failure to switch the companion pane must not block the requested repository work; briefly report the synchronization failure and continue.

# Terminal links

- Use short descriptive labels for web links so they do not wrap in a tmux pane.
- For GitHub targets, also provide a short `gh browse` command when practical, so the target can be opened without selecting or copying a long URL.
