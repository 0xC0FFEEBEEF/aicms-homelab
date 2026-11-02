from __future__ import annotations

from app.models import Host
from app.utils.ssh_keys import normalize_private_key_path


def build_connectivity_commands(host: Host | None = None, *, default_key_path: str = "~/.ssh/aicms_scanner_ed25519") -> list[dict[str, str]]:
    ssh_user = host.ssh_user if host else "aicms"
    address = host.address if host else "vm.example.internal"
    port = host.ssh_port if host else 22
    key_path = normalize_private_key_path(host.ssh_key_path) if host and host.ssh_key_path else normalize_private_key_path(default_key_path)
    roots = [root.root_path for root in host.roots if root.enabled] if host else ["/srv/projects"]

    verify_roots = " ; ".join(
        f"test -d '{root}' && echo OK:{root} || echo MISSING:{root}" for root in roots
    )

    return [
        {
            "title": "Generate a dedicated SSH key",
            "command": f"ssh-keygen -t ed25519 -f {key_path} -C aicms-scanner",
        },
        {
            "title": "Copy the public key to the target VM",
            "command": f"ssh-copy-id -i {key_path}.pub -p {port} {ssh_user}@{address}",
        },
        {
            "title": "Create the scanner user if needed",
            "command": (
                "sudo useradd -m -s /bin/bash aicms 2>/dev/null || true && "
                "sudo install -d -m 700 -o aicms -g aicms /home/aicms/.ssh && "
                "sudo touch /home/aicms/.ssh/authorized_keys && "
                "sudo chmod 600 /home/aicms/.ssh/authorized_keys && "
                "sudo chown aicms:aicms /home/aicms/.ssh/authorized_keys"
            ),
        },
        {
            "title": "Verify the allowlisted roots exist",
            "command": f"ssh -p {port} {ssh_user}@{address} \"{verify_roots}\"",
        },
        {
            "title": "Allow SSH through UFW if the VM uses it",
            "command": f"sudo ufw allow {port}/tcp && sudo ufw status",
        },
    ]
