#!/usr/bin/env python3
"""
Sammelt alle Template-YAML-Dateien aus dem NFS-Share (über die Filebrowser-API),
validiert sie, erkennt Duplikate und schreibt eine zusammengefasste templates.yml.

Erwartetes Format je Quell-Datei:

    version: 1
    templates:
      - id: ...
        name: ...
        file: ...
        ...

Abhängigkeiten installieren:
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

# ---------- Konfiguration ----------
FILEBROWSER_URL = os.environ.get("FILEBROWSER_URL", "http://10.20.20.171:4000")
USERNAME = os.environ.get("FILEBROWSER_USER", "admin")
# Passwort per Umgebungsvariable setzen, nicht hart im Script:
#   export FILEBROWSER_PASSWORD='DeinPasswort!123'
PASSWORD = os.environ.get("FILEBROWSER_PASSWORD", "")

OUTPUT_FILE = Path("/home/junioradmin/yaml-api/data/templates.yml")
LOG_FILE = Path("/home/junioradmin/yaml-api/logs/collect.log")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

REQUEST_TIMEOUT = 15

# Pflichtfelder, die jedes Template haben muss, um übernommen zu werden
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
    """Richtet Logging auf Konsole + rotierende Log-Datei ein."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("template_collector")
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Konsole (wichtig für systemd/journalctl)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # Datei, max. 5 MB, 5 Backups
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


log = setup_logging()


def login() -> str:
    """Loggt sich bei Filebrowser ein und gibt den Auth-Token zurück."""
    try:
        resp = requests.post(
            f"{FILEBROWSER_URL}/api/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Login fehlgeschlagen: %s", e)
        raise
    return resp.text.strip().strip('"')


def list_dir(token: str, path: str = "") -> dict:
    """Listet den Inhalt eines Verzeichnisses via API."""
    url = f"{FILEBROWSER_URL}/api/resources/{path}"
    resp = requests.get(url, headers={"X-Auth": token}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_raw_file(token: str, path: str) -> str:
    """Lädt den Rohinhalt einer Datei via API."""
    url = f"{FILEBROWSER_URL}/api/raw/{path}"
    resp = requests.get(url, headers={"X-Auth": token}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def find_yml_files(token: str, path: str = "") -> list[str]:
    """Durchsucht rekursiv alle Ordner nach .yml/.yaml Dateien."""
    yml_files = []
    try:
        data = list_dir(token, path)
    except requests.RequestException as e:
        log.error("Konnte Verzeichnis '%s' nicht auflisten: %s", path or "/", e)
        return yml_files

    for item in data.get("items", []):
        item_path = f"{path}/{item['name']}".lstrip("/")
        if item.get("isDir"):
            yml_files.extend(find_yml_files(token, item_path))
        elif item["name"].lower().endswith((".yml", ".yaml")):
            yml_files.append(item_path)

    return yml_files


def validate_template(template: dict, source: str) -> list[str]:
    """Prüft ein einzelnes Template auf Pflichtfelder und grobe Typkonsistenz.
    Gibt eine Liste von Fehlermeldungen zurück (leer = gültig)."""
    errors = []

    if not isinstance(template, dict):
        return [f"Eintrag ist kein Objekt/Dict: {template!r}"]

    for field in REQUIRED_FIELDS:
        if field not in template or template[field] in (None, ""):
            errors.append(f"Pflichtfeld '{field}' fehlt oder ist leer")

    # id sollte ein nicht-leerer String sein
    if "id" in template and not isinstance(template["id"], str):
        errors.append("'id' sollte ein String sein")

    # cpu sollte eine Zahl sein
    if "cpu" in template and not isinstance(template["cpu"], (int, float)):
        errors.append("'cpu' sollte eine Zahl sein")

    # tags/users/passwords sollten, falls vorhanden, Listen sein
    for list_field in ("tags", "users", "passwords"):
        if list_field in template and not isinstance(template[list_field], list):
            errors.append(f"'{list_field}' sollte eine Liste sein")

    if errors:
        for err in errors:
            log.warning(
                "  Validierungsfehler in %s (id=%s, name=%s): %s",
                source,
                template.get("id", "?"),
                template.get("name", "?"),
                err,
            )

    return errors


def collect_templates(token: str, yml_files: list[str]) -> tuple[list[dict], dict]:
    """Liest alle YAML-Dateien, validiert und dedupliziert nach 'id'.
    Gibt (gültige_templates, statistik) zurück."""
    seen_ids: dict[str, str] = {}  # id -> Quell-Datei des ersten Fundes
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
        log.info("Lese: %s", yml_path)
        try:
            raw = get_raw_file(token, yml_path)
            parsed = yaml.safe_load(raw)
        except requests.RequestException as e:
            log.error("  Konnte Datei nicht laden: %s", e)
            stats["files_failed"] += 1
            continue
        except yaml.YAMLError as e:
            log.error("  Konnte YAML nicht parsen: %s", e)
            stats["files_failed"] += 1
            continue

        if not isinstance(parsed, dict) or "templates" not in parsed:
            log.warning("  Kein 'templates'-Schlüssel gefunden, übersprungen.")
            stats["files_failed"] += 1
            continue

        templates = parsed["templates"]
        if not isinstance(templates, list):
            log.warning("  'templates' ist keine Liste, übersprungen.")
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
                    "  Duplikat-ID '%s' (name=%s) in %s — bereits vorhanden aus %s, "
                    "wird übersprungen.",
                    template_id,
                    template.get("name", "?"),
                    yml_path,
                    seen_ids[template_id],
                )
                stats["templates_duplicate"] += 1
                continue

            # Merkt sich den Ordner der Quell-YAML, damit der Downloader später
            # weiß, in welchem Verzeichnis die zugehörige .ova-Datei liegt.
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
    tmp_path.replace(OUTPUT_FILE)  # atomarer Austausch, kein halbfertiges File bei Absturz


def main() -> int:
    log.info("=" * 60)
    log.info("Starte Template-Sammlung")

    if not PASSWORD:
        log.error(
            "FILEBROWSER_PASSWORD ist nicht gesetzt. Abbruch. "
            "Bitte per Umgebungsvariable setzen."
        )
        return 1

    try:
        token = login()
    except requests.RequestException:
        log.error("Abbruch: Login fehlgeschlagen.")
        return 1

    log.info("Suche nach .yml/.yaml Dateien im Share...")
    yml_files = find_yml_files(token)
    log.info("%d YAML-Datei(en) gefunden.", len(yml_files))

    templates, stats = collect_templates(token, yml_files)

    try:
        write_output(templates)
    except OSError as e:
        log.error("Konnte Ausgabedatei nicht schreiben: %s", e)
        return 1

    log.info("-" * 60)
    log.info("Zusammenfassung:")
    log.info("  Dateien gesamt:        %d", stats["files_total"])
    log.info("  Dateien fehlgeschlagen: %d", stats["files_failed"])
    log.info("  Templates gefunden:    %d", stats["templates_found"])
    log.info("  Templates gültig:      %d", stats["templates_valid"])
    log.info("  Templates ungültig:    %d", stats["templates_invalid"])
    log.info("  Duplikate übersprungen: %d", stats["templates_duplicate"])
    log.info("  Geschrieben nach:      %s", OUTPUT_FILE)
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
