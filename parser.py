import re
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


HEADERS = {"User-Agent": "1010cyl@gmail.com"}
CURRENCY_SIGNS = "$€£¥₹₩₽¢"
MIN_TABLE_WORDS = 0

BASE_URL = "https://www.sec.gov/Archives/edgar/data/1652044/000165204426000018/goog-20251231.htm"
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
        table_title = _extract_table_title(table)
        rows, max_width = _expand_table_with_spans(table)
        if not rows:
            continue

        rows = deduplicate_columns(rows)
        rows = merge_adjacent_sign_columns(rows)

        max_width = max(len(row) for row in rows)
        padded_rows = [row + [""] * (max_width - len(row)) for row in rows]
        padded_rows = delete_empty_columns(padded_rows)
        columns, header_rows, data_rows = _extract_columns_and_data_rows(padded_rows)
        if _count_table_words(header_rows, data_rows) <= MIN_TABLE_WORDS:
            continue
        _normalize_parenthesized_negative_values(data_rows)
        results.append(
            {
                "index": index,
                "title": table_title,
                "table_html": str(table),
                "rows": padded_rows,
                "columns": columns,
                "header_rows": header_rows,
                "data_rows": data_rows,
                "text": "\n".join(
                    [" | ".join(columns)] + [" | ".join(row) for row in data_rows]
                ),
                "textualization": table_textualization(
                    table_title, columns, data_rows
                ),
            }
        )

    return results


def table_textualization(
    title: str, columns: List[str], data_rows: List[List[str]]
) -> str:
    lines: List[str] = []
    width = len(columns)
    padded_rows = [row + [""] * (width - len(row)) for row in data_rows]

    first_cell_prefix = ""
    if columns:
        first_cell_prefix = str(columns[0]).replace("\xa0", " ").strip()

    # Skip first column (row-label column) from textualization iteration.
    for col_idx, col_label in enumerate(columns[1:], start=1):
        for row_pos in range(len(padded_rows)):
            row = padded_rows[row_pos]
            row_label = row[0] if row else ""
            value = row[col_idx] if col_idx < len(row) else ""

            value_cells = row[1:] if len(row) > 1 else []
            row_empty = all(not str(cell).replace("\xa0", " ").strip() for cell in value_cells)
            if row_empty:
                lines.append(f"{row_label}:")
            else:
                if str(value).replace("\xa0", " ").strip():
                    lines.append(f"{row_label}, {col_label} = {value}")

    if lines and first_cell_prefix:
        lines[0] = f"{first_cell_prefix}.\n{lines[0]}"

    text = (title or "") + "\n" + ";".join(lines).replace(":;", ":")
    return text


def _extract_table_title(table_tag) -> str:
    """
    Find the nearest sentence-like context immediately above a table.
    """
    search_tags = ("p", "div", "font", "span", "b", "strong")
    seen = set()
    fallback = ""

    for node in table_tag.find_all_previous(search_tags, limit=120):
        if node.find_parent("table") is not None:
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        if not _is_probable_table_title_text(text):
            continue
        if not fallback:
            fallback = text
        if text.endswith(":") or text.endswith("."):
            return text
    return fallback


def _is_probable_table_title_text(text: str) -> bool:
    words = re.findall(r"\b\w+\b", text)
    if len(words) < 3 or len(words) > 40:
        return False
    if len(text) > 260:
        return False
    if "|" in text:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if re.fullmatch(r"[\d\s,.$()%\-–—/:;]+", text):
        return False
    return True


def _count_table_words(header_rows: List[List[str]], data_rows: List[List[str]]) -> int:
    rows = (header_rows or []) + (data_rows or [])
    return sum(len(re.findall(r"\S+", cell or "")) for row in rows for cell in row)


def _raw_column_names_from_headers(header_rows: List[List[str]], width: int) -> List[str]:
    """
    Build column names from header text only (no synthetic fallback names).
    """
    names: List[str] = []
    for col_idx in range(width):
        parts: List[str] = []
        for row in header_rows:
            value = row[col_idx].strip() if col_idx < len(row) else ""
            if value:
                parts.append(value)
        names.append(" - ".join(parts).strip())
    return names


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

    first_data_idx = _find_first_data_row_index(padded_rows)
    if first_data_idx is None:
        # No detected data rows: all columns are treated as header-only and dropped.
        return [[] for _ in padded_rows]

    data_rows = padded_rows[first_data_idx:]

    keep_indices = []
    for col_idx in range(width):
        has_data_content = any(
            data_rows[row_idx][col_idx].strip() for row_idx in range(len(data_rows))
        )
        if has_data_content:
            keep_indices.append(col_idx)

    return [[row[col_idx] for col_idx in keep_indices] for row in padded_rows]


def deduplicate_columns(rows: List[List[str]]) -> List[List[str]]:
    """
    Remove duplicate columns by exact full-column equality, including header cells.
    """
    if not rows:
        return rows

    width = max(len(row) for row in rows)
    padded_rows = [row + [""] * (width - len(row)) for row in rows]

    seen_column_signatures = set()
    keep_indices: List[int] = []

    for col_idx in range(width):
        signature = tuple(row[col_idx] for row in padded_rows)
        if signature not in seen_column_signatures:
            seen_column_signatures.add(signature)
            keep_indices.append(col_idx)

    return [[row[col_idx] for col_idx in keep_indices] for row in padded_rows]


def merge_adjacent_sign_columns(rows: List[List[str]]) -> List[List[str]]:
    """
    Merge sign-only adjacent columns into the current value column.

    Rules implemented:
    - Prefix merge from previous adjacent column when the previous cell is a
      currency sign marker (supports forms like "$", "($", "€", "(€").
    - Suffix merge from next adjacent column when the next cell is a percent
      marker (supports "%" and "%)").
    - Prefix merge candidates must have the same header-derived column name
      between source and target columns (when header names are present).
    - Never merge numeric content from adjacent cells, only sign markers.
    - Drop adjacent source columns used for merging, even if they contain
      non-sign values in some rows.
    """
    if not rows:
        return rows

    width = max(len(row) for row in rows)
    padded_rows = [row + [""] * (width - len(row)) for row in rows]

    first_data_idx = _find_first_data_row_index(padded_rows)
    if first_data_idx is None:
        first_data_idx = len(padded_rows)

    data_rows = padded_rows[first_data_idx:]
    if not data_rows:
        return padded_rows

    header_rows = padded_rows[:first_data_idx]
    raw_header_names = _raw_column_names_from_headers(header_rows, width)

    prefix_merge_targets: set[int] = set()
    suffix_merge_targets: set[int] = set()

    for col_idx in range(1, width):
        left_name = _normalize_marker(raw_header_names[col_idx - 1])
        curr_name = _normalize_marker(raw_header_names[col_idx])
        if left_name and curr_name and left_name != curr_name:
            continue

        marker_rows = 0
        mergeable_rows = 0
        for row in data_rows:
            marker = _normalize_marker(row[col_idx - 1])
            value = row[col_idx].strip()
            if marker and _is_currency_prefix_marker(marker):
                marker_rows += 1
                if value:
                    mergeable_rows += 1
        if marker_rows and mergeable_rows:
            prefix_merge_targets.add(col_idx)

    for col_idx in range(width - 1):
        marker_rows = 0
        mergeable_rows = 0
        for row in data_rows:
            marker = _normalize_marker(row[col_idx + 1])
            value = row[col_idx].strip()
            if marker and _is_percent_suffix_marker(marker):
                marker_rows += 1
                if value:
                    mergeable_rows += 1
        if marker_rows and mergeable_rows:
            suffix_merge_targets.add(col_idx)

    for row in data_rows:
        for col_idx in prefix_merge_targets:
            marker = _normalize_marker(row[col_idx - 1])
            value = row[col_idx].strip()
            if marker and value and _is_currency_prefix_marker(marker):
                if not _normalize_marker(value).startswith(marker):
                    row[col_idx] = f"{marker}{value}"
                row[col_idx - 1] = ""

        for col_idx in suffix_merge_targets:
            marker = _normalize_marker(row[col_idx + 1])
            value = row[col_idx].strip()
            if marker and value and _is_percent_suffix_marker(marker):
                if not _normalize_marker(value).endswith(marker):
                    row[col_idx] = f"{value}{marker}"
                row[col_idx + 1] = ""

    drop_indices = {col_idx - 1 for col_idx in prefix_merge_targets}
    drop_indices.update(col_idx + 1 for col_idx in suffix_merge_targets)
    if not drop_indices:
        return padded_rows

    keep_indices = [idx for idx in range(width) if idx not in drop_indices]
    return [[row[idx] for idx in keep_indices] for row in padded_rows]


def _drop_empty_columns(rows: List[List[str]]) -> List[List[str]]:
    return delete_empty_columns(rows)


def _normalize_marker(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip())


def _normalize_parenthesized_negative_values(data_rows: List[List[str]]) -> None:
    """
    Convert parenthesized numeric strings to negative values in value cells only.

    This intentionally skips:
    - header rows (caller passes data rows only)
    - row-label cells (first column in each data row)
    """
    for row in data_rows:
        for col_idx in range(1, len(row)):
            row[col_idx] = _convert_parenthesized_numeric_to_negative(row[col_idx])


def _convert_parenthesized_numeric_to_negative(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return value

    normalized = _normalize_marker(s)
    currency_class = re.escape(CURRENCY_SIGNS)
    number = r"\d[\d,]*(?:\.\d+)?"

    patterns = [
        # (0.5), (0.5%), ($2,439), (€2,439%)
        (rf"^\(([{currency_class}]?{number}%?)\)$", False),
        # (0.5)% and ($2,439)% (percent outside closing parenthesis)
        (rf"^\(([{currency_class}]?{number})\)%$", True),
        # $(2,439), €(2,439%), etc.
        (rf"^([{currency_class}])\(({number}%?)\)$", False),
        # $(2,439)% and €(2,439)%
        (rf"^([{currency_class}])\(({number})\)%$", True),
    ]

    for pattern, add_percent_suffix in patterns:
        match = re.fullmatch(pattern, normalized)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 1:
            token = groups[0]
        else:
            token = "".join(groups)
        if add_percent_suffix and not token.endswith("%"):
            token = f"{token}%"
        return f"-{token}"

    return value


def _is_currency_prefix_marker(value: str) -> bool:
    return bool(re.fullmatch(r"\(?[$€£¥₹₩₽¢]\)?", _normalize_marker(value)))


def _is_percent_suffix_marker(value: str) -> bool:
    return _normalize_marker(value) in {"%", "%)"}


def _has_currency_prefix(value: str) -> bool:
    normalized = _normalize_marker(value)
    return normalized.startswith("$") or normalized.startswith("($")


def _has_percent_suffix(value: str) -> bool:
    normalized = _normalize_marker(value)
    return normalized.endswith("%") or normalized.endswith("%)")


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
