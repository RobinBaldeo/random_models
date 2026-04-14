# SQL Validation Module Refactor — Prompt for Claude Opus 4.6

I need you to refactor a single Python file into several smaller files. This is a pure mechanical split — no logic changes, no behavior changes, no "improvements." I want to be able to diff the before and after and see only file-boundary changes.

## Context

The file is `database/sql_validation.py` in an internal LangGraph-based platform at my company. It's a SQL validation module built on SQLGlot that validates LLM-generated SQL queries before they get executed against a database. It's about to become a dependency wired into multiple SQL-executing tools, so I want it broken into focused files before that happens. Right now it's 557 lines and does too much in one place.

The current file contains:

- A `DialectConfig` dataclass plus a `DIALECT_CONFIGS` registry covering five SQL dialects: Dremio, T-SQL, MySQL, Spark, Oracle
- `get_dialect_config()` and `get_supported_dialects()` lookup helpers
- A `ValidationResult` dataclass
- A `Validation` class that orchestrates AST-based checks: parsing, hallucinated field detection (via SQLGlot's `qualify`), SELECT * detection, max columns, LIMIT/TOP/FETCH/ROWNUM handling, restricted ops (INSERT/UPDATE/DELETE/etc.), and date literal validation with a two-gate approach (regex shape + actual calendar date parse)
- An `@tool`-decorated async `sql_validate` function that wraps `Validation` for use as a LangChain tool, with `InjectedToolArg` parameters for design-time configuration

The full current file is included at the end of this prompt.

## Constraints — read these carefully

1. **The split must stay inside the existing `database/` folder.** Do NOT create a `sql_validation/` subpackage. Do NOT create a `checks/` subpackage. Everything goes in `database/` as sibling files. This is a hard requirement.

2. **No logic changes whatsoever.** Same function bodies, same class methods, same control flow, same error messages, same defaults, same third-party imports. If you spot a bug, leave it alone and mention it at the end in a "Bugs noticed but not fixed" section. Bugs get handled in a separate PR.

3. **Backward compatibility is mandatory.** Any code currently doing `from database.sql_validation import sql_validate` (or `Validation`, `ValidationResult`, `DialectConfig`, `DIALECT_CONFIGS`, `get_dialect_config`, `get_supported_dialects`) must keep working without modification. The original `database/sql_validation.py` becomes a thin re-export shim.

4. **The LangChain dependency must be isolated.** Only the file containing the `@tool sql_validate` wrapper should import from `langchain_core`. The core validator and dialect config files should be importable in environments where `langchain_core` isn't installed.

5. **Avoid circular imports.** `ValidationResult` is used by both the validator and (via re-export) the shim, so it lives in its own file.

6. **Preserve whitespace and indentation quirks in the original, including any stray tabs, trailing whitespace, or inconsistent blank lines.** If the original has a line with weird spacing between two definitions, keep it as close to the original as you reasonably can. Do not run the code through a formatter.

## Target file layout

Produce exactly these five files, no more, no fewer:

```
database/
├── sql_validation.py           # shim — re-exports everything for backward compat
├── sql_dialects.py             # DialectConfig, _DEFAULT_RESTRICTED, DIALECT_CONFIGS, get_dialect_config, get_supported_dialects
├── sql_validation_result.py    # ValidationResult dataclass only
├── sql_validator.py            # Validation class (all _check_* methods stay as methods on the class)
└── sql_validation_tool.py      # @tool sql_validate async function
```

## Expected file contents and rough sizes

Use this as a guide for what goes where. If your split deviates meaningfully from this, stop and explain why instead of proceeding.

### `database/sql_validation.py` — shim, ~15 lines

Re-exports everything from the four new files. Structure:

- Imports from `database.sql_dialects`: `DialectConfig`, `DIALECT_CONFIGS`, `get_dialect_config`, `get_supported_dialects`
- Imports from `database.sql_validation_result`: `ValidationResult`
- Imports from `database.sql_validator`: `Validation`
- Imports from `database.sql_validation_tool`: `sql_validate`
- No `__all__` unless the original had one

### `database/sql_dialects.py` — ~85 lines

Pure configuration. No SQLGlot, no LangChain, no regex imports.

- `from dataclasses import dataclass`
- `from typing import Dict, List`
- `DialectConfig` dataclass
- `_DEFAULT_RESTRICTED` frozenset
- `DIALECT_CONFIGS` dict with all five dialect entries (dremio, tsql, mysql, spark, oracle)
- `get_dialect_config()` function
- `get_supported_dialects()` function

### `database/sql_validation_result.py` — ~10 lines

- `from dataclasses import dataclass`
- `ValidationResult` dataclass only (fields: `error_present`, `error_details`, `error_type`)

### `database/sql_validator.py` — ~400 lines

The bulk of the code. No `@tool`, no `InjectedToolArg`, no LangChain imports.

- Imports: `sqlglot`, `re`, `datetime.datetime as dt`, `sqlglot.exp`, `sqlglot.optimizer.qualify`, `sqlglot.errors.ParseError`, `ast.literal_eval`, `typing.Dict`/`List`/`Optional`
- Imports from `database.sql_dialects`: `DialectConfig`, `get_dialect_config`
- Imports from `database.sql_validation_result`: `ValidationResult`
- The entire `Validation` class with all methods in the original order (do not reorder):
  - `__init__`
  - `validate`
  - `_parse_sql`
  - `_build_schema`
  - `_find_invalid_fields`
  - `_detect_select_star_final_only`
  - `_check_max_columns`
  - `_check_placeholder_dates`
  - `_extract_cast_date_literal`
  - `_get_date_func_name`
  - `_extract_func_date_literal`
  - `_collect_date_literals`
  - `_try_parse_date`
  - `_validate_limit_value`
  - `_check_limit_style`
  - `_check_top_style`
  - `_check_fetch_rownum_style`
  - `_check_limit`
  - `_extract_rownum_value`
  - `_find_rownum_limit`
  - `_detect_restricted_ops`

### `database/sql_validation_tool.py` — ~55 lines

The only file that imports LangChain.

- `from dataclasses import asdict`
- `from typing import Annotated`
- `from ast import literal_eval`
- `from langchain_core.tools import tool, InjectedToolArg`
- `from database.sql_validator import Validation`
- The `@tool async def sql_validate(...)` function, verbatim, with all seven `InjectedToolArg` parameters and the `literal_eval` unwrapping logic

## What to produce

For each of the five files, output:

1. The full file contents in a fenced code block, with the file path as a comment on the first line (e.g. `# database/sql_dialects.py`)
2. A one-sentence note after the code block saying what is in it

After all five files, give me:

- A short **Verification checklist** — the imports a consumer would test to confirm backward compat still works (e.g., `from database.sql_validation import sql_validate`)
- A **Bugs noticed but not fixed** section with line references from the original file, if anything stood out. Do not fix them.
- A **Things I deliberately did not change** section if you were tempted to clean something up but didn't, so I know what's still on the table for a follow-up PR.

## What NOT to do

- Do not rename functions, classes, methods, or parameters
- Do not change docstrings (copy them verbatim, including any inline `#` comments)
- Do not change default values
- Do not "modernize" type hints (leave `Optional[X]` as `Optional[X]`, do not convert to `X | None`)
- Do not add `__all__` exports unless the original had them
- Do not add new error handling, logging, or comments
- Do not split the `Validation` class methods into standalone functions — keep them as methods on the class
- Do not add tests (separate PR)
- Do not add type hints that weren't in the original
- Do not reorder methods within the `Validation` class
- Do not run the code through a formatter (no Black, no isort, no manual reformatting)

## Original file

```python
[PASTE THE FULL CONTENTS OF database/sql_validation.py HERE]
```

Begin.
