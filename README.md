# Tenant Ledger API
Python 3.11+, FastAPI, SQLAlchemy, and SQLite ledger service.
Run `pip install -r requirements.txt` then `uvicorn app.main:app --reload`.
Every request requires an `X-Tenant-ID` header; money is supplied as decimal strings.
Run the test suite with `pytest`.
