"""Run the repository fixture through the actual network-disabled Docker worker before publication."""

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from runner import command


def main(image):
    examples = Path(__file__).resolve().parent / "examples"
    source = examples / "sales.csv"
    handle = hashlib.sha256(source.read_bytes()).hexdigest() + ".csv"
    with tempfile.TemporaryDirectory() as tmp:
        inputs, outputs = Path(tmp) / "inputs", Path(tmp) / "outputs"
        inputs.mkdir()
        outputs.mkdir()
        shutil.copyfile(source, inputs / handle)
        plan = json.loads((examples / "revenue.json").read_text())
        plan["export"] = "csv"
        (inputs / "request.json").write_text(json.dumps({"datasets": {"sales": handle}, "plan": plan}))
        subprocess.run(command(image, "compute", inputs, outputs), check=True, timeout=90)
        with (outputs / "table.csv").open() as file:
            rows = list(csv.DictReader(file))
        if [(r["region"], float(r["revenue"])) for r in rows] != [("South", 27.5), ("West", 25.0)]:
            raise RuntimeError("Unexpected grouped totals")
        if not (outputs / "chart.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Missing chart")
    print("Container verified with fixture totals and PNG.")


if __name__ == "__main__":
    main(sys.argv[1])
