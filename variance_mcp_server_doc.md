# Variance Calculator MCP Server

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes **one tool** for deriving "calc" columns from "raw" columns of a tabular
dataset — the kind of repeating, drawn-down-the-rows formula common in
spreadsheets — and hands the completed rows back to an LLM.

---

## Why

Spreadsheets typically mix two kinds of columns:

| Kind | Meaning |
|------|---------|
| **Raw** | Values that come straight from the source file. |
| **Calc** | Values derived from the raw columns by a repeating formula. |

LLMs are unreliable at arithmetic, so the deterministic math belongs in a tool.
The model orchestrates (reads the table, decides to call the tool, narrates the
result); the tool guarantees the numbers.

---

## Flow

1. A file is uploaded containing only the **raw** columns.
2. Those rows are inserted into the prompt as a list of dicts.
3. The model calls the tool with those rows.
4. The tool returns each row with the **calc** columns filled in.
5. The model uses the completed rows for downstream work.

```
raw rows ──► model calls tool ──► tool returns rows + calc columns ──► model narrates
```

---

## The calc model

Each calc output is simply `actual - benchmark`:

```
vs_b1 = actual - benchmark_1
vs_b2 = actual - benchmark_2
vs_b3 = actual - benchmark_3
vs_b4 = actual - benchmark_4
```

Because every calc is the same operation, the logic lives in **one place**
(`CALC_SPECS`). Add, remove, or rename a comparison by editing that table —
no new functions, no duplicate tools.

---

## Usage

```bash
pip install mcp
python variance_mcp_server.py   # runs over stdio
```

### Input

```json
[
  {"id": 1, "label": "Row A",
   "actual": 1000, "benchmark_1": 1200, "benchmark_2": 950,
   "benchmark_3": 1100, "benchmark_4": 800}
]
```

### Output

```json
[
  {"id": 1, "label": "Row A",
   "actual": 1000, "benchmark_1": 1200, "benchmark_2": 950,
   "benchmark_3": 1100, "benchmark_4": 800,
   "vs_b1": -200, "vs_b2": 50, "vs_b3": -100, "vs_b4": 200}
]
```

---

## Behavior notes

- **Unit-agnostic** subtraction — currency, percentage points, and counts are
  all handled the same way.
- **Blank separator rows** (`null` / `{}`) pass through unchanged so table
  layout is preserved.
- **Missing or non-numeric inputs** yield `null` for that calc rather than
  raising.
- The core functions (`compute_calcs`, `compute_rows`) are importable and usable
  without MCP for any downstream pipeline.

---

## Source

```python

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("variance-calculator")

# -- Single source of truth -------------------------------------------------
# output_field -> (left_input_field, right_input_field)
# Each calc is: row[left] - row[right].
CALC_SPECS = {
    "vs_b1": ("actual", "benchmark_1"),
    "vs_b2": ("actual", "benchmark_2"),
    "vs_b3": ("actual", "benchmark_3"),
    "vs_b4": ("actual", "benchmark_4"),
}

# Fields that are echoed back unchanged (labels/identifiers carried per row).
# Anything not listed here and not a calc output is still passed through.
PASSTHROUGH_HINTS = ("id", "label", "category")

ROUND_DP = 4  # rounding for calc outputs


# -- Core logic (plain functions, importable without MCP) -------------------
def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def compute_calcs(row: dict) -> dict:
    """
    Compute every calc output for a single row.

    Returns a dict of {output_field: value}. A value is produced only when both
    inputs are present and numeric; otherwise it is None. The subtraction is
    unit-agnostic (currency, percentage points, counts - all handled the same).

    This is the ONLY place the calc logic lives; import and reuse it freely.
    """
    result = {}
    for out_field, (left, right) in CALC_SPECS.items():
        a = row.get(left)
        b = row.get(right)
        result[out_field] = round(a - b, ROUND_DP) if _is_number(a) and _is_number(b) else None
    return result


def compute_rows(rows: list) -> list:
    """
    Apply compute_calcs to every row.

    Falsy entries (None / empty dict) are treated as blank separator rows and
    pass through unchanged so the table layout is preserved. All original
    fields are echoed back alongside the new calc fields.
    """
    out = []
    for row in rows:
        if not row:
            out.append(row)
            continue
        merged = dict(row)
        merged.update(compute_calcs(row))
        out.append(merged)
    return out


# -- The single MCP tool ----------------------------------------------------
@mcp.tool()
def compute_variance_columns(rows: list[dict]) -> list[dict]:
    """
    Derive calc columns from raw columns for a list of rows.

    INPUT: a list of row objects. Each row should contain the raw numeric
    inputs referenced by CALC_SPECS:
        actual       - the current value
        benchmark_1  - first comparison value
        benchmark_2  - second comparison value
        benchmark_3  - third comparison value
        benchmark_4  - fourth comparison value
    Any label fields (e.g. id, label, category) are optional and echoed back
    untouched. Blank separator rows are allowed and pass through.

    OUTPUT: the same rows, each with calc columns added:
        vs_b1 = actual - benchmark_1
        vs_b2 = actual - benchmark_2
        vs_b3 = actual - benchmark_3
        vs_b4 = actual - benchmark_4
    A calc is null if either of its inputs is missing or non-numeric.
    """
    return compute_rows(rows)


if __name__ == "__main__":
    mcp.run()
```

---


