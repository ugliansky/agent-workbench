#!/usr/bin/env python3

"""Bridge GitHub Issues and persistent local Codex threads."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


MARKER = "<!-- agent-dispatch -->"
DISABLE_ACK_MARKER = "<!-- agent-dispatch:disable-ack -->"
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {
            "type": "string",
            "enum": ["completed", "needs_human", "failed"],
        },
        "message": {"type": "string", "minLength": 1},
    },
    "required": ["state", "message"],
    "additionalProperties": False,
}
TEMPLATE_FIELDS = {
    "project_name",
    "github_repo",
    "issue_number",
    "issue_title",
    "issue_url",
    "thread_id",
    "message",
    "comment_author",
    "comment_body",
    "comment_url",
    "resume_command",
}
PROTOCOL = """

Dispatcher protocol:
- Finish this turn with state `completed` only when the task no longer needs
  input from a person.
- Use state `needs_human` when a person must answer a question or perform an
  action before work can continue. Put the exact request in `message`.
- Use state `failed` only when the task cannot continue or recover.
- Do not use these states for ordinary progress updates.
""".strip()


class DispatchError(RuntimeError):
    """A user-visible dispatcher failure."""


@dataclasses.dataclass(frozen=True)
class Labels:
    running: str = "agent:running"
    waiting: str = "agent:waiting"
    completed: str = "agent:done"
    failed: str = "agent:failed"
    rejected: str = "agent:rejected"


@dataclasses.dataclass(frozen=True)
class Messages:
    start_prompt: str = "Выполни таск номер $issue_number."
    resume_prompt: str = (
        "В issue #$issue_number человек $comment_author ответил:\n\n"
        "$comment_body\n\nПродолжи работу над таском."
    )
    started_comment: str = "Локальный агент начал работу над issue #$issue_number."
    waiting_comment: str = (
        "Агенту нужна помощь:\n\n$message\n\n"
        "Ответьте комментарием `$resume_command <ответ>` в этом issue."
    )
    completed_comment: str = "Агент закончил работу:\n\n$message"
    failed_comment: str = "Агент не смог закончить задачу:\n\n$message"
    rejected_comment: str = "Задача отклонена локальным worker:\n\n$message"
    paused_comment: str = (
        "$resume_command не требуется: worker приостановлен, пока этот issue открыт. "
        "Закройте issue, чтобы worker снова начал принимать и продолжать задачи. "
        "Уже запущенный Codex turn принудительно не прерывается."
    )


@dataclasses.dataclass(frozen=True)
class Project:
    name: str
    github_repo: str
    local_path: Path
    allowed_authors: frozenset[str]
    trigger_label: str
    close_issue_on_complete: bool
    sandbox: str
    approve_for_me: bool
    network_access: bool
    resume_command: str
    turn_timeout_seconds: int
    completion_requires_closed_issue: bool
    disable_issue_title: str
    labels: Labels
    messages: Messages


@dataclasses.dataclass(frozen=True)
class Config:
    poll_interval_seconds: int
    state_db: Path
    projects: tuple[Project, ...]


@dataclasses.dataclass(frozen=True)
class Issue:
    project_name: str
    number: int
    title: str
    author: str
    url: str


@dataclasses.dataclass(frozen=True)
class Comment:
    comment_id: int
    author: str
    body: str
    url: str


@dataclasses.dataclass(frozen=True)
class Task:
    project_name: str
    issue_number: int
    issue_title: str
    issue_url: str
    issue_author: str
    state: str
    thread_id: str | None
    last_comment_id: int
    last_message: str


class Runner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                list(args),
                cwd=cwd,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise DispatchError(
                f"command timed out after {timeout_seconds}s: {args[0]}"
            ) from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise DispatchError(
                f"command failed ({result.returncode}): {args[0]}: {detail[-3000:]}"
            )
        return result


def now() -> str:
    return datetime.now(UTC).isoformat()


def required_string(table: dict[str, Any], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def optional_bool(
    table: dict[str, Any], key: str, default: bool, context: str
) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise DispatchError(f"{context}.{key} must be a boolean")
    return value


def optional_int(
    table: dict[str, Any],
    key: str,
    default: int,
    context: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DispatchError(f"{context}.{key} must be an integer")
    if not minimum <= value <= maximum:
        raise DispatchError(
            f"{context}.{key} must be between {minimum} and {maximum}"
        )
    return value


def validate_template(value: str, context: str) -> str:
    try:
        string.Template(value).substitute({field: "value" for field in TEMPLATE_FIELDS})
    except (KeyError, ValueError) as error:
        raise DispatchError(f"invalid template {context}: {error}") from error
    return value


def load_labels(raw: Any, context: str) -> Labels:
    if raw is None:
        return Labels()
    if not isinstance(raw, dict):
        raise DispatchError(f"{context} must be a table")
    defaults = dataclasses.asdict(Labels())
    values = {
        key: required_string(raw, key, context) if key in raw else default
        for key, default in defaults.items()
    }
    if len(set(values.values())) != len(values):
        raise DispatchError(f"{context} labels must be distinct")
    return Labels(**values)


def load_messages(raw: Any, context: str) -> Messages:
    if raw is None:
        return Messages()
    if not isinstance(raw, dict):
        raise DispatchError(f"{context} must be a table")
    defaults = dataclasses.asdict(Messages())
    values: dict[str, str] = {}
    unknown = set(raw) - set(defaults)
    if unknown:
        raise DispatchError(f"unknown {context} keys: {', '.join(sorted(unknown))}")
    for key, default in defaults.items():
        value = raw.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise DispatchError(f"{context}.{key} must be a non-empty string")
        values[key] = validate_template(value.strip(), f"{context}.{key}")
    return Messages(**values)


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise DispatchError(f"configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise DispatchError(f"invalid TOML in {path}: {error}") from error

    worker = raw.get("worker")
    projects_raw = raw.get("projects")
    if not isinstance(worker, dict):
        raise DispatchError("missing [worker] configuration")
    if not isinstance(projects_raw, list) or not projects_raw:
        raise DispatchError("at least one [[projects]] entry is required")

    interval = worker.get("poll_interval_seconds", 30)
    if not isinstance(interval, int) or not 5 <= interval <= 3600:
        raise DispatchError("worker.poll_interval_seconds must be between 5 and 3600")
    state_db = Path(required_string(worker, "state_db", "worker")).expanduser().resolve()

    projects: list[Project] = []
    names: set[str] = set()
    paths: set[Path] = set()
    repositories: set[str] = set()
    for index, values in enumerate(projects_raw):
        context = f"projects[{index}]"
        if not isinstance(values, dict):
            raise DispatchError(f"{context} must be a table")
        name = required_string(values, "name", context)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", name):
            raise DispatchError(f"invalid project name: {name}")
        github_repo = required_string(values, "github_repo", context)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repo):
            raise DispatchError(f"invalid GitHub repository: {github_repo}")
        local_path = Path(required_string(values, "local_path", context)).expanduser().resolve()

        authors = values.get("allowed_authors")
        if (
            not isinstance(authors, list)
            or not authors
            or not all(isinstance(author, str) and author.strip() for author in authors)
        ):
            raise DispatchError(f"{context}.allowed_authors must be a non-empty array")

        sandbox = values.get("sandbox", "workspace-write")
        if sandbox not in ("read-only", "workspace-write"):
            raise DispatchError(
                f"{context}.sandbox must be read-only or workspace-write"
            )
        approve_for_me = optional_bool(values, "approve_for_me", True, context)
        if approve_for_me and sandbox != "workspace-write":
            raise DispatchError(
                f"{context}.approve_for_me requires sandbox = workspace-write"
            )

        if name in names:
            raise DispatchError(f"duplicate project name: {name}")
        if local_path in paths:
            raise DispatchError(f"duplicate local project path: {local_path}")
        if github_repo.casefold() in repositories:
            raise DispatchError(f"duplicate GitHub repository: {github_repo}")
        names.add(name)
        paths.add(local_path)
        repositories.add(github_repo.casefold())

        projects.append(
            Project(
                name=name,
                github_repo=github_repo,
                local_path=local_path,
                allowed_authors=frozenset(
                    author.strip().casefold() for author in authors
                ),
                trigger_label=required_string(values, "trigger_label", context),
                close_issue_on_complete=optional_bool(
                    values, "close_issue_on_complete", False, context
                ),
                sandbox=sandbox,
                approve_for_me=approve_for_me,
                network_access=optional_bool(
                    values, "network_access", False, context
                ),
                resume_command=required_string(values, "resume_command", context)
                if "resume_command" in values
                else "/agent continue",
                turn_timeout_seconds=optional_int(
                    values,
                    "turn_timeout_seconds",
                    3600,
                    context,
                    minimum=60,
                    maximum=86400,
                ),
                completion_requires_closed_issue=optional_bool(
                    values,
                    "completion_requires_closed_issue",
                    False,
                    context,
                ),
                disable_issue_title=required_string(
                    values, "disable_issue_title", context
                )
                if "disable_issue_title" in values
                else "[DISABLE DAEMON]",
                labels=load_labels(values.get("labels"), f"{context}.labels"),
                messages=load_messages(values.get("messages"), f"{context}.messages"),
            )
        )

        state_labels = dataclasses.asdict(projects[-1].labels).values()
        if projects[-1].trigger_label in state_labels:
            raise DispatchError(
                f"{context}.trigger_label must differ from workflow state labels"
            )

    return Config(
        poll_interval_seconds=interval,
        state_db=state_db,
        projects=tuple(projects),
    )


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                project_name TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                issue_title TEXT NOT NULL,
                issue_url TEXT NOT NULL,
                issue_author TEXT NOT NULL,
                state TEXT NOT NULL,
                thread_id TEXT,
                last_comment_id INTEGER NOT NULL DEFAULT 0,
                last_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_name, issue_number)
            )
            """
        )
        self.connection.commit()

    @staticmethod
    def _task(row: sqlite3.Row | None) -> Task | None:
        if row is None:
            return None
        return Task(
            project_name=row["project_name"],
            issue_number=row["issue_number"],
            issue_title=row["issue_title"],
            issue_url=row["issue_url"],
            issue_author=row["issue_author"],
            state=row["state"],
            thread_id=row["thread_id"],
            last_comment_id=row["last_comment_id"],
            last_message=row["last_message"],
        )

    def get(self, project_name: str, issue_number: int) -> Task | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE project_name = ? AND issue_number = ?",
            (project_name, issue_number),
        ).fetchone()
        return self._task(row)

    def active(self, project_name: str) -> Task | None:
        row = self.connection.execute(
            """
            SELECT * FROM tasks
            WHERE project_name = ? AND state IN ('running', 'waiting')
            ORDER BY issue_number LIMIT 1
            """,
            (project_name,),
        ).fetchone()
        return self._task(row)

    def running(self) -> list[Task]:
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE state = 'running' ORDER BY project_name, issue_number"
        ).fetchall()
        return [task for row in rows if (task := self._task(row)) is not None]

    def all(self) -> list[Task]:
        rows = self.connection.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC, project_name, issue_number"
        ).fetchall()
        return [task for row in rows if (task := self._task(row)) is not None]

    def create(self, issue: Issue, state: str) -> Task:
        timestamp = now()
        self.connection.execute(
            """
            INSERT INTO tasks (
                project_name, issue_number, issue_title, issue_url, issue_author,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue.project_name,
                issue.number,
                issue.title,
                issue.url,
                issue.author,
                state,
                timestamp,
                timestamp,
            ),
        )
        self.connection.commit()
        task = self.get(issue.project_name, issue.number)
        assert task is not None
        return task

    def reset(self, issue: Issue, state: str) -> Task:
        timestamp = now()
        self.connection.execute(
            """
            UPDATE tasks
            SET issue_title = ?, issue_url = ?, issue_author = ?, state = ?,
                thread_id = NULL, last_comment_id = 0, last_message = '',
                updated_at = ?
            WHERE project_name = ? AND issue_number = ?
            """,
            (
                issue.title,
                issue.url,
                issue.author,
                state,
                timestamp,
                issue.project_name,
                issue.number,
            ),
        )
        self.connection.commit()
        task = self.get(issue.project_name, issue.number)
        if task is None:
            raise DispatchError(
                f"cannot reset unknown task {issue.project_name}#{issue.number}"
            )
        return task

    def update(self, task: Task, **values: Any) -> Task:
        allowed = {"state", "thread_id", "last_comment_id", "last_message"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown task fields: {unknown}")
        values["updated_at"] = now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = list(values.values()) + [task.project_name, task.issue_number]
        self.connection.execute(
            f"UPDATE tasks SET {assignments} WHERE project_name = ? AND issue_number = ?",
            parameters,
        )
        self.connection.commit()
        updated = self.get(task.project_name, task.issue_number)
        assert updated is not None
        return updated


class GitHub:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def ready_issues(self, project: Project) -> list[Issue]:
        result = self.runner.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                project.github_repo,
                "--state",
                "open",
                "--label",
                project.trigger_label,
                "--limit",
                "50",
                "--json",
                "number,title,author,url",
            ]
        )
        try:
            raw_issues = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DispatchError("gh returned invalid JSON for issue list") from error
        issues = []
        for raw in raw_issues:
            author = raw.get("author") or {}
            issues.append(
                Issue(
                    project_name=project.name,
                    number=int(raw["number"]),
                    title=str(raw.get("title") or f"Issue #{raw['number']}").strip(),
                    author=str(author.get("login") or ""),
                    url=str(raw.get("url") or ""),
                )
            )
        return sorted(issues, key=lambda issue: issue.number)

    def disable_issues(self, project: Project) -> list[Issue]:
        result = self.runner.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                project.github_repo,
                "--state",
                "open",
                "--search",
                f'"{project.disable_issue_title}" in:title',
                "--limit",
                "100",
                "--json",
                "number,title,author,url",
            ]
        )
        try:
            raw_issues = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DispatchError("gh returned invalid JSON for disable issue list") from error
        issues = []
        for raw in raw_issues:
            if str(raw.get("title") or "").strip() != project.disable_issue_title:
                continue
            author = raw.get("author") or {}
            issues.append(
                Issue(
                    project_name=project.name,
                    number=int(raw["number"]),
                    title=str(raw["title"]).strip(),
                    author=str(author.get("login") or ""),
                    url=str(raw.get("url") or ""),
                )
            )
        return sorted(issues, key=lambda issue: issue.number)

    def comments(self, project: Project, issue_number: int) -> list[Comment]:
        endpoint = f"repos/{project.github_repo}/issues/{issue_number}/comments?per_page=100"
        result = self.runner.run(["gh", "api", "--paginate", endpoint])
        pages = json_documents(result.stdout)
        comments: list[Comment] = []
        for page in pages:
            if not isinstance(page, list):
                raise DispatchError("gh returned a non-array issue comments page")
            if page and isinstance(page[0], list):
                page = [raw for nested in page for raw in nested]
            for raw in page:
                user = raw.get("user") or {}
                comments.append(
                    Comment(
                        comment_id=int(raw["id"]),
                        author=str(user.get("login") or ""),
                        body=str(raw.get("body") or ""),
                        url=str(raw.get("html_url") or ""),
                    )
                )
        return sorted(comments, key=lambda comment: comment.comment_id)

    def latest_label_actor(
        self,
        project: Project,
        issue_number: int,
        label: str,
    ) -> str | None:
        endpoint = (
            f"repos/{project.github_repo}/issues/{issue_number}/events?per_page=100"
        )
        result = self.runner.run(["gh", "api", "--paginate", endpoint])
        events = []
        for page in json_documents(result.stdout):
            if not isinstance(page, list):
                raise DispatchError("gh returned a non-array issue events page")
            if page and isinstance(page[0], list):
                page = [raw for nested in page for raw in nested]
            events.extend(page)
        matching = [
            event
            for event in events
            if event.get("event") == "labeled"
            and str((event.get("label") or {}).get("name") or "").casefold()
            == label.casefold()
        ]
        if not matching:
            return None
        actor = matching[-1].get("actor") or {}
        login = str(actor.get("login") or "").strip()
        return login or None

    def issue_completion(self, project: Project, issue_number: int) -> tuple[str, str]:
        result = self.runner.run(
            [
                "gh",
                "api",
                f"repos/{project.github_repo}/issues/{issue_number}",
            ]
        )
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DispatchError("gh returned invalid JSON for issue state") from error
        return (
            str(raw.get("state") or "").upper(),
            str(raw.get("state_reason") or "").upper(),
        )

    def comment(self, project: Project, issue_number: int, body: str) -> int:
        marked = f"{MARKER}\n{body.strip()}"
        result = self.runner.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{project.github_repo}/issues/{issue_number}/comments",
                "-f",
                f"body={marked[:65000]}",
                "--jq",
                ".id",
            ]
        )
        try:
            return int(result.stdout.strip())
        except ValueError as error:
            raise DispatchError("gh did not return a comment ID") from error

    def has_disable_ack(self, project: Project, issue_number: int) -> bool:
        return any(
            DISABLE_ACK_MARKER in comment.body
            for comment in self.comments(project, issue_number)
        )

    def transition(
        self,
        project: Project,
        issue_number: int,
        remove_label: str | Sequence[str],
        add_label: str,
    ) -> None:
        labels = [remove_label] if isinstance(remove_label, str) else list(remove_label)
        command = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            project.github_repo,
        ]
        if labels:
            command.extend(["--remove-label", ",".join(labels)])
        command.extend(["--add-label", add_label])
        self.runner.run(
            command
        )

    def close(self, project: Project, issue_number: int) -> None:
        self.runner.run(
            [
                "gh",
                "issue",
                "close",
                str(issue_number),
                "--repo",
                project.github_repo,
            ]
        )

    def bootstrap(self, project: Project) -> None:
        labels = {
            project.trigger_label: "1D76DB",
            project.labels.running: "FBCA04",
            project.labels.waiting: "D4C5F9",
            project.labels.completed: "0E8A16",
            project.labels.failed: "D93F0B",
            project.labels.rejected: "6A737D",
        }
        for name, color in labels.items():
            self.runner.run(
                [
                    "gh",
                    "label",
                    "create",
                    name,
                    "--repo",
                    project.github_repo,
                    "--color",
                    color,
                    "--force",
                ]
            )


def render(template: str, values: dict[str, Any]) -> str:
    fields = {field: "" for field in TEMPLATE_FIELDS}
    fields.update({key: str(value or "") for key, value in values.items()})
    try:
        return string.Template(template).substitute(fields).strip()
    except (KeyError, ValueError) as error:
        raise DispatchError(f"failed to render message template: {error}") from error


def thread_id_from_jsonl(output: str) -> str | None:
    thread_id: str | None = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            thread_id = str(event["thread_id"])
    return thread_id


def json_documents(output: str) -> list[Any]:
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    position = 0
    while position < len(output):
        while position < len(output) and output[position].isspace():
            position += 1
        if position >= len(output):
            break
        try:
            document, position = decoder.raw_decode(output, position)
        except json.JSONDecodeError as error:
            raise DispatchError("gh returned invalid paginated JSON") from error
        documents.append(document)
    return documents


class Dispatcher:
    def __init__(self, config: Config, runner: Runner | None = None) -> None:
        self.config = config
        self.runner = runner or Runner()
        self.github = GitHub(self.runner)
        self.store = StateStore(config.state_db)
        self.projects = {project.name: project for project in config.projects}
        self.logs_dir = config.state_db.parent / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.schema_path = config.state_db.parent / "result.schema.json"
        self.schema_path.write_text(
            json.dumps(RESULT_SCHEMA, indent=2) + "\n", encoding="utf-8"
        )

    def check(self) -> None:
        for binary in ("codex", "gh", "git"):
            if shutil.which(binary) is None:
                raise DispatchError(f"required executable is not in PATH: {binary}")
        self.runner.run(["gh", "auth", "status"])
        for project in self.config.projects:
            self.runner.run(
                ["gh", "repo", "view", project.github_repo, "--json", "nameWithOwner"]
            )
            if not project.local_path.is_dir():
                raise DispatchError(
                    f"local path does not exist ({project.name}): {project.local_path}"
                )
            result = self.runner.run(
                ["git", "-C", str(project.local_path), "rev-parse", "--show-toplevel"]
            )
            if Path(result.stdout.strip()).resolve() != project.local_path:
                raise DispatchError(
                    f"local_path is not the Git root ({project.name}): "
                    f"{project.local_path}"
                )
        print("agent-dispatch configuration is valid")

    def bootstrap(self) -> None:
        for project in self.config.projects:
            self.github.bootstrap(project)
            print(f"initialized labels in {project.github_repo}")

    def status(self) -> None:
        tasks = self.store.all()
        if not tasks:
            print("no tracked tasks")
            return
        for task in tasks:
            thread = task.thread_id or "-"
            print(
                f"{task.project_name}#{task.issue_number}\t{task.state}\t"
                f"thread={thread}\t{task.issue_title}"
            )

    def run_once(
        self,
        *,
        dry_run: bool = False,
        recover: bool = True,
        quiet: bool = False,
    ) -> int:
        if recover and not dry_run:
            self._recover_interrupted()
        processed = 0
        for project in self.config.projects:
            disable_issues = [
                issue
                for issue in self.github.disable_issues(project)
                if issue.author.casefold() in project.allowed_authors
            ]
            if disable_issues:
                for issue in disable_issues:
                    if dry_run:
                        print(f"would pause {project.name} for {issue.url}")
                    elif not self.github.has_disable_ack(project, issue.number):
                        body = (
                            f"{DISABLE_ACK_MARKER}\n"
                            + render(
                                project.messages.paused_comment,
                                {
                                    "project_name": project.name,
                                    "github_repo": project.github_repo,
                                    "issue_number": issue.number,
                                    "issue_title": issue.title,
                                    "issue_url": issue.url,
                                    "resume_command": project.resume_command,
                                },
                            )
                        )
                        self.github.comment(project, issue.number, body)
                processed += len(disable_issues)
                continue

            active = self.store.active(project.name)
            if active and active.state == "waiting":
                if dry_run:
                    print(
                        f"would check replies for {project.name}#{active.issue_number}"
                    )
                elif self._resume_from_comments(project, active):
                    processed += 1
                continue
            if active:
                continue

            ready = self.github.ready_issues(project)
            candidates = [
                issue
                for issue in ready
                if (task := self.store.get(project.name, issue.number)) is None
                or task.state in ("failed", "rejected")
            ]
            if not candidates:
                continue
            issue = candidates[0]
            if dry_run:
                label_actor = self.github.latest_label_actor(
                    project, issue.number, project.trigger_label
                )
                verdict = (
                    "would run"
                    if issue.author.casefold() in project.allowed_authors
                    and label_actor is not None
                    and label_actor.casefold() in project.allowed_authors
                    else "would reject"
                )
                print(f"{verdict} {project.name}#{issue.number}: {issue.title}")
            else:
                self._claim_and_start(project, issue)
            processed += 1
        if processed == 0 and not quiet:
            print("no actionable issues or replies")
        return processed

    def _recover_interrupted(self) -> None:
        for task in self.store.running():
            project = self.projects.get(task.project_name)
            if project is None:
                continue
            replay_comment: Comment | None = None
            if task.thread_id is not None:
                comments = self.github.comments(project, task.issue_number)
                resumable = [
                    comment
                    for comment in comments
                    if comment.comment_id <= task.last_comment_id
                    and comment.author.casefold() in project.allowed_authors
                    and MARKER not in comment.body
                    and comment.body.strip().casefold().startswith(
                        project.resume_command.casefold()
                    )
                ]
                if resumable:
                    replay_comment = resumable[-1]

            if replay_comment is not None:
                message = (
                    "Локальный worker был перезапущен во время resumed turn. "
                    "Последняя разрешённая resume-команда будет автоматически "
                    "переиграна в том же Codex thread после повторной проверки "
                    "live-состояния."
                )
                replay_watermark = replay_comment.comment_id - 1
            else:
                message = (
                    "Локальный worker был перезапущен во время выполнения задачи. "
                    f"Ответьте через `{project.resume_command} <указание>`, чтобы "
                    "безопасно продолжить."
                )
                replay_watermark = task.last_comment_id

            task = self.store.update(
                task,
                state="waiting",
                last_message=message,
                last_comment_id=replay_watermark,
            )
            try:
                if replay_comment is not None:
                    body = f"Восстановление после restart:\n\n{message}"
                else:
                    body = render(
                        project.messages.waiting_comment,
                        self._template_values(project, task, message=message),
                    )
                comment_id = self.github.comment(project, task.issue_number, body)
                self.github.transition(
                    project,
                    task.issue_number,
                    project.labels.running,
                    project.labels.waiting,
                )
                if replay_comment is None:
                    self.store.update(task, last_comment_id=comment_id)
            except DispatchError as error:
                print(
                    f"failed to publish interrupted state for "
                    f"{task.project_name}#{task.issue_number}: {error}",
                    file=sys.stderr,
                )

    def _claim_and_start(self, project: Project, issue: Issue) -> None:
        existing = self.store.get(project.name, issue.number)
        label_actor = self.github.latest_label_actor(
            project, issue.number, project.trigger_label
        )
        author_allowed = issue.author.casefold() in project.allowed_authors
        actor_allowed = (
            label_actor is not None
            and label_actor.casefold() in project.allowed_authors
        )
        if not author_allowed or not actor_allowed:
            task = (
                self.store.reset(issue, "rejected")
                if existing is not None
                else self.store.create(issue, "rejected")
            )
            reasons = []
            if not author_allowed:
                reasons.append(f"автор issue @{issue.author} не входит в allowed_authors")
            if label_actor is None:
                reasons.append("не найдено событие добавления trigger label")
            elif not actor_allowed:
                reasons.append(
                    f"trigger label добавил @{label_actor}, которого нет в allowed_authors"
                )
            message = "; ".join(reasons) + "."
            task = self.store.update(task, last_message=message)
            remove_labels = [project.trigger_label]
            if existing is not None:
                old_label = self._state_label(project, existing.state)
                if old_label is not None:
                    remove_labels.append(old_label)
            self.github.transition(
                project,
                issue.number,
                remove_labels,
                project.labels.rejected,
            )
            body = render(
                project.messages.rejected_comment,
                self._template_values(project, task, message=message),
            )
            self.github.comment(project, issue.number, body)
            return

        task = (
            self.store.reset(issue, "running")
            if existing is not None
            else self.store.create(issue, "running")
        )
        remove_labels = [project.trigger_label]
        if existing is not None:
            old_label = self._state_label(project, existing.state)
            if old_label is not None:
                remove_labels.append(old_label)
        self.github.transition(
            project,
            issue.number,
            remove_labels,
            project.labels.running,
        )
        started = render(
            project.messages.started_comment,
            self._template_values(project, task),
        )
        comment_id = self.github.comment(project, issue.number, started)
        task = self.store.update(task, last_comment_id=comment_id)

        dirty = self.runner.run(
            ["git", "-C", str(project.local_path), "status", "--porcelain"]
        )
        if dirty.stdout.strip():
            self._wait_for_human(
                project,
                task,
                "Локальный репозиторий содержит незакоммиченные изменения. "
                "Очистите рабочее дерево и ответьте в issue, чтобы повторить запуск.",
                thread_id=None,
            )
            return

        prompt = render(
            project.messages.start_prompt,
            self._template_values(project, task),
        )
        self._execute_turn(project, task, prompt, resume=False)

    @staticmethod
    def _state_label(project: Project, state: str) -> str | None:
        return {
            "running": project.labels.running,
            "waiting": project.labels.waiting,
            "completed": project.labels.completed,
            "failed": project.labels.failed,
            "rejected": project.labels.rejected,
        }.get(state)

    def _resume_from_comments(self, project: Project, task: Task) -> bool:
        comments = self.github.comments(project, task.issue_number)
        newer = [
            comment for comment in comments if comment.comment_id > task.last_comment_id
        ]
        if not newer:
            return False
        watermark = max(comment.comment_id for comment in newer)
        allowed = [
            comment
            for comment in newer
            if comment.author.casefold() in project.allowed_authors
            and MARKER not in comment.body
            and comment.body.strip().casefold().startswith(
                project.resume_command.casefold()
            )
        ]
        task = self.store.update(task, last_comment_id=watermark)
        if not allowed:
            return False

        author = ", ".join(dict.fromkeys(comment.author for comment in allowed))
        responses = []
        for comment in allowed:
            response = comment.body.strip()[len(project.resume_command) :].strip()
            responses.append(response or "Продолжай работу.")
        body = "\n\n---\n\n".join(responses)
        latest = allowed[-1]
        values = self._template_values(
            project,
            task,
            comment_author=author,
            comment_body=body,
            comment_url=latest.url,
        )
        prompt = render(project.messages.resume_prompt, values)
        self.github.transition(
            project,
            task.issue_number,
            project.labels.waiting,
            project.labels.running,
        )
        task = self.store.update(task, state="running")

        if task.thread_id is None:
            dirty = self.runner.run(
                ["git", "-C", str(project.local_path), "status", "--porcelain"]
            )
            if dirty.stdout.strip():
                self._wait_for_human(
                    project,
                    task,
                    "Локальный репозиторий всё ещё содержит незакоммиченные "
                    "изменения. Очистите его и ответьте ещё раз.",
                    thread_id=None,
                )
                return True
            start = render(
                project.messages.start_prompt,
                self._template_values(project, task),
            )
            prompt = f"{start}\n\n{prompt}"
            self._execute_turn(project, task, prompt, resume=False)
        else:
            self._execute_turn(project, task, prompt, resume=True)
        return True

    def _execute_turn(
        self,
        project: Project,
        task: Task,
        prompt: str,
        *,
        resume: bool,
    ) -> None:
        result_file = self.logs_dir / (
            f"{project.name}-issue-{task.issue_number}-{time.time_ns()}.result.json"
        )
        full_prompt = f"{prompt.strip()}\n\n{PROTOCOL}"
        config_args = (
            ["-c", "sandbox_workspace_write.network_access=true"]
            if project.network_access
            else []
        )
        if resume:
            assert task.thread_id is not None
            command = [
                "codex",
                "exec",
                "resume",
                *config_args,
                "--json",
                "--output-schema",
                str(self.schema_path),
                "--output-last-message",
                str(result_file),
                task.thread_id,
                full_prompt,
            ]
        else:
            command = [
                "codex",
                "exec",
                *config_args,
                "--json",
                "--output-schema",
                str(self.schema_path),
                "--output-last-message",
                str(result_file),
            ]
            if project.approve_for_me:
                command.append("--approve-for-me")
            else:
                command.extend(["--sandbox", project.sandbox])
            command.append(full_prompt)

        try:
            process = self.runner.run(
                command,
                cwd=project.local_path,
                check=False,
                timeout_seconds=project.turn_timeout_seconds,
            )
        except DispatchError as error:
            self._fail_task(project, task, str(error), thread_id=task.thread_id)
            return
        log_prefix = result_file.with_suffix("")
        log_prefix.with_suffix(".jsonl").write_text(process.stdout, encoding="utf-8")
        log_prefix.with_suffix(".stderr.log").write_text(
            process.stderr, encoding="utf-8"
        )
        discovered_thread = thread_id_from_jsonl(process.stdout)
        thread_id = discovered_thread or task.thread_id
        if process.returncode != 0:
            detail = process.stderr.strip() or "Codex exited without an error message"
            self._fail_task(project, task, detail[-4000:], thread_id=thread_id)
            return
        if thread_id is None:
            self._fail_task(
                project,
                task,
                "Codex completed without reporting a thread ID.",
                thread_id=None,
            )
            return
        try:
            raw_result = json.loads(result_file.read_text(encoding="utf-8"))
            state = raw_result["state"]
            message = raw_result["message"].strip()
        except (OSError, json.JSONDecodeError, KeyError, AttributeError) as error:
            self._fail_task(
                project,
                task,
                f"Не удалось прочитать структурированный результат Codex: {error}",
                thread_id=thread_id,
            )
            return
        if state not in ("completed", "needs_human", "failed") or not message:
            self._fail_task(
                project,
                task,
                "Codex вернул некорректное состояние или пустое сообщение.",
                thread_id=thread_id,
            )
            return

        task = self.store.update(task, thread_id=thread_id, last_message=message)
        if state == "completed":
            if project.completion_requires_closed_issue:
                issue_state, state_reason = self.github.issue_completion(
                    project, task.issue_number
                )
                if issue_state != "CLOSED" or state_reason != "COMPLETED":
                    self._wait_for_human(
                        project,
                        task,
                        "Агент сообщил completed, но issue ещё не закрыт с reason "
                        "completed. Для CodeCrawler это означает, что merge/completion "
                        "workflow не доказан. Проверьте PR и ответьте через "
                        f"`{project.resume_command} <указание>`.",
                        thread_id=thread_id,
                    )
                    return
            body = render(
                project.messages.completed_comment,
                self._template_values(project, task, message=message),
            )
            comment_id = self.github.comment(project, task.issue_number, body)
            self.github.transition(
                project,
                task.issue_number,
                project.labels.running,
                project.labels.completed,
            )
            self.store.update(
                task,
                state="completed",
                last_comment_id=comment_id,
            )
            if project.close_issue_on_complete:
                self.github.close(project, task.issue_number)
        elif state == "needs_human":
            self._wait_for_human(project, task, message, thread_id=thread_id)
        else:
            self._fail_task(project, task, message, thread_id=thread_id)

    def _wait_for_human(
        self,
        project: Project,
        task: Task,
        message: str,
        *,
        thread_id: str | None,
    ) -> None:
        task = self.store.update(task, thread_id=thread_id, last_message=message)
        body = render(
            project.messages.waiting_comment,
            self._template_values(project, task, message=message),
        )
        comment_id = self.github.comment(project, task.issue_number, body)
        self.github.transition(
            project,
            task.issue_number,
            project.labels.running,
            project.labels.waiting,
        )
        self.store.update(
            task,
            state="waiting",
            last_comment_id=comment_id,
        )

    def _fail_task(
        self,
        project: Project,
        task: Task,
        message: str,
        *,
        thread_id: str | None,
    ) -> None:
        task = self.store.update(
            task,
            state="failed",
            thread_id=thread_id,
            last_message=message,
        )
        body = render(
            project.messages.failed_comment,
            self._template_values(project, task, message=message),
        )
        comment_id = self.github.comment(project, task.issue_number, body)
        self.github.transition(
            project,
            task.issue_number,
            project.labels.running,
            project.labels.failed,
        )
        self.store.update(task, last_comment_id=comment_id)

    @staticmethod
    def _template_values(
        project: Project,
        task: Task,
        **extra: Any,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "project_name": project.name,
            "github_repo": project.github_repo,
            "issue_number": task.issue_number,
            "issue_title": task.issue_title,
            "issue_url": task.issue_url,
            "thread_id": task.thread_id or "",
            "message": task.last_message,
            "comment_author": "",
            "comment_body": "",
            "comment_url": "",
            "resume_command": project.resume_command,
        }
        values.update(extra)
        return values


def default_config_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_DISPATCH_CONFIG",
            "~/.config/agent-dispatch/config.toml",
        )
    ).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-dispatch",
        description="Bridge GitHub Issues and persistent local Codex threads.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="configuration file (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate configuration and dependencies")
    commands.add_parser("bootstrap", help="create or update workflow labels")
    commands.add_parser("status", help="show locally tracked tasks")
    run_once = commands.add_parser("run-once", help="process one polling cycle")
    run_once.add_argument(
        "--dry-run",
        action="store_true",
        help="show the next actions without changing issues or starting Codex",
    )
    commands.add_parser("daemon", help="poll configured repositories continuously")
    return parser


def locked(state_db: Path):
    state_db.parent.mkdir(parents=True, exist_ok=True)
    lock_file = state_db.with_suffix(state_db.suffix + ".lock").open(
        "w", encoding="utf-8"
    )
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise DispatchError("another agent-dispatch worker is already running") from error
    return lock_file


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config.expanduser().resolve())
        dispatcher = Dispatcher(config)
        if args.command == "check":
            dispatcher.check()
            return 0
        if args.command == "bootstrap":
            dispatcher.bootstrap()
            return 0
        if args.command == "status":
            dispatcher.status()
            return 0

        lock_file = locked(config.state_db)
        with lock_file:
            if args.command == "run-once":
                dispatcher.run_once(dry_run=args.dry_run)
                return 0
            if args.command == "daemon":
                while True:
                    try:
                        dispatcher.run_once(recover=True, quiet=True)
                    except Exception as error:
                        print(f"agent-dispatch polling error: {error}", file=sys.stderr)
                    time.sleep(config.poll_interval_seconds)
    except DispatchError as error:
        print(f"agent-dispatch: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
