#!/usr/bin/env python3
"""Web explorer for the Sprinklr reporting catalog database.

Find which reporting engine and report name support a field, or open a
report and see the dimensions and metrics it can query.

    python reporting_explorer.py
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, url_for

from reporting_db import DEFAULT_DATABASE, fts_query

ROOT = Path(__file__).resolve().parent
PAGE_SIZE = 50

KIND_LABELS = {
    "dimension": "Dimension",
    "metric": "Metric",
    "custom_dimension": "Custom dimension",
    "custom_metric": "Custom metric",
}
LIST_LABELS = {
    "channel_type": "Channels",
    "measurement_group": "Measurement groups",
    "group_name": "Groups",
    "dashboard_group": "Dashboard groups",
}
FIELD_COLUMNS = """
    id, reporting_engine_id, report_name, kind, name, field_name, display_name,
    unique_key, code, description, field_type, data_type, sortable, filterable, hidden
"""


def create_app(database: Path | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.config["DATABASE"] = Path(database or DEFAULT_DATABASE)
    app.jinja_env.finalize = lambda value: "" if value is None else value

    @app.route("/")
    def fields():
        query = request.args.get("q", "").strip()
        kind = request.args.get("kind", "").strip()
        engine = request.args.get("engine", "").strip()
        report = request.args.get("report", "").strip()
        include_hidden = request.args.get("hidden") == "1"
        page = _page()
        if kind and kind not in KIND_LABELS:
            abort(400)
        with open_database(app) as connection:
            engine_choices = _engines(connection)
            total = 0
            report_total = 0
            rows = []
            if query or engine or report or kind:
                where, parameters = _field_filters(
                    query, kind, engine, report, include_hidden
                )
                total, report_total = connection.execute(
                    f"""
                    SELECT count(*),
                           count(DISTINCT reporting_engine_id || char(31) || report_name)
                    FROM fields
                    WHERE {where}
                    """,
                    parameters,
                ).fetchone()
                rows = connection.execute(
                    f"""
                    SELECT {FIELD_COLUMNS}
                    FROM fields
                    WHERE {where}
                    ORDER BY reporting_engine_id, report_name, name
                    LIMIT ? OFFSET ?
                    """,
                    [*parameters, PAGE_SIZE, (page - 1) * PAGE_SIZE],
                ).fetchall()
        return render_template(
            "fields.html",
            query=query,
            kind=kind,
            engine=engine,
            report=report,
            include_hidden=include_hidden,
            kinds=KIND_LABELS,
            engines=engine_choices,
            rows=rows,
            total=total,
            report_total=report_total,
            page=page,
            pages=_pages(total),
        )

    @app.route("/fields/<int:field_id>")
    def field_detail(field_id: int):
        with open_database(app) as connection:
            field = connection.execute(
                f"""
                SELECT {FIELD_COLUMNS}, report_id
                FROM fields
                WHERE id = ?
                """,
                (field_id,),
            ).fetchone()
            if field is None:
                abort(404)
            label = connection.execute(
                """
                SELECT reporting_engine_label
                FROM reports
                WHERE reporting_engine_id = ? AND report_name = ?
                """,
                (field["reporting_engine_id"], field["report_name"]),
            ).fetchone()
            values = connection.execute(
                """
                SELECT list_name, value
                FROM field_values
                WHERE field_id = ?
                ORDER BY list_name, value
                """,
                (field_id,),
            ).fetchall()
            filters = connection.execute(
                """
                SELECT position, filter_type, field_name, values_json
                FROM field_filters
                WHERE field_id = ?
                ORDER BY position
                """,
                (field_id,),
            ).fetchall()
            projections = connection.execute(
                """
                SELECT position, measurement, aggregate_function
                FROM field_projections
                WHERE field_id = ?
                ORDER BY position
                """,
                (field_id,),
            ).fetchall()
            others = connection.execute(
                """
                SELECT id, reporting_engine_id, report_name, display_name
                FROM fields
                WHERE name = ? AND kind = ? AND id != ?
                ORDER BY reporting_engine_id, report_name
                LIMIT 100
                """,
                (field["name"], field["kind"], field_id),
            ).fetchall()
            other_count = connection.execute(
                """
                SELECT count(*)
                FROM fields
                WHERE name = ? AND kind = ? AND id != ?
                """,
                (field["name"], field["kind"], field_id),
            ).fetchone()[0]
        grouped: dict[str, list[str]] = {}
        for row in values:
            grouped.setdefault(row["list_name"], []).append(row["value"])
        return render_template(
            "field.html",
            field=field,
            engine_label=label["reporting_engine_label"] if label else None,
            kinds=KIND_LABELS,
            lists=[
                (LIST_LABELS[key], grouped[key])
                for key in LIST_LABELS
                if grouped.get(key)
            ],
            filters=[_filter_row(row) for row in filters],
            projections=projections,
            others=others,
            other_count=other_count,
        )

    @app.route("/reports")
    def reports():
        query = request.args.get("q", "").strip()
        engine = request.args.get("engine", "").strip()
        hide_empty = request.args.get("hide_empty", "1") != "0"
        page = _page()
        with open_database(app) as connection:
            engine_choices = _engines(connection)
            counts = _report_counts(connection)
            matched = []
            for row in counts:
                if engine and row["reporting_engine_id"].casefold() != engine.casefold():
                    label = (row["reporting_engine_label"] or "").casefold()
                    if label != engine.casefold():
                        continue
                if query and query.casefold() not in row["report_name"].casefold():
                    if query.casefold() not in row["reporting_engine_id"].casefold():
                        continue
                if hide_empty and not _report_has_fields(row):
                    continue
                matched.append(row)
        start = (page - 1) * PAGE_SIZE
        return render_template(
            "reports.html",
            query=query,
            engine=engine,
            hide_empty=hide_empty,
            engines=engine_choices,
            rows=matched[start : start + PAGE_SIZE],
            total=len(matched),
            page=page,
            pages=_pages(len(matched)),
        )

    @app.route("/reports/view")
    def report_detail():
        engine = request.args.get("engine", "").strip()
        report_name = request.args.get("report", "").strip()
        if not engine or not report_name:
            abort(400)
        with open_database(app) as connection:
            report = connection.execute(
                """
                SELECT reporting_engine_id, reporting_engine_label, report_name,
                       source_file, metadata_api, custom_metrics_api
                FROM reports
                WHERE reporting_engine_id = ? AND report_name = ?
                """,
                (engine, report_name),
            ).fetchone()
            if report is None:
                report = connection.execute(
                    """
                    SELECT reporting_engine_id, reporting_engine_label, report_name,
                           source_file, metadata_api, custom_metrics_api
                    FROM reports
                    WHERE lower(reporting_engine_id) = lower(?)
                      AND lower(report_name) = lower(?)
                    """,
                    (engine, report_name),
                ).fetchone()
            if report is None:
                abort(404)
            rows = connection.execute(
                f"""
                SELECT {FIELD_COLUMNS}
                FROM fields
                WHERE reporting_engine_id = ? AND report_name = ?
                ORDER BY kind, display_name, name
                """,
                (report["reporting_engine_id"], report["report_name"]),
            ).fetchall()
        grouped = {kind: [] for kind in KIND_LABELS}
        for row in rows:
            grouped.setdefault(row["kind"], []).append(row)
        return render_template(
            "report.html",
            report=report,
            kinds=KIND_LABELS,
            grouped=grouped,
        )

    @app.context_processor
    def page_links():
        def page_url(page: int) -> str:
            args = request.args.to_dict(flat=True)
            args["page"] = page
            return url_for(request.endpoint, **args)

        return {"page_url": page_url}

    @app.template_filter("yn")
    def yes_no(value):
        if value is None:
            return ""
        return "Yes" if value else "No"

    @app.template_filter("kind_label")
    def kind_label(value):
        return KIND_LABELS.get(value, value or "")

    return app


@contextmanager
def open_database(app: Flask):
    connection = _connect(app)
    try:
        yield connection
    finally:
        connection.close()


def _connect(app: Flask) -> sqlite3.Connection:
    path = Path(app.config["DATABASE"])
    if not path.is_file():
        abort(500, f"Database not found: {path}. Run python reporting_db.py build.")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _page() -> int:
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return 1
    return page if page > 0 else 1


def _report_has_fields(row: dict) -> bool:
    return any(row[kind] for kind in KIND_LABELS)


def _pages(total: int) -> int:
    if total <= 0:
        return 1
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


def _engines(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT reporting_engine_id,
               max(reporting_engine_label) AS reporting_engine_label
        FROM reports
        GROUP BY reporting_engine_id
        ORDER BY reporting_engine_id
        """
    ).fetchall()


def _report_counts(connection: sqlite3.Connection) -> list[dict]:
    cached = getattr(_report_counts, "rows", None)
    database = connection.execute("PRAGMA database_list").fetchone()["file"]
    if cached and cached[0] == database:
        return cached[1]
    reports = connection.execute(
        """
        SELECT id, reporting_engine_id, reporting_engine_label, report_name
        FROM reports
        ORDER BY reporting_engine_id, report_name
        """
    ).fetchall()
    counts = {
        (row["reporting_engine_id"], row["report_name"]): {
            "reporting_engine_id": row["reporting_engine_id"],
            "reporting_engine_label": row["reporting_engine_label"],
            "report_name": row["report_name"],
            "dimension": 0,
            "metric": 0,
            "custom_dimension": 0,
            "custom_metric": 0,
        }
        for row in reports
    }
    for row in connection.execute(
        """
        SELECT reporting_engine_id, report_name, kind, count(*) AS n
        FROM fields
        GROUP BY reporting_engine_id, report_name, kind
        """
    ):
        bucket = counts.get((row["reporting_engine_id"], row["report_name"]))
        if bucket is not None and row["kind"] in bucket:
            bucket[row["kind"]] = row["n"]
    rows = list(counts.values())
    _report_counts.rows = (database, rows)
    return rows


def _field_filters(query, kind, engine, report, include_hidden):
    clauses = ["1 = 1"]
    parameters: list = []
    if query:
        match = fts_query(query)
        if match is None:
            clauses.append(
                """(
                    instr(lower(ifnull(name, '')), lower(?)) > 0
                    OR instr(lower(ifnull(field_name, '')), lower(?)) > 0
                    OR instr(lower(ifnull(display_name, '')), lower(?)) > 0
                )"""
            )
            parameters.extend([query, query, query])
        else:
            clauses.append(
                """(
                    id IN (SELECT rowid FROM fields_name_fts WHERE fields_name_fts MATCH ?)
                    AND (
                        instr(lower(ifnull(name, '')), lower(?)) > 0
                        OR instr(lower(ifnull(field_name, '')), lower(?)) > 0
                        OR instr(lower(ifnull(display_name, '')), lower(?)) > 0
                    )
                )"""
            )
            parameters.extend([match, query, query, query])
    if kind:
        clauses.append("kind = ?")
        parameters.append(kind)
    if engine:
        clauses.append(
            """(
                lower(reporting_engine_id) = lower(?)
                OR report_id IN (
                    SELECT id FROM reports
                    WHERE lower(ifnull(reporting_engine_label, '')) = lower(?)
                )
            )"""
        )
        parameters.extend([engine, engine])
    if report:
        clauses.append("lower(report_name) = lower(?)")
        parameters.append(report)
    if not include_hidden:
        clauses.append("(hidden = 0 OR hidden IS NULL)")
    return " AND ".join(clauses), parameters


def _filter_row(row: sqlite3.Row) -> dict:
    values = []
    if row["values_json"]:
        try:
            parsed = json.loads(row["values_json"])
        except json.JSONDecodeError:
            parsed = [row["values_json"]]
        if isinstance(parsed, list):
            values = [str(item) for item in parsed]
        elif parsed is not None:
            values = [str(parsed)]
    return {
        "filter_type": row["filter_type"],
        "field_name": row["field_name"],
        "filter_values": values,
    }


def main():
    app = create_app()
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
