from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "agent_dispatch.py"
SPEC = importlib.util.spec_from_file_location("agent_dispatch", MODULE_PATH)
assert SPEC and SPEC.loader
agent_dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent_dispatch
SPEC.loader.exec_module(agent_dispatch)


class ConfigTests(unittest.TestCase):
    def write_config(self, root: Path, sandbox: str = "workspace-write") -> Path:
        repository = root / "repo"
        repository.mkdir()
        config = root / "config.toml"
        config.write_text(
            f"""
[worker]
poll_interval_seconds = 15
state_db = "{root / 'state.db'}"

[[projects]]
name = "codecrawler"
github_repo = "ugliansky/codecrawler"
local_path = "{repository}"
allowed_authors = ["ugliansky"]
trigger_label = "agent:ready"
sandbox = "{sandbox}"
approve_for_me = true

[projects.messages]
start_prompt = "Выполни таск номер $issue_number."
""",
            encoding="utf-8",
        )
        return config

    def test_loads_generic_project_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = agent_dispatch.load_config(self.write_config(root))
            project = config.projects[0]
            self.assertEqual(project.name, "codecrawler")
            self.assertEqual(project.github_repo, "ugliansky/codecrawler")
            self.assertEqual(project.allowed_authors, frozenset({"ugliansky"}))
            self.assertEqual(project.labels.waiting, "agent:waiting")
            self.assertIn("$issue_number", project.messages.start_prompt)
            self.assertEqual(project.resume_command, "/agent continue")
            self.assertEqual(project.turn_timeout_seconds, 3600)
            self.assertEqual(project.disable_issue_title, "[DISABLE DAEMON]")
            self.assertFalse(project.network_access)

    def test_rejects_danger_full_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(agent_dispatch.DispatchError, "sandbox"):
                agent_dispatch.load_config(
                    self.write_config(root, sandbox="danger-full-access")
                )

    def test_rejects_auto_approval_with_read_only_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                agent_dispatch.DispatchError, "approve_for_me"
            ):
                agent_dispatch.load_config(
                    self.write_config(root, sandbox="read-only")
                )

    def test_rejects_unknown_template_variable(self):
        with self.assertRaisesRegex(agent_dispatch.DispatchError, "unknown"):
            agent_dispatch.validate_template("Hello $unknown", "test")


class ProtocolTests(unittest.TestCase):
    def test_extracts_thread_id_from_jsonl(self):
        output = "\n".join(
            [
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "thread.started", "thread_id": "thread-42"}),
                json.dumps({"type": "turn.completed"}),
            ]
        )
        self.assertEqual(
            agent_dispatch.thread_id_from_jsonl(output),
            "thread-42",
        )

    def test_finds_actor_of_latest_trigger_label_event(self):
        class StubRunner:
            def run(self, args, **kwargs):
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        [[
                            {
                                "event": "labeled",
                                "label": {"name": "agent:ready"},
                                "actor": {"login": "old-user"},
                            },
                            {
                                "event": "labeled",
                                "label": {"name": "agent:ready"},
                                "actor": {"login": "ugliansky"},
                            },
                        ]]
                    ),
                    stderr="",
                )

        github = agent_dispatch.GitHub(StubRunner())
        project = SimpleNamespace(github_repo="ugliansky/codecrawler")
        self.assertEqual(
            github.latest_label_actor(project, 7, "agent:ready"),
            "ugliansky",
        )

    def test_reads_completion_reason_from_stable_rest_fields(self):
        class StubRunner:
            def run(self, args, **kwargs):
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        {"state": "closed", "state_reason": "completed"}
                    ),
                    stderr="",
                )

        github = agent_dispatch.GitHub(StubRunner())
        project = SimpleNamespace(github_repo="ugliansky/codecrawler")
        self.assertEqual(
            github.issue_completion(project, 175),
            ("CLOSED", "COMPLETED"),
        )

    def test_runner_reports_timeout(self):
        with self.assertRaisesRegex(agent_dispatch.DispatchError, "timed out"):
            agent_dispatch.Runner().run(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout_seconds=0.01,
            )

    def test_worker_marker_is_detectable(self):
        body = f"{agent_dispatch.MARKER}\nWaiting for input"
        self.assertIn(agent_dispatch.MARKER, body)

    def test_parses_paginated_json_without_gh_slurp(self):
        documents = agent_dispatch.json_documents('[{"id":1}]\n[{"id":2}]\n')
        self.assertEqual(documents, [[{"id": 1}], [{"id": 2}]])

    def test_template_renders_issue_and_comment_fields(self):
        rendered = agent_dispatch.render(
            "Task $issue_number: $comment_body",
            {"issue_number": 12, "comment_body": "continue"},
        )
        self.assertEqual(rendered, "Task 12: continue")


class StateStoreTests(unittest.TestCase):
    def test_persists_thread_and_comment_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            store = agent_dispatch.StateStore(Path(directory) / "state.db")
            issue = agent_dispatch.Issue(
                project_name="codecrawler",
                number=7,
                title="Fix movement",
                author="ugliansky",
                url="https://github.com/ugliansky/codecrawler/issues/7",
            )
            task = store.create(issue, "running")
            task = store.update(
                task,
                state="waiting",
                thread_id="thread-7",
                last_comment_id=99,
                last_message="Choose a format",
            )
            restored = store.get("codecrawler", 7)
            self.assertEqual(restored, task)
            self.assertEqual(restored.thread_id, "thread-7")
            self.assertEqual(restored.last_comment_id, 99)

    def test_resets_terminal_task_for_remote_requeue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = agent_dispatch.StateStore(Path(directory) / "state.db")
            issue = agent_dispatch.Issue(
                project_name="codecrawler",
                number=8,
                title="Initial title",
                author="ugliansky",
                url="https://example.test/8",
            )
            task = store.create(issue, "failed")
            store.update(
                task,
                thread_id="old-thread",
                last_comment_id=123,
                last_message="old failure",
            )
            refreshed = dataclasses.replace(issue, title="Updated title")
            reset = store.reset(refreshed, "running")
            self.assertEqual(reset.state, "running")
            self.assertEqual(reset.issue_title, "Updated title")
            self.assertIsNone(reset.thread_id)
            self.assertEqual(reset.last_comment_id, 0)
            self.assertEqual(reset.last_message, "")


class RecoveryTests(unittest.TestCase):
    def test_replays_consumed_resume_command_after_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = agent_dispatch.Project(
                name="codecrawler",
                github_repo="ugliansky/codecrawler",
                local_path=root,
                allowed_authors=frozenset({"ugliansky"}),
                trigger_label="agent:queued",
                close_issue_on_complete=False,
                sandbox="workspace-write",
                approve_for_me=True,
                network_access=False,
                resume_command="/agent continue",
                turn_timeout_seconds=3600,
                completion_requires_closed_issue=True,
                disable_issue_title="[DISABLE DAEMON]",
                labels=agent_dispatch.Labels(),
                messages=agent_dispatch.Messages(),
            )
            dispatcher = agent_dispatch.Dispatcher(
                agent_dispatch.Config(
                    poll_interval_seconds=30,
                    state_db=root / "state.db",
                    projects=(project,),
                )
            )
            issue = agent_dispatch.Issue(
                project_name="codecrawler",
                number=175,
                title="Dispatch protocol",
                author="ugliansky",
                url="https://example.test/175",
            )
            task = dispatcher.store.create(issue, "running")
            dispatcher.store.update(
                task,
                thread_id="thread-175",
                last_comment_id=200,
                last_message="previous question",
            )

            class FakeGitHub:
                def __init__(self):
                    self.transitions = []

                def comments(self, project, issue_number):
                    return [
                        agent_dispatch.Comment(
                            comment_id=199,
                            author="ugliansky",
                            body="<!-- agent-dispatch --> waiting",
                            url="https://example.test/worker",
                        ),
                        agent_dispatch.Comment(
                            comment_id=200,
                            author="ugliansky",
                            body="/agent continue accepted risk",
                            url="https://example.test/human",
                        ),
                    ]

                def comment(self, project, issue_number, body):
                    self.body = body
                    return 201

                def transition(self, project, issue_number, remove, add):
                    self.transitions.append((remove, add))

            github = FakeGitHub()
            dispatcher.github = github
            dispatcher._recover_interrupted()

            recovered = dispatcher.store.get("codecrawler", 175)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.state, "waiting")
            self.assertEqual(recovered.thread_id, "thread-175")
            self.assertEqual(recovered.last_comment_id, 199)
            self.assertIn("автоматически", github.body)
            self.assertEqual(
                github.transitions,
                [(project.labels.running, project.labels.waiting)],
            )


if __name__ == "__main__":
    unittest.main()
