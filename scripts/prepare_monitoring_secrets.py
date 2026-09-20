"""Prepare private host files for Compose's read-only, non-root monitoring services."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def prepare(directory: Path) -> None:
    values = {
        "metrics_token": os.environ.get("NEXORA_METRICS_TOKEN", ""),
        "grafana_admin_password": os.environ.get("NEXORA_GRAFANA_ADMIN_PASSWORD", ""),
    }
    # Validate everything before writing anything. Never echo values in errors.
    for name, value in values.items():
        minimum = 32 if name == "metrics_token" else 16
        if len(value) < minimum or any(char in value for char in "\r\n\x00"):
            raise ValueError(
                f"{name} must contain at least {minimum} characters and no line breaks"
            )
    if directory.is_symlink():
        raise ValueError("Secret directory must not be a symbolic link")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    for name, value in values.items():
        with NamedTemporaryFile(dir=directory, delete=False) as temporary:
            path = Path(temporary.name)
            try:
                temporary.write(value.encode())
                temporary.flush()
                # Non-root container UIDs must read bind-mounted secret files.
                # The host's 0700 parent prevents other host users reaching them.
                path.chmod(0o444)
                path.replace(directory / name)
            finally:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parents[1] / ".monitoring-secrets")
    print("Private monitoring secret files prepared.")
