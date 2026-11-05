"""Spreadsheets (XLSX via openpyxl) and CSV/TSV.

Every sheet is written in full as CSV next to the Markdown; the Markdown holds
a short preview per sheet (header + first rows) plus its size, so a model sees
the shape of the data without the whole table, and an agent can load the CSV
when it needs every row.
"""

from __future__ import annotations

import contextlib
import csv
import io
import itertools
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from dossier.convert.extractors.base import ExtractContext, ExtractionError
from dossier.convert.model import (
    DocumentIR,
    ExtractionResult,
    ExtractionWarning,
    Paragraph,
    Section,
    SectionKind,
    SheetData,
    Table,
)

MAX_COLUMNS = 256


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time.min else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:g}" if value == int(value) else repr(value)
    return str(value)


def trim(rows: list[list[str]]) -> list[list[str]]:
    """Drop trailing empty rows and columns; keep interior gaps."""
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        return rows
    width = max((i for row in rows for i, cell in enumerate(row) if cell), default=-1) + 1
    return [row[:width] + [""] * (width - len(row[:width])) for row in rows]


def sheet_section(name: str, index: int, rows: list[list[str]], preview: int) -> Section:
    data_rows = max(0, len(rows) - 1)
    width = max((len(row) for row in rows), default=0)
    blocks: list[Any] = [Paragraph(text=f"{data_rows} data rows, {width} columns.")]
    if rows:
        blocks.append(Table(rows=rows[: preview + 1], total_rows=data_rows))
    return Section(kind=SectionKind.SHEET, index=index, title=name, blocks=blocks)


class XlsxExtractor:
    name = "xlsx"
    version = 1
    suffixes = (".xlsx", ".xlsm")

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        from openpyxl import load_workbook

        try:
            values = load_workbook(str(path), read_only=True, data_only=True)
            formulas = load_workbook(str(path), read_only=True, data_only=False)
        except Exception as exc:
            raise ExtractionError(
                f"cannot open workbook ({type(exc).__name__})", code="corrupt_file", path=path
            ) from exc
        warnings: list[ExtractionWarning] = []
        sections: list[Section] = []
        sheets: list[SheetData] = []
        try:
            for index, (ws_values, ws_formulas) in enumerate(
                zip(values.worksheets, formulas.worksheets, strict=True), start=1
            ):
                title = str(ws_values.title)
                for ws in (ws_values, ws_formulas):
                    with contextlib.suppress(AttributeError):
                        ws.reset_dimensions()
                csv_rows, preview_rows, truncated, uncached = self._read(
                    ws_values, ws_formulas, ctx.max_sheet_rows
                )
                if truncated:
                    warnings.append(
                        ExtractionWarning(
                            code="SHEET_TRUNCATED",
                            message=f"more than {ctx.max_sheet_rows} rows; extra rows dropped",
                            location=f"sheet {title}",
                        )
                    )
                if uncached:
                    warnings.append(
                        ExtractionWarning(
                            code="FORMULA_NO_CACHED_VALUE",
                            message=f"{uncached} formula cell(s) have no saved value",
                            location=f"sheet {title}",
                        )
                    )
                sections.append(sheet_section(title, index, preview_rows, ctx.preview_rows))
                sheets.append(SheetData(name=title, rows=csv_rows))
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(
                f"cannot read workbook ({type(exc).__name__})", code="corrupt_file", path=path
            ) from exc
        finally:
            values.close()
            formulas.close()
        return ExtractionResult(
            document=DocumentIR(
                source_type="xlsx",
                sections=sections,
                properties={"Sheets": str(len(sheets))},
            ),
            warnings=warnings,
            sheets=sheets,
        )

    @staticmethod
    def _read(
        ws_values: Any, ws_formulas: Any, max_rows: int
    ) -> tuple[list[list[str]], list[list[str]], bool, int]:
        """Values for CSV; 'value (=formula)' for the preview."""
        csv_rows: list[list[str]] = []
        preview_rows: list[list[str]] = []
        truncated = False
        uncached = 0
        pairs = itertools.zip_longest(
            ws_values.iter_rows(values_only=True),
            ws_formulas.iter_rows(values_only=True),
            fillvalue=(),
        )
        for number, (value_row, formula_row) in enumerate(pairs, start=1):
            if number > max_rows + 1:
                truncated = True
                break
            plain: list[str] = []
            shown: list[str] = []
            for value, formula in itertools.zip_longest(
                value_row[:MAX_COLUMNS], formula_row[:MAX_COLUMNS], fillvalue=None
            ):
                if isinstance(formula, str) and formula.startswith("="):
                    if value is None or value == formula:
                        uncached += 1
                        plain.append(formula)
                        shown.append(formula)
                    else:
                        plain.append(format_value(value))
                        shown.append(f"{format_value(value)} ({formula})")
                else:
                    plain.append(format_value(value))
                    shown.append(format_value(value))
            csv_rows.append(plain)
            preview_rows.append(shown)
        return trim(csv_rows), trim(preview_rows), truncated, uncached


class CsvExtractor:
    name = "csv"
    version = 1
    suffixes = (".csv", ".tsv")

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        sample = text[:20_000]
        try:
            dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
        rows: list[list[str]] = []
        warnings: list[ExtractionWarning] = []
        for number, row in enumerate(csv.reader(io.StringIO(text), dialect), start=1):
            if number > ctx.max_sheet_rows + 1:
                warnings.append(
                    ExtractionWarning(
                        code="SHEET_TRUNCATED",
                        message=f"more than {ctx.max_sheet_rows} rows; extra rows dropped",
                    )
                )
                break
            rows.append(row[:MAX_COLUMNS])
        rows = trim(rows)
        name = path.stem
        return ExtractionResult(
            document=DocumentIR(
                source_type="csv",
                sections=[sheet_section(name, 1, rows, ctx.preview_rows)],
            ),
            warnings=warnings,
            sheets=[SheetData(name=name, rows=rows)],
        )
