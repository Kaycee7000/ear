import os
import unittest
from unittest.mock import patch

import jobs


class ApiUrlTest(unittest.TestCase):
    def test_default_is_github(self):
        with patch.dict(os.environ, {"EAR_GITHUB_REPO": "o/r"}, clear=True):
            self.assertEqual(jobs.repo_api(), "https://api.github.com/repos/o/r/actions")

    def test_a_runner_on_loopback_is_used(self):
        with patch.dict(os.environ, {"EAR_GITHUB_REPO": "o/r", "EAR_GITHUB_API_URL": "http://localhost:8295"}):
            self.assertEqual(jobs.repo_api(), "http://localhost:8295/repos/o/r/actions")

    def test_any_other_address_is_refused(self):
        for value in ("http://example.com:8295", "https://api.github.com.example.net", "http://127.0.0.1",
                      "http://127.0.0.1:8295/v3", "http://10.0.0.2:8295"):
            with self.subTest(value=value), patch.dict(os.environ, {"EAR_GITHUB_API_URL": value}):
                with self.assertRaises(ValueError):
                    jobs.repo_api()


if __name__ == "__main__":
    unittest.main()
