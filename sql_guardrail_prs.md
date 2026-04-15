# PR 2 + PR 3 + PR 4: SQL Validation Guardrail — Implementation

## Overview

Three small, related changes that build on each other:

- **PR 2** adds a `validate_guardrail()` helper method to the `Validation` class in `database/sql_validator.py`. It runs a minimal, always-safe subset of checks suitable for pre-execution validation inside SQL-executing tools.
- **PR 3** wires that helper into the two SQL execute tools (`run_dremio_query` and `run_query`) as a non-bypassable pre-flight gate. Validation failures raise `ValueError` before any database connection is attempted.
- **PR 4** replaces the `ValueError` from PR 3 with a domain-specific `SQLValidationError` exception class, modeled on an existing in-house pattern for connection errors. Same propagation path, richer structured fields, cleaner distinction in logs and UIs from generic tool failures.

Files touched across all three PRs:

1. `database/sql_validator.py` — PR 2 adds one new method (~15 lines)
2. `<tools>/execute_dremio_query.py` — PR 3 adds validator import, guardrail call, one new param (~15 lines added); PR 4 swaps the raise statement for a custom exception
3. `<tools>/sql_execute.py` — same as above
4. `database/sql_validation_errors.py` — PR 4 adds a new file (~35 lines) holding the `SQLValidationError` class

The existing `validate()` method on the `Validation` class is unchanged throughout. The standalone `sql_validate` tool and any bound-to-model usage continue to work exactly as they do today.

PR 4 is **optional polish** — ship PR 2 and PR 3 first, verify how the `ValueError` renders in the orchestration UI, and only do PR 4 if the distinction between validation failures and other tool errors needs to be sharper.

---

## PR 2: Add `validate_guardrail()` helper method

### What gets added

A single new method on the `Validation` class. Place it immediately after the existing `validate()` method so the two entry points are visually adjacent in the file.

```python
def validate_guardrail(self) -> List[ValidationResult]:
    """Run a minimal, always-safe subset of checks for use as a pre-execution
    gate inside SQL-executing tools.

    Runs: parse, restricted ops, limit, max columns (when set), SELECT *.
    Skips: hallucinated field detection (needs schema), date format check.

    If schema or table_name_pattern were not provided, the relevant checks
    are already no-ops, so guardrail callers can leave those unset.

    Returns an empty list when the query passes all checks. A non-empty
    list indicates one or more findings the caller should treat as blocking.
    """
    errors: List[ValidationResult] = []

    parse_error = self._parse_sql()
    if parse_error:
        errors.append(parse_error)
        return errors

    checks = [
        self._detect_restricted_ops,
        self._check_limit,
        self._check_max_columns,
        self._detect_select_star_final_only,
    ]

    for check in checks:
        result = check()
        if result is not None and result.error_present:
            errors.append(result)

    return errors
```

### Design notes

- **No "no errors" sentinel.** Unlike `validate()`, which appends a sentinel `ValidationResult` when everything passes, `validate_guardrail()` returns an empty list on success. The caller just needs a truthiness check. Simpler.
- **Parse errors short-circuit.** If `_parse_sql()` fails, the method returns immediately with just the parse error. No point running downstream checks on an unparsed tree.
- **The check list is deliberate.** Syntax, restricted ops, limit, max columns, SELECT *. Everything else (hallucinated fields, date formats) is excluded because those require context that an execute tool doesn't have.
- **Schema-dependent checks are safe when schema is null.** The existing `_find_invalid_fields` method already returns a clean `ValidationResult` when `self.schema` is falsy — but we don't call it at all from the guardrail, so this is irrelevant for this PR.

### What does NOT change

- `validate()` — untouched, still runs the full check suite
- Any `_check_*` private method — untouched
- `DialectConfig`, `DIALECT_CONFIGS`, dialect lookup helpers — untouched
- `ValidationResult` — untouched
- The `sql_validate` LangChain tool — untouched
- The backward-compat shim `sql_validation.py` — untouched
- Imports in `sql_validator.py` — unchanged
- No other files in the `database/` folder are modified

### Acceptance criteria

- [ ] New method `validate_guardrail()` exists on `Validation` class in `database/sql_validator.py`
- [ ] Method signature is `def validate_guardrail(self) -> List[ValidationResult]`
- [ ] Method runs exactly these checks in order: `_parse_sql`, `_detect_restricted_ops`, `_check_limit`, `_check_max_columns`, `_detect_select_star_final_only`
- [ ] Method does NOT call `_find_invalid_fields` or `_check_placeholder_dates`
- [ ] When `_parse_sql` returns a parse error, the method returns immediately with just that error
- [ ] When all checks pass, the method returns `[]` (empty list, no sentinel)
- [ ] `validate()` method body is byte-identical to its previous state
- [ ] No imports added or removed in `sql_validator.py`
- [ ] No other files in the repository are modified by this PR

### Unit tests to add

Add these to whatever test file covers `sql_validator.py`, or create a new one if none exists.

```python
import pytest
from database.sql_validator import Validation


def _make_validator(sql: str, dialect: str = "dremio", required_limit: int = 100):
    return Validation(
        raw_sql_string=sql,
        dialect=dialect,
        schema=None,
        required_limit=required_limit,
        max_columns=None,
        table_name_pattern=None,
        place_holder_table=None,
    )


class TestValidateGuardrail:

    def test_clean_query_passes(self):
        v = _make_validator("SELECT id, name FROM customers LIMIT 10")
        assert v.validate_guardrail() == []

    def test_drop_table_is_blocked(self):
        v = _make_validator("DROP TABLE customers")
        errors = v.validate_guardrail()
        assert len(errors) >= 1
        assert any(e.error_type == "operator" for e in errors)

    def test_update_is_blocked(self):
        v = _make_validator("UPDATE customers SET name = 'x' WHERE id = 1")
        errors = v.validate_guardrail()
        assert any(e.error_type == "operator" for e in errors)

    def test_delete_is_blocked(self):
        v = _make_validator("DELETE FROM customers WHERE id = 1")
        errors = v.validate_guardrail()
        assert any(e.error_type == "operator" for e in errors)

    def test_missing_limit_is_blocked(self):
        v = _make_validator("SELECT id FROM customers")
        errors = v.validate_guardrail()
        assert any(e.error_type == "limit" for e in errors)

    def test_limit_exceeds_max_is_blocked(self):
        v = _make_validator("SELECT id FROM customers LIMIT 5000", required_limit=100)
        errors = v.validate_guardrail()
        assert any(e.error_type == "limit" for e in errors)

    def test_select_star_is_blocked(self):
        v = _make_validator("SELECT * FROM customers LIMIT 10")
        errors = v.validate_guardrail()
        assert any(e.error_type == "star" for e in errors)

    def test_syntax_error_short_circuits(self):
        v = _make_validator("SELEKT bad query LIMIT 10")
        errors = v.validate_guardrail()
        assert len(errors) == 1
        assert errors[0].error_type == "syntax"

    def test_schema_check_not_run(self):
        # Even with an intentionally wrong-looking query, no "fake field" error
        # should come from the guardrail because the schema check is excluded.
        v = _make_validator("SELECT nonexistent_col FROM customers LIMIT 10")
        errors = v.validate_guardrail()
        assert not any(e.error_type == "fake field" for e in errors)

    def test_date_check_not_run(self):
        # A malformed date literal should not produce an "invalid date" error
        # because the date check is excluded from the guardrail.
        v = _make_validator(
            "SELECT id FROM customers WHERE created = CAST('not-a-date' AS DATE) LIMIT 10"
        )
        errors = v.validate_guardrail()
        assert not any(e.error_type == "invalid date" for e in errors)

    def test_tsql_dialect_with_top(self):
        v = _make_validator("SELECT TOP 10 id FROM customers", dialect="tsql")
        assert v.validate_guardrail() == []

    def test_tsql_missing_top_is_blocked(self):
        v = _make_validator("SELECT id FROM customers", dialect="tsql")
        errors = v.validate_guardrail()
        assert any(e.error_type == "limit" for e in errors)
```

---

## PR 3: Wire the guardrail into the SQL execute tools

### What gets added

Both execute tools import `Validation` from `database.sql_validator`, call `validate_guardrail()` before attempting any database connection, and raise `ValueError` if any findings come back.

One new parameter is added to each tool: `required_limit: int = 1000`. This is the only knob authors have for configuring the guardrail — it sets the maximum row count enforced by the limit check. All other validation inputs (schema, table pattern, max columns) are passed as `None`, which disables the corresponding checks inside `validate_guardrail()`.

### Wrapped `run_dremio_query`

```python
from typing import Any, Dict, Optional, Tuple
import pyarrow.flight as fl
import pyarrow as pa
from langchain_core.tools import tool
from runtime.models import OrchestrationState
from authx.auth_profile_resolver import (
    get_auth_profile,
    get_auth_credentials,
    validate_dremio_credentials,
)
import logging
import datetime
import numpy as np

# NEW IMPORT
from database.sql_validator import Validation


# get_dremio_connection_params, dremio_connect, convert_dates — UNCHANGED


@tool(description="Execute a SQL query against Dremio and return results as JSON.")
async def run_dremio_query(
    query: str,
    auth_profile_id: str,
    state: OrchestrationState,
    # NEW: guardrail row cap
    required_limit: int = 1000,
) -> str:
    """
    Execute a SQL query against Dremio using connection parameters from auth profile.
    Returns a JSON string of records from the query result.
    Raises ValueError for invalid input, validation failures, or connection/query errors.
    """
    if not query:
        raise ValueError("Invalid query or empty string.")
    if not auth_profile_id:
        raise ValueError("auth_profile_id is required.")

    # --- NEW: guardrail gate ---
    validator = Validation(
        raw_sql_string=query,
        dialect="dremio",
        schema=None,
        required_limit=required_limit,
        max_columns=None,
        table_name_pattern=None,
        place_holder_table=None,
    )
    guardrail_errors = validator.validate_guardrail()
    if guardrail_errors:
        error_msg = "; ".join(
            f"[{r.error_type}] {r.error_details}" for r in guardrail_errors
        )
        logging.warning(f"Dremio query blocked by guardrail: {error_msg}")
        raise ValueError(f"SQL validation failed: {error_msg}")
    # --- end guardrail ---

    try:
        client, options = await dremio_connect(state, auth_profile_id)
        info = client.get_flight_info(fl.FlightDescriptor.for_command(query), options)
        tables = []
        for i, ep in enumerate(info.endpoints):
            reader = client.do_get(ep.ticket, options)
            tbl = reader.read_all()
            tables.append(tbl)

        if not tables:
            return []

        final_table = pa.concat_tables(tables, promote=True)
        df = final_table.to_pandas()
        data = df.to_dict(orient="records")
        return convert_dates(data)
    except Exception as e:
        logging.error(f"Failed to execute Dremio query: {e}")
        raise ValueError("Failed to execute Dremio query. See logs for details.") from e
```

### Wrapped `run_query` (SQL Server)

```python
from typing import Any, Dict, List, Tuple

from langchain_core.tools import tool
import json
from runtime.models import OrchestrationState
from tools.sql_server.db_executor import DbExecutor, build_executor

# NEW IMPORT
from database.sql_validator import Validation


# NOTE: redact the connection string below to something appropriate for your
# environment before committing. It is kept here for structural reference only.
CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=<host>\\<instance>,<port>;"
    "DATABASE=<database>;"
    "Trusted_Connection=Yes;"
    "Encrypt=yes;"
    "TrustServerCertificate=yes;"
)


def _get_executor() -> DbExecutor:
    """Factory for the DB executor.

    Tests can monkeypatch this function to return a fake executor, which keeps
    unit tests runnable in environments without `pyodbc`.
    """
    return build_executor(CONN_STR)


@tool("sql_execute_query")
def run_query(
    query: str,
    params: Tuple[Any, ...] = (),
    auth_profile_id: str = None,
    state: OrchestrationState = None,
    # NEW: guardrail row cap
    required_limit: int = 1000,
) -> List[Dict]:
    """Execute a SQL query and return results as a list of dictionaries.

    Args:
        query: The SQL query string to execute.
        params: Optional tuple of parameters for parameterized queries.
        auth_profile_id: Optional auth profile identifier for connection override.
        state: Orchestration state for resolving auth profiles.
        required_limit: Maximum row limit enforced by the guardrail.

    Returns:
        A list of dictionaries where each dictionary represents a row,
        with column names as keys and row values as values.
    """
    # --- NEW: guardrail gate ---
    validator = Validation(
        raw_sql_string=query,
        dialect="tsql",
        schema=None,
        required_limit=required_limit,
        max_columns=None,
        table_name_pattern=None,
        place_holder_table=None,
    )
    guardrail_errors = validator.validate_guardrail()
    if guardrail_errors:
        error_msg = "; ".join(
            f"[{r.error_type}] {r.error_details}" for r in guardrail_errors
        )
        print(f"SQL Server query blocked by guardrail: {error_msg}")
        raise ValueError(f"SQL validation failed: {error_msg}")
    # --- end guardrail ---

    if auth_profile_id:
        auth_profile = state.get("authProfiles", {}).get(auth_profile_id)
        configuration = auth_profile.get("configuration")
        if configuration and "connectionString" in configuration:
            connection_string = configuration["connectionString"]
            print(f"Overriding connection string from auth profile {auth_profile_id}")
            executor = build_executor(connection_string)
    else:
        executor = _get_executor()
    return executor.query(query, params)
```

### Design notes

- **`required_limit` default of 1000.** Chosen as a reasonable cap that is low enough to prevent runaway queries from consuming excessive memory, but high enough that realistic production queries will not be clipped. Authors can override per graph via parameterOverride. Adjust the default if you have a better baseline for your workloads.
- **Only one new parameter.** `max_columns`, `sql_schema`, and `table_pattern` are deliberately omitted from the execute tool signatures. All three are passed as `None` to the `Validation` constructor, which makes the corresponding checks no-ops. Keeps the execute tool configuration simple. If you later want authors to configure these, adding each one is a five-line change.
- **Dialect is hardcoded per tool.** The Dremio tool is always `"dremio"`, the SQL Server tool is always `"tsql"`. The graph author cannot pick the wrong dialect because the tool is bound to a specific database type.
- **Validation runs before the connection attempt.** The guardrail gate sits above any database I/O, so an invalid query is rejected without opening a connection. This matters for cost, for latency, and for avoiding partial-state side effects.
- **`ValueError` reuses the existing error pattern.** Both tools already raise `ValueError` on other failure modes, so the self-healing loop sees validation errors the same way it sees DB errors today. No upstream routing changes required.
- **Logging style matches each tool's current style.** `run_dremio_query` uses `logging.warning`; `run_query` uses `print` because that is what the rest of the module does. A separate cleanup PR can unify these.

### What does NOT change

- The existing `dremio_connect`, `get_dremio_connection_params`, `convert_dates`, `_get_executor`, `_load_auth_profile` functions — untouched
- Flight connection logic, result conversion, auth profile resolution — unchanged
- The standalone `sql_validate` tool in `database/sql_validation_tool.py` — untouched
- `validate()` method on `Validation` — untouched
- No new dependencies added to either execute tool

### Acceptance criteria

- [ ] Both execute tool files import `Validation` from `database.sql_validator`
- [ ] Both tools have a new `required_limit: int = 1000` parameter
- [ ] Both tools construct a `Validation` instance with `schema=None`, `max_columns=None`, `table_name_pattern=None`, `place_holder_table=None`
- [ ] Dremio tool hardcodes `dialect="dremio"`
- [ ] SQL Server tool hardcodes `dialect="tsql"`
- [ ] Both tools call `validator.validate_guardrail()` before attempting any database connection
- [ ] Both tools raise `ValueError` with a semicolon-joined error message when the guardrail returns findings
- [ ] Dremio tool uses `logging.warning` to log guardrail blocks
- [ ] SQL Server tool uses `print` to log guardrail blocks (matching existing module style)
- [ ] The original execution logic in both tools is unchanged below the guardrail gate
- [ ] Existing graphs that use these tools without setting `required_limit` continue to work (default applies)
- [ ] A valid query executes end-to-end through both tools in an integration test
- [ ] A query containing `DROP TABLE` raises `ValueError` through both tools without attempting a database connection
- [ ] A query missing a `LIMIT` (Dremio) or `TOP` (SQL Server) clause raises `ValueError` without attempting a database connection

---

## Verification checklist (before opening PRs)

Run these locally before pushing:

1. `from database.sql_validator import Validation` imports successfully from both execute tool files
2. `Validation(raw_sql_string="SELECT id FROM foo LIMIT 10", dialect="dremio", ...).validate_guardrail()` returns `[]`
3. Same call with `"DROP TABLE foo"` returns a non-empty list containing an `error_type="operator"` result
4. Same call with `"SELECT id FROM foo"` (no LIMIT) returns a list containing an `error_type="limit"` result
5. Same call with `"SELECT * FROM foo LIMIT 5"` returns a list containing an `error_type="star"` result
6. The existing `sql_validate` standalone tool still runs end-to-end in a real graph (regression check — PR 2 should not affect it)
7. A valid query through the wrapped Dremio tool executes and returns results as before
8. A valid query through the wrapped SQL Server tool executes and returns results as before
9. An invalid query through the wrapped Dremio tool raises `ValueError` before any Flight RPC is attempted (verify by temporarily logging inside `dremio_connect` and confirming it is never reached)
10. Same check for the SQL Server tool — confirm the executor is never built when validation fails

---

## Out of scope for PR 2 and PR 3

- **Schema loading redesign.** The `_build_schema()` method's current string-of-dicts approach is unchanged. A separate future PR will replace it with a JSON artifact loaded from object storage.
- **Error surfacing improvements.** Raising `ValueError` is a functional first step. A follow-up PR will introduce a domain-specific exception class (`SQLValidationError`) with structured fields, mirroring the pattern used for connection error handling elsewhere in the codebase.
- **Cherry-picked blocking error types.** Currently the guardrail raises on *any* finding. A follow-up PR can add a `blocking_errors` parameterOverride that lets authors select which error types are blocking vs. logged-only.
- **Latent bug fixes in the execute tools.** Any pre-existing bugs in the execute tools (e.g., null dereferences in auth profile handling) are deliberately left alone in this PR. File them as separate tickets.
- **Additional parameterOverrides.** Only `required_limit` is added. `max_columns`, schema, and table pattern stay null. Adding them later is a small change.
- **Unit tests for the execute tool wraps.** Unit tests are included for `validate_guardrail()` in PR 2. Integration-level tests for the wrapped execute tools depend on your existing test harness and are left to the author.

---

## Rollback plan

If PR 3 causes production issues, it can be reverted independently of PR 2. PR 2 adds a method that nothing calls, so leaving it in place after a PR 3 revert has no impact. If PR 2 itself needs to be reverted, it is a single-method removal with no cascading effects.

The validation gate can also be hot-disabled per deployment by setting `required_limit` to an extremely high number (e.g., `required_limit=10_000_000`), which effectively turns off the limit check. Restricted operation detection cannot be disabled without reverting PR 3 — which is the intended behavior of a guardrail.

PR 4 can be reverted independently of PR 3 — reverting PR 4 just swaps `SQLValidationError` back to `ValueError` at the raise sites and removes the `sql_validation_errors.py` file. No cascading effects.

---

## PR 4: Custom `SQLValidationError` exception class

### Why this PR exists

After PR 3, both execute tools raise `ValueError` on validation failure. This is functional — the self-healing loop sees the error, the LLM self-corrects, and the log line is written — but it has two drawbacks:

1. **It blends in with other failure modes.** Both execute tools already raise `ValueError` for bad inputs and unrelated failures. A consumer looking at logs or exception dashboards cannot easily tell a validation block from a connection failure from a malformed argument.
2. **The error payload is a flat string.** If any downstream code ever wants to pattern-match on validation failures — a test assertion, a telemetry hook, a future UI enhancement — it has to parse the message string, which is brittle.

This PR introduces a domain-specific exception class `SQLValidationError` that carries structured fields (`dialect`, `tool_name`, `errors`, `sql_query`) while producing the same readable message via `__str__`. The class follows the same structural pattern as an existing in-house exception used for connection errors, so the review conversation is short and the UI rendering path is already known-good.

### Important design note

PR 4 does **not** introduce streaming events, custom stream writers, or modifications to any SSE / event-rendering module. The exception propagates up through the normal tool-node path and is rendered the same way any other tool exception is rendered. If the in-house orchestration UI already distinguishes known exception classes from generic `Exception`, `SQLValidationError` will ride that same path. If it does not, the user-visible output is still a readable message — same as `ValueError`, just with a clearer class name in the logs.

### Pre-work before implementing

Before writing PR 4, verify one thing: check how the existing in-house connection-error exception class surfaces in the orchestration UI. Look for one of:

- A class-based handler somewhere upstream (`isinstance(exc, ConnectionErrorClass)`) that routes to a distinct UI treatment
- A safety-net try/except at a well-defined boundary (similar to how tool-manager code wraps MCP tool retrieval) that catches any exception from a layer and normalizes it
- No special handling — the UI just renders `str(exc)` for any unhandled exception

If the answer is "no special handling," PR 4 is largely cosmetic and can be deprioritized. If there is class-based handling or a safety-net wrapper, PR 4 needs to mirror that pattern so `SQLValidationError` is recognized by the same logic.

The fastest way to check: grep the codebase for the name of the existing connection-error class and see where it is caught, re-raised, or type-checked.

### What gets added

**A new file** `database/sql_validation_errors.py`:

```python
"""Domain-specific exceptions for SQL validation failures.

Modeled on the in-house connection-error pattern used elsewhere in the
codebase — structured fields on the exception itself, a readable __str__,
no streaming or formatter changes. The exception propagates up through the
tool-node path and is rendered by whatever layer already handles tool
exceptions.
"""

from typing import List, Optional


class SQLValidationError(Exception):
    """Raised when a SQL query fails guardrail validation inside an execute tool.

    Attributes:
        dialect: The SQL dialect the query was validated against (e.g. "dremio", "tsql").
        tool_name: The execute tool that triggered validation (e.g. "run_dremio_query").
        errors: List of {error_type, error_details} dicts from the validator findings.
        sql_query: The original SQL string that failed validation, if available.
    """

    def __init__(
        self,
        dialect: str,
        tool_name: str,
        errors: List[dict],
        sql_query: Optional[str] = None,
    ):
        self.dialect = dialect
        self.tool_name = tool_name
        self.errors = errors
        self.sql_query = sql_query
        super().__init__(self._format())

    def _format(self) -> str:
        """Produce a human-readable summary of the validation failure."""
        if not self.errors:
            return f"SQL validation failed in {self.tool_name} ({self.dialect})"

        error_parts = "; ".join(
            f"[{e.get('error_type', 'unknown')}] {e.get('error_details', '')}"
            for e in self.errors
        )
        return (
            f"SQL validation failed in {self.tool_name} ({self.dialect}): "
            f"{error_parts}"
        )
```

### What gets modified

**`<tools>/execute_dremio_query.py`**

Add one new import alongside the `Validation` import from PR 3:

```python
from database.sql_validation_errors import SQLValidationError
```

Replace the guardrail raise block from PR 3 with:

```python
guardrail_errors = validator.validate_guardrail()
if guardrail_errors:
    error_dicts = [
        {"error_type": r.error_type, "error_details": r.error_details}
        for r in guardrail_errors
    ]
    exc = SQLValidationError(
        dialect="dremio",
        tool_name="run_dremio_query",
        errors=error_dicts,
        sql_query=query,
    )
    logging.warning(f"Dremio query blocked by guardrail: {exc}")
    raise exc
```

The log line still fires — it just now logs the exception's `__str__` output, which produces the same readable format as before.

**`<tools>/sql_execute.py`**

Add the same import:

```python
from database.sql_validation_errors import SQLValidationError
```

Replace the guardrail raise block from PR 3 with:

```python
guardrail_errors = validator.validate_guardrail()
if guardrail_errors:
    error_dicts = [
        {"error_type": r.error_type, "error_details": r.error_details}
        for r in guardrail_errors
    ]
    exc = SQLValidationError(
        dialect="tsql",
        tool_name="sql_execute_query",
        errors=error_dicts,
        sql_query=query,
    )
    print(f"SQL Server query blocked by guardrail: {exc}")
    raise exc
```

### Design notes

- **Modeled on an existing pattern.** The shape of `SQLValidationError` mirrors an existing in-house exception class used for connection errors — same `__init__` style with structured fields, same `_format()` approach for the readable message, same standard-library-only imports. Reviewers recognize the pattern immediately.
- **No shared code with the existing pattern.** Despite the structural similarity, `SQLValidationError` imports nothing from the connection-error module. The two classes are siblings, not inheritors. This keeps the dependency graph clean and lets each class evolve independently.
- **Structured fields for future use.** Carrying `dialect`, `tool_name`, `errors`, and `sql_query` as attributes means any future code (tests, telemetry, UI enhancements) can introspect the exception without parsing strings. You do not need any of this today — it is there for when you do.
- **`__str__` preserves the PR 3 message format.** The `_format()` method produces output nearly identical to the string built by PR 3's `ValueError`, so log lines and error messages look the same. Only the exception class name changes.
- **Log statements stay.** PR 4 does not remove or alter the log calls — only the exception type. If logs are currently grepped for `"blocked by guardrail"`, those greps continue to work.
- **Self-healing loop continues to work.** The tool-node path catches any `Exception`, including `SQLValidationError`, and feeds the message back to the agent on its next turn. No routing changes required.

### What does NOT change

- `sql_validator.py` — untouched
- `validate_guardrail()` — untouched
- The existing `validate()` method — untouched
- The standalone `sql_validate` tool — untouched
- The backward-compat shim — untouched
- `http_client` or any connection-error module — not imported, not modified
- No streaming, SSE, or event-formatter code is touched anywhere in this PR
- No changes to the tool-node wrapper or any upstream dispatcher

### Acceptance criteria

- [ ] New file `database/sql_validation_errors.py` exists and contains only the `SQLValidationError` class
- [ ] `SQLValidationError` extends `Exception`
- [ ] Constructor signature is `(dialect: str, tool_name: str, errors: List[dict], sql_query: Optional[str] = None)`
- [ ] Constructor stores all four arguments as instance attributes
- [ ] Constructor calls `super().__init__(self._format())` so the exception's message matches `__str__`
- [ ] `_format()` returns the readable message, including all findings joined by `"; "`
- [ ] Both execute tool files import `SQLValidationError` from `database.sql_validation_errors`
- [ ] Both execute tool files raise `SQLValidationError` instead of `ValueError` when the guardrail returns findings
- [ ] Log statements in both tools are preserved (still fire before the raise)
- [ ] The error message format in log lines is unchanged from PR 3 (verify by grepping logs before and after)
- [ ] A valid query still executes end-to-end through both tools
- [ ] A query containing `DROP TABLE` raises `SQLValidationError` (verified by integration test)
- [ ] A query missing a `LIMIT` / `TOP` clause raises `SQLValidationError`
- [ ] The self-healing loop continues to trigger on `SQLValidationError` — verified by running a bad query through a Codeless Agent graph and confirming the agent receives the error and retries with a corrected query
- [ ] Manual verification in the orchestration UI: the rendered error is at least as readable as the `ValueError` from PR 3, and ideally distinguishable by class name

### Unit tests to add

Add these to whatever test file is appropriate for the new module.

```python
import pytest
from database.sql_validation_errors import SQLValidationError


class TestSQLValidationError:

    def test_constructs_with_all_fields(self):
        exc = SQLValidationError(
            dialect="dremio",
            tool_name="run_dremio_query",
            errors=[{"error_type": "limit", "error_details": "No LIMIT given"}],
            sql_query="SELECT id FROM customers",
        )
        assert exc.dialect == "dremio"
        assert exc.tool_name == "run_dremio_query"
        assert exc.errors == [{"error_type": "limit", "error_details": "No LIMIT given"}]
        assert exc.sql_query == "SELECT id FROM customers"

    def test_sql_query_is_optional(self):
        exc = SQLValidationError(
            dialect="dremio",
            tool_name="run_dremio_query",
            errors=[{"error_type": "operator", "error_details": "DROP"}],
        )
        assert exc.sql_query is None

    def test_str_contains_tool_name_and_dialect(self):
        exc = SQLValidationError(
            dialect="tsql",
            tool_name="sql_execute_query",
            errors=[{"error_type": "operator", "error_details": "DELETE"}],
        )
        message = str(exc)
        assert "sql_execute_query" in message
        assert "tsql" in message
        assert "DELETE" in message
        assert "operator" in message

    def test_str_with_multiple_errors(self):
        exc = SQLValidationError(
            dialect="dremio",
            tool_name="run_dremio_query",
            errors=[
                {"error_type": "limit", "error_details": "No LIMIT given"},
                {"error_type": "operator", "error_details": "DROP"},
            ],
        )
        message = str(exc)
        assert "No LIMIT given" in message
        assert "DROP" in message
        assert ";" in message  # findings joined by semicolons

    def test_str_with_empty_errors_list(self):
        exc = SQLValidationError(
            dialect="dremio",
            tool_name="run_dremio_query",
            errors=[],
        )
        message = str(exc)
        assert "run_dremio_query" in message
        assert "dremio" in message

    def test_is_catchable_as_exception(self):
        with pytest.raises(Exception):
            raise SQLValidationError(
                dialect="dremio",
                tool_name="run_dremio_query",
                errors=[{"error_type": "operator", "error_details": "DROP"}],
            )

    def test_is_catchable_as_sql_validation_error(self):
        with pytest.raises(SQLValidationError) as exc_info:
            raise SQLValidationError(
                dialect="dremio",
                tool_name="run_dremio_query",
                errors=[{"error_type": "operator", "error_details": "DROP"}],
            )
        assert exc_info.value.dialect == "dremio"
```

### Verification checklist (PR 4 specific)

- [ ] `from database.sql_validation_errors import SQLValidationError` works from both execute tool files
- [ ] Constructing `SQLValidationError(...)` with the four fields produces a usable exception
- [ ] `str(exc)` returns a readable single-line message containing tool name, dialect, and findings
- [ ] Raising `SQLValidationError` from a wrapped execute tool still triggers the self-healing loop in a Codeless Agent graph
- [ ] Log lines before and after PR 4 look roughly the same (only the class name in the underlying Python traceback changes)
- [ ] The orchestration UI renders the error readably — at minimum as well as PR 3's `ValueError`, ideally distinguishable

### Out of scope for PR 4

- **No streaming events.** PR 4 does not touch any SSE, stream-writer, or event-formatter code. Exception propagation is the only mechanism used.
- **No tool-node wrapper changes.** If the orchestration platform needs special handling for `SQLValidationError` at the tool-node level, that is a separate ticket filed against whoever owns that layer.
- **No changes to the existing connection-error class.** The two exception classes are siblings, not related by inheritance or shared code.
- **No retry logic.** The tool raises once; the self-healing loop decides whether to retry.
- **No severity levels.** All findings from the guardrail are treated as blocking. A future PR can introduce severity if authors need to distinguish "critical" from "advisory" findings per tool.
- **No cherry-picking of blocking error types.** All guardrail findings block execution. A future PR can add a `blocking_errors` parameterOverride if teams need per-tool control over which error types raise vs. log.

### Dependencies

- PR 1 (refactor) must be merged
- PR 2 (`validate_guardrail()`) must be merged
- PR 3 (execute tool wraps) must be merged
- The in-house connection-error pattern (whatever module holds it) must still exist and still be rendered distinctly in the UI — if that pattern changes, PR 4's approach should be revisited to match

### Recommendation on timing

Ship PR 2 and PR 3 first. Watch how the `ValueError` renders in a real orchestration run with a bad query. Compare it to how the existing connection-error class renders. If the `ValueError` already looks fine and is clearly distinguishable from other failure modes, PR 4 is optional polish and can be deprioritized or dropped. If the distinction matters — for logs, for the UI, for downstream pattern-matching — do PR 4 using the code above.
