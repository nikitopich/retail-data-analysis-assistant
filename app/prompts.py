"""All prompts are hardcoded here (spec §6). No external prompt management.

The model is always asked to answer in the language of the user's question.
"""
from __future__ import annotations

# --- 6.1 Supervisor (classification) ---
SUPERVISOR_PROMPT = """You are a router for a retail analytics assistant.
Classify the user's message into EXACTLY one label:
- "analytical": a question about the retail data (customers, products, orders, revenue,
  time-based metrics) OR about the database structure (tables/columns).
- "destructive": an intent to delete or modify the user's SAVED REPORTS library
  (e.g. "delete all reports about client X", "delete today's reports", "rename reports ...").
- "other": greetings, off-topic, or unintelligible requests.
Reply with only one word: analytical | destructive | other.

User message: {question}"""


# --- 6.2 SQL generation (analytical) ---
SQL_GEN_PROMPT = """You are a senior data analyst. Generate ONE BigQuery Standard SQL query that answers the
user's question using ONLY these tables (full names required):
`bigquery-public-data.thelook_ecommerce.orders`
`bigquery-public-data.thelook_ecommerce.order_items`
`bigquery-public-data.thelook_ecommerce.products`
`bigquery-public-data.thelook_ecommerce.users`

Schema:
{schema}

Rules:
- Output ONLY the SQL, no prose, no markdown fences.
- A single SELECT statement. Never DML/DDL.
- Add a reasonable LIMIT (e.g. 100) for row-listing queries; do NOT add LIMIT to pure aggregates.
- Revenue = order_items.sale_price (use SUM where appropriate).
- Use only the four tables above.

User question: {question}
{error_hint}"""

# Appended to SQL_GEN_PROMPT during self-correction.
SQL_ERROR_HINT = "\nPrevious SQL failed with: {error}. Fix it and output ONLY the corrected SQL."
SQL_GUARD_HINT = (
    "\nPrevious SQL was rejected by the safety guard ({reason}). "
    "Return a single valid SELECT statement only — no DML, no DDL, no comments, no extra statements."
)


# --- 6.3 SQL empty-result revision ---
SQL_EMPTY_REVISION_PROMPT = """The previous query returned 0 rows. The filters may be too strict or wrong.
Revise the SQL (broaden/fix filters, check date ranges and joins). Output ONLY the SQL.
Question: {question}
Previous SQL: {sql}"""


# --- 6.4 Destructive NL -> SQL (saved_reports) ---
DESTRUCTIVE_PROMPT = """The user wants to delete or modify their SAVED REPORTS. Generate SQLite SQL over the table
`saved_reports(id, owner_id, question, sql_query, report_md, created_at, published_to_golden)`.

Return a JSON object with two fields:
- "preview_sql": a SELECT id, question, created_at FROM saved_reports WHERE <condition>
- "dml_sql": the matching DELETE (or UPDATE) FROM saved_reports WHERE <same condition>

Rules:
- "today" -> date(created_at) = date('now'). Mentions of a client/term X -> report_md LIKE '%X%' (or question LIKE).
- Do NOT add owner filtering (it is enforced by the application).
- Only saved_reports. No DDL. Single statement per field.
- Output ONLY the JSON object, no prose, no markdown fences.

User message: {question}"""

DESTRUCTIVE_JSON_HINT = (
    "\nYour previous output was not a valid JSON object with string fields "
    "'preview_sql' and 'dml_sql'. Return ONLY that JSON object."
)
DESTRUCTIVE_GUARD_HINT = (
    "\nYour previous SQL was rejected ({reason}). The DML must be a single DELETE or UPDATE "
    "on saved_reports only, with no DDL, no comments and no extra statements."
)


# --- 6.5 Report generation ---
REPORT_PROMPT = """You are an analytics assistant for company executives. Write a concise, clear report in
{output_format} format answering the user's question, based ONLY on the query result below.
Do not invent numbers. Reply in the same language as the question.

Question: {question}
SQL result (first rows):
{rows_markdown}"""
