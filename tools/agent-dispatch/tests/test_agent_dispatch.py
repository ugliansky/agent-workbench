from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


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

    def test_rejects_danger_full_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(agent_dispatch.DispatchError, "sandbox"):
                agent_dispatch.load_config(
                    self.write_config(root, sandbox="danger-full-access")
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


if __name__ == "__main__":
    unittest.main()
