# Kafka Infrastructure

This folder contains the local or single-VM Kafka infrastructure for this project.

It is intentionally simple:

- 1 Kafka broker running in KRaft mode
- 1 Kafka UI instance for inspection
- 1 Python lifecycle script for repeatable operations
- 1 smoke test for proving real producer and consumer flow

This is a good learning and development setup. It is not a full production Kafka platform yet.

## What Is In This Folder

- `docker-compose.yml`
  Defines the Kafka broker, Kafka UI, ports, health checks, volumes, and Docker network.
- `.env.example`
  Holds the default environment values for host, ports, partitions, and JVM sizing.
- `scripts/kafka_infrastructure.py`
  The main operational entry point for starting, stopping, checking, and inspecting the stack.
- `smoke_tests/kafka_smoke_test.py`
  An isolated end-to-end smoke test that produces and consumes a real Kafka record.

## Listener Model

This setup uses two different listener contexts:

- `kafka:9092`
  Internal Docker network listener. Containers use this.
- `<host>:9094`
  External listener exposed to your machine or VM clients. Applications outside Docker use this.

For local development, the external bootstrap server is usually:

```text
localhost:9094
```

Kafka UI is exposed at:

```text
http://localhost:8080
```

## Quick Start

Run these commands from the `infrastructure/` directory.

### 1. Create `.env`

```powershell
Copy-Item .env.example .env
```

If `.env` already exists, keep it and only update values when you intentionally want to change them.

### 2. Set the external host

Open `.env` and set:

- `KAFKA_EXTERNAL_HOST=localhost` for local development
- `KAFKA_EXTERNAL_HOST=<vm-ip-or-dns>` for a VM that external clients will reach

### 3. Start the stack

```powershell
python scripts/kafka_infrastructure.py up
```

The script will:

- create `.env` from `.env.example` if needed
- validate Docker and `docker compose`
- validate the compose configuration
- start Kafka and Kafka UI

### 4. Check status

```powershell
python scripts/kafka_infrastructure.py status
python scripts/kafka_infrastructure.py health
```

Expected outcome:

- `status` shows the containers running
- `health` prints `healthy`

### 5. Open Kafka UI

[Kafka UI](http://localhost:8080)

## How Applications Should Connect

### From your host machine

Use:

```python
bootstrap_servers = "localhost:9094"
```

### From another machine talking to a VM

Use:

```python
bootstrap_servers = "<KAFKA_EXTERNAL_HOST>:9094"
```

### From another container on the same Docker network

Use:

```python
bootstrap_servers = "kafka:9092"
```

## Operational Commands

### Show status

```powershell
python scripts/kafka_infrastructure.py status
```

### Run health check

```powershell
python scripts/kafka_infrastructure.py health
```

### View logs

Kafka broker logs:

```powershell
python scripts/kafka_infrastructure.py logs --service kafka --lines 200
```

Kafka UI logs:

```powershell
python scripts/kafka_infrastructure.py logs --service kafka-ui --lines 200
```

### Restart the stack

```powershell
python scripts/kafka_infrastructure.py restart
```

### Stop the stack

```powershell
python scripts/kafka_infrastructure.py down
```

### Full reset

This removes Kafka data volumes and is destructive:

```powershell
python scripts/kafka_infrastructure.py down --remove-volumes
```

Use this only when you intentionally want to wipe broker state and start fresh.

## Smoke Test

The smoke test is the fastest way to prove the infrastructure is actually usable, not just running.

It validates:

- connection to Kafka
- produce success
- consume success

### Install smoke test dependency

```powershell
python -m pip install -r smoke_tests/requirements.txt
```

### Run the smoke test

```powershell
python smoke_tests/kafka_smoke_test.py --bootstrap-servers localhost:9094
```

Expected success output includes:

```text
SMOKE TEST RESULT: PASS
```

If it fails, the script prints the reason directly so you can debug from there.

## Configuration You Should Actually Care About

The most important values in `.env` for this setup are:

- `KAFKA_EXTERNAL_HOST`
  What clients outside Docker should use to reach Kafka.
- `KAFKA_EXTERNAL_PORT`
  The host port exposed for Kafka clients. Default is `9094`.
- `KAFKA_UI_PORT`
  The host port exposed for Kafka UI. Default is `8080`.
- `KAFKA_DEFAULT_PARTITIONS`
  Default partition count for auto-created topics if auto-create is enabled.
- `KAFKA_AUTO_CREATE_TOPICS`
  Whether Kafka may auto-create topics. Default is `false`, which is a safer production habit.
- `KAFKA_JVM_XMS` and `KAFKA_JVM_XMX`
  Heap sizing for the broker.
- `KAFKA_KRAFT_CLUSTER_ID`
  Keep this stable once the broker has data.

## Common Issues

### `health` fails even though containers exist

Usually means Kafka has started at the container level but is not yet ready at the broker API level. Wait a little longer, then run:

```powershell
python scripts/kafka_infrastructure.py health
```

### App cannot connect from host

Check:

- are you using `localhost:9094` rather than `kafka:9092`
- is the Kafka container healthy
- is `KAFKA_EXTERNAL_HOST` correct in `.env`

### App cannot connect from another machine

Check:

- `KAFKA_EXTERNAL_HOST` is set to a reachable VM IP or DNS name
- port `9094` is open in firewall or security-group rules
- the client is not trying to use `localhost`

### Kafka UI opens but no cluster data appears

Kafka UI depends on the broker health check. If Kafka is not healthy yet, UI may load before useful cluster data is available. Recheck broker health first.

### Smoke test fails

Check in this order:

1. `python scripts/kafka_infrastructure.py status`
2. `python scripts/kafka_infrastructure.py health`
3. `python scripts/kafka_infrastructure.py logs --service kafka --lines 200`
4. rerun `python smoke_tests/kafka_smoke_test.py --bootstrap-servers localhost:9094`

## Production Hardening Path

This setup is intentionally plaintext and single-broker to keep learning and onboarding straightforward.

Before calling it real production infrastructure, you should plan for:

1. multi-broker Kafka cluster
2. stronger replication and ISR policies
3. TLS for listeners
4. SASL or mTLS authentication
5. restricted network access
6. metrics, dashboards, and alerts
7. backup and recovery plan
8. schema governance and topic management policy

## When This Setup Is The Right Fit

Use this setup when:

- you are learning Kafka
- you want a reproducible local development stack
- you want a single-VM demo or portfolio deployment
- you want to validate producer and consumer behavior against a real broker

Do not mistake it for a complete enterprise Kafka platform. It is the right foundation for this project stage, not the final shape of a large production deployment.
