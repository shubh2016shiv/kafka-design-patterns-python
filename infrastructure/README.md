# Self-Managed Kafka Infrastructure (Local + VM)

This folder gives you an enterprise-style baseline for running Kafka without a managed service.

## Why Docker Compose (Production Decision for Your Current Stage)

For your current setup (local machine now, VM later), `docker-compose.yml` is the best fit because:

1. It models multi-service infrastructure (Kafka broker + UI) in one declarative file.
2. It is portable from local laptop to a single cloud VM with minimal changes.
3. It supports deterministic startup, health checks, and controlled teardown.

A single `Dockerfile` would only package one service and is not ideal for orchestration.

## Infrastructure-Level Dependencies

## Stage 1 - Host Dependencies
1. Docker Engine or Docker Desktop (Linux/Windows/macOS).
2. Docker Compose plugin (`docker compose version` must work).
3. Minimum recommended VM sizing:
1. CPU: 2 vCPU (4 vCPU preferred).
2. RAM: 4 GB minimum (8 GB preferred).
3. Disk: 20+ GB SSD.

## Stage 2 - Network Dependencies
1. Open inbound port `9094` for Kafka client traffic (if remote clients connect).
2. Open inbound port `8080` for Kafka UI (optional, restrict by firewall).
3. Ensure DNS/IP used in `.env` (`KAFKA_EXTERNAL_HOST`) is reachable by clients.

## Stage 3 - Security Dependencies (Production Hardening Path)
1. TLS for Kafka listeners.
2. SASL authentication (SCRAM or mTLS strategy).
3. Restricted network access (private subnets, security groups/firewall rules).
4. Secrets management (do not keep credentials in plaintext `.env`).

Current compose is PLAINTEXT to keep onboarding simple; hardening is the next step before true production use.

## Files in This Folder

1. `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\docker-compose.yml`
2. `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\.env.example`
3. `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py`

## Lifecycle Stages (Operational Runbook)

## Stage 0 - Bootstrap Configuration
1. Copy env template:

```powershell
Copy-Item "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\.env.example" "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\.env"
```

2. Update `KAFKA_EXTERNAL_HOST`:
1. Local machine: `localhost`
2. VM: VM private/public IP or DNS name used by your applications

## Stage 1 - Bring Infrastructure Up

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py" up
```

## Stage 2 - Validate Runtime

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py" status
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py" health
```

Kafka UI URL: [http://localhost:8080](http://localhost:8080)

## Stage 3 - Observe Logs

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py" logs --service kafka --lines 200
```

## Stage 4 - Controlled Restart

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py" restart
```

## Stage 5 - Controlled Shutdown

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py" down
```

Full reset (destructive, removes Kafka data volumes):

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py" down --remove-volumes
```

## How Your Python Apps Should Connect

From your host machine, use:

```python
bootstrap_servers = "localhost:9094"
```

From another machine in network/VM setup, use:

```python
bootstrap_servers = "<KAFKA_EXTERNAL_HOST>:9094"
```

## Notes for Cloud VM Migration

1. Keep this same folder and move it to your VM.
2. Install Docker + Compose on VM.
3. Set `KAFKA_EXTERNAL_HOST` to VM IP/DNS.
4. Open firewall/security-group ports (`9094`, optional `8080`).
5. Run the same Python lifecycle script commands.

## Infrastructure Smoke Test (Isolated)

Use the isolated smoke test folder to validate real infra messaging flow:

1. `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\smoke_tests\README.md`
2. `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\smoke_tests\kafka_smoke_test.py`

Run:

```powershell
python "D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\smoke_tests\kafka_smoke_test.py" --bootstrap-servers "localhost:19094"
```

## Next Enterprise Upgrades (Recommended)

1. 3-broker KRaft cluster with dedicated controllers.
2. TLS + SASL auth for all external listeners.
3. Metrics stack (Prometheus + Grafana) and alerting.
4. Schema Registry + ACL policy management.
5. Backup/restore plan for Kafka volumes and configs.
