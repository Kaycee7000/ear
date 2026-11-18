import json
import resource
import sys
from pathlib import Path

from ear_data.plan import MAX_FILE, DataError, Request, Shape

from .formats import MAX_DECODED, draw, export, load_table
from .query import analyze


def compute(inputs, output):
    task = Request.model_validate_json((inputs / "request.json").read_bytes())
    tables = {}
    remaining = MAX_DECODED
    for name, handle in task.datasets.items():
        table = load_table(inputs / handle, remaining)
        remaining -= table.nbytes
        tables[name] = table
    result = analyze(tables, task.plan)
    table_path = output / ("table." + task.format)
    export(result, table_path, task.format)
    if table_path.stat().st_size > MAX_FILE:
        raise DataError("limit_exceeded")
    if task.plan.chart:
        draw(result, task.plan.chart, output / "chart.png")
    (output / "shape.json").write_text(
        Shape(rows=result.num_rows, columns=result.num_columns).model_dump_json()
    )


def main():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE))
    out = Path("/output")
    try:
        compute(Path("/input"), out)
    except Exception as exc:
        code = exc.code if isinstance(exc, DataError) else "analysis_failed"
        if isinstance(exc, (MemoryError, OSError)) and (
            isinstance(exc, MemoryError) or exc.errno in (27, 28)
        ):
            code = "limit_exceeded"
        (out / "failure.json").write_text(json.dumps({"error_code": code}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
