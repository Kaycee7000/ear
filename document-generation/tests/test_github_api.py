import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ear_document.github import GitHub, api_url

CFG = SimpleNamespace(repo="o/r", workflow="w.yml", ref="main")


class ApiUrlTest(unittest.TestCase):
    def test_default_is_github(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(api_url(), "https://api.github.com")

    def test_a_runner_on_loopback_is_used(self):
        with patch.dict(os.environ, {"EAR_GITHUB_API_URL": "http://127.0.0.1:8295/"}):
            self.assertEqual(api_url(), "http://127.0.0.1:8295")
            gh = GitHub(CFG, "token")
            try:
                self.assertEqual(str(gh.http.base_url), "http://127.0.0.1:8295/repos/o/r/actions/")
            finally:
                asyncio.run(gh.http.aclose())

    def test_any_other_address_is_refused(self):
        for value in ("http://example.com:8295", "https://api.github.com.example.net", "http://127.0.0.1",
                      "http://user@127.0.0.1:8295", "https://127.0.0.1:8295", "http://127.0.0.1:8295/v3",
                      "http://192.168.1.5:8295"):
            with self.subTest(value=value), patch.dict(os.environ, {"EAR_GITHUB_API_URL": value}):
                with self.assertRaises(ValueError):
                    api_url()
                with self.assertRaises(ValueError):
                    GitHub(CFG, "token")


if __name__ == "__main__":
    unittest.main()
