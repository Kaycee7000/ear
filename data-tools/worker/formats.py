"""Bounded local readers and writers for the disposable analysis container."""

import csv
import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from zipfile import ZipFile

import openpyxl
import pyarrow as pa
import pyarrow.csv as acsv
import pyarrow.json as ajson
import pyarrow.parquet as pq
from openpyxl.cell import WriteOnlyCell

from ear_data.plan import MAX_COLS, MAX_FILE, DataError

MAX_INPUT_ROWS = 1_000_000
MAX_DECODED = 512 * 1024**2


def excel_table(path):
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(item.file_size for item in entries) > 256 * 1024**2:
            raise DataError("limit_exceeded")
    book = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        sheet = book.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        header = list(next(rows))
        if not 1 <= len(header) <= MAX_COLS or not all(
            isinstance(v, str) and v.strip() for v in header
        ):
            raise DataError("dataset_invalid")
        columns = [[] for _ in header]
        for index, row in enumerate(rows, 1):
            if index * len(header) > 1_000_000:
                raise DataError("limit_exceeded")
            if len(row) > len(header) and any(v is not None for v in row[len(header) :]):
                raise DataError("dataset_invalid")
            for i, column in enumerate(columns):
                column.append(row[i] if i < len(row) else None)
        return pa.Table.from_arrays([pa.array(v) for v in columns], names=header)
    finally:
        book.close()


def load_table(path, remaining):
    fmt = path.suffix[1:]
    try:
        if not 0 < path.stat().st_size <= MAX_FILE:
            raise DataError("limit_exceeded")
        if fmt == "xlsx":
            table = excel_table(path)
        else:
            if fmt in ("csv", "tsv"):
                stream = acsv.open_csv(
                    path, parse_options=acsv.ParseOptions(delimiter="," if fmt == "csv" else "\t")
                )
                schema, batches = stream.schema, stream
            elif fmt == "parquet":
                source = pq.ParquetFile(path, arrow_extensions_enabled=False)
                schema, batches = source.schema_arrow, source.iter_batches(batch_size=8192)
            else:
                stream = ajson.open_json(path)
                schema, batches = stream.schema, stream
            if len(schema) > MAX_COLS:
                raise DataError("limit_exceeded")
            loaded = []
            rows = size = 0
            for batch in batches:
                rows += batch.num_rows
                size += batch.nbytes
                if rows > MAX_INPUT_ROWS or size > remaining:
                    raise DataError("limit_exceeded")
                loaded.append(batch)
            table = pa.Table.from_batches(loaded, schema=schema)
        if table.nbytes > remaining or table.num_columns > MAX_COLS:
            raise DataError("limit_exceeded")
        names = table.column_names
        if (
            not names
            or any(not n.strip() or len(n) > 128 for n in names)
            or len({n.casefold() for n in names}) != len(names)
        ):
            raise DataError("dataset_invalid")
        return table
    except DataError:
        raise
    except Exception:
        raise DataError("dataset_invalid") from None


def json_value(v):
    if isinstance(v, (date, datetime, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    raise TypeError("Unsupported value")


def literal(sheet, value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.isoformat()
    if isinstance(value, str) and len(value) > 32767:
        raise DataError("limit_exceeded")
    cell = WriteOnlyCell(sheet, value=value)
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def export(table, path, fmt):
    if fmt == "parquet":
        pq.write_table(table, path, compression="zstd")
    elif fmt == "xlsx":
        book = openpyxl.Workbook(write_only=True)
        sheet = book.create_sheet("Result")
        sheet.append([literal(sheet, n) for n in table.column_names])
        for batch in table.to_batches(max_chunksize=1024):
            for row in batch.to_pylist():
                sheet.append([literal(sheet, v) for v in row.values()])
        book.save(path)
    else:
        with path.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            if fmt == "csv":
                writer.writerow(table.column_names)
            for batch in table.to_batches(max_chunksize=1024):
                for row in batch.to_pylist():
                    if fmt == "csv":
                        writer.writerow(row.values())
                    else:
                        out.write(
                            json.dumps(row, default=json_value, ensure_ascii=False, allow_nan=False)
                            + "\n"
                        )


def draw(table, chart, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if (
        chart.x not in table.column_names
        or chart.y not in table.column_names
        or not 1 <= len(table) <= 500
    ):
        raise DataError("chart_invalid")
    x, values = table[chart.x].to_pylist(), table[chart.y].to_pylist()
    if any(v is None for v in x) or not all(
        isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) and math.isfinite(v)
        for v in values
    ):
        raise DataError("chart_invalid")
    with plt.rc_context(
        {"text.parse_math": False, "axes.spines.top": False, "axes.spines.right": False}
    ):
        fig, ax = plt.subplots(figsize=(10, 5.5), layout="constrained")
        try:
            plot = {"line": ax.plot, "bar": ax.bar, "scatter": ax.scatter}[chart.kind]
            plot(x, [float(v) for v in values], color="#19766c")
            ax.set(xlabel=chart.x, ylabel=chart.y, title=chart.title or "")
            ax.tick_params(axis="x", labelrotation=25)
            fig.savefig(path, format="png", dpi=120)
        except (ValueError, TypeError, OverflowError):
            raise DataError("chart_invalid") from None
        finally:
            plt.close(fig)
