# Enterprise Kafka Design Patterns Library

A comprehensive, production-ready collection of Kafka design patterns for building scalable, resilient distributed systems.

## Overview

This library provides enterprise-grade Kafka implementations organized by architectural patterns. Each pattern is designed for specific use cases and scaling requirements, extracted from real-world production systems.

## Architecture Philosophy

1. **Separation of Concerns**: Producers, Consumers, and Queue management are isolated
2. **Scalability by Design**: All patterns support horizontal scaling
3. **Fault Tolerance**: Built-in error handling and recovery mechanisms
4. **Performance Optimization**: Patterns optimized for different workload characteristics

## Folder Structure

```
kafka/
├── producers/           # Producer design patterns
│   ├── singleton/       # Shared producer instance pattern
│   ├── topic_routing/   # Intelligent message routing
│   └── resilient/       # High-availability producers
├── consumers/           # Consumer design patterns
│   ├── sequential/      # Simple, ordered processing
│   ├── concurrent/      # Thread-pool based processing
│   ├── priority/        # Priority-based consumption
│   └── workload_aware/  # Adaptive consumer strategies
├── queue_management/    # Broker and topic configuration
│   ├── partitioning/    # Partition strategies
│   ├── ordering/        # Message ordering patterns
│   └── monitoring/      # Health and performance monitoring
├── examples/            # Practical implementation examples
└── config/              # Configuration templates
```

## Quick Start

```python
# High-priority real-time processing
from producers.topic_routing import TopicRoutingProducer
from consumers.priority import PriorityConsumer

# Send urgent message
producer = TopicRoutingProducer()
producer.send_high_priority({"user_id": 123, "action": "password_reset"})

# Process with dedicated high-priority consumer
consumer = PriorityConsumer(topic="high-priority-events", workers=10)
consumer.start()
```

## Self-Managed Infrastructure (Local/VM)

If you want to run Kafka without a managed service, use:

- `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\docker-compose.yml`
- `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\scripts\kafka_infrastructure.py`
- `D:\Generative AI Portfolio Projects\kafka_version_3\infrastructure\README.md`

This gives you a portable local-to-VM baseline with lifecycle commands (`up`, `down`, `status`, `health`, `logs`).

## Design Patterns Summary

### Producer Patterns
- **Singleton Pattern**: Shared producer instance for application lifecycle
- **Topic Routing**: Intelligent message distribution based on priority/type
- **Resilient Producer**: High-availability with retry and circuit breaker

### Consumer Patterns
- **Sequential Consumer**: Order-critical, CPU-bound processing
- **Concurrent Consumer**: I/O-bound, high-throughput processing
- **Priority Consumer**: Time-sensitive message handling
- **Workload-Aware**: Adaptive processing based on message characteristics

### Queue Management
- **Partition Strategy**: Optimal parallelism configuration
- **Key-Based Ordering**: Entity-specific message ordering
- **Monitoring**: Consumer lag and performance tracking

## Configuration

Update `config/kafka_config.py` with your cluster details:

```python
KAFKA_CONFIG = {
    "bootstrap.servers": "10.60.1.204:9092,10.60.1.205:9092,10.60.1.206:9092",
    "default_port": 9092
}
```

## Best Practices

1. **Always use consumer groups** for scalability and fault tolerance
2. **Implement proper error handling** to prevent poison pill messages
3. **Choose the right consumer pattern** based on workload characteristics
4. **Monitor consumer lag** as your primary health metric
5. **Use topic-based routing** for priority separation

## Enterprise Considerations

- **Security**: TLS/SASL configuration templates included
- **Monitoring**: Integration with Prometheus/Grafana metrics
- **Scaling**: Kubernetes deployment examples
- **Disaster Recovery**: Cross-datacenter replication patterns

## AI Agent + Quality Standards

This repository uses a canonical agent-instruction and quality-gate setup.

- Canonical instructions: `AGENTS.md`
- Tool wrappers: `AGENT.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, `.cursor/rules/`
- Setup guide: [docs/ai-agent-setup.md](docs/ai-agent-setup.md)

Required local quality sequence before commit/PR:

1. `ruff check config common producers consumers test --fix --no-cache`
2. `ruff format config common producers consumers test`
3. `ruff check config common producers consumers test --no-cache`
4. `python -m unittest test.producer.test_simple_producer`
