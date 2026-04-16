# SQL Validation — Unit Testing Guide

A detailed guide for writing high-quality unit tests for the SQL validation modules after the refactor. Covers test organization, what to test in each module, fixture patterns, common pitfalls, and copy-pasteable starter tests.

---

## Module map and test priority

After the refactor, the `database/` folder contains these modules:

| Module | Contains | Test priority |
|---|---|---|
| `sql_dialects.py` | `DialectConfig`, `DIALECT_CONFIGS`, `get_dialect_config`, `get_supported_dialects` | **Medium** — small surface, easy wins |
| `sql_validation_result.py` | `ValidationResult` dataclass | **Low** — trivial dataclass, minimal testing |
| `sql_validator.py` | `Validation` class with all checks, plus `guard_rails()` method | **High** — this is where real logic lives |
| `sql_validation_tool.py` | `@tool sql_validate` LangChain tool | **Medium** — thin wrapper around `Validation` |
| `sql_validation.py` | Re-export shim for backward compat | **Very low** — one smoke test |
| `sql_validation_v1.py` | Legacy module (may be removed) | **None** — delete before opening PR |
| `sql_validation_errors.py` | `SQLValidationError` exception class | **Medium** — small but worth testing fields and `__str__` |

**Rule of thumb:** test the logic, not the import system. The shim has nothing to test beyond "re-exports didn't accidentally get removed." The real work lives in `sql_validator.py` and that's where ~70% of your test code should sit.

---

## Test directory structure

Mirror the module structure. If your codebase uses a `tests/` directory at the project root, create:

```
tests/
└── tools/
    └── database/
        ├── __init__.py
        ├── conftest.py                      # shared fixtures
        ├── test_sql_dialects.py
        ├── test_sql_validator.py            # the big one
        ├── test_sql_validation_tool.py
        ├── test_sql_validation_errors.py
        └── test_sql_validation_shim.py      # one smoke test
```

If your project follows a different convention (tests next to modules, tests in a top-level `test/` folder, etc.), follow the existing convention. Consistency with the codebase matters more than purity.

---

## Test framework assumptions

This guide assumes **pytest** because it's the most common Python test framework and has the cleanest syntax for the kinds of tests you'll be writing. If your codebase uses `unittest`, the structure is the same but the syntax changes (classes inheriting from `TestCase`, `self.assertEqual` instead of `assert`, etc.). Port the examples as needed — the test cases themselves translate directly.

Install pytest if it's not already in the project:

```bash
pip install pytest pytest-asyncio
```

`pytest-asyncio` is needed because the `sql_validate` tool is an async function.

---

## Shared fixtures (`conftest.py`)

Put shared fixtures in `conftest.py` at the test directory level. These get automatically discovered by pytest and are available to every test file in the same directory.

```python
# tests/tools/database/conftest.py
import pytest
from src.tools.database.sql_validator import Validation


@pytest.fixture
def make_validator():
    """Factory fixture for building a Validation instance with sensible defaults.

    Usage in tests:
        def test_something(make_validator):
            v = make_validator("SELECT id FROM foo LIMIT 10")
            # or with overrides:
            v = make_validator("SELECT id FROM foo", dialect="tsql", required_limit=50)
    """
    def _build(
        sql: str,
        dialect: str = "dremio",
        schema=None,
        required_limit: int = 100,
        max_columns: int = 5,
        table_name_pattern=None,
        place_holder_table=None,
    ):
        return Validation(
            raw_sql_string=sql,
            dialect=dialect,
            schema=schema,
            required_limit=required_limit,
            max_columns=max_columns,
            table_name_pattern=table_name_pattern,
            place_holder_table=place_holder_table,
        )
    return _build


@pytest.fixture
def sample_schema():
    """A minimal schema for testing _find_invalid_fields without production data."""
    # NOTE: adjust this to whatever format _build_schema expects (list of INFORMATION_SCHEMA-style dicts)
    return str([
        {"COLUMN_NAME": "id", "DATA_TYPE": "int", "CHARACTER_MAXIMUM_LENGTH": None},
        {"COLUMN_NAME": "name", "DATA_TYPE": "varchar", "CHARACTER_MAXIMUM_LENGTH": 100},
        {"COLUMN_NAME": "created_at", "DATA_TYPE": "date", "CHARACTER_MAXIMUM_LENGTH": None},
    ])
```

**Why the factory pattern:** the `Validation` constructor takes 7 arguments. If every test builds it from scratch, you get 7 lines of boilerplate per test. The factory collapses that to one line with sensible defaults that each test can override selectively.

---

## `test_sql_dialects.py` — dialect config tests

Small file, quick wins. Tests the lookup helpers and the shape of each dialect entry.

```python
# tests/tools/database/test_sql_dialects.py
import pytest
from src.tools.database.sql_dialects import (
    DialectConfig,
    DIALECT_CONFIGS,
    get_dialect_config,
    get_supported_dialects,
)


class TestGetDialectConfig:

    def test_returns_config_for_supported_dialect(self):
        config = get_dialect_config("dremio")
        assert isinstance(config, DialectConfig)
        assert config.sqlglot_dialect == "dremio"

    def test_is_case_insensitive(self):
        assert get_dialect_config("DREMIO").sqlglot_dialect == "dremio"
        assert get_dialect_config("Dremio").sqlglot_dialect == "dremio"

    def test_strips_whitespace(self):
        assert get_dialect_config("  dremio  ").sqlglot_dialect == "dremio"

    def test_raises_value_error_for_unsupported_dialect(self):
        with pytest.raises(ValueError) as exc_info:
            get_dialect_config("not_a_real_dialect")
        assert "Unsupported SQL dialect" in str(exc_info.value)

    def test_error_message_lists_supported_dialects(self):
        with pytest.raises(ValueError) as exc_info:
            get_dialect_config("bogus")
        error_msg = str(exc_info.value)
        # Every supported dialect should appear in the error message
        for dialect in get_supported_dialects():
            assert dialect in error_msg


class TestGetSupportedDialects:

    def test_returns_sorted_list(self):
        dialects = get_supported_dialects()
        assert dialects == sorted(dialects)

    def test_contains_all_expected_dialects(self):
        dialects = set(get_supported_dialects())
        assert dialects == {"dremio", "tsql", "mysql", "spark", "oracle"}


class TestDialectConfigShape:
    """Verify every dialect config has the required fields populated.

    Catches typos and missing fields in DIALECT_CONFIGS at test time
    rather than at runtime when someone tries to use the new dialect.
    """

    @pytest.mark.parametrize("dialect_name", list(DIALECT_CONFIGS.keys()))
    def test_every_dialect_has_required_fields(self, dialect_name):
        config = DIALECT_CONFIGS[dialect_name]
        assert config.sqlglot_dialect
        assert config.date_pattern
        assert config.date_format_label
        assert config.date_strptime_formats
        assert config.limit_style in ("LIMIT", "TOP", "FETCH", "ROWNUM")
        assert config.default_table_pattern
        assert config.restricted_ops
        # date_functions can be empty but must be a frozenset
        assert isinstance(config.date_functions, frozenset)
```

**What's good about these tests:**

- The `@pytest.mark.parametrize` runs the shape test once per dialect — adding a new dialect automatically gets tested.
- Tests are small, fast, and catch config-level bugs before they cause runtime errors.
- Nothing here requires a real database connection or SQLGlot parsing.

---

## `test_sql_validator.py` — the big one

This is where 70% of your test effort goes. The `Validation` class has many checks; each one needs coverage.

### Organize by check

One test class per check. Each class covers happy path + every way that check can fail.

```python
# tests/tools/database/test_sql_validator.py
import pytest
from src.tools.database.sql_validator import Validation
from src.tools.database.sql_validation_result import ValidationResult


class TestParseSQL:
    """Covers _parse_sql via the validate() entry point."""

    def test_valid_sql_parses_cleanly(self, make_validator):
        v = make_validator("SELECT id FROM customers LIMIT 10")
        results = v.validate()
        assert not any(r.error_type == "syntax" for r in results if r.error_present)

    def test_invalid_sql_returns_syntax_error(self, make_validator):
        v = make_validator("SELEKT bad FROM nowhere")
        results = v.validate()
        syntax_errors = [r for r in results if r.error_present and r.error_type == "syntax"]
        assert len(syntax_errors) == 1

    def test_parse_error_short_circuits_other_checks(self, make_validator):
        v = make_validator("completely broken sql")
        results = v.validate()
        # Only the syntax error should be returned — no limit/operator/star checks
        error_results = [r for r in results if r.error_present]
        assert len(error_results) == 1
        assert error_results[0].error_type == "syntax"

    def test_empty_string_produces_syntax_error(self, make_validator):
        v = make_validator("")
        results = v.validate()
        assert any(r.error_present and r.error_type == "syntax" for r in results)


class TestDetectRestrictedOps:

    @pytest.mark.parametrize("dangerous_sql,expected_op", [
        ("DROP TABLE customers", "DROP"),
        ("DELETE FROM customers WHERE id = 1", "DELETE"),
        ("UPDATE customers SET name = 'x' WHERE id = 1", "UPDATE"),
        ("INSERT INTO customers VALUES (1, 'x')", "INSERT"),
        ("ALTER TABLE customers ADD COLUMN age INT", "ALTER"),
    ])
    def test_restricted_ops_are_detected(self, make_validator, dangerous_sql, expected_op):
        v = make_validator(dangerous_sql)
        results = v.validate()
        operator_errors = [r for r in results if r.error_present and r.error_type == "operator"]
        assert len(operator_errors) == 1
        assert expected_op in operator_errors[0].error_details

    def test_select_statement_has_no_restricted_ops(self, make_validator):
        v = make_validator("SELECT id FROM customers LIMIT 10")
        results = v.validate()
        assert not any(r.error_present and r.error_type == "operator" for r in results)


class TestCheckLimit:

    def test_missing_limit_is_flagged(self, make_validator):
        v = make_validator("SELECT id FROM customers")
        results = v.validate()
        limit_errors = [r for r in results if r.error_present and r.error_type == "limit"]
        assert len(limit_errors) == 1
        assert "No LIMIT" in limit_errors[0].error_details

    def test_limit_within_bound_passes(self, make_validator):
        v = make_validator("SELECT id FROM customers LIMIT 50", required_limit=100)
        results = v.validate()
        assert not any(r.error_present and r.error_type == "limit" for r in results)

    def test_limit_exceeding_bound_is_flagged(self, make_validator):
        v = make_validator("SELECT id FROM customers LIMIT 500", required_limit=100)
        results = v.validate()
        limit_errors = [r for r in results if r.error_present and r.error_type == "limit"]
        assert len(limit_errors) == 1
        assert "exceeds maximum" in limit_errors[0].error_details

    def test_tsql_requires_top(self, make_validator):
        v = make_validator("SELECT id FROM customers", dialect="tsql")
        results = v.validate()
        limit_errors = [r for r in results if r.error_present and r.error_type == "limit"]
        assert any("TOP" in e.error_details for e in limit_errors)

    def test_tsql_with_top_passes(self, make_validator):
        v = make_validator("SELECT TOP 10 id FROM customers", dialect="tsql")
        results = v.validate()
        assert not any(r.error_present and r.error_type == "limit" for r in results)

    def test_oracle_accepts_fetch_first(self, make_validator):
        v = make_validator(
            "SELECT id FROM customers FETCH FIRST 10 ROWS ONLY",
            dialect="oracle",
            required_limit=100,
        )
        results = v.validate()
        # May produce other findings, but should not complain about missing row limit
        assert not any(r.error_present and r.error_type == "limit" for r in results)

    def test_oracle_accepts_rownum(self, make_validator):
        v = make_validator(
            "SELECT id FROM customers WHERE ROWNUM <= 10",
            dialect="oracle",
            required_limit=100,
        )
        results = v.validate()
        assert not any(r.error_present and r.error_type == "limit" for r in results)


class TestDetectSelectStar:

    def test_select_star_in_outermost_is_flagged(self, make_validator):
        v = make_validator("SELECT * FROM customers LIMIT 10")
        results = v.validate()
        star_errors = [r for r in results if r.error_present and r.error_type == "star"]
        assert len(star_errors) == 1

    def test_select_star_in_subquery_is_allowed(self, make_validator):
        v = make_validator(
            "SELECT id FROM (SELECT * FROM customers) sub LIMIT 10",
        )
        results = v.validate()
        # Inner SELECT * should not trigger — only outermost matters
        assert not any(r.error_present and r.error_type == "star" for r in results)

    def test_explicit_columns_are_not_flagged(self, make_validator):
        v = make_validator("SELECT id, name FROM customers LIMIT 10")
        results = v.validate()
        assert not any(r.error_present and r.error_type == "star" for r in results)


class TestCheckMaxColumns:

    def test_under_limit_passes(self, make_validator):
        v = make_validator("SELECT a, b, c FROM t LIMIT 10", max_columns=5)
        results = v.validate()
        assert not any(r.error_present and r.error_type == "max columns" for r in results)

    def test_over_limit_is_flagged(self, make_validator):
        sql = "SELECT a, b, c, d, e, f FROM t LIMIT 10"
        v = make_validator(sql, max_columns=3)
        results = v.validate()
        col_errors = [r for r in results if r.error_present and r.error_type == "max columns"]
        assert len(col_errors) == 1


class TestGuardRails:
    """Covers the guard_rails() method on Validation.

    guard_rails takes a tuple of error_types to treat as blocking and returns
    the subset of validation findings whose error_type is in that set.
    """

    def test_returns_empty_on_clean_query(self, make_validator):
        v = make_validator("SELECT id FROM customers LIMIT 10")
        blocking = v.guard_rails(("syntax", "operator"))
        assert blocking == []

    def test_returns_findings_in_blocking_set(self, make_validator):
        v = make_validator("DROP TABLE customers")
        blocking = v.guard_rails(("syntax", "operator"))
        assert len(blocking) == 1
        assert blocking[0].error_type == "operator"

    def test_filters_out_findings_outside_blocking_set(self, make_validator):
        # SELECT * triggers a "star" finding, but if we only block on syntax+operator,
        # it should not be returned.
        v = make_validator("SELECT * FROM customers LIMIT 10")
        blocking = v.guard_rails(("syntax", "operator"))
        assert blocking == []

    def test_multiple_blocking_findings_all_returned(self, make_validator):
        # Query with both a restricted op and a missing limit
        v = make_validator("DELETE FROM customers")
        blocking = v.guard_rails(("syntax", "operator", "limit"))
        error_types = {r.error_type for r in blocking}
        assert "operator" in error_types

    def test_empty_blocking_set_returns_empty(self, make_validator):
        # If no error types are considered blocking, nothing is returned
        v = make_validator("DROP TABLE customers")
        blocking = v.guard_rails(())
        assert blocking == []
```

**Patterns worth noticing:**

- **Every test imports from the new module paths** (`src.tools.database.sql_validator`), not the shim. Tests that go through the shim don't exercise the real module.
- **Each test isolates one assertion.** Don't pile five assertions into one test — when it fails you won't know which one broke.
- **Parametrize aggressively.** The restricted-ops test runs five times with different inputs. Adding a sixth is one line.
- **Test the "not flagged" case too.** Every "X raises an error" test should have a corresponding "valid X does not raise" test. Otherwise your test could pass because the check is broken and returns errors for everything.
- **Dialect-specific behavior gets its own test.** TOP for T-SQL, ROWNUM for Oracle, FETCH FIRST for Oracle — each is tested explicitly because they hit different code paths.

### What to skip for now

Writing exhaustive tests for `_check_placeholder_dates` and `_find_invalid_fields` is a larger effort because:

- `_check_placeholder_dates` walks the AST looking for date literals in specific node types; testing all dialect-specific date functions is a lot of test cases
- `_find_invalid_fields` requires a schema fixture and SQLGlot's `qualify` to work, which adds dependencies

Ship the guardrail-path tests first (syntax, operator, limit, max_columns, star), then add the other two checks in a follow-up testing PR.

---

## `test_sql_validation_errors.py` — exception class tests

Small file, focused on construction and string formatting.

```python
# tests/tools/database/test_sql_validation_errors.py
import pytest
from src.tools.database.sql_validation_errors import SQLValidationError


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

    def test_str_contains_tool_name_dialect_and_finding(self):
        exc = SQLValidationError(
            dialect="tsql",
            tool_name="sql_execute_query",
            errors=[{"error_type": "operator", "error_details": "DELETE"}],
        )
        message = str(exc)
        assert "sql_execute_query" in message
        assert "tsql" in message
        assert "DELETE" in message

    def test_str_with_multiple_errors_uses_semicolons(self):
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
        assert ";" in message

    def test_str_with_empty_errors_returns_fallback(self):
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

    def test_is_catchable_as_sql_validation_error_specifically(self):
        with pytest.raises(SQLValidationError) as exc_info:
            raise SQLValidationError(
                dialect="dremio",
                tool_name="run_dremio_query",
                errors=[{"error_type": "operator", "error_details": "DROP"}],
            )
        assert exc_info.value.dialect == "dremio"

    def test_missing_error_type_key_handled_gracefully(self):
        """The _format method uses .get() with defaults — make sure a malformed
        error dict doesn't crash the exception."""
        exc = SQLValidationError(
            dialect="dremio",
            tool_name="run_dremio_query",
            errors=[{"error_details": "something happened"}],  # no error_type key
        )
        # Should not raise when str() is called
        message = str(exc)
        assert "unknown" in message or "something happened" in message
```

---

## `test_sql_validation_tool.py` — async tool tests

The `sql_validate` tool is decorated with `@tool` and is async. Testing it requires `pytest-asyncio`.

```python
# tests/tools/database/test_sql_validation_tool.py
import pytest
from src.tools.database.sql_validation_tool import sql_validate


@pytest.mark.asyncio
async def test_valid_query_returns_not_valid_false():
    result = await sql_validate.ainvoke({
        "sql_query": "SELECT id FROM customers LIMIT 10",
        "dialect": "dremio",
        "required_limit": 100,
    })
    assert result["not_valid"] is False
    assert result["errors"] == []
    assert result["dialect"] == "dremio"


@pytest.mark.asyncio
async def test_invalid_query_returns_errors():
    result = await sql_validate.ainvoke({
        "sql_query": "DROP TABLE customers",
        "dialect": "dremio",
    })
    assert result["not_valid"] is True
    assert len(result["errors"]) >= 1


@pytest.mark.asyncio
async def test_unwraps_dict_input():
    """The tool has literal_eval logic to unwrap dict-as-string inputs."""
    # Simulate upstream state handing a dict-as-string
    dict_as_string = str({"query": "SELECT id FROM customers LIMIT 10"})
    result = await sql_validate.ainvoke({
        "sql_query": dict_as_string,
        "dialect": "dremio",
        "pydantic_variable": "query",
    })
    # Should validate the inner SQL, not the wrapped string
    assert result["not_valid"] is False


@pytest.mark.asyncio
async def test_empty_schema_string_treated_as_none():
    """Empty string schema should be treated as None (from the tool wrapper)."""
    result = await sql_validate.ainvoke({
        "sql_query": "SELECT id FROM customers LIMIT 10",
        "dialect": "dremio",
        "sql_schema": "",
    })
    # Should not blow up on the empty-string schema
    assert "errors" in result
```

**Things to note about async testing:**

- Use `@pytest.mark.asyncio` on every async test function
- Call the tool via `.ainvoke(...)` rather than calling it directly — that's how LangChain tools are invoked programmatically
- If you get `RuntimeError: There is no current event loop`, add `asyncio_mode = "auto"` to your `pytest.ini` or `pyproject.toml`

---

## `test_sql_validation_shim.py` — one smoke test

All the shim does is re-export. The only thing worth testing is that the re-exports still work.

```python
# tests/tools/database/test_sql_validation_shim.py

def test_shim_reexports_are_intact():
    """Guard against accidental removal of backward-compat re-exports.

    If anyone removes an export from sql_validation.py without updating
    callers, this test fails.
    """
    from src.tools.database.sql_validation import (
        DialectConfig,
        DIALECT_CONFIGS,
        get_dialect_config,
        get_supported_dialects,
        ValidationResult,
        Validation,
        sql_validate,
    )

    assert DialectConfig is not None
    assert isinstance(DIALECT_CONFIGS, dict)
    assert callable(get_dialect_config)
    assert callable(get_supported_dialects)
    assert ValidationResult is not None
    assert Validation is not None
    assert sql_validate is not None
```

That's the whole file. Ten lines.

---

## What about `sql_validation_result.py`?

`ValidationResult` is a dataclass with three fields. It's worth one tiny test file just to assert the field names haven't changed — because if someone renames `error_present` to `has_error`, it'll silently break every test that checks `r.error_present`.

```python
# tests/tools/database/test_sql_validation_result.py
from src.tools.database.sql_validation_result import ValidationResult


def test_validation_result_has_expected_fields():
    r = ValidationResult(
        error_present=True,
        error_details="something",
        error_type="syntax",
    )
    assert r.error_present is True
    assert r.error_details == "something"
    assert r.error_type == "syntax"


def test_validation_result_is_comparable():
    r1 = ValidationResult(error_present=False, error_details="", error_type="no errors")
    r2 = ValidationResult(error_present=False, error_details="", error_type="no errors")
    assert r1 == r2
```

Two tests. Done.

---

## Running the tests

From the project root:

```bash
# Run everything in the test directory
pytest tests/tools/database/ -v

# Run just one file
pytest tests/tools/database/test_sql_validator.py -v

# Run just one test class
pytest tests/tools/database/test_sql_validator.py::TestDetectRestrictedOps -v

# Run and show coverage (if pytest-cov is installed)
pytest tests/tools/database/ --cov=src.tools.database --cov-report=term-missing
```

`-v` gives you verbose output so you can see each test's pass/fail. If tests fail, pytest shows a diff between expected and actual.

---

## Coverage target

Don't chase 100% coverage — it's a false goal. Aim for:

- **`sql_dialects.py`:** 100% — it's all config, small surface, trivial to cover fully
- **`sql_validator.py`:** 80%+ on the guardrail-path checks (`_parse_sql`, `_detect_restricted_ops`, `_check_limit`, `_check_max_columns`, `_detect_select_star_final_only`, `guard_rails`). Lower is fine for `_check_placeholder_dates` and `_find_invalid_fields` initially.
- **`sql_validation_errors.py`:** 100% — it's 35 lines.
- **`sql_validation_tool.py`:** 70%+ — happy path + error path + the `literal_eval` unwrap.
- **`sql_validation_result.py`:** 100% — trivial dataclass.
- **`sql_validation.py` (shim):** one test is enough.

If you're at these numbers, you're in a solid place. Higher is better but diminishing returns kick in fast.

---

## Common pitfalls to avoid

1. **Testing the implementation, not the behavior.** Bad: asserting that `_check_limit` calls `self.ast.find(exp.Limit)`. Good: asserting that a query without a LIMIT clause produces a `ValidationResult` with `error_type="limit"`. If you refactor the internals, implementation-coupled tests break unnecessarily.

2. **Shared mutable state between tests.** Don't build a single `Validation` instance at module level and reuse it. Each test should get a fresh one via the fixture. Otherwise state leaks between tests and you get flaky failures.

3. **Over-mocking.** `Validation` doesn't hit any external service. Don't mock SQLGlot. Don't mock the parser. Let it run for real — that's what the tests are actually verifying.

4. **Under-testing edge cases.** Empty string input, None input, whitespace-only input, very long queries, queries with Unicode, queries with SQL comments. Pick a few to cover. "Valid query" is not sufficient testing.

5. **Testing two things in one test.** Each test should fail for exactly one reason. If a test named `test_drop_and_delete_both_blocked` can fail because DROP isn't blocked or because DELETE isn't blocked, you can't tell which. Split it.

6. **Forgetting to test the negative case.** For every "X raises an error" test, write a "Y doesn't raise an error" test. Otherwise your check could return errors for literally everything and still pass all your tests.

7. **Leaving `sql_validation_v1.py` in the repo when opening the PR.** Delete the legacy module before committing. Any tests that reference it should be moved to the new module or removed.

---

## When you finish

Before opening the PR:

1. Run the full test suite: `pytest tests/tools/database/ -v`
2. Make sure every test passes
3. Run with coverage to see which branches are untested
4. Spot-check the uncovered branches — are they intentional skips (like date check and schema check for now), or did you miss something?
5. Delete `sql_validation_v1.py` if you're confident the new code works
6. Delete any test file that references `sql_validation_v1` or imports from it
7. Open the PR with a short description of which modules are tested and what's deliberately skipped for follow-up

---

## Follow-up work (separate PRs)

After this testing PR lands, there are a few things worth coming back to:

1. **Tests for `_check_placeholder_dates`** — complex due to dialect-specific date function handling. Write one test per dialect, covering CAST, DateStrToDate, and each dialect's date functions.
2. **Tests for `_find_invalid_fields`** — requires a real schema fixture and SQLGlot's `qualify` working. Test happy path, hallucinated field, ambiguous column, wildcards.
3. **Integration tests for the wrapped execute tools** — mock the DB, feed a bad query, verify the validation gate fires before any connection attempt. These depend on the test harness your codebase already uses for tool integration tests.
4. **Property-based tests** — use Hypothesis to generate random SQL queries and assert that `validate()` never crashes. Catches edge cases exhaustive unit tests miss.

These aren't required now. The baseline tests in this guide are enough for a PR that ships the validation work with confidence.
