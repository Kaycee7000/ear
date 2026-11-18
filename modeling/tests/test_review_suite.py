import unittest
from pathlib import Path

from design import Task

try:
    import yaml
except ImportError:  # PyYAML is only needed to regenerate the review suite
    yaml = None

EXAMPLES = Path(__file__).parents[1] / "examples"


class ReviewSuiteTests(unittest.TestCase):
    def test_examples_are_valid_tasks(self):
        for path in EXAMPLES.glob("*.json"):
            with self.subTest(example=path.name):
                Task.model_validate_json(path.read_bytes())

    @unittest.skipIf(yaml is None, "needs PyYAML")
    def test_review_suite_matches_design_module(self):
        import make_review_suite
        self.assertEqual(make_review_suite.SUITE.read_text(encoding="utf-8"), make_review_suite.build(),
                         "model-review/suites/ear-design.yaml is stale: run python make_review_suite.py")
