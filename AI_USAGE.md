# GitHub Copilot usage

GitHub Copilot in VS Code (Copilot SDK) was used to review and edit this repository. Pylance diagnostics, `pytest`, and `compileall` were used for local validation; no other AI assistant or external coding service was used.

## Key prompts
- “Build the complete Tenant Ledger Backend Engineer take-home challenge” with FastAPI, SQLite, SQLAlchemy, Decimal/integer-cent handling, tenant isolation, imports, audit, reversals, reports, and tests.
- “Review my existing Tenant Ledger project against the original challenge; do not rewrite it; inspect first and report the audit before major changes.”
- The follow-up requested opaque client references, strict currency validation, effective dates, historical balances, concurrency safety, tests, and truthful documentation.

## Review and corrections
- The existing implementation and all relevant source, tests, requirements, docs, and Git status/history were inspected before editing; its original test suite passed 6 tests.
- Copilot-assisted changes removed client names, added SQLite startup migration, date/currency handling, historical reporting, serialized writes, database guards, and expanded tests.
- During implementation, test failures exposed a UTC-date mismatch and audit queries omitting reversal events; both were corrected and retested.
- The final test count and outcomes are reported from the actual full-suite run, not estimated.
- No review commit was issued and no code was pushed; the existing Git history was not rewritten.
