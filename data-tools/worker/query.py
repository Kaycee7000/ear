"""Run one read-only analytical query over explicitly registered Arrow tables."""

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp

from ear_data.plan import MAX_COLS, MAX_ROWS, DataError

# SQLGlot's names: STR_TO_TIME is strptime, DATE_FROM_PARTS make_date, DAY_OF_WEEK dayofweek, DAYOFWEEK_ISO isodow.
FUNCTIONS = set(
    """
ABS AVG APPROX_DISTINCT APPROX_COUNT_DISTINCT APPROX_QUANTILE ARRAY_AGG CASE CAST CEIL COALESCE CONCAT
CONCAT_WS CORR COUNT CUME_DIST CURRENT_DATE DATE DATE_ADD DATE_DIFF DATE_SUB DATE_TRUNC DENSE_RANK
EXTRACT FIRST FIRST_VALUE FLOOR GREATEST IF LAG LAST LAST_VALUE LEAD LEAST LENGTH LN LOWER MAX MEDIAN MIN
MONTH NULLIF NTILE PERCENT_RANK PERCENTILE_CONT POW RANK REPLACE ROUND ROW_NUMBER SQRT STDDEV STDDEV_POP
STDDEV_SAMP STRFTIME SUBSTRING SUM TIME_TO_STR TIMESTAMP_DIFF TIMESTAMP_TRUNC TRIM TRY_CAST UPPER
VARIANCE VARIANCE_POP YEAR DAY AND OR
STR_TO_TIME TRY_STRPTIME DATE_FROM_PARTS DATE_PART HOUR MINUTE QUARTER WEEK DAY_OF_WEEK DAYOFWEEK_ISO
DAYNAME MONTHNAME SPLIT_PART
""".split()
)


def select_sql(text, aliases):
    parsed = sqlglot.parse(text, read="duckdb")
    if len(parsed) != 1 or not isinstance(parsed[0], (exp.Select, exp.SetOperation)):
        raise DataError("query_invalid")
    query = parsed[0]
    names = set(aliases) | {cte.alias.lower() for cte in query.find_all(exp.CTE)}
    for node in query.walk():
        if node.key in {
            "into",
            "pragma",
            "command",
            "copy",
            "attach",
            "lock",
            "install",
            "load",
            "insert",
            "delete",
            "update",
        }:
            raise DataError("query_invalid")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise DataError("query_invalid")
        if isinstance(node, exp.Table):
            if (
                not isinstance(node.this, exp.Identifier)
                or node.db
                or node.catalog
                or node.name.lower() not in names
            ):
                raise DataError("query_invalid")
        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            if name.upper() not in FUNCTIONS:
                raise DataError("query_invalid")
    return query.sql(dialect="duckdb", comments=False)


def analyze(tables, plan):
    try:
        with duckdb.connect(
            config={
                "enable_external_access": "false",
                "python_enable_replacements": "false",
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "threads": "2",
                "memory_limit": "1GB",
            }
        ) as db:
            for name, table in tables.items():
                db.register(name, table)
            db.execute("SET lock_configuration=true")
            if plan.profile:
                sql = f'SUMMARIZE "{plan.profile}"'
            else:
                sql = select_sql(plan.sql, tables)
                statements = db.extract_statements(sql)
                if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                    raise DataError("query_invalid")
            reader = db.sql(sql).to_arrow_reader(4096)
            names = reader.schema.names
            if len(set(n.casefold() for n in names)) != len(names) or any(
                not n or len(n) > 128 for n in names
            ):
                raise DataError("query_invalid")
            if len(names) > MAX_COLS:
                raise DataError("limit_exceeded")
            rows = size = 0
            batches = []
            for batch in reader:
                rows += batch.num_rows
                size += batch.nbytes
                if rows > MAX_ROWS or size > 192 * 1024**2:
                    raise DataError("limit_exceeded")
                batches.append(batch)
            return pa.Table.from_batches(batches, schema=reader.schema)
    except DataError:
        raise
    except (MemoryError, duckdb.OutOfMemoryException):
        raise DataError("limit_exceeded") from None
    except Exception:
        raise DataError("query_invalid") from None
