import unittest

from jarvis.dashboard import server


class DashboardLoginTests(unittest.TestCase):
    def test_login_badge_uses_beta(self):
        self.assertIn("<span class=beta>BETA</span>", server.LOGIN_HTML)
        self.assertNotIn("EARLY BETA", server.LOGIN_HTML)

    def test_chat_thought_does_not_fall_back_to_tool_evidence(self):
        html = server.HTML.read_text()

        self.assertNotIn("function evidenceThought", html)
        self.assertIn("Thought is for real model reasoning only", html)
        self.assertIn("const shownThought=visibleThinking(thinking)", html)


if __name__ == "__main__":
    unittest.main()
