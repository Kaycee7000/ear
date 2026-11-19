"""Run the example project through the real containers (and, with --model, the real coding model).

    python3 selftest.py IMAGE            offline checks: failing test, passing build with an artifact
    python3 selftest.py IMAGE --model    also: failure analysis and a review, printed with timings

Writes a Markdown summary to $GITHUB_STEP_SUMMARY when that is set. Exits non-zero on a broken pipeline.
The model's wording is printed for a person to judge; only its shape is checked.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE / "examples"
DISCOUNT = """// Discount codes for checkout.
const CODES = { SAVE10: 10, HALF: 50 };

export function applyDiscount(total, code) {
  const percent = CODES[code];
  return total - percent;
}
"""


def run(project, plan, image, out, model, base=None):
    args = [sys.executable, str(HERE / "runner.py"), "--local", str(project), "--plan", str(plan),
            "--image", image, "--out", str(out)]
    if base:
        args += ["--base", base]
    if not model:
        args.append("--no-model")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if proc.returncode:
        raise SystemExit(f"runner failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout)


def check(condition, message):
    if not condition:
        raise SystemExit("SELF-TEST FAILED: " + message)


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def main():
    image, model = sys.argv[1], "--model" in sys.argv
    lines = ["## code-check self-test", ""]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        project = tmp / "cart"
        shutil.copytree(EXAMPLES / "cart", project)

        failing = run(project, EXAMPLES / "test-plan.json", image, tmp / "out1", model)
        test = failing["steps"][-1]
        check(failing["outcome"] == "failed" and test["name"] == "test" and test["exit_code"] not in (0, None),
              "the failing test was not reported as failed")
        check("31.5" in test["tail"], "the test log tail does not show the assertion")
        check(failing["artifacts"] == [], "a failed run must not collect artifacts")
        lines += [f"- Failing test reported: exit {test['exit_code']}, build {failing['steps'][0]['seconds']} s, "
                  f"test {test['seconds']} s, whole check {failing['total_seconds']} s"]
        if model:
            check(failing["analysis"] is not None, "no failure analysis: " + json.dumps(failing["model"]))
            m = failing["model"]
            lines += [f"- Failure analysis: {m['seconds']} s with {m['model']} ({m['prompt_tokens']} prompt tokens, "
                      f"{m['output_tokens']} written)", "", "```json", json.dumps(failing["analysis"], indent=1), "```", ""]

        passing = run(project, EXAMPLES / "build-plan.json", image, tmp / "out2", False)
        check(passing["outcome"] == "passed" and passing["artifacts"] == ["dist.zip"], "build artifact missing")
        check((tmp / "out2" / "artifacts" / "dist.zip").stat().st_size > 0, "empty artifact")
        lines += [f"- Passing build collected {passing['artifacts']} in {passing['total_seconds']} s"]

        git(project, "init", "-q")
        git(project, "add", "-A")
        git(project, "commit", "-qm", "cart")
        (project / "src" / "discount.js").write_text(DISCOUNT)
        reviewed = run(project, EXAMPLES / "review-plan.json", image, tmp / "out3", model)
        if model:
            check(reviewed["review"] is not None, "no review: " + json.dumps(reviewed["model"]))
            m = reviewed["model"]
            lines += [f"- Review: **{reviewed['outcome']}** in {m['seconds']} s ({m['prompt_tokens']} prompt tokens, "
                      f"{m['output_tokens']} written)", "", "```json", json.dumps(reviewed["review"], indent=1), "```"]
        else:
            check(reviewed["outcome"] == "not_reviewed", "review without a model")
            diff = (tmp / "out3" / "report.md").read_text()
            check("Outcome: **not_reviewed**" in diff, "report missing")
    text = "\n".join(lines) + "\n"
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
