"""MCP host helpers that decide what the Claude CLI is handed — pure config building, no network."""
import os
import unittest

from jarvis import mcp


class ExposeToCliTests(unittest.TestCase):
    def test_http_always_exposed(self):
        self.assertTrue(mcp._expose_to_cli({"url": "https://example.com/mcp"}))

    def test_stdio_skipped_by_default(self):
        self.assertFalse(mcp._expose_to_cli({"command": "npx", "args": ["x"]}))

    def test_stdio_forced_with_cli_flag(self):
        self.assertTrue(mcp._expose_to_cli({"command": "npx", "args": ["x"], "cli": True}))


class ClaudeCliConfigTests(unittest.TestCase):
    def test_stdio_filesystem_skipped_http_kept_logic(self):
        # default stdio (filesystem demo) is dropped; a cli-forced stdio is kept. (http needs a token,
        # which isn't present in tests, so we assert the stdio behavior here.)
        cfg = {"mcp": {"servers": [
            {"name": "filesystem", "command": "npx", "args": ["-y", "srv-fs", "/tmp"], "enabled": True},
            {"name": "customdb", "command": "node", "args": ["db.js"], "cli": True, "enabled": True},
        ]}}
        conf, allowed = mcp.claude_cli_config(cfg)
        self.assertNotIn("filesystem", conf["mcpServers"])     # redundant stdio dropped (no per-turn spawn)
        self.assertIn("customdb", conf["mcpServers"])          # explicitly forced stdio kept
        self.assertEqual(conf["mcpServers"]["customdb"]["command"], "node")
        self.assertEqual(allowed, ["mcp__customdb"])

    def test_env_refs_resolved_for_forced_stdio(self):
        os.environ["JARVIS_TEST_SECRET"] = "s3cr3t"
        try:
            cfg = {"mcp": {"servers": [
                {"name": "withenv", "command": "node", "args": [], "cli": True,
                 "env": {"API_KEY": "${env:JARVIS_TEST_SECRET}"}, "enabled": True},
            ]}}
            conf, _ = mcp.claude_cli_config(cfg)
            self.assertEqual(conf["mcpServers"]["withenv"]["env"]["API_KEY"], "s3cr3t")
        finally:
            os.environ.pop("JARVIS_TEST_SECRET", None)

    def test_disabled_servers_excluded(self):
        cfg = {"mcp": {"servers": [
            {"name": "off", "command": "node", "args": [], "cli": True, "enabled": False},
        ]}}
        conf, allowed = mcp.claude_cli_config(cfg)
        self.assertEqual(conf["mcpServers"], {})
        self.assertEqual(allowed, [])


if __name__ == "__main__":
    unittest.main()
