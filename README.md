# Disbursement Desk

The Disbursement Desk, moving here from [payments-api-simulator](https://github.com/jeffreykkirkwood-commits/payments-api-simulator) (issue [#122](https://github.com/jeffreykkirkwood-commits/payments-api-simulator/issues/122)).

This first commit holds only the repo's checks, copied unchanged from payments-api-simulator at `d272119`:
CI, Card format, Secret scan (gitleaks), Inventory updated, Live smoke check (manual), Dependabot, and the PR and issue templates.
The Desk's code arrives in later pull requests, with its git history kept.

Synthetic data only. No secrets in this repo.

## Run the app shell locally

The app shell (payments-api-simulator #122, step 2) answers `/health`.
The payout engine (step 3) gives each visitor's demo the payout tables and 40 fictional
starting payouts. Step 4 brings the Desk page at `/desk` (the root address redirects there)
and the payout routes, unchanged from the Lab.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                                   # http://127.0.0.1:8000/desk
.venv/bin/python -m unittest                              # tests
STORAGE_TEST_BACKEND=libsql-local .venv/bin/python -m unittest
```

To see a moved file's history from the Lab, run for example `git log --follow -- storage.py`.
