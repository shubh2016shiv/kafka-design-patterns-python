# Kafka Infrastructure Smoke Tests

This folder is intentionally isolated for **real infrastructure smoke tests** only.

## Scope
1. Connectivity to Kafka external listener.
2. Producer send success.
3. Consumer receive success.

## Test Script
`D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\smoke_tests\kafka_smoke_test.py`

## Dependency
Install once:

```powershell
python -m pip install -r "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\smoke_tests\requirements.txt"
```

## Run (Default)
Uses `localhost:19094` by default:

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\smoke_tests\kafka_smoke_test.py"
```

## Run (Custom Bootstrap Server)

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\smoke_tests\kafka_smoke_test.py" --bootstrap-servers "localhost:19094"
```

## Expected Result
Success:
- `SMOKE TEST RESULT: PASS`

Failure:
- `SMOKE TEST RESULT: FAIL`
- reason will be printed for debugging.
