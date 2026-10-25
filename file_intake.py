"""File-based invoice import: CSV, Excel (.xlsx/.xls), Google Sheets (public), and PDF.

Each importer returns a report dict compatible with ``format_import_report`` from intake.py.
The unified entry point is ``import_from_file(store, file_path_or_url)``.
"""
import csv
import io
import os
import re
import traceback

import requests

from intake import (
    ALIASES,
    MAX_ROWS,
    _header_map,
    format_import_report,
    parse_row,
)

# --------------- optional heavy dependencies (graceful degradation) -----------

try:
    import openpyxl  # noqa: F401 – used for Excel reading
except ImportError:
    openpyxl = None

try:
    import pandas as pd  # noqa: F401
except ImportError:
    pd = None

try:
    import pdfplumber  # noqa: F401
except ImportError:
    pdfplumber = None


# ------------------------------------------------------------------ helpers

_GOOGLE_SHEET_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)", re.I
)

_GOOGLE_DOC_RE = re.compile(
    r"https?://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", re.I
)


def _norm_col(s: str) -> str:
    return re.sub(r"[\s_.\-]+", " ", str(s or "").strip().lower())


def _map_columns_from_headers(headers: list[str]) -> dict[str, int]:
    """Build a field→column-index mapping using the canonical ALIASES from intake."""
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        raw_key = str(raw or "").strip().lower()
        norm_key = _norm_col(raw)
        for field, names in ALIASES.items():
            norm_names = [_norm_col(n) for n in names]
            if (raw_key in names or norm_key in norm_names) and field not in mapping:
                mapping[field] = idx
                break
    return mapping if len(mapping) >= 3 else {}


def _rows_to_report(store, rows: list[list[str]], mapping: dict[str, int]) -> dict:
    """Validate *rows* using intake.parse_row and upsert into *store*."""
    rep: dict = {"added": [], "updated": [], "errors": [], "warnings": []}
    for n, cells in enumerate(rows[:MAX_ROWS], 1):
        rec, errs, warns = parse_row(cells, mapping)
        label = cells[0] if cells and cells[0] else f"row {n}"
        rep["warnings"] += [f"{label}: {w}" for w in warns]
        if errs:
            rep["errors"].append(f"{label}: " + "; ".join(errs))
            continue
        (rep["updated"] if store.upsert_invoice(**rec) else rep["added"]).append(
            rec["invoice_number"]
        )
    return rep


def _positional_mapping(headers: list[str]) -> dict[str, int]:
    """Fall back to positional mapping when header detection fails."""
    from intake import FIELDS

    return {f: i for i, f in enumerate(FIELDS) if i < len(headers)}


# ------------------------------------------------------------------ CSV

def import_from_csv(store, file_path: str) -> dict:
    """Import invoices from a CSV file on disk.

    Returns a report dict compatible with ``format_import_report``.
    """
    if not os.path.isfile(file_path):
        return {"added": [], "updated": [], "errors": [f"File not found: {file_path}"], "warnings": []}

    try:
        with open(file_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            all_rows = [row for row in reader]
    except Exception as exc:
        return {"added": [], "updated": [], "errors": [f"Could not read CSV: {exc}"], "warnings": []}

    if not all_rows:
        return {"added": [], "updated": [], "errors": ["CSV file is empty"], "warnings": []}

    mapping = _map_columns_from_headers(all_rows[0])
    if mapping:
        data_rows = all_rows[1:]
    else:
        mapping = _positional_mapping(all_rows[0])
        data_rows = all_rows

    if not data_rows:
        return {"added": [], "updated": [], "errors": ["CSV has no data rows"], "warnings": []}

    return _rows_to_report(store, data_rows, mapping)


# ------------------------------------------------------------------ Excel

def import_from_excel(store, file_path: str, sheet_name: str | None = None) -> dict:
    """Import invoices from an Excel workbook (.xlsx / .xls).

    When *sheet_name* is ``None`` the first sheet is used.
    """
    if not os.path.isfile(file_path):
        return {"added": [], "updated": [], "errors": [f"File not found: {file_path}"], "warnings": []}

    if openpyxl is None and pd is None:
        return {"added": [], "updated": [],
                "errors": ["openpyxl (or pandas) is not installed – run: pip install openpyxl pandas"],
                "warnings": []}

    try:
        if pd is not None:
            df = pd.read_excel(file_path, sheet_name=sheet_name or 0, dtype=str, engine="openpyxl")
            df = df.fillna("")
            headers = [str(c) for c in df.columns]
            mapping = _map_columns_from_headers(headers)
            if mapping:
                data_rows = [list(row) for row in df.values]
            else:
                mapping = _positional_mapping(headers)
                data_rows = [headers] + [list(row) for row in df.values]
        else:
            # pure openpyxl path
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
            rows_iter = ws.iter_rows(values_only=True)
            all_rows = [[str(c) if c is not None else "" for c in row] for row in rows_iter]
            wb.close()
            if not all_rows:
                return {"added": [], "updated": [], "errors": ["Excel file is empty"], "warnings": []}
            mapping = _map_columns_from_headers(all_rows[0])
            if mapping:
                data_rows = all_rows[1:]
            else:
                mapping = _positional_mapping(all_rows[0])
                data_rows = all_rows
    except Exception as exc:
        return {"added": [], "updated": [], "errors": [f"Could not read Excel file: {exc}"], "warnings": []}

    if not data_rows:
        return {"added": [], "updated": [], "errors": ["Excel file has no data rows"], "warnings": []}

    return _rows_to_report(store, data_rows, mapping)


# ------------------------------------------------------------------ Google Sheets (public)

def import_from_google_sheet(store, url_or_id: str) -> dict:
    """Import invoices from a publicly shared Google Sheet.

    Uses the public CSV-export URL so no API keys or OAuth are needed.
    """
    m = _GOOGLE_SHEET_RE.search(url_or_id)
    sheet_id = m.group(1) if m else url_or_id.strip()

    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    try:
        resp = requests.get(export_url, timeout=30)
    except requests.RequestException as exc:
        return {"added": [], "updated": [],
                "errors": [f"Could not reach Google Sheets: {exc}"],
                "warnings": []}

    if resp.status_code != 200:
        hint = (
            "The sheet might not be publicly shared. "
            "Open the sheet → Share → 'Anyone with the link' → Viewer, then try again."
        )
        return {"added": [], "updated": [],
                "errors": [f"Google Sheets returned HTTP {resp.status_code}. {hint}"],
                "warnings": []}

    try:
        reader = csv.reader(io.StringIO(resp.text))
        all_rows = [row for row in reader]
    except Exception as exc:
        return {"added": [], "updated": [],
                "errors": [f"Could not parse Google Sheet CSV: {exc}"],
                "warnings": []}

    if not all_rows:
        return {"added": [], "updated": [],
                "errors": ["Google Sheet appears to be empty"],
                "warnings": []}

    mapping = _map_columns_from_headers(all_rows[0])
    if mapping:
        data_rows = all_rows[1:]
    else:
        mapping = _positional_mapping(all_rows[0])
        data_rows = all_rows

    if not data_rows:
        return {"added": [], "updated": [],
                "errors": ["Google Sheet has headers but no data rows"],
                "warnings": []}

    return _rows_to_report(store, data_rows, mapping)


# ------------------------------------------------------------------ Google Docs (public)

def import_from_google_doc(store, url_or_id: str) -> dict:
    """Import invoices from a publicly shared Google Doc.

    Uses the public TXT-export URL so no API keys or OAuth are needed.
    """
    m = _GOOGLE_DOC_RE.search(url_or_id)
    doc_id = m.group(1) if m else url_or_id.strip()

    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    try:
        resp = requests.get(export_url, timeout=30)
    except requests.RequestException as exc:
        return {"added": [], "updated": [],
                "errors": [f"Could not reach Google Docs: {exc}"],
                "warnings": []}

    if resp.status_code != 200:
        hint = (
            "The Google Doc might not be publicly shared. "
            "Open the doc → Share → 'Anyone with the link' → Viewer, then try again."
        )
        return {"added": [], "updated": [],
                "errors": [f"Google Docs returned HTTP {resp.status_code}. {hint}"],
                "warnings": []}

    from intake import import_invoices
    rep = import_invoices(store, resp.text)
    if rep is None:
        return {"added": [], "updated": [],
                "errors": ["Google Doc does not contain recognisable invoice data."],
                "warnings": []}
    return rep


# ------------------------------------------------------------------ Text file (.txt / .tsv)

def import_from_txt(store, file_path: str) -> dict:
    """Import invoices from a plain text file (.txt / .tsv)."""
    if not os.path.isfile(file_path):
        return {"added": [], "updated": [], "errors": [f"File not found: {file_path}"], "warnings": []}
    try:
        with open(file_path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except Exception as exc:
        return {"added": [], "updated": [], "errors": [f"Could not read text file: {exc}"], "warnings": []}

    from intake import import_invoices
    rep = import_invoices(store, text)
    if rep is None:
        return {"added": [], "updated": [],
                "errors": ["Text file does not contain recognisable invoice data."],
                "warnings": []}
    return rep


# ------------------------------------------------------------------ PDF

def import_from_pdf(store, file_path: str) -> dict:
    """Import invoices from a PDF.

    First tries to extract tables via pdfplumber; falls back to line-by-line
    text extraction if no tables are found.
    """
    if pdfplumber is None:
        return {"added": [], "updated": [],
                "errors": ["pdfplumber is not installed – run: pip install pdfplumber"],
                "warnings": []}

    if not os.path.isfile(file_path):
        return {"added": [], "updated": [], "errors": [f"File not found: {file_path}"], "warnings": []}

    try:
        with pdfplumber.open(file_path) as pdf:
            all_tables: list[list[list[str]]] = []
            full_text_lines: list[str] = []
            for page in pdf.pages:
                tables = page.extract_tables()
                for tbl in (tables or []):
                    all_tables.append([[str(c) if c is not None else "" for c in row] for row in tbl])
                text = page.extract_text()
                if text:
                    full_text_lines.extend(text.splitlines())
    except Exception as exc:
        return {"added": [], "updated": [],
                "errors": [f"Could not read PDF: {exc}"],
                "warnings": []}

    # --- strategy 1: tables found in the PDF
    if all_tables:
        combined_rep: dict = {"added": [], "updated": [], "errors": [], "warnings": []}
        for tbl in all_tables:
            if not tbl:
                continue
            mapping = _map_columns_from_headers(tbl[0])
            if mapping:
                data_rows = tbl[1:]
            else:
                mapping = _positional_mapping(tbl[0])
                data_rows = tbl
            if data_rows:
                sub = _rows_to_report(store, data_rows, mapping)
                for k in combined_rep:
                    combined_rep[k].extend(sub[k])
        if combined_rep["added"] or combined_rep["updated"] or combined_rep["errors"]:
            return combined_rep

    # --- strategy 2: line-by-line text parsing
    if not full_text_lines:
        return {"added": [], "updated": [],
                "errors": ["PDF contains no extractable text or tables"],
                "warnings": []}

    # attempt to treat each line as comma-separated invoice data
    from intake import split_line, DATE_LIKE

    csv_rows: list[list[str]] = []
    header_mapping: dict[str, int] | None = None
    for line in full_text_lines:
        cells = split_line(line)
        if header_mapping is None:
            hm = _map_columns_from_headers(cells)
            if hm:
                header_mapping = hm
                continue
        if len(cells) >= 5:
            csv_rows.append(cells)

    if not csv_rows:
        return {"added": [], "updated": [],
                "errors": ["Could not find invoice data in the PDF text"],
                "warnings": []}

    if header_mapping is None:
        header_mapping = _positional_mapping(csv_rows[0])

    return _rows_to_report(store, csv_rows, header_mapping)


# ------------------------------------------------------------------ unified entry

_FILE_IMPORT_RE = re.compile(
    r"^\s*(?:import|upload|load)\s+(?:from\s+)?(?:csv|excel|xlsx?|xls|google\s*sheets?|google\s*docs?|docs?|sheets?|pdf|txt|tsv|file)\s+(.+)$",
    re.I,
)

_URL_IMPORT_RE = re.compile(
    r"^\s*(?:import|upload|load)\s+(?:from\s+)?(?:google\s*sheets?|google\s*docs?|url|sheets?|docs?)\s+(https?://.+)$",
    re.I,
)


def _extract_path_or_url(text: str) -> str | None:
    """Extract a supported file path or Google link from *text*, ignoring any trailing English."""
    # 1. Google Sheets or Docs URL
    m_url = re.search(r"https?://docs\.google\.com/(?:spreadsheets|document)/[^\s'\"]+", text, re.I)
    if m_url:
        return m_url.group(0).strip()

    # 2. Other generic URL
    m_gen_url = re.search(r"https?://[^\s'\"]+", text, re.I)
    if m_gen_url:
        return m_gen_url.group(0).strip()

    # 3. File path with supported extension (handles Windows absolute paths, relative paths, and quoted paths)
    m_path = re.search(
        r"['\"]?([a-zA-Z]:[\\/][^'\"\n\r\t,]+\.(?:csv|xlsx?|xls|pdf|txt|tsv)|[^'\"\s\n\r\t,]+\.(?:csv|xlsx?|xls|pdf|txt|tsv))['\"]?",
        text, re.I,
    )
    if m_path:
        return m_path.group(1).strip()

    return None


def detect_import_intent(text: str) -> str | None:
    """Return the file path / URL from the user message, or None if it's not a file-import request."""
    # 1. Strip leading @mentions (e.g. @agent1... or @collectiq)
    s = re.sub(r"^(@\S+\s*)+", "", (text or "").strip()).strip()

    # 2. Strip outer single/double quotes or backticks if user wrapped the whole command
    if len(s) >= 2 and (
        (s.startswith("'") and s.endswith("'")) or
        (s.startswith('"') and s.endswith('"')) or
        (s.startswith("`") and s.endswith("`"))
    ):
        s = s[1:-1].strip()

    # If the user is asking for sample/demo or general question, it's not a file import
    if re.search(r"\b(demo|sample)\b", s, re.I):
        return None
    if re.search(r"\bhow\s+(?:to|do|can)\s+(?:i\s+)?(?:import|add|upload)\b", s, re.I):
        return None

    # Check for direct file path or URL first (even with trailing instructions like 'and send remainder to all')
    extracted = _extract_path_or_url(s)
    if extracted:
        # Check if the message has an import keyword OR contains an existing file/supported URL
        has_import_word = bool(re.search(r"\b(import|upload|load|read|parse|add)\b", s, re.I))
        is_known_file = os.path.isfile(extracted) or "docs.google.com" in extracted.lower()
        if has_import_word or is_known_file:
            return extracted

    # Flexible command matching: "import csv d:\invoices.csv", "upload invoices from d:\..."
    m = re.match(
        r"^\s*(?:please\s+)?(?:import|upload|load)\s+(?:invoices?\s+)?(?:from\s+)?(?:csv|excel|xlsx?|xls|google\s*sheets?|google\s*docs?|docs?|sheets?|pdf|txt|tsv|file)?\s*[:\-]?\s*(.+)$",
        s, re.I,
    )
    if m:
        target = m.group(1).strip().strip("'\"")
        if re.search(r"\b(demo|sample)\b", target, re.I):
            return None
        # Try extracting path from target
        sub_extracted = _extract_path_or_url(target)
        if sub_extracted:
            return sub_extracted
        # Only accept target if it is an existing file or has a recognizable file extension / URL
        if os.path.isfile(target) or re.search(r"\.[a-zA-Z0-9]{2,4}$", target) or "docs.google.com" in target.lower():
            if not any(w == target.lower() for w in ("invoices", "bills", "csv", "excel", "file", "all", "demo")):
                return target

    return None


def import_from_file(store, file_path_or_url: str) -> dict:
    """Detect file type by extension or URL pattern and route to the right importer.

    Returns a standard report dict.
    """
    target = file_path_or_url.strip().strip("'\"")

    # Google Sheets URL
    if _GOOGLE_SHEET_RE.search(target) or "docs.google.com/spreadsheets" in target.lower():
        return import_from_google_sheet(store, target)

    # Google Docs URL
    if _GOOGLE_DOC_RE.search(target) or "docs.google.com/document" in target.lower():
        return import_from_google_doc(store, target)

    ext = os.path.splitext(target)[1].lower()

    if ext == ".csv":
        return import_from_csv(store, target)
    if ext in (".xlsx", ".xls"):
        return import_from_excel(store, target)
    if ext == ".pdf":
        return import_from_pdf(store, target)
    if ext in (".txt", ".tsv"):
        return import_from_txt(store, target)

    return {
        "added": [], "updated": [],
        "errors": [f"Unsupported file type '{ext or '(none)'}'. Supported: .csv, .xlsx, .xls, .pdf, .txt, or Google Sheets/Docs URL."],
        "warnings": [],
    }
