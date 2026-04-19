# Kafka Infrastructure Smoke Tests

This folder contains isolated smoke tests for the real Kafka infrastructure.

The goal is intentionally small:

- prove the client can reach Kafka
- produce one record successfully
- consume that same record successfully

## Files

- `kafka_smoke_test.py`
  Runs the end-to-end smoke test against a live Kafka broker.
- `requirements.txt`
  Python dependency list for the smoke test.

## Install Dependency

Run from the `infrastructure/` directory:

```powershell
python -m pip install -r smoke_tests/requirements.txt
```

## Default Bootstrap Server

The smoke test defaults to:

```text
localhost:9094
```

That matches the external listener exposed by this project's `docker-compose.yml`.

## Run

```powershell
python smoke_tests/kafka_smoke_test.py
```

## Run With Custom Bootstrap Server

```powershell
python smoke_tests/kafka_smoke_test.py --bootstrap-servers "<host>:9094"
```

## Expected Result

Success:

- `SMOKE TEST RESULT: PASS`

Failure:

- `SMOKE TEST RESULT: FAIL`
- the reason is printed for debugging
