from __future__ import annotations

import logging
import os
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import requests
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("template_api")

app = FastAPI(title="Template API", version="2.0")

# ---------- Konfiguration ----------
DATA_DIR = Path("/data")
TEMPLATES_FILE = DATA_DIR / "templates.yml"

FILEBROWSER_URL = os.environ.get("FILEBROWSER_URL", "http://10.20.20.171:4000")
FILEBROWSER_USER = os.environ.get("FILEBROWSER_USER", "admin")
FILEBROWSER_PASSWORD = os.environ.get("FILEBROWSER_PASSWORD", "")

NAME_WEIGHT = 0.6
TAGS_WEIGHT = 0.4
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
REQUEST_TIMEOUT = 15
# ------------------------------------


# ---------- Hilfsfunktionen: Templates laden & bewerten ----------
def load_templates() -> list[dict]:
    if not TEMPLATES_FILE.exists():
        raise HTTPException(
            500, f"templates.yml nicht gefunden unter {TEMPLATES_FILE}"
        )
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("templates", []) if isinstance(data, dict) else []


def name_similarity(query: str, candidate: str) -> float:
    return SequenceMatcher(
        None, query.lower().strip(), candidate.lower().strip()
    ).ratio()


def tag_similarity(query_tags: list[str], candidate_tags: list[str]) -> float:
    if not query_tags:
        return 0.0
    q = {t.lower().strip() for t in query_tags}
    c = {t.lower().strip() for t in (candidate_tags or [])}
    if not q or not c:
        return 0.0
    intersection = q & c
    union = q | c
    return len(intersection) / len(union) if union else 0.0


def score_template(
    template: dict, name_query: Optional[str], tags_query: list[str]
) -> float:
    has_name = bool(name_query)
    has_tags = bool(tags_query)

    name_score = (
        name_similarity(name_query, template.get("name", "")) if has_name else 0.0
    )
    tags_score = (
        tag_similarity(tags_query, template.get("tags", [])) if has_tags else 0.0
    )

    if has_name and has_tags:
        return NAME_WEIGHT * name_score + TAGS_WEIGHT * tags_score
    if has_name:
        return name_score
    if has_tags:
        return tags_score
    return 0.0


def rank_templates(
    templates: list[dict], name_query: Optional[str], tags_query: list[str]
) -> list[tuple[float, dict]]:
    scored = [(score_template(t, name_query, tags_query), t) for t in templates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


# ---------- Hilfsfunktionen: Filebrowser-Proxy ----------
def filebrowser_login() -> str:
    resp = requests.post(
        f"{FILEBROWSER_URL}/api/login",
        json={"username": FILEBROWSER_USER, "password": FILEBROWSER_PASSWORD},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text.strip().strip('"')


def stream_from_filebrowser(remote_path: str):
    token = filebrowser_login()
    url = f"{FILEBROWSER_URL}/api/raw/{remote_path}"
    resp = requests.get(
        url, headers={"X-Auth": token}, stream=True, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
        if chunk:
            yield chunk


def build_remote_path(template: dict) -> str:
    source_dir = template.get("_source_dir")
    file_name = template.get("file")

    if not file_name:
        raise HTTPException(500, "Template hat kein 'file'-Feld.")

    return f"{source_dir}/{file_name}" if source_dir else file_name


def proxy_download(template: dict) -> StreamingResponse:
    if not FILEBROWSER_PASSWORD:
        raise HTTPException(
            500, "FILEBROWSER_PASSWORD ist auf dem API-Server nicht gesetzt."
        )

    remote_path = build_remote_path(template)
    file_name = template["file"]

    try:
        generator = stream_from_filebrowser(remote_path)
    except requests.RequestException as e:
        log.error("Fehler beim Zugriff auf Filebrowser: %s", e)
        raise HTTPException(502, f"Fehler beim Zugriff auf den Share: {e}")

    return StreamingResponse(
        generator,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


# ---------- Routen ----------
@app.get("/")
def root():
    return {"name": "Template API", "version": "2.0"}


@app.get("/api/templates")
def list_templates():
    """Gibt alle bekannten Templates als JSON zurück."""
    templates = load_templates()
    return {"count": len(templates), "templates": templates}


@app.get("/api/search")
def search_templates(
    name: Optional[str] = Query(None, description="Gesuchter Name"),
    tags: Optional[str] = Query(None, description="Kommagetrennte Tags"),
    top: int = Query(5, ge=1, le=50),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
):
    """Sucht Templates per Fuzzy-Matching auf Name und/oder Tags."""
    if not name and not tags:
        raise HTTPException(400, "Bitte mindestens 'name' oder 'tags' angeben.")

    tags_query = [t.strip() for t in tags.split(",")] if tags else []
    templates = load_templates()
    scored = rank_templates(templates, name, tags_query)

    results = [
        {"score": round(score, 4), "template": t}
        for score, t in scored
        if score >= min_score
    ][:top]

    return {"query": {"name": name, "tags": tags_query}, "results": results}


@app.get("/api/download/{template_id}")
def download_by_id(template_id: str):
    """Lädt die .ova-Datei anhand der exakten Template-ID herunter."""
    templates = load_templates()
    match = next((t for t in templates if t.get("id") == template_id), None)
    if not match:
        raise HTTPException(404, f"Kein Template mit id={template_id} gefunden.")

    return proxy_download(match)


@app.get("/api/download")
def download_by_query(
    name: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    min_score: float = Query(0.3, ge=0.0, le=1.0),
):
    """Sucht den besten Treffer per Name/Tags und lädt ihn direkt herunter."""
    if not name and not tags:
        raise HTTPException(400, "Bitte mindestens 'name' oder 'tags' angeben.")

    tags_query = [t.strip() for t in tags.split(",")] if tags else []
    templates = load_templates()
    scored = rank_templates(templates, name, tags_query)

    if not scored or scored[0][0] < min_score:
        raise HTTPException(404, "Kein ausreichend guter Treffer gefunden.")

    best_score, best_template = scored[0]
    log.info("Bester Treffer: %s (score=%.2f)", best_template.get("name"), best_score)
    return proxy_download(best_template)


# ---------- nur yml  ----------
@app.get("/{name}")
def get_file(name: str):
    """Liefert eine rohe .yml-Datei nach Namen aus /data zurück (Altbestand)."""
    file = DATA_DIR / f"{name}.yml"
    if not file.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(file, media_type="application/x-yaml")
