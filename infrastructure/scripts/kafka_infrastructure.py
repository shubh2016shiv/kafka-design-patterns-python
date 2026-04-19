"""
Kafka infrastructure lifecycle manager for local/VM environments.

This script follows SOLID principles:
- Single Responsibility: each class has one clear purpose.
- Open/Closed: new commands can be added without changing core abstractions.
- Liskov Substitution: command executors can be swapped via protocol.
- Interface Segregation: services depend on minimal interfaces.
- Dependency Inversion: orchestration depends on abstractions, not subprocess directly.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


class InfrastructureError(Exception):
    """Base exception for all infrastructure automation failures."""


class DependencyError(InfrastructureError):
    """Raised when host-level dependencies are missing."""


class CommandExecutionError(InfrastructureError):
    """Raised when an executed command fails."""

    def __init__(self, command: Sequence[str], stderr: str, exit_code: int) -> None:
        command_text = " ".join(command)
        message = (
            f"Command failed with exit code {exit_code}: {command_text}\n"
            f"stderr: {stderr.strip() or '(no stderr output)'}"
        )
        super().__init__(message)


class CommandExecutor(Protocol):
    """Abstraction for command execution, enabling testability and extension."""

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Execute command and return completed process."""


class SubprocessExecutor:
    """Concrete command executor based on Python subprocess."""

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise CommandExecutionError(command, completed.stderr, completed.returncode)
        return completed


@dataclass(frozen=True)
class InfrastructurePaths:
    """Immutable path model for infrastructure assets."""

    root_dir: Path
    compose_file: Path
    env_file: Path
    env_template: Path


class HostDependencyValidator:
    """Validates host-level prerequisites before orchestration begins."""

    def __init__(self, executor: CommandExecutor) -> None:
        self._executor = executor

    def validate(self) -> None:
        # Stage 1.1 - Host Dependency Check:
        # Confirm Docker CLI exists and is callable from current shell.
        if shutil.which("docker") is None:
            raise DependencyError(
                "Docker CLI not found in PATH. Install Docker Engine/Desktop before running infrastructure setup."
            )

        # Stage 1.2 - Compose Plugin Check:
        # Validate that `docker compose` is available (required by this project).
        self._executor.run(["docker", "compose", "version"])


class DockerComposeGateway:
    """Encapsulates Docker Compose operations behind intent-driven methods."""

    def __init__(self, paths: InfrastructurePaths, executor: CommandExecutor, project_name: str = "kafka_enterprise") -> None:
        self._paths = paths
        self._executor = executor
        self._project_name = project_name

    def _base_command(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self._project_name,
            "--file",
            str(self._paths.compose_file),
            "--env-file",
            str(self._paths.env_file),
        ]

    def validate_compose_configuration(self) -> str:
        completed = self._executor.run([*self._base_command(), "config"])
        return completed.stdout.strip()

    def up(self, detach: bool = True) -> str:
        command = [*self._base_command(), "up"]
        if detach:
            command.append("-d")
        completed = self._executor.run(command)
        return completed.stdout.strip()

    def down(self, remove_volumes: bool = False) -> str:
        command = [*self._base_command(), "down"]
        if remove_volumes:
            command.append("--volumes")
        completed = self._executor.run(command)
        return completed.stdout.strip()

    def restart(self) -> str:
        completed = self._executor.run([*self._base_command(), "restart"])
        return completed.stdout.strip()

    def status(self) -> str:
        completed = self._executor.run([*self._base_command(), "ps"])
        return completed.stdout.strip()

    def logs(self, service: str | None, lines: int) -> str:
        command = [*self._base_command(), "logs", "--tail", str(lines)]
        if service:
            command.append(service)
        completed = self._executor.run(command)
        return completed.stdout.strip()

    def health_probe(self) -> bool:
        # Stage 4.1 - Broker Health Probe:
        # A successful topic list indicates broker API readiness.
        self._executor.run(
            [
                *self._base_command(),
                "exec",
                "-T",
                "kafka",
                "kafka-topics",
                "--bootstrap-server",
                "localhost:9092",
                "--list",
            ]
        )
        return True


class InfrastructureLifecycleManager:
    """Coordinates validation and lifecycle actions as a high-level service."""

    def __init__(
        self,
        paths: InfrastructurePaths,
        dependency_validator: HostDependencyValidator,
        compose_gateway: DockerComposeGateway,
    ) -> None:
        self._paths = paths
        self._dependency_validator = dependency_validator
        self._compose_gateway = compose_gateway

    def prepare_environment(self) -> None:
        # Stage 0.1 - Filesystem Contract Validation:
        # Ensure infrastructure descriptor files exist before any runtime action.
        if not self._paths.compose_file.exists():
            raise InfrastructureError(f"Compose file not found: {self._paths.compose_file}")

        # Stage 0.2 - Environment File Bootstrapping:
        # Generate `.env` from template on first run to keep onboarding simple.
        if not self._paths.env_file.exists():
            template_content = self._paths.env_template.read_text(encoding="utf-8")
            self._paths.env_file.write_text(template_content, encoding="utf-8")

    def validate(self) -> None:
        # Stage 1 - Prerequisite Validation:
        self._dependency_validator.validate()
        self._compose_gateway.validate_compose_configuration()

    def up(self) -> str:
        # Stage 2 - Provision Infrastructure:
        return self._compose_gateway.up(detach=True)

    def down(self, *, remove_volumes: bool) -> str:
        # Stage 5 - Controlled Teardown:
        return self._compose_gateway.down(remove_volumes=remove_volumes)

    def restart(self) -> str:
        # Stage 3 - Controlled Restart:
        return self._compose_gateway.restart()

    def status(self) -> str:
        return self._compose_gateway.status()

    def logs(self, service: str | None, lines: int) -> str:
        return self._compose_gateway.logs(service=service, lines=lines)

    def health(self) -> bool:
        return self._compose_gateway.health_probe()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enterprise-style Kafka infrastructure lifecycle manager for local/VM environments.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("up", help="Bring Kafka infrastructure up.")

    down_parser = subparsers.add_parser("down", help="Stop Kafka infrastructure.")
    down_parser.add_argument(
        "--remove-volumes",
        action="store_true",
        help="Also remove Kafka data volumes (destructive).",
    )

    subparsers.add_parser("restart", help="Restart running Kafka infrastructure.")
    subparsers.add_parser("status", help="Show infrastructure status.")
    subparsers.add_parser("health", help="Run broker health probe.")

    logs_parser = subparsers.add_parser("logs", help="Show logs.")
    logs_parser.add_argument("--service", default=None, help="Optional service name (kafka or kafka-ui).")
    logs_parser.add_argument("--lines", type=int, default=200, help="Number of log lines to print.")

    return parser


def resolve_paths() -> InfrastructurePaths:
    script_dir = Path(__file__).resolve().parent
    root_dir = script_dir.parent
    return InfrastructurePaths(
        root_dir=root_dir,
        compose_file=root_dir / "docker-compose.yml",
        env_file=root_dir / ".env",
        env_template=root_dir / ".env.example",
    )


def build_lifecycle_manager() -> InfrastructureLifecycleManager:
    paths = resolve_paths()
    executor = SubprocessExecutor()
    dependency_validator = HostDependencyValidator(executor=executor)
    compose_gateway = DockerComposeGateway(paths=paths, executor=executor)
    return InfrastructureLifecycleManager(
        paths=paths,
        dependency_validator=dependency_validator,
        compose_gateway=compose_gateway,
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    manager = build_lifecycle_manager()

    try:
        manager.prepare_environment()
        manager.validate()

        if args.command == "up":
            print(manager.up())
            print("Kafka infrastructure is starting. Check status/health in ~30-60 seconds.")
            return 0

        if args.command == "down":
            print(manager.down(remove_volumes=args.remove_volumes))
            return 0

        if args.command == "restart":
            print(manager.restart())
            return 0

        if args.command == "status":
            print(manager.status())
            return 0

        if args.command == "logs":
            print(manager.logs(service=args.service, lines=args.lines))
            return 0

        if args.command == "health":
            is_healthy = manager.health()
            print("healthy" if is_healthy else "unhealthy")
            return 0

        parser.print_help()
        return 1

    except InfrastructureError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
