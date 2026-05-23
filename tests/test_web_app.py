from __future__ import annotations

import unittest

from futu_opend_execution.web.app import _index_html


class WebAppTests(unittest.TestCase):
    def test_main_page_contains_agent_sections(self) -> None:
        page = _index_html()
        self.assertIn("OpenD Trading Agent", page)
        self.assertIn("Kill Switch", page)
        self.assertIn("/api/state", page)


if __name__ == "__main__":
    unittest.main()
