import re
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


HEADERS = {"User-Agent": "1010cyl@gmail.com"}

BASE_URL = "https://www.sec.gov/Archives/edgar/data/313927/000119312525260515"
FILING_URL = f"{BASE_URL}/chd-20250930.htm"


def parse_all_tables(
    url: str = FILING_URL,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
):
    """
    Parse all HTML tables from an SEC filing page.

    Behavior:
    - Preserve original table HTML (including colspan) via `table_html`.
    - Expand spans in JSON rows by replicating values across spanned columns.
    """
    if headers is None:
        headers = HEADERS

    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    results = []
    for index, table in enumerate(soup.find_all("table")):
        rows, max_width = _expand_table_with_spans(table)
        if not rows:
            continue

        padded_rows = [row + [""] * (max_width - len(row)) for row in rows]
        padded_rows = delete_empty_columns(padded_rows)
        columns, header_rows, data_rows = _extract_columns_and_data_rows(padded_rows)
        results.append(
            {
                "index": index,
                "heading": "",
                "table_html": str(table),
                "rows": padded_rows,
                "columns": columns,
                "header_rows": header_rows,
                "data_rows": data_rows,
                "text": "\n".join(" | ".join(row) for row in data_rows),
            }
        )

    return results


def _expand_table_with_spans(table_tag) -> Tuple[List[List[str]], int]:
    rows: List[List[str]] = []
    # Maps absolute column index -> (remaining rowspan depth, value).
    active_rowspans: Dict[int, Tuple[int, str]] = {}
    max_width = 0

    for tr in _direct_trs(table_tag):
        row: List[str] = []
        col_idx = 0

        while col_idx in active_rowspans:
            remaining, value = active_rowspans[col_idx]
            row.append(value)
            if remaining <= 1:
                del active_rowspans[col_idx]
            else:
                active_rowspans[col_idx] = (remaining - 1, value)
            col_idx += 1

        for cell in tr.find_all(["th", "td"], recursive=False):
            while col_idx in active_rowspans:
                remaining, value = active_rowspans[col_idx]
                row.append(value)
                if remaining <= 1:
                    del active_rowspans[col_idx]
                else:
                    active_rowspans[col_idx] = (remaining - 1, value)
                col_idx += 1

            value = _cell_text(cell)
            colspan = _positive_int(cell.get("colspan"), 1)
            rowspan = _positive_int(cell.get("rowspan"), 1)

            for _ in range(colspan):
                row.append(value)
                if rowspan > 1:
                    active_rowspans[col_idx] = (rowspan - 1, value)
                col_idx += 1

        while col_idx in active_rowspans:
            remaining, value = active_rowspans[col_idx]
            row.append(value)
            if remaining <= 1:
                del active_rowspans[col_idx]
            else:
                active_rowspans[col_idx] = (remaining - 1, value)
            col_idx += 1

        if any(cell.strip() for cell in row):
            rows.append(row)
            max_width = max(max_width, len(row))

    return rows, max_width


def _direct_trs(table_tag):
    rows = []
    for child in table_tag.children:
        if not getattr(child, "name", None):
            continue
        if child.name == "tr":
            rows.append(child)
        elif child.name in ("thead", "tbody", "tfoot"):
            rows.extend(child.find_all("tr", recursive=False))
    return rows


def delete_empty_columns(rows: List[List[str]]) -> List[List[str]]:
    if not rows:
        return rows

    width = max(len(row) for row in rows)
    padded_rows = [row + [""] * (width - len(row)) for row in rows]

    keep_indices = []
    for col_idx in range(width):
        if any(padded_rows[row_idx][col_idx].strip() for row_idx in range(len(padded_rows))):
            keep_indices.append(col_idx)

    return [[row[col_idx] for col_idx in keep_indices] for row in padded_rows]


def _drop_empty_columns(rows: List[List[str]]) -> List[List[str]]:
    return delete_empty_columns(rows)


def _extract_columns_and_data_rows(rows: List[List[str]]) -> Tuple[List[str], List[List[str]], List[List[str]]]:
    """
    Determine first data row and build column names.

    Rules:
    - First column is row label and excluded from first-data-row detection.
    - First data row is either all-empty (except first column), or has at least one numeric-like
      value/sign in non-first columns. Date-like values do not count as numeric-like.
    - Header rows are all rows above first data row.
    - If no data row is detected, use first row as column names.
    """
    if not rows:
        return [], [], []

    width = max(len(row) for row in rows)
    padded_rows = [row + [""] * (width - len(row)) for row in rows]

    first_data_idx = _find_first_data_row_index(padded_rows)
    if first_data_idx is None:
        columns = []
        for i, cell in enumerate(padded_rows[0]):
            value = cell.strip()
            if i == 0:
                columns.append(value)
            else:
                columns.append(value or f"Column {i + 1}")
        return columns, [padded_rows[0]], padded_rows[1:]

    header_rows = padded_rows[:first_data_idx]
    data_rows = padded_rows[first_data_idx:]
    columns = _build_combined_column_names(header_rows, width)
    return columns, header_rows, data_rows


def _find_first_data_row_index(rows: List[List[str]]) -> Optional[int]:
    for idx, row in enumerate(rows):
        non_label_cells = row[1:] if len(row) > 1 else []
        if not non_label_cells:
            continue
        if all(not cell.strip() for cell in non_label_cells):
            return idx
        if any(_is_numeric_like_non_date(cell) for cell in non_label_cells):
            return idx
    return None


def _build_combined_column_names(header_rows: List[List[str]], width: int) -> List[str]:
    if not header_rows:
        return [f"Column {i + 1}" for i in range(width)]

    columns: List[str] = []
    for col_idx in range(width):
        parts = []
        for row in header_rows:
            value = row[col_idx].strip() if col_idx < len(row) else ""
            if value:
                parts.append(value)
        if parts:
            columns.append(" - ".join(parts))
        elif col_idx == 0:
            columns.append("")
        else:
            columns.append(f"Column {col_idx + 1}")
    return columns


def _is_numeric_like_non_date(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    if _is_date_like(s):
        return False
    if _is_numeric_sign_only(s):
        return True

    normalized = s.replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    normalized = normalized.replace("$", "").replace("%", "").strip()
    if normalized.startswith(("+", "-")):
        normalized = normalized[1:].strip()

    if not normalized:
        return False
    return bool(re.fullmatch(r"\d+(\.\d+)?", normalized))


def _is_numeric_sign_only(value: str) -> bool:
    s = (value or "").strip()
    return s in {"-", "--", "—", "%", "$", "()", "( )"}


def _is_date_like(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False

    patterns = [
        r"^\d{4}$",  # 2023, 2024
        r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$",  # 9/30/2025, 09-30-25
        r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$",  # 2025-09-30
        r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4}$",  # Sep 30, 2025
        r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{2,4}$",  # Sep 2025
    ]
    lowered = s.lower()
    return any(re.fullmatch(p, lowered) for p in patterns)


def _cell_text(cell) -> str:
    text = cell.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _positive_int(value, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default
