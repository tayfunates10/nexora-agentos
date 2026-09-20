import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/prepare_monitoring_secrets.py"
spec = importlib.util.spec_from_file_location("prepare_monitoring_secrets", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_secret_files_are_private_on_host_and_readable_by_container(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEXORA_METRICS_TOKEN", "m" * 32)
    monkeypatch.setenv("NEXORA_GRAFANA_ADMIN_PASSWORD", "p" * 16)
    directory = tmp_path / "secrets"
    module.prepare(directory)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "metrics_token").read_text() == "m" * 32
    assert (directory / "grafana_admin_password").stat().st_mode & 0o777 == 0o444
    monkeypatch.setenv("NEXORA_METRICS_TOKEN", "n" * 32)
    module.prepare(directory)
    assert (directory / "metrics_token").read_text() == "n" * 32
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["", "short", "m" * 32 + "\n"])
def test_invalid_secret_writes_nothing(tmp_path, monkeypatch, value):
    monkeypatch.setenv("NEXORA_METRICS_TOKEN", value)
    monkeypatch.setenv("NEXORA_GRAFANA_ADMIN_PASSWORD", "p" * 16)
    directory = tmp_path / "secrets"
    with pytest.raises(ValueError):
        module.prepare(directory)
    assert not directory.exists()


def test_secret_preparation_refuses_symlink_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXORA_METRICS_TOKEN", "m" * 32)
    monkeypatch.setenv("NEXORA_GRAFANA_ADMIN_PASSWORD", "p" * 16)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        module.prepare(link)
    assert list(target.iterdir()) == []
