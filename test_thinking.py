#!/usr/bin/env python3
"""Profile thinking defaults: enum, absent/invalid fail-soft, and fallback.

Run with python test_thinking.py. Fixtures never use a production working root.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import persona
from agentd_dependency import require_agentd
require_agentd("safe_delete")
from safe_delete import safe_rmtree


class ThinkingTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="persona-thinking-"))
        self.root = self.base / "fixture"
        (self.root / "bots/profiles").mkdir(parents=True)
        self.warnings = []

    def tearDown(self):
        deleted, reason = safe_rmtree(self.root, authorized_root=self.base)
        self.assertTrue(deleted, reason)

    def profile(self, name, **extra):
        doc = dict(name=name, form="interactive", caps=[], **extra)
        (self.root / "bots/profiles" / (name + ".json")).write_text(json.dumps(doc))

    def resolve(self, name):
        return persona.resolve(str(self.root), name, emit=lambda f, *a: self.warnings.append(f % a if a else f))

    def test_levels_and_model_unchanged(self):
        for level in persona.THINKING_LEVELS:
            with self.subTest(level=level):
                self.profile("arbitrary", model="provider/unchanged", thinkingLevel=level)
                result = self.resolve("arbitrary")
                self.assertEqual(result["thinkingLevel"], level)
                self.assertEqual(result["model"], "provider/unchanged")
                self.assertEqual(result["provider"], "provider")
                self.assertEqual(result["schemaVersion"], 2)

    def test_absent_is_not_high(self):
        self.profile("plain")
        self.assertIsNone(self.resolve("plain")["thinkingLevel"])
        self.assertFalse(any("thinkingLevel" in w for w in self.warnings))

    def test_invalid_warns_and_skips(self):
        for value in (None, False, 1, "", "HIGH", " high ", [], {}, "extreme"):
            with self.subTest(value=value):
                self.warnings.clear()
                self.profile("bad", thinkingLevel=value)
                self.assertIsNone(self.resolve("bad")["thinkingLevel"])
                self.assertTrue(any("thinkingLevel" in w and "WARN" in w for w in self.warnings))

    def test_fallback_uses_same_default(self):
        self.profile("executor", thinkingLevel="high")
        result = self.resolve("")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["thinkingLevel"], "high")
        self.assertIsNone(self.resolve("missing")["thinkingLevel"])


if __name__ == "__main__":
    unittest.main()
