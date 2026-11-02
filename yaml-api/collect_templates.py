#!/usr/bin/env python3
"""
Collects all template YAML files from the NFS share (via the Filebrowser API, or straight from a local
directory when TEMPLATE_STORE_DIR is set), validates them, detects duplicates, and writes a combined templates.yml.

Expected format for each source file:

    version: 1
    templates:
      - id: ...
        name: ...
        file: ...
        ...

Install dependencies:
    pip3 install requests pyyaml
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml

# ---------- Configuration ----------
FILEBROWSER_URL = os.environ.get("FILEBROWSER_URL", "http://10.20.20.171:4000")
USERNAME = os.environ.get("FILEBROWSER_USER", "admin")
# Set the password via an environment variable, not hardcoded in the script:
#   export FILEBROWSER_PASSWORD='YourPassword!123'
PASSWORD = os.environ.get("FILEBROWSER_PASSWORD", "")

# Where the templates are read from: a directory (e.g. the NFS share mounted on this machine) when
# TEMPLATE_STORE_DIR is set - no Filebrowser needed then - otherwise through Filebrowser as before.
STORE_DIR = os.environ.get("TEMPLATE_STORE_DIR", "")

OUTPUT_FILE = Path(
    os.environ.get("TEMPLATES_OUTPUT_FILE", "/home/junioradmin/yaml-api/data/templates.yml")
)
LOG_FILE = Path(
    os.environ.get("TEMPLATES_LOG_FILE", "/home/junioradmin/yaml-api/logs/collect.log")
)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

REQUEST_TIMEOUT = 15

# Required fields every template must have to be accepted
REQUIRED_FIELDS = [
    "id",
    "name",
    "file",
    "category",
    "os",
    "cpu",
    "memory",
    "disk",
    "hostname",
]
# ------------------------------------


def setup_logging() -> logging.Logger:
    """Sets up logging to console + a rotating log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("template_collector")
    if logger.handlers:  # already set up (main() ran before in this process)
        return logger
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console (important for systemd/journalctl)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # File, max 5 MB, 5 backups
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


# The handlers (and the log directory) are only created when the collector actually runs, see main().
log = logging.getLogger("template_collector")


def login() -> str:
    """Logs into Filebrowser and returns the auth token."""
    try:
        resp = requests.post(
            f"{FILEBROWSER_URL}/api/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Login failed: %s", e)
        raise
    return resp.text.strip().strip('"')


def list_dir(token: str, path: str = "") -> dict:
    """Lists the contents of a directory via the API."""
    url = f"{FILEBROWSER_URL}/api/resources/{path}"
    resp = requests.get(url, headers={"X-Auth": token}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_raw_file(token: str, path: str) -> str:
    """Loads the raw content of a file via the API."""
    url = f"{FILEBROWSER_URL}/api/raw/{path}"
    resp = requests.get(url, headers={"X-Auth": token}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def find_yml_files(token: str, path: str = "") -> list[str]:
    """Recursively searches all folders for .yml/.yaml files."""
    yml_files = []
    try:
        data = list_dir(token, path)
    except requests.RequestException as e:
        log.error("Could not list directory '%s': %s", path or "/", e)
        return yml_files

    for item in data.get("items", []):
        item_path = f"{path}/{item['name']}".lstrip("/")
        if item.get("isDir"):
            yml_files.extend(find_yml_files(token, item_path))
        elif item["name"].lower().endswith((".yml", ".yaml")):
            yml_files.append(item_path)

    return yml_files


class FilebrowserSource:
    """The YAML files of the share, read through Filebrowser's API."""

    def __init__(self) -> None:
        self.token = login()

    def find_yml_files(self) -> list[str]:
        return find_yml_files(self.token)

    def read(self, path: str) -> str:
        return get_raw_file(self.token, path)


class DirectorySource:
    """The YAML files below a local directory (the share mounted here); paths are relative, with "/"."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def find_yml_files(self) -> list[str]:
        found = []
        for folder, _dirs, names in os.walk(self.root):  # symlinked folders are not followed
            for name in names:
                if name.lower().endswith((".yml", ".yaml")):
                    found.append((Path(folder) / name).relative_to(self.root).as_posix())
        return sorted(found)

    def read(self, path: str) -> str:
        file = (self.root / path).resolve()
        if not file.is_relative_to(self.root.resolve()):
            raise OSError(f"{path!r} is outside of the template directory")
        return file.read_text(encoding="utf-8")


def validate_template(template: dict, source: str) -> list[str]:
    """Checks a single template for required fields and rough type
    consistency. Returns a list of error messages (empty = valid)."""
    errors = []

    if not isinstance(template, dict):
        return [f"Entry is not an object/dict: {template!r}"]

    for field in REQUIRED_FIELDS:
        if field not in template or template[field] in (None, ""):
            errors.append(f"Required field '{field}' is missing or empty")

    # id should be a non-empty string
    if "id" in template and not isinstance(template["id"], str):
        errors.append("'id' should be a string")

    # cpu should be a number
    if "cpu" in template and not isinstance(template["cpu"], (int, float)):
        errors.append("'cpu' should be a number")

    # tags/users/passwords should be lists, if present
    for list_field in ("tags", "users", "passwords"):
        if list_field in template and not isinstance(template[list_field], list):
            errors.append(f"'{list_field}' should be a list")

    if errors:
        for err in errors:
            log.warning(
                "  Validation error in %s (id=%s, name=%s): %s",
                source,
                template.get("id", "?"),
                template.get("name", "?"),
                err,
            )

    return errors


def collect_templates(source, yml_files: list[str]) -> tuple[list[dict], dict]:
    """Reads all YAML files, validates and deduplicates by 'id'.
    Returns (valid_templates, stats)."""
    seen_ids: dict[str, str] = {}  # id -> source file of the first occurrence
    valid_templates: list[dict] = []

    stats = {
        "files_total": len(yml_files),
        "files_failed": 0,
        "templates_found": 0,
        "templates_valid": 0,
        "templates_invalid": 0,
        "templates_duplicate": 0,
    }

    for yml_path in yml_files:
        log.info("Reading: %s", yml_path)
        try:
            raw = source.read(yml_path)
            parsed = yaml.safe_load(raw)
        except (requests.RequestException, OSError, UnicodeDecodeError) as e:
            log.error("  Could not load file: %s", e)
            stats["files_failed"] += 1
            continue
        except yaml.YAMLError as e:
            log.error("  Could not parse YAML: %s", e)
            stats["files_failed"] += 1
            continue

        if not isinstance(parsed, dict) or "templates" not in parsed:
            log.warning("  No 'templates' key found, skipped.")
            stats["files_failed"] += 1
            continue

        templates = parsed["templates"]
        if not isinstance(templates, list):
            log.warning("  'templates' is not a list, skipped.")
            stats["files_failed"] += 1
            continue

        for template in templates:
            stats["templates_found"] += 1

            errors = validate_template(template, yml_path)
            if errors:
                stats["templates_invalid"] += 1
                continue

            template_id = template["id"]
            if template_id in seen_ids:
                log.warning(
                    "  Duplicate id '%s' (name=%s) in %s - already present "
                    "from %s, skipping.",
                    template_id,
                    template.get("name", "?"),
                    yml_path,
                    seen_ids[template_id],
                )
                stats["templates_duplicate"] += 1
                continue

            # Remember the source YAML's folder, so the downloader later
            # knows which directory the associated .ova file lives in.
            template["_source_dir"] = yml_path.rsplit("/", 1)[0] if "/" in yml_path else ""

            seen_ids[template_id] = yml_path
            valid_templates.append(template)
            stats["templates_valid"] += 1

    return valid_templates, stats


def write_output(templates: list[dict]) -> None:
    output = {
        "version": 1,
        "templates": templates,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = OUTPUT_FILE.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(output, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp_path.replace(OUTPUT_FILE)  # atomic swap, no half-written file on a crash


def main() -> int:
    setup_logging()
    log.info("=" * 60)
    log.info("Starting template collection")

    if STORE_DIR:
        if not Path(STORE_DIR).is_dir():
            log.error("TEMPLATE_STORE_DIR %s is not a directory. Aborting.", STORE_DIR)
            return 1
        log.info("Reading the templates from the directory %s", STORE_DIR)
        source = DirectorySource(STORE_DIR)
    else:
        if not PASSWORD:
            log.error(
                "FILEBROWSER_PASSWORD is not set. Aborting. "
                "Please set it via an environment variable."
            )
            return 1

        try:
            source = FilebrowserSource()
        except requests.RequestException:
            log.error("Aborting: login failed.")
            return 1

    log.info("Searching for .yml/.yaml files on the share...")
    yml_files = source.find_yml_files()
    log.info("%d YAML file(s) found.", len(yml_files))

    templates, stats = collect_templates(source, yml_files)

    try:
        write_output(templates)
    except OSError as e:
        log.error("Could not write output file: %s", e)
        return 1

    log.info("-" * 60)
    log.info("Summary:")
    log.info("  Files total:            %d", stats["files_total"])
    log.info("  Files failed:           %d", stats["files_failed"])
    log.info("  Templates found:        %d", stats["templates_found"])
    log.info("  Templates valid:        %d", stats["templates_valid"])
    log.info("  Templates invalid:      %d", stats["templates_invalid"])
    log.info("  Duplicates skipped:     %d", stats["templates_duplicate"])
    log.info("  Written to:             %s", OUTPUT_FILE)
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
