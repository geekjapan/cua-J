from __future__ import annotations

import re
import unittest
from pathlib import Path


class AuthorizedWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[6]
        cls.workflow = (
            root / ".github/workflows/ci.yml"
        ).read_text(encoding="utf-8")

    def test_actions_are_pinned_to_commits(self) -> None:
        action_refs = re.findall(r"^\s*(?:-\s+)?uses:\s*[^@\s]+@([^\s]+)", self.workflow, re.MULTILINE)
        self.assertTrue(action_refs)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs))

    def test_workflow_is_credential_free(self) -> None:
        self.assertNotIn("TYPESAFE_API_KEY", self.workflow)
        self.assertNotIn("secrets.", self.workflow)
        self.assertNotIn("run_live", self.workflow)
        self.assertNotIn("--live", self.workflow)

    def test_python_decisions_use_locked_dependencies_and_credential_free_suite(self) -> None:
        self.assertIn("python-decisions:", self.workflow)
        self.assertIn("uv sync --frozen", self.workflow)
        self.assertIn("uv run --frozen python -m unittest discover -s python/tests", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)


if __name__ == "__main__":
    unittest.main()
