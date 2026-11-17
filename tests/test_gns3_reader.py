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
    monkeypatch.delenv("OPERATOR_URL", raising=False)
    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
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


class FakeResponse:
    def __init__(self, status=200, body=None, reason="OK"):
        self.status_code = status
        self._body = body
        self.reason = reason

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _operator(reader, monkeypatch, answers):
    """Replaces the HTTP call to the Operator; answers maps an ip to a FakeResponse or an exception."""
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append({"url": url, "ip": params["ip"], "headers": headers})
        answer = answers[params["ip"]]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(reader.requests, "get", fake_get)
    monkeypatch.setenv("OPERATOR_URL", "http://operator.test:8123/")
    monkeypatch.setenv("OPERATOR_TOKEN", "tok-" + "x" * 30)
    return seen


def test_a_host_with_its_own_password_does_not_ask_the_operator_and_the_environment_password_is_ignored(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _secret(profiles, "a.yml", esxi_password="own-a")
    monkeypatch.setenv("ESXI_PASSWORD", "from-env")
    seen = _operator(reader, monkeypatch, {})

    code, calls, _ = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles)
    )

    assert code == 0 and calls == [("a", "own-a")] and seen == []


def test_a_host_without_its_own_password_gets_its_login_from_the_operator(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _profile(profiles, "b.yml", {"esxi": {"ip": "10.1.1.2"}})
    _secret(profiles, "a.yml", esxi_password="own-a")
    monkeypatch.setenv("ESXI_PASSWORD", "from-env")  # must never be used
    seen = _operator(
        reader,
        monkeypatch,
        {
            "10.1.1.2": FakeResponse(
                200, {"username": "admin", "password": "from-operator"}
            )
        },
    )
    cfgs = []

    def fake(server_cfg, timeout, log, esxi_password):
        cfgs.append(server_cfg)
        return {
            "name": server_cfg["name"],
            "esxi_host": server_cfg["esxi_host"],
            "status": "ok",
            "templates": [],
        }

    code, _, output = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles), read_server=fake
    )

    assert code == 0
    assert [(c["name"], c["esxi_username"]) for c in cfgs] == [
        ("a", "root"),
        ("b", "admin"),
    ]
    assert [s["ip"] for s in seen] == ["10.1.1.2"]
    assert seen[0]["url"] == "http://operator.test:8123/internal/host-credentials"
    assert seen[0]["headers"] == {"Authorization": "Bearer tok-" + "x" * 30}
    assert [s["name"] for s in yaml.safe_load(output.read_text())["servers"]] == [
        "a",
        "b",
    ]


def test_the_operators_password_is_what_read_server_receives(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _operator(
        reader,
        monkeypatch,
        {"10.1.1.1": FakeResponse(200, {"username": "root", "password": "pw-op"})},
    )
    code, calls, _ = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles)
    )
    assert code == 0 and calls == [("a", "pw-op")]


def test_without_a_password_and_without_an_operator_the_host_is_skipped_with_the_exact_text(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _profile(profiles, "b.yml", {"esxi": {"ip": "10.1.1.2"}})
    _secret(profiles, "b.yml", esxi_password="own-b")
    monkeypatch.setenv("ESXI_PASSWORD", "from-env")

    code, calls, output = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles)
    )

    assert code == 0 and calls == [("b", "own-b")]
    by_name = {s["name"]: s for s in yaml.safe_load(output.read_text())["servers"]}
    assert by_name["a"]["status"] == "esxi_unreachable"
    assert "no ESXi login" in by_name["a"]["error"]
    assert by_name["b"]["status"] == "ok"
    log_text = (tmp_path / "reader.log").read_text()
    assert (
        "Server a: no ESXi login - set esxi_password in the config or OPERATOR_URL + OPERATOR_TOKEN "
        "so the Operator can be asked." in log_text
    )


def test_an_operator_error_is_reported_with_its_status_and_detail_and_the_other_hosts_still_run(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _profile(profiles, "b.yml", {"esxi": {"ip": "10.1.1.2"}})
    detail = "No host profile with ESXi address 10.1.1.1 has a stored ESXi password - add the host in the Operator."
    _operator(
        reader,
        monkeypatch,
        {
            "10.1.1.1": FakeResponse(404, {"detail": detail}, "Not Found"),
            "10.1.1.2": FakeResponse(200, {"username": "root", "password": "pw-b"}),
        },
    )

    code, calls, output = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles)
    )

    assert code == 0 and calls == [("b", "pw-b")]
    log_text = (tmp_path / "reader.log").read_text()
    assert (
        f"Server a: the Operator answered HTTP 404 for 10.1.1.1 ({detail}) - add the host in the Operator."
        in log_text
    )
    by_name = {s["name"]: s for s in yaml.safe_load(output.read_text())["servers"]}
    assert by_name["a"]["status"] == "esxi_unreachable"


def test_an_unreachable_operator_is_reported_without_the_token(
    reader, monkeypatch, tmp_path, profiles
):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _operator(
        reader, monkeypatch, {"10.1.1.1": reader.requests.ConnectionError("boom")}
    )

    code, calls, _ = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles)
    )

    assert code == 0 and calls == []
    log_text = (tmp_path / "reader.log").read_text()
    assert (
        "Server a: the Operator at http://operator.test:8123/ could not be reached (ConnectionError) - no ESXi login."
        in log_text
    )
    assert "tok-" not in log_text


def test_the_password_never_reaches_the_log(reader, monkeypatch, tmp_path, profiles):
    _profile(profiles, "a.yml", {"esxi": {"ip": "10.1.1.1"}})
    _operator(
        reader,
        monkeypatch,
        {"10.1.1.1": FakeResponse(200, {"username": "root", "password": "secret-pw"})},
    )
    _run(reader, monkeypatch, tmp_path, "--profiles-dir", str(profiles))
    assert "secret-pw" not in (tmp_path / "reader.log").read_text()


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


def test_config_servers_use_their_own_password_or_ask_the_operator(
    reader, monkeypatch, tmp_path
):
    servers = [
        {"name": "s1", "esxi_host": "10.9.9.1", "esxi_password": "in-config"},
        {"name": "s2", "esxi_host": "10.9.9.2"},
    ]
    monkeypatch.setenv("ESXI_PASSWORD", "from-env")  # not read any more
    _operator(
        reader,
        monkeypatch,
        {"10.9.9.2": FakeResponse(200, {"username": "root", "password": "pw2"})},
    )

    code, calls, _ = _run(reader, monkeypatch, tmp_path, servers=servers)

    assert code == 0 and calls == [("s1", "in-config"), ("s2", "pw2")]


def test_config_servers_without_any_login_source_are_skipped_not_a_failed_run(
    reader, monkeypatch, tmp_path
):
    monkeypatch.setenv("ESXI_PASSWORD", "from-env")
    code, calls, output = _run(
        reader, monkeypatch, tmp_path, servers=[{"name": "s1", "esxi_host": "10.9.9.9"}]
    )
    assert code == 0 and calls == []
    [server] = yaml.safe_load(output.read_text())["servers"]
    assert server["status"] == "esxi_unreachable"


def test_a_missing_profiles_folder_means_no_hosts_not_a_failed_run(
    reader, monkeypatch, tmp_path
):
    # a fresh machine: the app has not saved a host profile yet, so its folder does not exist
    code, calls, output = _run(
        reader, monkeypatch, tmp_path, "--profiles-dir", str(tmp_path / "not-there")
    )

    assert code == 0
    assert calls == []
    assert yaml.safe_load(output.read_text()) == {"version": 1, "servers": []}


def test_a_run_that_reads_no_server_keeps_the_last_good_output(
    reader, monkeypatch, tmp_path
):
    servers = [{"name": "a", "esxi_host": "10.0.0.1"}]
    code, _, output = _run(reader, monkeypatch, tmp_path, servers=servers)
    assert code == 0
    good = output.read_text()

    def unreachable(server_cfg, timeout, log, esxi_password):
        return {
            "name": server_cfg["name"],
            "esxi_host": server_cfg["esxi_host"],
            "status": "unreachable",
            "templates": [],
        }

    code, _, output = _run(
        reader, monkeypatch, tmp_path, servers=servers, read_server=unreachable
    )
    assert code == 1
    assert output.read_text() == good


def test_a_first_run_that_reads_no_server_still_writes_the_file(
    reader, monkeypatch, tmp_path
):
    def unreachable(server_cfg, timeout, log, esxi_password):
        return {
            "name": server_cfg["name"],
            "esxi_host": server_cfg["esxi_host"],
            "status": "unreachable",
            "templates": [],
        }

    code, _, output = _run(
        reader,
        monkeypatch,
        tmp_path,
        servers=[{"name": "a", "esxi_host": "10.0.0.1"}],
        read_server=unreachable,
    )
    assert code == 0 and output.exists()
