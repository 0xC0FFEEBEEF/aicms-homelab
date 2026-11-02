from __future__ import annotations

import socket
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from threading import Event
from typing import Iterable

import paramiko

from app.jobs.cancellation import JobCancelled
from app.models import Host
from app.schemas import ConnectionTestResult, RootCheckResult
from app.utils.ssh_keys import resolve_private_key_path, resolve_public_key_path


class SSHCommandError(RuntimeError):
    pass


@dataclass
class SSHExecutor:
    connect_timeout: int
    command_timeout: int

    @staticmethod
    def _connect_or_raise(client: paramiko.SSHClient, connect_kwargs: dict[str, object]) -> None:
        try:
            client.connect(**connect_kwargs)
        except socket.gaierror as exc:
            hostname = str(connect_kwargs.get("hostname") or "unknown-host")
            raise RuntimeError(
                f"Could not resolve SSH hostname '{hostname}'. Use a reachable IP address or a hostname your AICMS VM can resolve."
            ) from exc

    def _read_public_key(self, ssh_key_path: str | None) -> str:
        public_key_path = resolve_public_key_path(ssh_key_path)
        try:
            return public_key_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"SSH public key not found at {public_key_path}") from exc

    def _build_bootstrap_setup_script(self, *, target_user: str, public_key: str, roots: Iterable[str]) -> str:
        allowlisted_roots = [root for root in roots if root.strip().startswith("/")]
        acl_commands: list[str] = []
        needs_proxmox_group_access = any(root == "/etc/pve" or root.startswith("/etc/pve/") for root in allowlisted_roots)

        if allowlisted_roots:
            acl_commands.extend(
                [
                    "if ! command -v setfacl >/dev/null 2>&1; then",
                    "  if command -v apt-get >/dev/null 2>&1; then",
                    "    export DEBIAN_FRONTEND=noninteractive;",
                    "    apt-get update >/dev/null 2>&1 || true;",
                    "    apt-get install -y acl >/dev/null 2>&1 || true;",
                    "  fi;",
                    "fi;",
                    "command -v setfacl >/dev/null 2>&1 || { echo 'setfacl is required to grant readonly access for allowlisted roots.' >&2; exit 1; };",
                ]
            )

            seen_ancestors: set[str] = set()
            for root in allowlisted_roots:
                root_path = PurePosixPath(root)
                ancestor = root_path.parent
                ancestor_chain: list[str] = []
                while str(ancestor).startswith("/"):
                    ancestor_text = str(ancestor)
                    if ancestor_text not in seen_ancestors:
                        ancestor_chain.append(ancestor_text)
                        seen_ancestors.add(ancestor_text)
                    if ancestor_text == "/":
                        break
                    ancestor = ancestor.parent

                for ancestor_text in reversed(ancestor_chain):
                    acl_commands.append(
                        f"setfacl -m u:{shlex.quote(target_user)}:--x {shlex.quote(ancestor_text)} >/dev/null 2>&1 || true;"
                    )

                acl_commands.append(
                    f"if [ -d {shlex.quote(root)} ]; then "
                    f"setfacl -R -m u:{shlex.quote(target_user)}:rX {shlex.quote(root)} >/dev/null 2>&1 || true; "
                    f"find {shlex.quote(root)} -type d -exec setfacl -m d:u:{shlex.quote(target_user)}:rX {{}} + >/dev/null 2>&1 || true; "
                    "fi;"
                )

        home_lookup = f'HOME_DIR="$(getent passwd {shlex.quote(target_user)} | cut -d: -f6)"'
        setup_parts = [
            "set -e;",
            f"id -u {shlex.quote(target_user)} >/dev/null 2>&1 || useradd -m -s /bin/bash {shlex.quote(target_user)};",
            f"{home_lookup};",
            '[ -n "$HOME_DIR" ];',
            f"install -d -m 700 -o {shlex.quote(target_user)} -g {shlex.quote(target_user)} \"$HOME_DIR/.ssh\";",
            "touch \"$HOME_DIR/.ssh/authorized_keys\";",
            f"chmod 600 \"$HOME_DIR/.ssh/authorized_keys\";",
            f"chown {shlex.quote(target_user)}:{shlex.quote(target_user)} \"$HOME_DIR/.ssh/authorized_keys\";",
            f"grep -qxF {shlex.quote(public_key)} \"$HOME_DIR/.ssh/authorized_keys\" || printf '%s\\n' {shlex.quote(public_key)} >> \"$HOME_DIR/.ssh/authorized_keys\";",
            f"chown {shlex.quote(target_user)}:{shlex.quote(target_user)} \"$HOME_DIR/.ssh/authorized_keys\";",
        ]
        if needs_proxmox_group_access:
            setup_parts.append(
                f"getent group www-data >/dev/null 2>&1 && usermod -aG www-data {shlex.quote(target_user)} || true;"
            )
        setup_parts.extend(acl_commands)
        return " ".join(setup_parts)

    def open_client(self, host: Host) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": host.address,
            "port": host.ssh_port,
            "username": host.ssh_user,
            "timeout": self.connect_timeout,
            "banner_timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
        }

        resolved_key_path = resolve_private_key_path(host.ssh_key_path)
        if resolved_key_path:
            if not resolved_key_path.exists():
                raise FileNotFoundError(f"SSH private key not found at {resolved_key_path}")
            connect_kwargs["key_filename"] = str(resolved_key_path)
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        else:
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        self._connect_or_raise(client, connect_kwargs)
        return client

    def run(
        self,
        client: paramiko.SSHClient,
        command: str,
        timeout: int | None = None,
        stop_event: Event | None = None,
    ) -> tuple[int, str, str]:
        effective_timeout = timeout or self.command_timeout
        _, stdout, stderr = client.exec_command(command, timeout=effective_timeout)
        channel = stdout.channel
        deadline = time.monotonic() + effective_timeout
        inherited_stop_event = stop_event or getattr(self, "stop_event", None)

        while not channel.exit_status_ready():
            if isinstance(inherited_stop_event, Event) and inherited_stop_event.is_set():
                try:
                    channel.close()
                finally:
                    raise JobCancelled("Scan stopped by user.")
            if time.monotonic() >= deadline:
                try:
                    channel.close()
                finally:
                    raise TimeoutError(f"Remote command timed out after {effective_timeout} seconds.")
            time.sleep(0.2)

        exit_code = channel.recv_exit_status()
        return (
            exit_code,
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
        )

    def run_lines(self, client: paramiko.SSHClient, command: str, timeout: int | None = None) -> list[str]:
        code, stdout, stderr = self.run(client, command, timeout)
        if code != 0:
            raise SSHCommandError(stderr.strip() or f"Command failed: {command}")
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def test_connection(self, host: Host, roots: Iterable[str]) -> ConnectionTestResult:
        client = self.open_client(host)
        try:
            results: list[RootCheckResult] = []
            for root_path in roots:
                command = (
                    "bash -lc "
                    + shlex.quote(
                        f"if [ ! -d {shlex.quote(root_path)} ]; then "
                        "echo MISSING; "
                        f"elif [ ! -r {shlex.quote(root_path)} ] || [ ! -x {shlex.quote(root_path)} ]; then "
                        "echo NO_ACCESS; "
                        "else "
                        "echo OK; "
                        "fi"
                    )
                )
                _, stdout, stderr = self.run(client, command)
                status = stdout.strip() or "UNKNOWN"
                exists = status == "OK"
                if status == "OK":
                    message = "Root is accessible."
                elif status == "NO_ACCESS":
                    message = "Root exists but is not readable/traversable by the SSH user."
                else:
                    message = stderr.strip() or "Root does not exist."
                results.append(
                    RootCheckResult(
                        root_path=root_path,
                        exists=exists,
                        message=message,
                    )
                )

            success = all(item.exists for item in results)
            message = (
                "SSH connectivity test succeeded."
                if success
                else "SSH connectivity succeeded, but one or more roots are missing or inaccessible."
            )
            return ConnectionTestResult(success=success, message=message, root_results=results)
        finally:
            client.close()

    def bootstrap_host_access(
        self,
        host: Host,
        *,
        bootstrap_user: str,
        bootstrap_password: str | None = None,
        sudo_password: str | None = None,
        roots: Iterable[str],
    ) -> ConnectionTestResult:
        public_key = self._read_public_key(host.ssh_key_path)
        target_user = host.ssh_user.strip()
        if not bootstrap_user.strip():
            raise RuntimeError("Bootstrap SSH user is required.")
        if not target_user:
            raise RuntimeError("The saved host SSH user is required before bootstrapping.")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": host.address,
            "port": host.ssh_port,
            "username": bootstrap_user.strip(),
            "timeout": self.connect_timeout,
            "banner_timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
        }
        if bootstrap_password:
            connect_kwargs["password"] = bootstrap_password
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        else:
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        self._connect_or_raise(client, connect_kwargs)

        setup_script = self._build_bootstrap_setup_script(
            target_user=target_user,
            public_key=public_key,
            roots=roots,
        )

        try:
            effective_bootstrap_user = bootstrap_user.strip()
            if effective_bootstrap_user in {"root", target_user}:
                command = f"bash -lc {shlex.quote(setup_script)}"
            else:
                effective_sudo_password = sudo_password or bootstrap_password
                if not effective_sudo_password:
                    raise RuntimeError("A sudo password is required to create/configure the remote scanner user.")
                command = (
                    f"printf '%s\\n' {shlex.quote(effective_sudo_password)} | "
                    f"sudo -S -p '' bash -lc {shlex.quote(setup_script)}"
                )

            code, stdout, stderr = self.run(client, command, timeout=max(self.command_timeout, 90))
            if code != 0:
                raise SSHCommandError(stderr.strip() or stdout.strip() or "Remote bootstrap failed.")
        finally:
            client.close()

        result = self.test_connection(host, roots)
        result.message = f"Bootstrap completed. {result.message}"
        return result
