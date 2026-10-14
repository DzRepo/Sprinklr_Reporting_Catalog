#!/usr/bin/env python3
"""Build and query the Sprinklr reporting catalog database.

Reads the JSON files in reporting_catalog and loads them into SQLite.
The database answers which reports support a dimension or metric and stores
the characteristics a front end needs. It does not call the Sprinklr API.

    python reporting_db.py build
    python reporting_db.py query --name date --kind dimension
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "reporting_catalog"
DEFAULT_DATABASE = DEFAULT_CATALOG / "reporting_catalog.sqlite"

logger = logging.getLogger("reporting_db")

KINDS = (
    ("dimensions", "dimension"),
    ("metrics", "metric"),
    ("custom_dimensions", "custom_dimension"),
    ("custom_metrics", "custom_metric"),
)
LISTS = (
    ("channelTypes", "channel_type"),
    ("measurementGroups", "measurement_group"),
    ("groupNames", "group_name"),
    ("dashboardGroups", "dashboard_group"),
)
SKIP_FILES = {"index.json", "_errors.json"}
SORTS = {
    "display_name": "display_name",
    "name": "name",
    "engine": "reporting_engine_id",
    "report": "report_name",
    "kind": "kind",
    "type": "field_type",
}
QUERY_COLUMNS = (
    "reporting_engine_id",
    "report_name",
    "kind",
    "name",
    "field_name",
    "display_name",
    "unique_key",
    "code",
    "description",
    "field_type",
)

SCHEMA = """
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    reporting_engine_id TEXT NOT NULL,
    reporting_engine_label TEXT,
    report_name TEXT NOT NULL,
    source_file TEXT,
    metadata_api TEXT,
    custom_metrics_api TEXT,
    UNIQUE (reporting_engine_id, report_name)
);

CREATE TABLE fields (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    reporting_engine_id TEXT NOT NULL,
    report_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    field_name TEXT,
    display_name TEXT,
    unique_key TEXT,
    code TEXT,
    description TEXT,
    field_type TEXT,
    data_type TEXT,
    sortable INTEGER,
    filterable INTEGER,
    hidden INTEGER,
    payload TEXT NOT NULL
);

CREATE TABLE field_values (
    id INTEGER PRIMARY KEY,
    reporting_engine_id TEXT NOT NULL,
    report_name TEXT NOT NULL,
    field_id INTEGER NOT NULL REFERENCES fields(id),
    list_name TEXT NOT NULL,
    value TEXT NOT NULL
);

CREATE TABLE field_filters (
    id INTEGER PRIMARY KEY,
    reporting_engine_id TEXT NOT NULL,
    report_name TEXT NOT NULL,
    field_id INTEGER NOT NULL REFERENCES fields(id),
    position INTEGER NOT NULL,
    filter_type TEXT,
    field_name TEXT,
    values_json TEXT
);

CREATE TABLE field_projections (
    id INTEGER PRIMARY KEY,
    reporting_engine_id TEXT NOT NULL,
    report_name TEXT NOT NULL,
    field_id INTEGER NOT NULL REFERENCES fields(id),
    position INTEGER NOT NULL,
    measurement TEXT,
    aggregate_function TEXT
);
"""

INDEXES = """
CREATE INDEX fields_engine_report_kind_display
    ON fields (reporting_engine_id, report_name, kind, display_name);
CREATE INDEX field_values_engine_report
    ON field_values (reporting_engine_id, report_name);
CREATE INDEX field_values_list_value
    ON field_values (list_name, value);
CREATE INDEX field_values_field
    ON field_values (field_id);
CREATE INDEX field_filters_engine_report
    ON field_filters (reporting_engine_id, report_name);
CREATE INDEX field_filters_field
    ON field_filters (field_id);
CREATE INDEX field_projections_engine_report
    ON field_projections (reporting_engine_id, report_name);
CREATE INDEX field_projections_field
    ON field_projections (field_id);
"""

FTS = """
CREATE VIRTUAL TABLE fields_name_fts USING fts5(
    name,
    field_name,
    display_name,
    content='fields',
    content_rowid='id',
    tokenize='trigram case_sensitive 0'
);

CREATE VIRTUAL TABLE fields_description_fts USING fts5(
    description,
    content='fields',
    content_rowid='id',
    tokenize='trigram case_sensitive 0'
);
"""


def text(value) -> str | None:
    if value is None:
        return None
    rendered = value if isinstance(value, str) else str(value)
    rendered = rendered.strip()
    return rendered or None


def as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return None


def flag(obj: dict, *keys: str) -> int | None:
    seen = False
    result = False
    for key in keys:
        if key not in obj:
            continue
        seen = True
        if as_bool(obj.get(key)):
            result = True
    if not seen:
        return None
    return int(result)


def is_deprecated(obj: dict) -> bool:
    return as_bool(obj.get("hiddenAndDeprecated")) is True


def description_of(obj: dict) -> str | None:
    direct = text(obj.get("description"))
    if direct:
        return direct
    locales = obj.get("langVsTranslatedFieldValues")
    if isinstance(locales, dict):
        english = locales.get("en")
        if isinstance(english, dict):
            return text(english.get("description"))
    return None


def load_index(catalog: Path) -> dict[tuple[str, str], dict]:
    path = catalog / "index.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    index = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        engine = text(entry.get("reporting_engineId"))
        report = text(entry.get("report_name"))
        if engine and report:
            index[(engine, report)] = entry
    return index


def catalog_files(catalog: Path) -> list[Path]:
    return sorted(
        path
        for path in catalog.glob("*.json")
        if path.name not in SKIP_FILES and path.is_file()
    )


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def prepare_build(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA cache_size = -200000")
    connection.executescript(SCHEMA)


def finish_build(connection: sqlite3.Connection) -> None:
    logger.info("Creating indexes")
    connection.executescript(INDEXES)
    logger.info("Building name and description search indexes")
    connection.executescript(FTS)
    connection.execute("INSERT INTO fields_name_fts(fields_name_fts) VALUES('rebuild')")
    connection.execute(
        "INSERT INTO fields_description_fts(fields_description_fts) VALUES('rebuild')"
    )


def insert_report(connection, document, source_name, index_entry) -> int:
    engine = text(document.get("reporting_engineId"))
    report = text(document.get("report_name"))
    if not engine or not report:
        raise ValueError(f"{source_name} is missing reporting_engineId or report_name")
    label = text(index_entry.get("reporting_engine_label")) if index_entry else None
    metadata_api = text(index_entry.get("metadata_api")) if index_entry else None
    custom_api = text(index_entry.get("custom_metrics_api")) if index_entry else None
    cursor = connection.execute(
        """
        INSERT INTO reports (
            reporting_engine_id, reporting_engine_label, report_name,
            source_file, metadata_api, custom_metrics_api
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (engine, label, report, source_name, metadata_api, custom_api),
    )
    return cursor.lastrowid


def string_values(value) -> list[str]:
    if not isinstance(value, list):
        return []
    values = []
    for item in value:
        rendered = text(item) if isinstance(item, str) else None
        if rendered:
            values.append(rendered)
    return values


def insert_field(connection, report_id, engine, report, kind, obj) -> None:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    cursor = connection.execute(
        """
        INSERT INTO fields (
            report_id, reporting_engine_id, report_name, kind,
            name, field_name, display_name, unique_key, code, description,
            field_type, data_type, sortable, filterable, hidden, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            engine,
            report,
            kind,
            text(obj.get("name")),
            text(obj.get("fieldName")),
            text(obj.get("displayName")),
            text(obj.get("uniqueKey")),
            text(obj.get("code")),
            description_of(obj),
            text(obj.get("type")),
            text(obj.get("dataType")),
            flag(obj, "sortable"),
            flag(obj, "filterDimension", "filterMeasurement"),
            flag(obj, "hidden"),
            payload,
        ),
    )
    field_id = cursor.lastrowid
    value_rows = []
    for source_key, list_name in LISTS:
        for item in string_values(obj.get(source_key)):
            value_rows.append((engine, report, field_id, list_name, item))
    if value_rows:
        connection.executemany(
            """
            INSERT INTO field_values (
                reporting_engine_id, report_name, field_id, list_name, value
            ) VALUES (?, ?, ?, ?, ?)
            """,
            value_rows,
        )

    filters = obj.get("filters")
    if isinstance(filters, list):
        filter_rows = []
        for position, item in enumerate(filters):
            if not isinstance(item, dict):
                continue
            values = item.get("values")
            filter_rows.append(
                (
                    engine,
                    report,
                    field_id,
                    position,
                    text(item.get("filterType") or item.get("type")),
                    text(item.get("field") or item.get("dimensionName") or item.get("fieldName")),
                    json.dumps(values, ensure_ascii=False, separators=(",", ":"))
                    if values is not None
                    else None,
                )
            )
        if filter_rows:
            connection.executemany(
                """
                INSERT INTO field_filters (
                    reporting_engine_id, report_name, field_id, position,
                    filter_type, field_name, values_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                filter_rows,
            )

    projections = obj.get("requiredProjections")
    if isinstance(projections, list):
        projection_rows = []
        for position, item in enumerate(projections):
            if not isinstance(item, dict):
                continue
            projection_rows.append(
                (
                    engine,
                    report,
                    field_id,
                    position,
                    text(item.get("measurement") or item.get("measurementName")),
                    text(item.get("aggregateFunction")),
                )
            )
        if projection_rows:
            connection.executemany(
                """
                INSERT INTO field_projections (
                    reporting_engine_id, report_name, field_id, position,
                    measurement, aggregate_function
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                projection_rows,
            )


def import_document(connection, document, source_name, index_entry) -> tuple[int, int]:
    report_id = insert_report(connection, document, source_name, index_entry)
    engine = text(document.get("reporting_engineId"))
    report = text(document.get("report_name"))
    imported = 0
    skipped = 0
    for source_key, kind in KINDS:
        items = document.get(source_key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or is_deprecated(item):
                skipped += 1
                continue
            insert_field(connection, report_id, engine, report, kind, item)
            imported += 1
    return imported, skipped


def build_database(catalog: Path, database: Path) -> dict[str, int]:
    if not catalog.is_dir():
        raise SystemExit(f"Catalog directory not found: {catalog}")
    files = catalog_files(catalog)
    if not files:
        raise SystemExit(f"No catalog JSON files in {catalog}")
    index = load_index(catalog)
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(database.name + ".building")
    if temporary.exists():
        temporary.unlink()
    connection = connect(temporary)
    try:
        prepare_build(connection)
        imported = 0
        skipped = 0
        with connection:
            for number, path in enumerate(files, start=1):
                document = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(document, dict):
                    logger.warning("Skipping %s because it is not an object", path.name)
                    continue
                engine = text(document.get("reporting_engineId"))
                report = text(document.get("report_name"))
                entry = index.get((engine, report)) if engine and report else None
                file_imported, file_skipped = import_document(
                    connection, document, path.name, entry
                )
                imported += file_imported
                skipped += file_skipped
                if number % 50 == 0 or number == len(files):
                    logger.info(
                        "Imported %s/%s files (%s fields, %s skipped)",
                        number,
                        len(files),
                        imported,
                        skipped,
                    )
        finish_build(connection)
        connection.commit()
    except Exception:
        connection.close()
        if temporary.exists():
            temporary.unlink()
        raise
    connection.close()
    if database.exists():
        database.unlink()
    temporary.replace(database)
    return {"files": len(files), "fields": imported, "skipped": skipped}


def fts_query(term: str) -> str | None:
    cleaned = term.strip()
    if len(cleaned) < 3:
        return None
    return '"' + cleaned.replace('"', '""') + '"'


def query_database(database: Path, args) -> tuple[int, list[sqlite3.Row]]:
    if not database.is_file():
        raise SystemExit(f"Database not found: {database}. Run build first.")
    connection = connect(database)
    clauses = []
    parameters: list = []
    if args.name:
        match = fts_query(args.name)
        if match is None:
            clauses.append(
                """(
                    instr(lower(ifnull(name, '')), lower(?)) > 0
                    OR instr(lower(ifnull(field_name, '')), lower(?)) > 0
                    OR instr(lower(ifnull(display_name, '')), lower(?)) > 0
                )"""
            )
            parameters.extend([args.name, args.name, args.name])
        else:
            clauses.append(
                "id IN (SELECT rowid FROM fields_name_fts WHERE fields_name_fts MATCH ?)"
            )
            parameters.append(match)
    if args.search:
        match = fts_query(args.search)
        if match is None:
            clauses.append("instr(lower(ifnull(description, '')), lower(?)) > 0")
            parameters.append(args.search)
        else:
            clauses.append(
                "id IN (SELECT rowid FROM fields_description_fts WHERE fields_description_fts MATCH ?)"
            )
            parameters.append(match)
    if args.kind:
        clauses.append("lower(kind) = lower(?)")
        parameters.append(args.kind)
    if args.engine:
        clauses.append(
            """(
                lower(reporting_engine_id) = lower(?)
                OR report_id IN (
                    SELECT id FROM reports
                    WHERE lower(ifnull(reporting_engine_label, '')) = lower(?)
                )
            )"""
        )
        parameters.extend([args.engine, args.engine])
    if args.report:
        clauses.append("lower(report_name) = lower(?)")
        parameters.append(args.report)
    if args.hidden is not None:
        clauses.append("hidden = ?" if args.hidden else "(hidden = 0 OR hidden IS NULL)")
        if args.hidden:
            parameters.append(1)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order_column = SORTS[args.sort] if args.sort else None
    order = (
        f" ORDER BY {order_column}, reporting_engine_id, report_name, name"
        if order_column
        else " ORDER BY reporting_engine_id, report_name, name"
    )
    count = connection.execute(f"SELECT count(*) FROM fields{where}", parameters).fetchone()[0]
    limit = ""
    limit_parameters = list(parameters)
    if args.limit > 0:
        limit = " LIMIT ?"
        limit_parameters.append(args.limit)
    columns = ", ".join(QUERY_COLUMNS)
    rows = connection.execute(
        f"SELECT {columns} FROM fields{where}{order}{limit}",
        limit_parameters,
    ).fetchall()
    connection.close()
    return count, rows


def print_rows(rows: list[sqlite3.Row]) -> None:
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(QUERY_COLUMNS)
    for row in rows:
        writer.writerow("" if value is None else value for value in row)


HELP = """\
Build and query a SQLite database of Sprinklr reporting dimensions and metrics.

The database is for finding which reports support a field and reading that
field's characteristics. Objects with hiddenAndDeprecated set to true are
left out. Description is one column: the object's description when it has
text, otherwise the English text in langVsTranslatedFieldValues.
"""

EXAMPLES = """\
samples:
  Build the database from reporting_catalog:
    python reporting_db.py build

  Find date fields:
    python reporting_db.py query --name date

  Dimensions on one engine:
    python reporting_db.py query --name date --kind dimension --engine PLATFORM

  Match description text:
    python reporting_db.py query --search engagement --kind metric --limit 20
"""


def parse_bool(value: str) -> bool:
    parsed = as_bool(value)
    if parsed is None:
        raise argparse.ArgumentTypeError("expected true or false")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="reporting_db.py",
        description=HELP,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Load the catalog JSON files into SQLite.")
    build.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help=f"Directory of catalog JSON files. Default: {DEFAULT_CATALOG}",
    )
    build.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database file to replace. Default: {DEFAULT_DATABASE}",
    )

    query = commands.add_parser(
        "query",
        help="Find fields by a partial name and print matching rows.",
    )
    query.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database file. Default: {DEFAULT_DATABASE}",
    )
    query.add_argument(
        "--name",
        help=(
            "Partial match against name, field name, and display name. "
            "Example: date."
        ),
    )
    query.add_argument(
        "--search",
        help="Partial match against the description column.",
    )
    query.add_argument(
        "--kind",
        choices=[kind for _, kind in KINDS],
        help="dimension, metric, custom_dimension, or custom_metric.",
    )
    query.add_argument("--engine", help="Reporting engine id or display label.")
    query.add_argument("--report", help="Report name.")
    query.add_argument(
        "--hidden",
        type=parse_bool,
        default=None,
        help="true to keep only hidden fields, false to exclude them.",
    )
    query.add_argument(
        "--sort",
        choices=sorted(SORTS),
        help="Sort by display_name, name, engine, report, kind, or type.",
    )
    query.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum rows to print. Use 0 for every match. Default: 100.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if args.command == "build":
        summary = build_database(args.catalog, args.database)
        logger.info(
            "Wrote %s (%s files, %s fields, %s skipped)",
            args.database,
            summary["files"],
            summary["fields"],
            summary["skipped"],
        )
        return
    if args.limit < 0:
        raise SystemExit("--limit must be 0 or greater")
    if not any((args.name, args.search, args.engine, args.report, args.kind)):
        raise SystemExit("Provide --name, --search, --kind, --engine, or --report")
    count, rows = query_database(args.database, args)
    print(f"{count} matches, showing {len(rows)}", file=sys.stderr)
    print_rows(rows)


if __name__ == "__main__":
    main()
