# Tenant Ledger API
Python 3.11+, FastAPI, SQLAlchemy, and SQLite ledger service.
Run `pip install -r requirements.txt` then `uvicorn app.main:app --reload`.
Tenant-scoped API calls use demo `X-Tenant-ID`; decimal strings support USD, EUR, GBP, CAD, AUD, CHF, NZD, SGD.
Run the test suite with `pytest`.
