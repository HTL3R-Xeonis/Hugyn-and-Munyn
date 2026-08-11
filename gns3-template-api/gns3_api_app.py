from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import requests
import yaml
from fastapi import FastAPI, HTTPException, Query

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("gns3_template_api")

app = FastAPI(title="GNS3 Template API", version="1.1")

DATA_DIR = Path("/data")
TEMPLATES_FILE = DATA_DIR / "gns3_templates.yml"
LIVE_REQUEST_TIMEOUT = 15


# ---------- Live-Abfrage eines beliebigen, im Request angegebenen Servers ----------
def live_get_templates(base_url: str, auth: Optional[tuple], timeout: int) -> list[dict]:
    resp = requests.get(f"{base_url}/v2/templates", auth=auth, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def live_get_docker_images(base_url: str, auth: Optional[tuple], timeout: int) -> list[dict]:
    try:
        resp = requests.get(f"{base_url}/v2/compute/docker/images", auth=auth, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("Docker-Images nicht abrufbar (%s): %s", base_url, e)
        return []


def live_extract_image_names(images_data: list[dict]) -> set[str]:
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


def live_enrich_docker_status(templates: list[dict], available_images: set[str]) -> None:
    for template in templates:
        if template.get("template_type") == "docker":
            image_name = template.get("image")
            template["image_available"] = (
                image_name in available_images if image_name else None
            )


# ---------- Gecachte Daten (vom Collector geschrieben) ----------
def load_data() -> dict:
    if not TEMPLATES_FILE.exists():
        raise HTTPException(
            500, f"gns3_templates.yml nicht gefunden unter {TEMPLATES_FILE}"
        )
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def flatten_templates(data: dict) -> list[dict]:
    """Baut aus der Server-strukturierten Rohdatei eine flache Liste aller
    Templates, jeweils angereichert um Server-Kontext (Name/Host)."""
    flat: list[dict] = []
    for server in data.get("servers", []):
        server_name = server.get("name")
        server_host = server.get("host")
        server_status = server.get("status")

        for template in server.get("templates", []):
            entry = dict(template)  # Kopie, um Original nicht zu verändern
            entry["_server_name"] = server_name
            entry["_server_host"] = server_host
            entry["_server_status"] = server_status
            flat.append(entry)

    return flat


def matches_filters(
    template: dict,
    name: Optional[str],
    template_type: Optional[str],
    category: Optional[str],
    builtin: Optional[bool],
    server: Optional[str],
) -> bool:
    if name and name.lower() not in str(template.get("name", "")).lower():
        return False
    if template_type and template.get("template_type", "").lower() != template_type.lower():
        return False
    if category and template.get("category", "").lower() != category.lower():
        return False
    if builtin is not None and template.get("builtin") != builtin:
        return False
    if server and template.get("_server_name", "").lower() != server.lower():
        return False
    return True


# ---------- Routen ----------
@app.get("/api/live-templates")
def get_live_templates(
    host: str = Query(..., description="IP oder Hostname des GNS3-Servers"),
    port: int = Query(80, description="Port des GNS3-Servers (Standard: 80)"),
    username: Optional[str] = Query(None, description="Nur nötig, falls HTTP-Auth aktiv ist"),
    password: Optional[str] = Query(None, description="Nur nötig, falls HTTP-Auth aktiv ist"),
    include_docker_status: bool = Query(
        True, description="Bei Docker-Templates zusätzlich Image-Verfügbarkeit prüfen"
    ),
):
    """
    Fragt einen beliebigen GNS3-Server LIVE ab, unabhängig davon, ob er in
    gns3_config.yml des Collectors eingetragen ist. Praktisch für Server,
    die (noch) nicht Teil der periodischen Sammlung sind, oder um den
    aktuellsten Stand ohne Wartezeit auf den nächsten Collector-Lauf zu
    bekommen.

    Beispiel:
        GET /api/live-templates?host=10.20.20.221&port=80
    """
    base_url = f"http://{host}:{port}"
    auth = (username, password) if username and password else None

    try:
        templates = live_get_templates(base_url, auth, LIVE_REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(
            502, f"GNS3-Server unter {base_url} nicht erreichbar: {e}"
        )

    docker_images: list[dict] = []
    if include_docker_status:
        docker_images = live_get_docker_images(base_url, auth, LIVE_REQUEST_TIMEOUT)
        available_images = live_extract_image_names(docker_images)
        live_enrich_docker_status(templates, available_images)

    return {
        "host": host,
        "port": port,
        "status": "ok",
        "count": len(templates),
        "templates": templates,
        "docker_images_on_compute": sorted(live_extract_image_names(docker_images)),
    }


@app.get("/")
def root():
    return {"name": "GNS3 Template API", "version": "1.0"}


@app.get("/api/templates")
def get_templates(
    name: Optional[str] = Query(None, description="Teilstring-Suche im Namen"),
    template_type: Optional[str] = Query(
        None, description="z.B. docker, qemu, dynamips, iou, vpcs, cloud"
    ),
    category: Optional[str] = Query(
        None, description="z.B. router, switch, guest, firewall"
    ),
    builtin: Optional[bool] = Query(
        None, description="true = nur eingebaute, false = nur eigene Templates"
    ),
    server: Optional[str] = Query(None, description="Name des GNS3-Servers"),
):
    """
    Liefert die Liste aller Templates über alle konfigurierten GNS3-Server.

    Ohne Parameter: komplette Liste.
    Mit Parametern: gefilterte Liste (alle angegebenen Filter müssen zutreffen).
    """
    data = load_data()
    templates = flatten_templates(data)

    filtered = [
        t
        for t in templates
        if matches_filters(t, name, template_type, category, builtin, server)
    ]

    return {
        "count": len(filtered),
        "total_available": len(templates),
        "filters": {
            "name": name,
            "template_type": template_type,
            "category": category,
            "builtin": builtin,
            "server": server,
        },
        "templates": filtered,
    }


@app.get("/api/servers")
def get_servers():
    """Liefert den Rohzustand aller Server (inkl. nicht erreichbarer, mit Fehlermeldung)."""
    data = load_data()
    servers = data.get("servers", [])
    return {
        "count": len(servers),
        "servers": [
            {
                "name": s.get("name"),
                "host": s.get("host"),
                "port": s.get("port"),
                "status": s.get("status"),
                "template_count": len(s.get("templates", [])),
                "error": s.get("error"),
            }
            for s in servers
        ],
    }


@app.get("/api/templates/types")
def get_template_types():
    """Liefert alle vorkommenden template_type-Werte (praktisch, um zu wissen,
    wonach man mit dem 'template_type'-Filter überhaupt suchen kann)."""
    data = load_data()
    templates = flatten_templates(data)
    types = sorted({t.get("template_type") for t in templates if t.get("template_type")})
    return {"template_types": types}


@app.get("/api/templates/categories")
def get_template_categories():
    """Liefert alle vorkommenden category-Werte."""
    data = load_data()
    templates = flatten_templates(data)
    categories = sorted({t.get("category") for t in templates if t.get("category")})
    return {"categories": categories}
