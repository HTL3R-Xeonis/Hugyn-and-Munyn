"""
The GNS3 template collector takes its servers from the app's host profiles when asked to (--profiles-dir).

Needs pytest, pyvmomi, pyyaml and requests. No ESXi is contacted: read_server is replaced where a run is tested.
"""

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.delenv("HOST_PROFILES_DIR", raising=False)
    monkeypatch.delenv("ESXI_PASSWORD", raising=False)
    spec = importlib.util.spec_from_file_location(
        "gns3_reader_under_test", ROOT / "gns3-reader" / "read_gns3_templates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(directory: Path, name: str, body: dict | str) -> None:
    text = body if isinstance(body, str) else yaml.safe_dump(body)
    (directory / name).write_text(text)


def _secret(directory: Path, profile: str, **values: str) -> None:
    secrets = directory / ".secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / f"{profile}.json").write_text(json.dumps(values))


@pytest.fixture
def profiles(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    return root


# ---------- servers_from_profiles ----------


def test_each_profile_with_an_esxi_ip_becomes_a_server(reader, profiles):
    _profile(
        profiles,
        "lab.yml",
        {
            "esxi": {"ip": "10.1.1.1", "username": "admin", "gns3_vm_name": "GNS-VM-1"},
            "gns3": {"port": 3080},
        },
    )
    _profile(profiles, "second.yaml", {"esxi": {"ip": "10.1.1.2"}})

    servers = reader.servers_from_profiles(str(profiles), logging.getLogger("t"))

    assert servers == [
        {
            "name": "lab",
            "esxi_host": "10.1.1.1",
            "esxi_username": "admin",
            "port": 3080,
            "gns3_vm_name": "GNS-VM-1",
        },
        {
            "name": "second",
            "esxi_host": "10.1.1.2",
            "esxi_username": "root",
            "port": 80,
        },
    ]


def test_unusable_profiles_are_skipped_with_a_warning(reader, profiles, caplog):
    _profile(profiles, "no_esxi.yml", {"gns3": {"port": 80}})
    _profile(profiles, "no_ip.yml", {"esxi": {"username": "root"}})
    _profile(profiles, "broken.yml", "esxi: [unclosed")
    _profile(profiles, "empty.yml", "")
    (profiles / "readme.txt").write_text("not a profile")
    _profile(profiles, "ok.yml", {"esxi": {"ip": "10.1.1.1"}})

    with caplog.at_level(logging.WARNING):
        servers = reader.servers_from_profiles(str(profiles), logging.getLogger("t"))

    assert [s["name"] for s in servers] == ["ok"]
    skipped = " ".join(record.getMessage() for record in caplog.records)
    for name in ("no_esxi.yml", "no_ip.yml", "broken.yml", "empty.yml"):
        assert name in skipped


def test_a_host_two_profiles_share_appears_once(reader, profiles):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1", "gns3_vm_name": "VM"}})
    _profile(profiles, "b.yml", {"esxi": {"ip": "10.1.1.1", "gns3_vm_name": "VM"}})
    _profile(profiles, "c.yml", {"esxi": {"ip": "10.1.1.1", "gns3_vm_name": "OTHER"}})

    servers = reader.servers_from_profiles(str(profiles), logging.getLogger("t"))

    assert [s["name"] for s in servers] == ["a", "c"]


def test_the_stored_esxi_password_travels_with_its_server_and_is_never_logged(
    reader, profiles, caplog
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _profile(profiles, "b.yml", {"esxi": {"ip": "10.1.1.2"}})
    _secret(
        profiles, "a.yml", esxi_password="s3cret-for-a", gns3_password="ssh-only-secret"
    )

    with caplog.at_level(logging.DEBUG):
        servers = {
            s["name"]: s
            for s in reader.servers_from_profiles(str(profiles), logging.getLogger("t"))
        }

    assert servers["a"]["esxi_password"] == "s3cret-for-a"
    assert "esxi_password" not in servers["b"]
    assert "ssh-only-secret" not in json.dumps(
        servers
    )  # the GNS3 (ssh) password is not the ESXi login
    assert "s3cret" not in " ".join(r.getMessage() for r in caplog.records)


def test_the_secrets_folder_is_not_mistaken_for_profiles(reader, profiles):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _secret(profiles, "a.yml", esxi_password="x")
    assert [
        s["name"]
        for s in reader.servers_from_profiles(str(profiles), logging.getLogger("t"))
    ] == ["a"]


# ---------- running it ----------


def _run(reader, monkeypatch, tmp_path, *args: str, servers=None, read_server=None):
    config = {
        "output_file": str(tmp_path / "out" / "gns3_templates.yml"),
        "log_file": str(tmp_path / "reader.log"),
    }
    if servers is not None:
        config["servers"] = servers
    config_file = tmp_path / "config.yml"
    config_file.write_text(yaml.safe_dump(config))
    calls = []

    def fake(server_cfg, timeout, log, esxi_password):
        calls.append((server_cfg["name"], esxi_password))
        return {
            "name": server_cfg["name"],
            "esxi_host": server_cfg["esxi_host"],
            "status": "ok",
            "templates": [{"n": 1}],
        }

    monkeypatch.setattr(reader, "read_server", read_server or fake)
    monkeypatch.setattr(
        sys, "argv", ["read_gns3_templates.py", "--config", str(config_file), *args]
    )
    code = reader.main()
    return code, calls, Path(config["output_file"])


def test_a_run_with_profiles_uses_each_hosts_own_password_and_falls_back_to_the_environment(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _profile(profiles, "b.yml", {"esxi": {"ip": "10.1.1.2"}})
    _secret(profiles, "a.yml", esxi_password="own-a")
    monkeypatch.setenv("ESXI_PASSWORD", "from-env")

    code, calls, output = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles)
    )

    assert code == 0
    assert calls == [("a", "own-a"), ("b", "from-env")]
    assert [s["name"] for s in yaml.safe_load(output.read_text())["servers"]] == [
        "a",
        "b",
    ]


def test_a_profile_host_without_any_password_is_reported_and_the_others_still_run(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _profile(profiles, "b.yml", {"esxi": {"ip": "10.1.1.2"}})
    _secret(profiles, "b.yml", esxi_password="own-b")

    code, calls, output = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles)
    )

    assert code == 0
    assert calls == [("b", "own-b")]
    by_name = {s["name"]: s for s in yaml.safe_load(output.read_text())["servers"]}
    assert by_name["a"]["status"] == "esxi_unreachable"
    assert "no ESXi password" in by_name["a"]["error"]
    assert by_name["b"]["status"] == "ok"


def test_the_profiles_folder_can_come_from_the_environment(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _secret(profiles, "a.yml", esxi_password="own-a")
    monkeypatch.setenv("HOST_PROFILES_DIR", str(profiles))

    code, calls, _ = _run(
        reader, monkeypatch, tmp_path
    )  # no --profiles-dir, no servers in the config

    assert code == 0 and calls == [("a", "own-a")]


def test_a_missing_profiles_folder_stops_the_run(reader, monkeypatch, tmp_path):
    code, calls, output = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(tmp_path / "nope")
    )
    assert code == 1 and calls == [] and not output.exists()


def test_without_profiles_the_config_servers_and_the_environment_password_work_as_before(
    reader, monkeypatch, tmp_path
):
    servers = [{"name": "s1", "esxi_host": "10.9.9.9"}]
    monkeypatch.setenv("ESXI_PASSWORD", "from-env")

    code, calls, _ = _run(reader, monkeypatch, tmp_path, servers=servers)

    assert code == 0 and calls == [("s1", "from-env")]


def test_without_profiles_a_missing_environment_password_still_aborts(
    reader, monkeypatch, tmp_path
):
    code, calls, output = _run(
        reader, monkeypatch, tmp_path, servers=[{"name": "s1", "esxi_host": "10.9.9.9"}]
    )
    assert code == 1 and calls == [] and not output.exists()
