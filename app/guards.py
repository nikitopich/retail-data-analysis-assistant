"""Deterministic SQL guards (spec §5.1, §4.1). No LLM involved.

These run BEFORE any execution and are the first of the two defence lines for
destructive ops (the second being owner-scoping in the repository).
"""
from __future__ import annotations

import re
from typing import Tuple

# Keywords that must never appear in an analytical (SELECT-only) query.
# NOTE: REPLACE is intentionally omitted (it is also a BigQuery string function).
_FORBIDDEN_IN_SELECT = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "MERGE", "GRANT", "REVOKE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
]

_FORBIDDEN_IN_DML = [
    "DROP", "ALTER", "TRUNCATE", "CREATE", "ATTACH", "DETACH", "PRAGMA",
    "VACUUM", "INSERT", "MERGE",
]


def _normalize(sql: str) -> str:
    """Strip surrounding whitespace and trailing semicolons."""
    return (sql or "").strip().rstrip(";").rstrip()


def _has_comment(sql: str) -> bool:
    return "--" in sql or "/*" in sql


def select_only_guard(sql: str) -> Tuple[bool, str]:
    """Allow exactly one SELECT (or WITH ... SELECT). Reject everything else."""
    if not sql or not sql.strip():
        return False, "empty query"
    s = _normalize(sql)
    if ";" in s:
        return False, "multiple statements are not allowed"
    if _has_comment(s):
        return False, "SQL comments are not allowed"
    upper = s.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, "only a single SELECT (or WITH ... SELECT) is allowed"
    for kw in _FORBIDDEN_IN_SELECT:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"forbidden keyword: {kw}"
    return True, ""


def preview_guard(sql: str) -> Tuple[bool, str]:
    """A destructive-preview must be a SELECT over saved_reports."""
    ok, reason = select_only_guard(sql)
    if not ok:
        return ok, reason
    if not re.search(r"\bsaved_reports\b", sql, re.IGNORECASE):
        return False, "preview must select from saved_reports"
    return True, ""


def dml_guard(sql: str) -> Tuple[bool, str]:
    """Allow exactly one DELETE/UPDATE on saved_reports. Reject everything else."""
    if not sql or not sql.strip():
        return False, "empty statement"
    s = _normalize(sql)
    if ";" in s:
        return False, "multiple statements are not allowed"
    if _has_comment(s):
        return False, "SQL comments are not allowed"
    upper = s.upper()
    parts = upper.split()
    verb = parts[0] if parts else ""
    if verb not in ("DELETE", "UPDATE"):
        return False, f"only DELETE/UPDATE allowed (got '{verb or '?'}')"
    if verb == "DELETE":
        if not re.match(r"DELETE\s+FROM\s+SAVED_REPORTS\b", upper):
            return False, "DELETE must target saved_reports"
    else:  # UPDATE
        if not re.match(r"UPDATE\s+SAVED_REPORTS\b", upper):
            return False, "UPDATE must target saved_reports"
    for kw in _FORBIDDEN_IN_DML:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"forbidden keyword: {kw}"
    return True, ""
