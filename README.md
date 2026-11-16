# Disbursement Desk

The Disbursement Desk, moving here from [payments-api-simulator](https://github.com/jeffreykkirkwood-commits/payments-api-simulator) (issue [#122](https://github.com/jeffreykkirkwood-commits/payments-api-simulator/issues/122)).

This first commit holds only the repo's checks, copied unchanged from payments-api-simulator at `d272119`:
CI, Card format, Secret scan (gitleaks), Inventory updated, Live smoke check (manual), Dependabot, and the PR and issue templates.
The Desk's code arrives in later pull requests, with its git history kept.

Synthetic data only. No secrets in this repo.

## Run the app shell locally

The app shell (payments-api-simulator #122, step 2) has one route so far, `/health`.
The payout engine and the Desk page arrive in later pull requests.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                                   # http://127.0.0.1:8000/health
.venv/bin/python -m unittest                              # tests
STORAGE_TEST_BACKEND=libsql-local .venv/bin/python -m unittest
```

To see a moved file's history from the Lab, run for example `git log --follow -- storage.py`.
