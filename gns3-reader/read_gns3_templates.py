#!/usr/bin/env python3
"""
Liest von allen in der Konfigurationsdatei angegebenen GNS3-Servern
sämtliche Templates aus und gleicht bei Docker-Templates zusätzlich ab,
ob das referenzierte Image auf dem jeweiligen Server tatsächlich
vorhanden (gepullt) ist.

Jeder Server-Eintrag in der Config gibt einen ESXi-Host an, nicht direkt
eine GNS3-Server-IP - die GNS3-VM auf diesem Host wird bei jedem Lauf
frisch über die ESXi-API gesucht (Name enthält "gns3") und ihre aktuelle
IP verwendet, statt sich auf eine fest eingetragene, potenziell veraltete
IP zu verlassen (GNS3-VMs bekommen ihre IP oft per DHCP, und ein
'--fresh-gns3-vm'-Lauf von topologybuilder ersetzt die VM komplett -
beides macht eine statische IP unzuverlässig). Braucht deshalb zusätzlich
zu requests/pyyaml auch pyvmomi und ein ESXi-Passwort über die
Umgebungsvariable ESXI_PASSWORD (siehe .env.example).

Verwendung:
    ./venv/bin/python read_gns3_templates.py --config gns3_config.yml

Abhängigkeiten installieren:
    pip3 install -r requirements.txt
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import re
import ssl
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import requests
import yaml
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

DEFAULT_CONFIG_PATH = "gns3_config.yml"
DEFAULT_TIMEOUT = 15
ESXI_PORT = 443

# Matches the backup-name suffix a topologybuilder '--fresh-gns3-vm' run
# gives the VM it replaces (f"{vm_name}-backup-{timestamp:%Y%m%d%H%M%S}") -
# excluded from auto-detection so a powered-off backup is never mistaken
# for the live GNS3 VM. Same pattern topologybuilder itself uses.
_BACKUP_VM_NAME_PATTERN = re.compile(r"-backup-\d{14}")


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

    for server_cfg in config["servers"]:
        if "esxi_host" not in server_cfg:
            print(
                f"FEHLER: Server-Eintrag {server_cfg!r} hat kein 'esxi_host'. "
                f"Ein direkter GNS3-'host:' Eintrag wird nicht mehr unterstützt "
                f"- siehe Kommentare in gns3_config.yml.",
                file=sys.stderr,
            )
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


def connect_esxi(host: str, username: str, password: str) -> vim.ServiceInstance:
    """
    Baut eine Verbindung zum ESXi-Host auf (direkt, nicht über vCenter) -
    portiert von topologybuilder's ESXiConnection.connect(), gleiche
    TLS-Handhabung (selbstsignierte Zertifikate der ESXi-Hosts werden
    akzeptiert, das ist hier bewusst so, nicht übersehen).
    """
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return SmartConnect(
        host=host, user=username, pwd=password, port=ESXI_PORT, sslContext=ssl_context
    )


def find_gns3_vm_ip(
    instance: vim.ServiceInstance, vm_name: Optional[str], log: logging.Logger
) -> Optional[str]:
    """
    Sucht die GNS3-VM auf dem gegebenen ESXi-Host und liefert ihre aktuelle
    IPv4-Adresse zurück - None, falls keine eindeutige GNS3-VM gefunden
    wird oder sie (noch) keine IP über VMware Tools meldet.
    :param vm_name: exakter VM-Name, falls angegeben (gns3_vm_name in der
        Config) - sonst automatische Erkennung wie in topologybuilder:
        Name enthält "gns3" (Groß-/Kleinschreibung egal), Backup-VMs
        (Name endet auf "-backup-<Zeitstempel>") werden ausgeschlossen,
        nur bei genau einem verbleibenden Kandidaten wird geraten.
    """
    content = instance.RetrieveContent()
    container_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        all_vms = list(container_view.view)
    finally:
        container_view.Destroy()

    if vm_name is not None:
        matches = [vm for vm in all_vms if vm.name == vm_name]
        if not matches:
            log.warning("  GNS3-VM '%s' nicht gefunden.", vm_name)
            return None
    else:
        matches = [
            vm
            for vm in all_vms
            if "gns3" in vm.name.lower() and not _BACKUP_VM_NAME_PATTERN.search(vm.name)
        ]
        if len(matches) > 1:
            log.warning(
                "  Mehrere GNS3-VMs gefunden (%s) - überspringe, gns3_vm_name "
                "in der Config angeben um eindeutig zu machen.",
                [vm.name for vm in matches],
            )
            return None
        if not matches:
            log.warning("  Keine GNS3-VM auf diesem ESXi-Host gefunden.")
            return None

    vm = matches[0]
    for nic in vm.guest.net or []:
        for address in nic.ipAddress or []:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.version != 4:
                continue
            if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast:
                continue
            log.info("  GNS3-VM '%s' gefunden, IP: %s", vm.name, address)
            return address

    log.warning("  GNS3-VM '%s' gefunden, aber noch keine IP gemeldet.", vm.name)
    return None


def read_server(server_cfg: dict, timeout: int, log: logging.Logger, esxi_password: str) -> dict:
    name = server_cfg.get("name", server_cfg["esxi_host"])
    esxi_host = server_cfg["esxi_host"]
    esxi_username = server_cfg.get("esxi_username", "root")
    gns3_vm_name = server_cfg.get("gns3_vm_name")
    port = server_cfg.get("port", 80)

    log.info("Server '%s': ermittle GNS3-VM über ESXi-Host %s ...", name, esxi_host)

    result = {
        "name": name,
        "esxi_host": esxi_host,
        "port": port,
        "status": "ok",
        "templates": [],
    }

    try:
        instance = connect_esxi(esxi_host, esxi_username, esxi_password)
    except vim.fault.VimFault as e:
        log.error("  Konnte nicht mit ESXi-Host %s verbinden: %s", esxi_host, e.msg)
        result["status"] = "esxi_unreachable"
        result["error"] = e.msg
        return result
    except Exception as e:
        # Breiter als nur vim.fault.VimFault (Login-Fehler): SmartConnect kann
        # auch bei Netzwerkproblemen (Host down, DNS, TLS-Handshake) mit
        # anderen Exception-Typen scheitern (socket/ssl-Ebene). Ein einzelner
        # nicht erreichbarer ESXi-Host soll nicht den ganzen Lauf abbrechen
        # und die Ergebnisse der übrigen Server verlieren.
        log.error("  Konnte nicht mit ESXi-Host %s verbinden: %s", esxi_host, e)
        result["status"] = "esxi_unreachable"
        result["error"] = str(e)
        return result

    try:
        host = find_gns3_vm_ip(instance, gns3_vm_name, log)
    finally:
        Disconnect(instance)

    if host is None:
        result["status"] = "gns3_vm_not_found"
        return result

    result["host"] = host
    base_url = f"http://{host}:{port}"

    username = server_cfg.get("username")
    password = server_cfg.get("password")
    auth = (username, password) if username and password else None

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

    esxi_password = os.getenv("ESXI_PASSWORD")
    if not esxi_password:
        log.error(
            "ESXI_PASSWORD Umgebungsvariable nicht gesetzt - kann keine "
            "ESXi-Verbindung aufbauen. Siehe .env.example."
        )
        return 1

    log.info("=" * 60)
    log.info("Starte GNS3-Template-Auslesung für %d Server", len(config["servers"]))

    all_results = []
    for server_cfg in config["servers"]:
        all_results.append(read_server(server_cfg, timeout, log, esxi_password))

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
