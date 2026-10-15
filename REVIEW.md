# Code Review — Employee Attendance API

## Defects Found

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `create_indexes()` | The unique compound index on attendance `emp_code` and `date` is declared twice. This is redundant. | Inspect the index definitions and start the application. | Remove the duplicate declaration and keep one unique compound index. |
| 2 | Analytics endpoints | The required employee monthly analytics endpoint is missing from the reviewed `app/main.py`. | Check `/docs` or `/openapi.json` for `GET /analytics/employees/{emp_code}/monthly`. | Not fixed yet; implement the endpoint and test its aggregation results. |
| 3 | Analytics endpoints | The required department summary endpoint is missing from the reviewed `app/main.py`. | Check `/docs` or `/openapi.json` for `GET /analytics/departments/summary`. | Not fixed yet; implement and test the MongoDB aggregation. |
| 4 | Analytics endpoints | The required late leaderboard endpoint is missing from the reviewed `app/main.py`. | Check `/docs` or `/openapi.json` for `GET /analytics/leaderboard/late`. | Not fixed yet; implement ranking, tie handling, and limit behavior. |
| 5 | Analytics endpoints | The required department trend endpoint is missing from the reviewed `app/main.py`. | Check `/docs` or `/openapi.json` for `GET /analytics/departments/{department}/trend`. | Not fixed yet; implement the daily trend and seven-day moving average. |
| 6 | Admin explain endpoint | The required MongoDB query-plan inspection endpoint is missing from the reviewed `app/main.py`. | Check `/docs` or `/openapi.json` for `GET /admin/explain/{endpoint}`. | Not fixed yet; implement `explain` with execution statistics for the supported queries. |

## Checked and Not Considered Defects

- `load_dotenv()` loads local environment configuration while allowing already-set environment variables to take priority.
- MongoDB URI and database name are read from environment variables, with local defaults.
- `python -m py_compile app/main.py` completed without a syntax error. This verifies syntax, not business-rule correctness.
- The application includes employee creation and listing, attendance punch-in and punch-out, attendance listing, and attendance regularization routes.

## Outstanding Work

The missing analytics and explain endpoints must be implemented and tested before submission. Other potential defects in validation, attendance calculations, concurrency, and database query performance still need verification against the assignment requirements.
