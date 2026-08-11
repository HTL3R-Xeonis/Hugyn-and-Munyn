#!/usr/bin/env python3
"""
Liest von allen in der Konfigurationsdatei angegebenen GNS3-Servern
sämtliche Templates aus und gleicht bei Docker-Templates zusätzlich ab,
ob das referenzierte Image auf dem jeweiligen Server tatsächlich
vorhanden (gepullt) ist.

Komplett unabhängig von anderer Infrastruktur (z.B. Filebrowser/NFS-Setup).

Verwendung:
    ./venv/bin/python read_gns3_templates.py --config gns3_config.yml

Abhängigkeiten installieren:
    pip3 install requests pyyaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import requests
import yaml

DEFAULT_CONFIG_PATH = "gns3_config.yml"
DEFAULT_TIMEOUT = 15


def setup_logging(log_file: Optional[str]) -> logging.Logger:
    """Richtet Logging auf Konsole + optional rotierende Log-Datei ein."""
    logger = logging.getLogger("gns3_template_reader")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"FEHLER: Config-Datei nicht gefunden: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not config or "servers" not in config or not config["servers"]:
        print("FEHLER: Config enthält keine Server unter 'servers'.", file=sys.stderr)
        sys.exit(1)

    return config


def get_templates(base_url: str, auth: Optional[tuple], timeout: int, log: logging.Logger) -> list[dict]:
    """Holt ALLE Templates von einem GNS3-Server (ungefiltert, inkl. eingebauter)."""
    url = f"{base_url}/v2/templates"
    resp = requests.get(url, auth=auth, timeout=timeout)
    resp.raise_for_status()
    templates = resp.json()
    log.info("  %d Template(s) gefunden.", len(templates))
    return templates


def get_docker_images(base_url: str, auth: Optional[tuple], timeout: int, log: logging.Logger) -> list[dict]:
    """Holt die Liste der auf dem Compute tatsächlich vorhandenen Docker-Images."""
    url = f"{base_url}/v2/compute/docker/images"
    try:
        resp = requests.get(url, auth=auth, timeout=timeout)
        resp.raise_for_status()
        images = resp.json()
        log.info("  %d Docker-Image(s) auf dem Compute gefunden.", len(images))
        return images
    except requests.RequestException as e:
        log.warning("  Konnte Docker-Images nicht abrufen (evtl. kein Docker-Compute): %s", e)
        return []


def extract_available_image_names(images_data: list[dict]) -> set[str]:
    """Extrahiert alle bekannten Image-Namen/Tags aus der Docker-Images-Antwort,
    unabhängig vom genauen Antwortformat (verschiedene GNS3-Versionen liefern
    teils leicht unterschiedliche Felder)."""
    names: set[str] = set()
    for img in images_data:
        if not isinstance(img, dict):
            continue
        if img.get("image"):
            names.add(img["image"])
        repo_tags = img.get("RepoTags")
        if isinstance(repo_tags, list):
            names.update(repo_tags)
    return names


def enrich_templates_with_docker_status(
    templates: list[dict], available_images: set[str]
) -> None:
    """Fügt Docker-Templates ein Feld 'image_available' hinzu (True/False/None)."""
    for template in templates:
        if template.get("template_type") == "docker":
            image_name = template.get("image")
            template["image_available"] = (
                image_name in available_images if image_name else None
            )


def read_server(server_cfg: dict, timeout: int, log: logging.Logger) -> dict:
    name = server_cfg.get("name", server_cfg["host"])
    host = server_cfg["host"]
    port = server_cfg.get("port", 3080)
    base_url = f"http://{host}:{port}"

    username = server_cfg.get("username")
    password = server_cfg.get("password")
    auth = (username, password) if username and password else None

    log.info("Server '%s' (%s):", name, base_url)

    result = {
        "name": name,
        "host": host,
        "port": port,
        "status": "ok",
        "templates": [],
    }

    try:
        templates = get_templates(base_url, auth, timeout, log)
    except requests.RequestException as e:
        log.error("  Konnte Templates nicht abrufen: %s", e)
        result["status"] = "unreachable"
        result["error"] = str(e)
        return result

    docker_images = get_docker_images(base_url, auth, timeout, log)
    available_images = extract_available_image_names(docker_images)
    enrich_templates_with_docker_status(templates, available_images)

    result["templates"] = templates
    result["docker_images_on_compute"] = sorted(available_images)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Liest Templates von mehreren GNS3-Servern aus (per Config)."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Pfad zur Config-Datei (Standard: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)

    log = setup_logging(config.get("log_file"))
    timeout = config.get("request_timeout", DEFAULT_TIMEOUT)

    log.info("=" * 60)
    log.info("Starte GNS3-Template-Auslesung für %d Server", len(config["servers"]))

    all_results = []
    for server_cfg in config["servers"]:
        all_results.append(read_server(server_cfg, timeout, log))

    output = {
        "version": 1,
        "servers": all_results,
    }

    output_file = config.get("output_file", "gns3_templates.yml")
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(output, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp_path.replace(output_path)

    log.info("-" * 60)
    total_templates = sum(len(r["templates"]) for r in all_results)
    reachable = sum(1 for r in all_results if r["status"] == "ok")
    log.info("Server erreichbar:     %d / %d", reachable, len(all_results))
    log.info("Templates gesamt:      %d", total_templates)
    log.info("Geschrieben nach:      %s", output_path)
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
