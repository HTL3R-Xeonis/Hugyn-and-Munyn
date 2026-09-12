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


# ---------- Live query of any server given in the request ----------
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
        log.warning("Could not fetch Docker images (%s): %s", base_url, e)
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


# ---------- Cached data (written by the collector) ----------
def load_data() -> dict:
    if not TEMPLATES_FILE.exists():
        raise HTTPException(
            500, f"gns3_templates.yml not found at {TEMPLATES_FILE}"
        )
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def flatten_templates(data: dict) -> list[dict]:
    """Builds a flat list of all templates from the per-server raw data,
    each enriched with server context (name/host)."""
    flat: list[dict] = []
    for server in data.get("servers", []):
        server_name = server.get("name")
        server_host = server.get("host")
        server_status = server.get("status")

        for template in server.get("templates", []):
            entry = dict(template)  # copy, don't mutate the original
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


# ---------- Routes ----------
@app.get("/api/live-templates")
def get_live_templates(
    host: str = Query(..., description="IP or hostname of the GNS3 server"),
    port: int = Query(80, description="Port of the GNS3 server (default: 80)"),
    username: Optional[str] = Query(None, description="Only needed if HTTP auth is enabled"),
    password: Optional[str] = Query(None, description="Only needed if HTTP auth is enabled"),
    include_docker_status: bool = Query(
        True, description="Also check image availability for Docker templates"
    ),
):
    """
    Queries an arbitrary GNS3 server LIVE, regardless of whether it's
    listed in the collector's gns3_config.yml. Useful for servers that
    aren't (yet) part of the periodic collection, or to get the latest
    state without waiting for the next collector run.

    Example:
        GET /api/live-templates?host=10.20.20.221&port=80
    """
    base_url = f"http://{host}:{port}"
    auth = (username, password) if username and password else None

    try:
        templates = live_get_templates(base_url, auth, LIVE_REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(
            502, f"GNS3 server at {base_url} unreachable: {e}"
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
    name: Optional[str] = Query(None, description="Substring search on the name"),
    template_type: Optional[str] = Query(
        None, description="e.g. docker, qemu, dynamips, iou, vpcs, cloud"
    ),
    category: Optional[str] = Query(
        None, description="e.g. router, switch, guest, firewall"
    ),
    builtin: Optional[bool] = Query(
        None, description="true = builtin only, false = custom templates only"
    ),
    server: Optional[str] = Query(None, description="Name of the GNS3 server"),
):
    """
    Returns the list of all templates across every configured GNS3 server.

    Without parameters: the full list.
    With parameters: filtered list (every given filter must match).
    """
    data = load_data()
    templates = flatten_templates(data)

    filtered = [
        t
        for t in templates
        if matches_filters(t, name, template_type, category, builtin, server)
    ]

    # Surfaced directly on this endpoint (not just /api/servers) so a
    # caller seeing an unexpectedly low/zero count doesn't have to make
    # a second request to find out why - e.g. "count: 0" alone looks
    # like a broken API, when the real cause (a configured server's
    # GNS3 VM being powered off, its ESXi host unreachable, etc.) was
    # sitting right there in the collector's own last run.
    server_warnings = [
        f"{s.get('name')}: {s.get('error') or s.get('status')}"
        for s in data.get("servers", [])
        if s.get("status") != "ok"
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
        "server_warnings": server_warnings,
    }


@app.get("/api/servers")
def get_servers():
    """Returns the raw state of every server (including unreachable ones, with an error message)."""
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
                "vm_name": s.get("vm_name"),
                "vm_power_state": s.get("vm_power_state"),
            }
            for s in servers
        ],
    }


@app.get("/api/templates/types")
def get_template_types():
    """Returns every template_type value seen (useful to know what values
    the 'template_type' filter actually accepts)."""
    data = load_data()
    templates = flatten_templates(data)
    types = sorted({t.get("template_type") for t in templates if t.get("template_type")})
    return {"template_types": types}


@app.get("/api/templates/categories")
def get_template_categories():
    """Returns every category value seen."""
    data = load_data()
    templates = flatten_templates(data)
    categories = sorted({t.get("category") for t in templates if t.get("category")})
    return {"categories": categories}
