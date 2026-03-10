"""
SQL Validation Tool for VAAF

Validates SQL queries using SQLGlot AST-based parsing. The user picks a SQL
dialect in the UI and sets limit / max_columns — everything else (date format,
table patterns, limit style, restricted ops) is derived from the dialect.

Design-time injected args (parameterOverrides on the tool node):
    - dialect, required_limit, max_columns, table_schema, table_name_pattern (optional override)

LLM-visible args:
    - sql_query
"""

import json
import sqlglot
import re
from sqlglot import exp
from sqlglot.optimizer import qualify
from sqlglot.errors import ParseError
from dataclasses import dataclass, asdict
from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import tool, InjectedToolArg


# ---------------------------------------------------------------------------
# Dialect configuration — one selection drives all validation behaviour
# ---------------------------------------------------------------------------

@dataclass
class DialectConfig:
    """All validation settings derived from a single dialect choice."""
    sqlglot_dialect: str
    date_pattern: str
    date_format_label: str          # friendly label for error messages
    limit_style: str                # "LIMIT" | "TOP" | "FETCH" | "ROWNUM"
    default_table_pattern: str      # default regex for table name normalization
    restricted_ops: frozenset       # SQL operations to block


_DEFAULT_RESTRICTED = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "MERGE", "GRANT", "CREATE",
})

DIALECT_CONFIGS: Dict[str, DialectConfig] = {
    "dremio": DialectConfig(
        sqlglot_dialect="dremio",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"iCFO AI Workstream.?.ILMS?.BALANCE_EVENT",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "tsql": DialectConfig(
        sqlglot_dialect="tsql",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="TOP",
        default_table_pattern=r"dbo\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "bigquery": DialectConfig(
        sqlglot_dialect="bigquery",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "postgres": DialectConfig(
        sqlglot_dialect="postgres",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "mysql": DialectConfig(
        sqlglot_dialect="mysql",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "snowflake": DialectConfig(
        sqlglot_dialect="snowflake",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "spark": DialectConfig(
        sqlglot_dialect="spark",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "databricks": DialectConfig(
        sqlglot_dialect="databricks",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "redshift": DialectConfig(
        sqlglot_dialect="redshift",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "oracle": DialectConfig(
        sqlglot_dialect="oracle",
        date_pattern=r"\d{2}-[A-Z]{3}-\d{4}",
        date_format_label="DD-MON-YYYY",
        limit_style="ROWNUM",
        default_table_pattern=r"\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "sqlite": DialectConfig(
        sqlglot_dialect="sqlite",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "trino": DialectConfig(
        sqlglot_dialect="trino",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "presto": DialectConfig(
        sqlglot_dialect="presto",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "hive": DialectConfig(
        sqlglot_dialect="hive",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
    "clickhouse": DialectConfig(
        sqlglot_dialect="clickhouse",
        date_pattern=r"\d{4}-\d{2}-\d{2}",
        date_format_label="YYYY-MM-DD",
        limit_style="LIMIT",
        default_table_pattern=r"\w+\.\w+",
        restricted_ops=_DEFAULT_RESTRICTED,
    ),
}


def get_dialect_config(dialect: str) -> DialectConfig:
    """Look up dialect config by name. Raises ValueError if unsupported."""
    key = dialect.strip().lower()
    if key not in DIALECT_CONFIGS:
        raise ValueError(
            f"Unsupported SQL dialect: '{dialect}'. "
            f"Supported: {', '.join(sorted(DIALECT_CONFIGS.keys()))}"
        )
    return DIALECT_CONFIGS[key]


def get_supported_dialects() -> List[str]:
    """Return list of supported dialect names — useful for UI dropdowns."""
    return sorted(DIALECT_CONFIGS.keys())


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """Result of a single validation check."""
    error_present: bool
    error_details: str
    error_type: str


# ---------------------------------------------------------------------------
# Core validation class
# ---------------------------------------------------------------------------

class Validation:
    """
    AST-based SQL validation using SQLGlot.

    All dialect-specific behaviour is driven by DialectConfig — the caller
    only needs to specify the dialect name and any overrides (limit, max_columns).
    """

    def __init__(
        self,
        raw_sql_string: str,
        dialect: str = "dremio",
        schema: Optional[dict] = None,
        required_limit: int = 5,
        max_columns: Optional[int] = None,
        table_name_pattern: Optional[str] = None,
    ):
        self.raw_sql_string = raw_sql_string
        self.schema = schema or {}
        self.required_limit = required_limit
        self.max_columns = max_columns

        # Resolve all config from dialect
        self.config = get_dialect_config(dialect)
        self.sql_type = self.config.sqlglot_dialect
        self.date_pattern = self.config.date_pattern
        self.table_name = table_name_pattern or self.config.default_table_pattern

        self.ast: Optional[exp.Expression] = None
        self.sql_normalized: Optional[str] = None

    def validate(self) -> List[ValidationResult]:
        """Run all validation checks and return results."""
        errors: List[ValidationResult] = []

        parse_error = self._parse_sql()
        if parse_error:
            errors.append(parse_error)
            return errors

        checks = [
            self._find_invalid_fields,
            self._check_placeholder_dates,
            self._detect_select_star_final_only,
            self._check_max_columns,
            self._check_limit,
            self._detect_restricted_ops,
        ]

        for check in checks:
            result = check()
            if result is not None and result.error_present:
                errors.append(result)

        if not errors:
            errors.append(
                ValidationResult(
                    error_present=False,
                    error_details="",
                    error_type="no errors",
                )
            )

        return errors

    # ---- Internal checks --------------------------------------------------

    def _parse_sql(self) -> Optional[ValidationResult]:
        try:
            self.sql_normalized = re.sub(
                self.table_name,
                "PLACEHOLDER_SCHEMA.TABLE",
                self.raw_sql_string,
            )
            self.ast = sqlglot.parse_one(self.sql_normalized, read=self.sql_type)
            return None
        except ParseError as e:
            return ValidationResult(True, str(e), "syntax")
        except Exception as e:
            return ValidationResult(True, str(e), "syntax")

    def _find_invalid_fields(self) -> ValidationResult:
        """Check for fields not present in the provided schema."""
        if not self.schema:
            return ValidationResult(False, "", "fake field")

        try:
            tree = sqlglot.parse_one(self.sql_normalized, read=self.sql_type)
            qualify.qualify(tree, schema=self.schema)
        except Exception as e:
            return ValidationResult(
                True,
                f"Remove hallucinated field not in schema: {str(e)}",
                "fake field",
            )

        return ValidationResult(False, "", "fake field")

    def _detect_select_star_final_only(self) -> ValidationResult:
        """Detect SELECT * in the outermost SELECT."""
        has_star = False

        try:
            for select_node in self.ast.find_all(exp.Select):
                if select_node.parent_select is not None:
                    continue
                for node in select_node.expressions:
                    if isinstance(node, exp.Star):
                        has_star = True
                        break
        except Exception:
            pass

        return ValidationResult(
            has_star,
            "SELECT * found in the main statement" if has_star else "",
            "star",
        )

    def _check_max_columns(self) -> Optional[ValidationResult]:
        """Check that the outermost SELECT does not exceed max_columns."""
        if self.max_columns is None:
            return None

        try:
            for select_node in self.ast.find_all(exp.Select):
                if select_node.parent_select is not None:
                    continue

                # If SELECT *, we can't count — skip (caught by star check)
                has_star = any(isinstance(e, exp.Star) for e in select_node.expressions)
                if has_star:
                    return None

                col_count = len(select_node.expressions)

                if col_count > self.max_columns:
                    return ValidationResult(
                        True,
                        f"Outermost SELECT has {col_count} columns, "
                        f"exceeds maximum of {self.max_columns}",
                        "max columns",
                    )
        except Exception:
            pass

        return ValidationResult(False, "", "max columns")

    def _check_placeholder_dates(self) -> ValidationResult:
        """Check for invalid date literal formats based on dialect."""
        lst = []
        date_re = re.compile(self.date_pattern)

        for literal in self.ast.find_all(exp.Literal):
            parent = literal.parent
            if isinstance(parent, exp.Cast) and isinstance(
                parent.args.get("to"), exp.DataType
            ):
                if parent.args["to"].this == exp.DataType.Type.DATE:
                    if not date_re.fullmatch(literal.this):
                        lst.append(
                            f"{literal.this} is not a valid "
                            f"{self.config.date_format_label} date pattern."
                        )

        error_present = len(lst) > 0
        return ValidationResult(
            error_present,
            "\n".join(sorted(set(lst))),
            "placeholder date",
        )

    def _check_limit(self) -> ValidationResult:
        """Check row-limiting clause exists and value is within bound.

        Handles LIMIT, TOP, FETCH FIRST, and ROWNUM depending on dialect.
        """
        limit_style = self.config.limit_style

        # --- LIMIT (most dialects) ---
        if limit_style == "LIMIT":
            limit_expr = self.ast.find(exp.Limit)
            if limit_expr is None:
                return ValidationResult(
                    True,
                    f"No LIMIT given (add LIMIT {self.required_limit})",
                    "limit",
                )
            try:
                value = int(limit_expr.expression.name)
            except (ValueError, AttributeError) as e:
                return ValidationResult(True, f"Invalid LIMIT value: {e}", "limit")

            if value > self.required_limit:
                return ValidationResult(
                    True,
                    f"LIMIT {value} exceeds maximum of {self.required_limit}",
                    "limit",
                )
            return ValidationResult(False, "", "limit")

        # --- TOP (T-SQL) ---
        if limit_style == "TOP":
            # SQLGlot parses SELECT TOP N as a Limit node in the AST for tsql
            limit_expr = self.ast.find(exp.Limit)
            if limit_expr is None:
                return ValidationResult(
                    True,
                    f"No TOP clause given (add TOP {self.required_limit})",
                    "limit",
                )
            try:
                value = int(limit_expr.expression.name)
            except (ValueError, AttributeError) as e:
                return ValidationResult(True, f"Invalid TOP value: {e}", "limit")

            if value > self.required_limit:
                return ValidationResult(
                    True,
                    f"TOP {value} exceeds maximum of {self.required_limit}",
                    "limit",
                )
            return ValidationResult(False, "", "limit")

        # --- FETCH FIRST / ROWNUM (Oracle, DB2) ---
        if limit_style in ("FETCH", "ROWNUM"):
            limit_expr = self.ast.find(exp.Limit)
            fetch_expr = self.ast.find(exp.Fetch)

            found_expr = limit_expr or fetch_expr
            if found_expr is None:
                return ValidationResult(
                    True,
                    f"No row limit clause found (add FETCH FIRST {self.required_limit} ROWS ONLY "
                    f"or WHERE ROWNUM <= {self.required_limit})",
                    "limit",
                )
            try:
                value = int(found_expr.expression.name)
            except (ValueError, AttributeError) as e:
                return ValidationResult(True, f"Invalid row limit value: {e}", "limit")

            if value > self.required_limit:
                return ValidationResult(
                    True,
                    f"Row limit {value} exceeds maximum of {self.required_limit}",
                    "limit",
                )
            return ValidationResult(False, "", "limit")

        # Fallback
        return ValidationResult(False, "", "limit")

    def _detect_restricted_ops(self) -> ValidationResult:
        """Detect restricted SQL operations."""
        restricted = self.config.restricted_ops

        found = set()
        for node in self.ast.walk():
            if isinstance(
                node,
                (
                    exp.Insert, exp.Update, exp.Delete,
                    exp.Drop, exp.Alter, exp.Merge,
                    exp.Grant, exp.Create,
                ),
            ):
                found.add(node.key.upper())

        found = sorted(found & restricted)

        if found:
            return ValidationResult(True, ", ".join(found), "operator")

        return ValidationResult(False, "", "operator")


# ---------------------------------------------------------------------------
# VAAF Tool wrapper
# ---------------------------------------------------------------------------

@tool
async def sql_validate(
    sql_query: str,
    # --- Injected at design-time via parameterOverrides on the tool node ---
    # UI shows: dialect dropdown, limit number input, max_columns number input
    # Everything else (date format, limit style, etc.) is derived from dialect
    dialect: Annotated[str, InjectedToolArg] = "dremio",
    required_limit: Annotated[int, InjectedToolArg] = 5,
    max_columns: Annotated[int, InjectedToolArg] = None,
    table_schema: Annotated[dict, InjectedToolArg] = None,
    table_name_pattern: Annotated[str, InjectedToolArg] = None,
) -> str:
    """Validate a SQL query for syntax errors, hallucinated fields, SELECT *,
    excessive columns, missing LIMIT/TOP, restricted operations, and invalid
    date formats. All validation rules are derived from the SQL dialect.

    Args:
        sql_query: The raw SQL query string to validate.

    Returns:
        JSON string with validation results including any errors found.
    """
    validator = Validation(
        raw_sql_string=sql_query,
        dialect=dialect,
        schema=table_schema,
        required_limit=required_limit,
        max_columns=max_columns,
        table_name_pattern=table_name_pattern,
    )

    results = validator.validate()

    config = get_dialect_config(dialect)
    output = {
        "valid": not any(r.error_present for r in results),
        "errors": [asdict(r) for r in results if r.error_present],
        "dialect": dialect,
        "settings": {
            "limit_style": config.limit_style,
            "date_format": config.date_format_label,
            "required_limit": required_limit,
            "max_columns": max_columns,
        },
    }

    return json.dumps(output, indent=2)