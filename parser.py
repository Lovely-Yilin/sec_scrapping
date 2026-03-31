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
        padded_rows = deduplicate_columns_by_data_values(padded_rows)
        width = len(padded_rows[0]) if padded_rows else 0
        results.append(
            {
                "index": index,
                "heading": "",
                "table_html": str(table),
                "rows": padded_rows,
                "columns": [f"Column {i + 1}" for i in range(width)],
                "header_rows": [],
                "data_rows": padded_rows,
                "text": "\n".join(" | ".join(row) for row in padded_rows),
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


def deduplicate_columns_by_data_values(rows: List[List[str]]) -> List[List[str]]:
    """
    Remove duplicate columns when all non-header cell values are identical.

    Header row (row index 0) is ignored for duplicate detection.
    """
    if not rows:
        return rows
    if len(rows) <= 1:
        return rows

    width = max(len(row) for row in rows)
    padded_rows = [row + [""] * (width - len(row)) for row in rows]

    keep_indices = []
    seen_signatures = set()
    for col_idx in range(width):
        signature = tuple(padded_rows[row_idx][col_idx] for row_idx in range(1, len(padded_rows)))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        keep_indices.append(col_idx)

    return [[row[col_idx] for col_idx in keep_indices] for row in padded_rows]


def _drop_empty_columns(rows: List[List[str]]) -> List[List[str]]:
    return delete_empty_columns(rows)


def _cell_text(cell) -> str:
    text = cell.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _positive_int(value, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default
