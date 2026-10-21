"""
Table extraction: finds tables on each page via pdfplumber, converts each to
a Markdown table, and prefixes it with the nearest caption-like text (e.g.
"Table 1: Revenue by Year") so the chunk carries context on its own instead
of being a bare grid of numbers.
"""
import pdfplumber
from app.services.caption_utils import find_nearby_caption

# A caption line is usually short and starts with a recognizable label —
# this keeps the heuristic cheap and avoids grabbing an unrelated paragraph
# that merely happens to be near the table.
CAPTION_PREFIXES = ("table", "tab.", "tbl")
CAPTION_MAX_WORDS = 20

MIN_TABLE_ROWS = 2       # header + at least one data row
MAX_TABLE_COLUMNS = 12   # real tables in these documents don't run wider than this
MAX_EMPTY_CELL_RATIO = 0.5
MIN_MULTI_CELL_ROWS = 2  # at least this many rows must each have 2+ filled cells


def _is_plausible_table(rows: list[list[str | None]]) -> bool:
    """
    pdfplumber's line-based table detector frequently misfires on two things
    that are NOT real data tables:
      - chart/graph gridlines and legends — these produce very wide,
        mostly-empty grids (axis ticks, legend boxes)
      - a single bordered text box (e.g. a prompt or callout) — this
        produces a "table" with only 1 column

    This filters both out before bothering to build Markdown or look for a
    caption:
      - fewer than 2 columns → a bordered text box, not tabular data
      - more than MAX_TABLE_COLUMNS columns → almost always a misread
        chart axis grid, not a real table this wide
      - fewer than MIN_TABLE_ROWS non-empty rows → too little structure
        to be a real table
      - mostly empty cells (aggregate) → sparse grids like this come from
        chart gridlines, not genuine tabular data
      - fewer than MIN_MULTI_CELL_ROWS rows that individually have 2+
        filled cells → catches a dense header row sitting on top of mostly
        sparse rows, which the aggregate ratio alone can let through
    """
    cleaned_rows = [
        [(cell or "").strip() for cell in row]
        for row in rows
        if row and any((cell or "").strip() for cell in row)
    ]
    if len(cleaned_rows) < MIN_TABLE_ROWS:
        return False

    col_count = len(cleaned_rows[0])
    if col_count < 2 or col_count > MAX_TABLE_COLUMNS:
        return False

    total_cells = sum(len(row) for row in cleaned_rows)
    empty_cells = sum(1 for row in cleaned_rows for cell in row if not cell)
    if total_cells and (empty_cells / total_cells) > MAX_EMPTY_CELL_RATIO:
        return False

    multi_cell_rows = sum(1 for row in cleaned_rows if sum(1 for cell in row if cell) >= 2)
    if multi_cell_rows < MIN_MULTI_CELL_ROWS:
        return False

    return True


def _rows_to_markdown(rows: list[list[str | None]]) -> str:
    """
    Converts pdfplumber's row-of-cells output into a Markdown table.
    Empty/None cells become blank cells rather than the literal "None".
    """
    cleaned_rows = [
        [(cell or "").strip().replace("\n", " ") for cell in row]
        for row in rows
        if row and any((cell or "").strip() for cell in row)
    ]
    if not cleaned_rows:
        return ""

    header, *body_rows = cleaned_rows
    col_count = len(header)

    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in body_rows:
        # pdfplumber sometimes returns rows with a different cell count than
        # the header (merged cells, ragged tables) — pad/truncate to keep the
        # Markdown well-formed rather than emitting a broken row.
        row = (row + [""] * col_count)[:col_count]
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def extract_tables_from_page(
    page,
    page_number: int,
    filename: str,
    table_regions: list[dict] | None = None,
) -> list[dict]:
    """
    Returns a list of table entries for one page, each ready to become its
    own chunk: {"text": <caption + markdown>, "page": ..., "source": ...,
    "element_type": "table"}.
    """
    results = []
    found_tables = page.find_tables()

    # A common report style uses horizontal color-banded rows (one rect per
    # row, no vertical ruling lines between columns) to visually separate a
    # table's columns — pdfplumber's default "lines" strategy can't split
    # columns without vertical lines, so it collapses each row into a single
    # cell. If the default strategy found only single-column tables AND the
    # page has multiple stacked rects (the row-bands), retry with text-based
    # column detection anchored to those bands.
    #
    # The rect-count check matters: a single bordered text/prompt box (one
    # background rect for the whole box, not one per row) would otherwise
    # get the same treatment and have its paragraph text force-split into
    # fake columns — that's a text box, not a table, and retrying on it
    # was exactly what produced a false positive during testing.
    MIN_ROW_BAND_RECTS = 3
    if found_tables and len(page.rects) >= MIN_ROW_BAND_RECTS and all(len(rows[0]) <= 1 for t in found_tables if (rows := t.extract())):
        found_tables = page.find_tables(table_settings={
            "vertical_strategy": "text",
            "horizontal_strategy": "lines",
        })

    if table_regions is not None:
        table_regions.extend(
            {"page": page_number, "bbox": tuple(table.bbox)}
            for table in found_tables
        )

    caption_words = [{"text": w["text"], "top": w["top"]} for w in page.extract_words()]

    for table in found_tables:
        rows = table.extract()
        if not _is_plausible_table(rows):
            continue

        markdown = _rows_to_markdown(rows)
        if not markdown:
            continue

        caption = find_nearby_caption(caption_words, table.bbox, CAPTION_PREFIXES, CAPTION_MAX_WORDS)
        text = f"{caption}\n\n{markdown}" if caption else markdown

        results.append({
            "text": text,
            "page": page_number,
            "source": filename,
            "element_type": "table",
        })

    return results


def extract_tables_from_pdf(
    file_path: str,
    display_name: str | None = None,
    table_regions: list[dict] | None = None,
) -> list[dict]:
    filename = display_name or file_path.split("\\")[-1].split("/")[-1]

    table_chunks = []
    with pdfplumber.open(file_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            table_chunks.extend(extract_tables_from_page(
                page, page_number, filename, table_regions=table_regions
            ))

    return table_chunks


if __name__ == "__main__":
    import sys
    test_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not test_file:
        print("Usage: python table_extractor.py <path_to_pdf>")
    else:
        tables = extract_tables_from_pdf(test_file)
        print(f"Found {len(tables)} table(s).")
        for i, t in enumerate(tables, start=1):
            print(f"\n--- Table {i} (page {t['page']}) ---")
            print(t["text"])
