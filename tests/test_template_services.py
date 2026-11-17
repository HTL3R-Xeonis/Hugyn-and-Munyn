"""
The template services run from a local template directory (no Filebrowser needed) and report their health.

Needs pytest, fastapi (with httpx for TestClient), pyyaml and requests. Each service module is loaded fresh with its
own environment, because they read their settings when they are imported.
"""

import importlib.util
import itertools
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
_counter = itertools.count()


def _load(relative: str, monkeypatch, **env: str):
    for key in (
        "TEMPLATE_STORE_DIR",
        "DATA_DIR",
        "FILEBROWSER_URL",
        "FILEBROWSER_USER",
        "FILEBROWSER_PASSWORD",
        "TEMPLATES_OUTPUT_FILE",
        "TEMPLATES_LOG_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(
        f"service_{next(_counter)}", ROOT / relative
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _template(template_id="deb12", **overrides):
    template = {
        "id": template_id,
        "name": f"Template {template_id}",
        "file": f"{template_id}.ova",
        "category": "linux",
        "os": {"family": "debian", "version": "12"},
        "cpu": 2,
        "memory": 2048,
        "disk": "20GB",
        "hostname": template_id,
    }
    template.update(overrides)
    return template


def _write_yaml(path: Path, *templates: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "templates": list(templates)}))


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "store"
    _write_yaml(root / "debian" / "debian.yml", _template("deb12"))
    (root / "debian" / "deb12.ova").write_bytes(b"OVA-BYTES-1234")
    _write_yaml(root / "top.yaml", _template("top1"))
    (root / "notes.txt").write_text("not a template file")
    return root


# ---------- the collector ----------


def _collector(monkeypatch, **env):
    return _load("yaml-api/collect_templates.py", monkeypatch, **env)


def test_the_directory_source_finds_nested_yaml_files_only(
    monkeypatch, store, tmp_path
):
    collector = _collector(monkeypatch)
    outside = tmp_path / "outside"
    _write_yaml(outside / "secret.yml", _template("leak"))
    (store / "link").symlink_to(outside, target_is_directory=True)

    found = collector.DirectorySource(store).find_yml_files()

    assert found == [
        "debian/debian.yml",
        "top.yaml",
    ]  # sorted, relative, no .txt, the symlinked folder not followed


def test_the_directory_source_refuses_a_path_outside_of_the_directory(
    monkeypatch, store, tmp_path
):
    collector = _collector(monkeypatch)
    _write_yaml(tmp_path / "elsewhere.yml", _template("x"))
    with pytest.raises(OSError, match="outside"):
        collector.DirectorySource(store).read("../elsewhere.yml")


def test_collecting_from_a_directory_validates_deduplicates_and_remembers_the_folder(
    monkeypatch, store
):
    collector = _collector(monkeypatch)
    _write_yaml(
        store / "more.yml", _template("deb12"), _template("broken", hostname="")
    )  # a duplicate and an invalid one
    (store / "bad.yml").write_text("templates: [unclosed")

    source = collector.DirectorySource(store)
    templates, stats = collector.collect_templates(source, source.find_yml_files())

    assert sorted(t["id"] for t in templates) == ["deb12", "top1"]
    by_id = {t["id"]: t for t in templates}
    assert by_id["deb12"]["_source_dir"] == "debian"  # where its .ova lives
    assert by_id["top1"]["_source_dir"] == ""
    assert stats["templates_duplicate"] == 1
    assert stats["templates_invalid"] == 1
    assert stats["files_failed"] == 1


def test_main_with_a_template_directory_writes_the_combined_file_without_filebrowser(
    monkeypatch, store, tmp_path
):
    out = tmp_path / "out" / "templates.yml"
    collector = _collector(
        monkeypatch,
        TEMPLATE_STORE_DIR=str(store),
        TEMPLATES_OUTPUT_FILE=str(out),
        TEMPLATES_LOG_FILE=str(tmp_path / "logs" / "collect.log"),
    )
    assert collector.main() == 0
    written = yaml.safe_load(out.read_text())
    assert sorted(t["id"] for t in written["templates"]) == ["deb12", "top1"]
    assert (tmp_path / "logs" / "collect.log").exists()


def test_importing_the_collector_creates_no_log_directory(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    _collector(monkeypatch, TEMPLATES_LOG_FILE=str(log_dir / "collect.log"))
    assert not log_dir.exists()


def test_main_stops_when_the_template_directory_is_missing(monkeypatch, tmp_path):
    collector = _collector(
        monkeypatch,
        TEMPLATE_STORE_DIR=str(tmp_path / "nope"),
        TEMPLATES_OUTPUT_FILE=str(tmp_path / "templates.yml"),
        TEMPLATES_LOG_FILE=str(tmp_path / "collect.log"),
    )
    assert collector.main() == 1
    assert not (tmp_path / "templates.yml").exists()


NO_SOURCE = (
    "Neither TEMPLATE_STORE_DIR nor FILEBROWSER_URL + FILEBROWSER_PASSWORD is set - the template list cannot be read. "
    "Set TEMPLATE_STORE_DIR (preferred) or the Filebrowser variables."
)


def _configured(tmp_path, **env):
    return {
        "TEMPLATES_OUTPUT_FILE": str(tmp_path / "templates.yml"),
        "TEMPLATES_LOG_FILE": str(tmp_path / "collect.log"),
        **env,
    }


def test_without_any_source_the_collector_refuses_to_run(monkeypatch, tmp_path, capsys):
    collector = _collector(monkeypatch, **_configured(tmp_path))
    assert collector.main() == 1
    assert capsys.readouterr().err.strip() == NO_SOURCE
    assert not (tmp_path / "templates.yml").exists()


@pytest.mark.parametrize(
    "env",
    [
        {"FILEBROWSER_URL": "http://fb.test", "FILEBROWSER_USER": "u"},  # no password
        {"FILEBROWSER_PASSWORD": "pw", "FILEBROWSER_USER": "u"},  # no url
    ],
)
def test_an_incomplete_filebrowser_configuration_is_refused_with_the_same_text(
    monkeypatch, tmp_path, capsys, env
):
    collector = _collector(monkeypatch, **_configured(tmp_path, **env))
    assert collector.main() == 1
    assert capsys.readouterr().err.strip() == NO_SOURCE


def test_a_filebrowser_configuration_without_a_user_is_refused(
    monkeypatch, tmp_path, capsys
):
    collector = _collector(
        monkeypatch,
        **_configured(
            tmp_path, FILEBROWSER_URL="http://fb.test", FILEBROWSER_PASSWORD="s3cret"
        ),
    )
    assert collector.main() == 1
    err = capsys.readouterr().err
    assert (
        err.strip()
        == "FILEBROWSER_USER is not set - the Filebrowser login needs a user name."
    )
    assert "s3cret" not in err


def test_the_collector_needs_an_output_file_and_a_log_file(
    monkeypatch, store, tmp_path, capsys
):
    collector = _collector(monkeypatch, TEMPLATE_STORE_DIR=str(store))
    assert collector.main() == 1
    assert (
        capsys.readouterr().err.strip()
        == "TEMPLATES_OUTPUT_FILE is not set - refusing to write into a default home folder."
    )
    collector = _collector(
        monkeypatch,
        TEMPLATE_STORE_DIR=str(store),
        TEMPLATES_OUTPUT_FILE=str(tmp_path / "templates.yml"),
    )
    assert collector.main() == 1
    assert (
        capsys.readouterr().err.strip()
        == "TEMPLATES_LOG_FILE is not set - refusing to write into a default home folder."
    )
    assert not (tmp_path / "templates.yml").exists()


def test_no_default_points_at_a_lab_host_or_a_home_folder(monkeypatch):
    collector = _collector(monkeypatch)
    assert collector.FILEBROWSER_URL == "" and collector.USERNAME == ""
    assert collector.PASSWORD == ""
    assert collector.OUTPUT_FILE is None and collector.LOG_FILE is None


def test_the_filebrowser_source_is_unchanged(monkeypatch):
    collector = _collector(monkeypatch)
    listing = {
        "": {
            "items": [{"name": "sub", "isDir": True}, {"name": "a.yml", "isDir": False}]
        },
        "sub": {
            "items": [
                {"name": "b.YAML", "isDir": False},
                {"name": "c.ova", "isDir": False},
            ]
        },
    }
    monkeypatch.setattr(collector, "login", lambda: "token")
    monkeypatch.setattr(collector, "list_dir", lambda token, path="": listing[path])
    monkeypatch.setattr(collector, "get_raw_file", lambda token, path: f"raw:{path}")

    source = collector.FilebrowserSource()
    assert source.find_yml_files() == ["sub/b.YAML", "a.yml"]
    assert source.read("a.yml") == "raw:a.yml"


# ---------- the ESXi template API ----------


def _api(monkeypatch, tmp_path, store=None, with_templates=True, **env):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if with_templates:
        _write_yaml(
            data / "templates.yml",
            _template("deb12", _source_dir="debian"),
            _template("top1", _source_dir=""),
        )
    settings = {"DATA_DIR": str(data), **env}
    if store is not None:
        settings["TEMPLATE_STORE_DIR"] = str(store)
    return TestClient(_load("yaml-api/app.py", monkeypatch, **settings).app)


def test_health_is_ok_with_the_templates_and_names_how_downloads_work(
    monkeypatch, tmp_path, store
):
    body = _api(monkeypatch, tmp_path, store).get("/health")
    assert body.status_code == 200
    assert body.json() == {
        "status": "ok",
        "templates": 2,
        "download": "directory",
        "problems": [],
    }


def test_health_in_filebrowser_mode(monkeypatch, tmp_path):
    body = (
        _api(
            monkeypatch,
            tmp_path,
            FILEBROWSER_URL="http://fb.test",
            FILEBROWSER_USER="u",
            FILEBROWSER_PASSWORD="pw",
        )
        .get("/health")
        .json()
    )
    assert body["status"] == "ok" and body["download"] == "filebrowser"


def test_health_is_503_without_a_directory_and_without_a_complete_filebrowser(
    monkeypatch, tmp_path
):
    response = _api(monkeypatch, tmp_path, FILEBROWSER_PASSWORD="s3cret").get("/health")
    assert response.status_code == 503
    assert response.json()["problems"] == [NO_SOURCE]
    assert "s3cret" not in response.text


def test_health_names_a_missing_filebrowser_user(monkeypatch, tmp_path):
    response = _api(
        monkeypatch,
        tmp_path,
        FILEBROWSER_URL="http://fb.test",
        FILEBROWSER_PASSWORD="pw",
    ).get("/health")
    assert response.status_code == 503
    assert response.json()["problems"] == [
        "FILEBROWSER_USER is not set - the Filebrowser login needs a user name."
    ]


def test_health_is_503_when_the_templates_file_is_missing(monkeypatch, tmp_path, store):
    response = _api(monkeypatch, tmp_path, store, with_templates=False).get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert "templates.yml" in response.json()["problems"][0]


def test_health_is_503_when_the_template_directory_is_missing(monkeypatch, tmp_path):
    response = _api(monkeypatch, tmp_path, tmp_path / "gone").get("/health")
    assert response.status_code == 503
    assert "gone" in response.json()["problems"][0]


def test_a_template_is_downloaded_straight_from_the_directory(
    monkeypatch, tmp_path, store
):
    response = _api(monkeypatch, tmp_path, store).get("/api/download/deb12")
    assert response.status_code == 200
    assert response.content == b"OVA-BYTES-1234"
    assert 'filename="deb12.ova"' in response.headers["content-disposition"]


def test_a_template_whose_file_is_not_in_the_directory_is_404(
    monkeypatch, tmp_path, store
):
    response = _api(monkeypatch, tmp_path, store).get(
        "/api/download/top1"
    )  # top1.ova was never created
    assert response.status_code == 404


def test_a_template_pointing_outside_of_the_directory_is_refused(
    monkeypatch, tmp_path, store
):
    (tmp_path / "secret.ova").write_bytes(b"SECRET")
    data = tmp_path / "data"
    data.mkdir()
    _write_yaml(
        data / "templates.yml", _template("evil", file="secret.ova", _source_dir="..")
    )
    client = TestClient(
        _load(
            "yaml-api/app.py",
            monkeypatch,
            DATA_DIR=str(data),
            TEMPLATE_STORE_DIR=str(store),
        ).app
    )
    response = client.get("/api/download/evil")
    assert response.status_code == 400
    assert b"SECRET" not in response.content


def test_without_a_directory_downloads_need_a_complete_filebrowser_configuration(
    monkeypatch, tmp_path
):
    response = _api(monkeypatch, tmp_path).get("/api/download/deb12")
    assert response.status_code == 500
    assert response.json()["detail"] == NO_SOURCE


# ---------- the GNS3 template API ----------


def test_the_gns3_api_health_counts_the_collected_templates(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gns3_templates.yml").write_text(
        yaml.safe_dump(
            {
                "servers": [
                    {"name": "s1", "templates": [{"name": "a"}, {"name": "b"}]},
                    {"name": "s2"},
                ]
            }
        )
    )
    client = TestClient(
        _load("gns3-template-api/gns3_api_app.py", monkeypatch, DATA_DIR=str(data)).app
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "templates": 2,
        "servers": 2,
        "problems": [],
    }


def test_the_gns3_api_health_is_503_without_the_collected_file(monkeypatch, tmp_path):
    client = TestClient(
        _load(
            "gns3-template-api/gns3_api_app.py", monkeypatch, DATA_DIR=str(tmp_path)
        ).app
    )
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_the_gns3_api_refresh_request_drops_the_trigger_file(monkeypatch, tmp_path):
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    client = TestClient(
        _load(
            "gns3-template-api/gns3_api_app.py",
            monkeypatch,
            DATA_DIR=str(tmp_path),
            TRIGGER_DIR=str(trigger),
        ).app
    )
    response = client.post("/api/refresh")
    assert response.status_code == 202
    assert response.json() == {"requested": True}
    assert (trigger / "refresh").exists()


def test_the_gns3_api_refresh_request_says_so_when_it_is_not_set_up(
    monkeypatch, tmp_path
):
    client = TestClient(
        _load(
            "gns3-template-api/gns3_api_app.py",
            monkeypatch,
            DATA_DIR=str(tmp_path),
            TRIGGER_DIR=str(tmp_path / "missing"),
        ).app
    )
    assert client.post("/api/refresh").status_code == 501


def test_the_template_api_refresh_request_drops_the_trigger_file(monkeypatch, tmp_path):
    trigger = tmp_path / "trigger"
    trigger.mkdir()
    client = TestClient(
        _load(
            "yaml-api/app.py",
            monkeypatch,
            DATA_DIR=str(tmp_path),
            TRIGGER_DIR=str(trigger),
        ).app
    )
    response = client.post("/api/refresh")
    assert response.status_code == 202
    assert (trigger / "refresh").exists()
    assert (
        TestClient(
            _load(
                "yaml-api/app.py",
                monkeypatch,
                DATA_DIR=str(tmp_path),
                TRIGGER_DIR=str(tmp_path / "missing"),
            ).app
        )
        .post("/api/refresh")
        .status_code
        == 501
    )
