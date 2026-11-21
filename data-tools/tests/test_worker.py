import io
import json
import tempfile
import unittest
from datetime import date
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from ear_data.plan import DataError, Dispatch, Request
from runner import command

HAS_ENGINES = find_spec("duckdb") is not None
if HAS_ENGINES:
    import openpyxl
    import pyarrow as pa
    from PIL import Image

    from worker.compute import compute
    from worker.formats import export, load_table
    from worker.query import analyze
    from worker.transfer import fail, publish, stage

ROOT = Path(__file__).resolve().parents[1]


class Objects:
    def __init__(self):
        self.data = {}
        self.writes = []
        self.metadata = {}

    def put_object(self, Bucket, Key, Body, ContentType, Metadata=None, **kw):
        self.data[Key] = Body.read() if hasattr(Body, "read") else Body
        self.writes.append(Key)
        self.metadata[Key] = (ContentType, Metadata or {})

    def get_object(self, Bucket, Key):
        data = self.data[Key]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}


@unittest.skipUnless(HAS_ENGINES, "requires worker wheels")
class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "in"
        self.src.mkdir()
        self.out = Path(self.tmp.name) / "out"
        self.out.mkdir()
        self.s3 = Objects()
        data = (ROOT / "examples/sales.csv").read_bytes()
        self.handle = sha256(data).hexdigest() + ".csv"
        self.s3.data["data/requests/datasets/" + self.handle] = data
        self.plan = json.loads((ROOT / "examples/revenue.json").read_text())
        self.dispatch = self.request(self.plan)
        self.base = f"data/results/{self.dispatch.job_id}/42/2"

    def request(self, plan):
        raw = Request(datasets={"sales": self.handle}, plan=plan).payload_json().encode()
        d = Dispatch(job_id=str(uuid4()), request_sha256=sha256(raw).hexdigest())
        self.s3.data[f"data/requests/{d.job_id}.json"] = raw
        return d

    def test_real_io_phases_grouping_xlsx_chart(self):
        stage(self.s3, "bucket", "data", self.dispatch, self.src)
        compute(self.src, self.out)
        book = openpyxl.load_workbook(self.out / "table.xlsx", read_only=True)
        self.assertEqual(
            list(book.active.values), [("region", "revenue"), ("South", 27.5), ("West", 25)]
        )
        book.close()
        with Image.open(self.out / "chart.png") as im:
            self.assertEqual(im.size, (1200, 660))
            im.verify()
        publish(self.s3, "bucket", self.base, self.dispatch, 42, 2, self.src, self.out)
        self.assertEqual(
            self.s3.writes,
            [self.base + "/table.xlsx", self.base + "/chart.png", self.base + "/result.json"],
        )
        manifest = json.loads(self.s3.data[self.s3.writes[-1]])
        self.assertEqual((manifest["rows"], manifest["columns"], manifest["run_attempt"]), (2, 2, 2))
        self.assertEqual(
            manifest["table"]["sha256"], sha256((self.out / "table.xlsx").read_bytes()).hexdigest()
        )
        self.assertNotIn("SELECT", str(manifest))

    def test_failed_chart_upload_never_commits_manifest(self):
        stage(self.s3, "bucket", "data", self.dispatch, self.src)
        compute(self.src, self.out)
        put = self.s3.put_object

        def flaky(**kw):
            if kw["Key"].endswith(".png"):
                raise OSError
            put(**kw)

        with patch.object(self.s3, "put_object", side_effect=flaky), self.assertRaises(OSError):
            publish(self.s3, "bucket", self.base, self.dispatch, 42, 2, self.src, self.out)
        self.assertNotIn(self.base + "/result.json", self.s3.data)
        fail(self.s3, "bucket", self.base, self.dispatch, 42, 2, "private raw error text")
        self.assertEqual(
            json.loads(self.s3.data[self.base + "/error.json"])["error_code"], "analysis_failed"
        )

    def test_checksum_and_upload_boundary(self):
        self.s3.data["data/requests/datasets/" + self.handle] = b"changed"
        with self.assertRaisesRegex(DataError, "checksum"):
            stage(self.s3, "bucket", "data", self.dispatch, self.src)

    def test_profile_each_input_and_format(self):
        table = pa.table({"id": [1, 2], "qty": [3, 4]})
        for fmt in ("csv", "ndjson", "parquet", "xlsx", "tsv"):
            path = self.src / ("dataset." + fmt)
            if fmt == "tsv":
                path.write_text("id\tqty\n1\t3\n2\t4\n")
            else:
                export(table, path, fmt)
            loaded = load_table(path, 1024**2)
            self.assertEqual(loaded.to_pylist(), table.to_pylist(), fmt)
            plan = Request(datasets={"sales": self.handle}, plan={"profile": "sales"}).plan
            self.assertEqual(analyze({"sales": loaded}, plan)["column_name"].to_pylist(), ["id", "qty"])
        with self.assertRaises(DataError):
            load_table(self.src / "dataset.parquet", 1)

    def test_join_window_cast_and_null_filter(self):
        tables = {
            "sales": pa.table({"id": [1, 1, 2, None], "amount": [2, 3, 7, 100]}),
            "customers": pa.table({"id": [1, 2], "name": ["a", "b"]}),
        }
        sql = """WITH totals AS (SELECT c.name, SUM(s.amount) AS total FROM sales s
               JOIN customers c ON s.id=c.id WHERE s.id IS NOT NULL GROUP BY c.name)
               SELECT name, CAST(total AS INTEGER) AS total, RANK() OVER (ORDER BY total DESC) AS pos
               FROM totals ORDER BY pos"""
        plan = Request(
            datasets={"sales": self.handle, "customers": self.handle}, plan={"sql": sql}
        ).plan
        self.assertEqual(
            analyze(tables, plan).to_pylist(),
            [{"name": "b", "total": 7, "pos": 1}, {"name": "a", "total": 5, "pos": 2}],
        )

    def test_text_dates_and_times(self):
        # Exported meter data often has d/m/yy text dates and text readings with "?" for gaps.
        table = pa.table(
            {
                "Date": ["16/12/06", "17/12/06", "1/1/07", "9/6/07"],
                "Time": ["17:24:00", "0:00:00", "12:30:00", "23:59:00"],
                "kw": ["4.216", "?", "1.5", "2.0"],
            }
        )
        monthly = """WITH r AS (SELECT strptime(Date || ' ' || Time, '%d/%m/%y %H:%M:%S') AS at,
                         TRY_CAST(kw AS DOUBLE) AS kw FROM sales)
                  SELECT strftime(at, '%Y-%m') AS month, quarter(at) AS q, monthname(at) AS name,
                         COUNT(kw) AS readings, SUM(kw) AS kw, MAX(hour(at)) AS last_hour
                  FROM r GROUP BY 1, 2, 3 ORDER BY month"""
        parts = """SELECT split_part(Date, '/', 2) AS m, try_strptime(Date, '%Y-%m-%d') AS wrong,
                          dayofweek(strptime(Date, '%d/%m/%y')) AS dow, isodow(strptime(Date, '%d/%m/%y')) AS iso,
                          week(strptime(Date, '%d/%m/%y')) AS wk, date_part('day', strptime(Date, '%d/%m/%y')) AS d,
                          dayname(strptime(Date, '%d/%m/%y')) AS dn, minute(strptime(Time, '%H:%M:%S')) AS mi,
                          make_date(2007, 1, 1) AS start
                   FROM sales WHERE Date = '16/12/06'"""
        plans = [Request(datasets={"sales": self.handle}, plan={"sql": q}).plan for q in (monthly, parts)]
        self.assertEqual(
            analyze({"sales": table}, plans[0]).to_pylist(),
            [
                {"month": "2006-12", "q": 4, "name": "December", "readings": 1, "kw": 4.216, "last_hour": 17},
                {"month": "2007-01", "q": 1, "name": "January", "readings": 1, "kw": 1.5, "last_hour": 12},
                {"month": "2007-06", "q": 2, "name": "June", "readings": 1, "kw": 2.0, "last_hour": 23},
            ],
        )
        self.assertEqual(
            analyze({"sales": table}, plans[1]).to_pylist(),
            [
                {"m": "12", "wrong": None, "dow": 6, "iso": 6, "wk": 50, "d": 16, "dn": "Saturday", "mi": 24,
                 "start": date(2007, 1, 1)}
            ],
        )

    def test_question_mark_is_a_missing_number(self):
        # The household power export marks gaps with "?": numbers must stay numbers, text keeps its "?".
        path = self.src / "power.csv"
        path.write_text("Date,Global_active_power,Note\n16/12/2006,4.25,ok\n21/12/2006,?,?\n22/12/2006,1.5,\n")
        loaded = load_table(path, 1024**2)
        self.assertEqual(str(loaded.schema.field("Global_active_power").type), "double")
        self.assertEqual(loaded.column("Global_active_power").to_pylist(), [4.25, None, 1.5])
        self.assertEqual(loaded.column("Note").to_pylist(), ["ok", "?", ""])
        sql = "SELECT COUNT(Global_active_power) AS readings, SUM(Global_active_power) AS kw FROM power"
        plan = Request(datasets={"power": self.handle}, plan={"sql": sql}).plan
        self.assertEqual(analyze({"power": loaded}, plan).to_pylist(), [{"readings": 2, "kw": 5.75}])

    def test_out_of_scope_queries_and_cardinality(self):
        table = pa.table({"x": list(range(501))})
        for sql in (
            "SELECT 1; SELECT 2",
            "CREATE TABLE q AS SELECT 1",
            "SELECT * FROM missing",
            "SELECT * FROM read_csv('unregistered.csv')",
            "SELECT * FROM information_schema.tables",
            "SELECT a.x AS x1, b.x AS x2 FROM sales a CROSS JOIN sales b",
        ):
            plan = Request(datasets={"sales": self.handle}, plan={"sql": sql}).plan
            with self.assertRaises(DataError) as exc:
                analyze({"sales": table}, plan)
            self.assertEqual(
                exc.exception.code, "limit_exceeded" if "CROSS JOIN" in sql else "query_invalid"
            )

    def test_xlsx_literal_and_empty_table(self):
        table = pa.table({"formula": ["=1+2"]})
        path = self.out / "literal.xlsx"
        export(table, path, "xlsx")
        book = openpyxl.load_workbook(path)
        self.assertEqual(book.active["A2"].data_type, "s")
        book.close()
        export(table.slice(0, 0), self.out / "empty.ndjson", "ndjson")
        self.assertEqual((self.out / "empty.ndjson").read_bytes(), b"")
        from worker.formats import draw

        req = Request(datasets={"sales": self.handle}, plan=self.plan)
        with self.assertRaisesRegex(DataError, "chart"):
            draw(table, req.plan.chart, self.out / "chart.png")


class Isolation(unittest.TestCase):
    def test_only_transfer_phases_get_network_and_keys(self):
        args = command("image", "compute", Path("/src"), Path("/out"))
        self.assertIn("none", args)
        self.assertIn("--read-only", args)
        self.assertFalse(any("KEY" in s or s == "--env" for s in args))
        self.assertTrue(any("target=/input,readonly" in s for s in args))
        args = command("image", "publish", Path("/src"), Path("/out"), ["EAR_IO_KEY"])
        self.assertIn("EAR_IO_KEY", args)
        self.assertTrue(any("target=/output,readonly" in s for s in args))
