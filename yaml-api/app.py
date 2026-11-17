from __future__ import annotations

import logging
import os
from difflib import SequenceMatcher
from pathlib import Path

import requests
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("template_api")

app = FastAPI(title="Template API", version="2.0")

# ---------- Configuration ----------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TEMPLATES_FILE = DATA_DIR / "templates.yml"
# A file in this folder asks the host to run the collector now (see yaml-collector.path)
TRIGGER_DIR = Path(os.environ.get("TRIGGER_DIR", "/trigger"))

# When set, downloads are served straight from this directory (the share mounted on this machine) instead of being
# proxied through Filebrowser. Unset keeps the Filebrowser behaviour.
STORE_DIR = os.environ.get("TEMPLATE_STORE_DIR", "")

# No defaults: without a template directory the Filebrowser must be configured completely (see no_source_problem).
FILEBROWSER_URL = os.environ.get("FILEBROWSER_URL", "")
FILEBROWSER_USER = os.environ.get("FILEBROWSER_USER", "")
FILEBROWSER_PASSWORD = os.environ.get("FILEBROWSER_PASSWORD", "")
NO_SOURCE_PROBLEM = (
    "Neither TEMPLATE_STORE_DIR nor FILEBROWSER_URL + FILEBROWSER_PASSWORD is set - the template list cannot be read. "
    "Set TEMPLATE_STORE_DIR (preferred) or the Filebrowser variables."
)

NAME_WEIGHT = 0.6
TAGS_WEIGHT = 0.4
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
REQUEST_TIMEOUT = 15
# ------------------------------------


# ---------- Helpers: load & score templates ----------
def load_templates() -> list[dict]:
    if not TEMPLATES_FILE.exists():
        raise HTTPException(500, f"templates.yml not found at {TEMPLATES_FILE}")
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
    template: dict, name_query: str | None, tags_query: list[str]
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
    templates: list[dict], name_query: str | None, tags_query: list[str]
) -> list[tuple[float, dict]]:
    scored = [(score_template(t, name_query, tags_query), t) for t in templates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


# ---------- Helpers: Filebrowser proxy ----------
def no_source_problem() -> str | None:
    """Set when files can neither be read from a directory nor through a completely configured Filebrowser."""
    if STORE_DIR:
        return None
    if not FILEBROWSER_URL or not FILEBROWSER_PASSWORD:
        return NO_SOURCE_PROBLEM
    if not FILEBROWSER_USER:
        return "FILEBROWSER_USER is not set - the Filebrowser login needs a user name."
    return None


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
        raise HTTPException(500, "Template has no 'file' field.")

    return f"{source_dir}/{file_name}" if source_dir else file_name


def local_download(template: dict) -> FileResponse:
    """The template's file from the local store directory; never outside of it."""
    store = Path(STORE_DIR).resolve()
    path = (store / build_remote_path(template)).resolve()
    if not path.is_relative_to(store):
        raise HTTPException(400, "The template's file lies outside of the store.")
    if not path.is_file():
        raise HTTPException(
            404, f"The file of this template is not in the store: {template['file']}"
        )
    return FileResponse(
        path, media_type="application/octet-stream", filename=template["file"]
    )


def proxy_download(template: dict) -> StreamingResponse:
    if STORE_DIR:
        return local_download(template)
    problem = no_source_problem()
    if problem:
        log.error(problem)
        raise HTTPException(500, problem)

    remote_path = build_remote_path(template)
    file_name = template["file"]

    try:
        generator = stream_from_filebrowser(remote_path)
    except requests.RequestException as e:
        log.error("Error accessing Filebrowser: %s", e)
        raise HTTPException(502, f"Error accessing the share: {e}")

    return StreamingResponse(
        generator,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


# ---------- Routes ----------
@app.get("/")
def root():
    return {"name": "Template API", "version": "2.0"}


@app.get("/health")
def health():
    """Whether this service can do its job: the templates file is there and, with a local store, the directory too."""
    problems = []
    count = None
    try:
        count = len(load_templates())
    except HTTPException as exc:
        problems.append(str(exc.detail))
    if STORE_DIR and not Path(STORE_DIR).is_dir():
        problems.append(f"the template directory {STORE_DIR} is not there")
    source_problem = no_source_problem()
    if source_problem:
        problems.append(source_problem)
    body = {
        "status": "ok" if not problems else "degraded",
        "templates": count,
        "download": "directory" if STORE_DIR else "filebrowser",
        "problems": problems,
    }
    return JSONResponse(body, status_code=503 if problems else 200)


@app.post("/api/refresh", status_code=202)
def request_refresh():
    """Asks for the collector to run now instead of at its next timer run. Only drops a request file; the host's path
    unit starts the collector."""
    if not TRIGGER_DIR.is_dir():
        raise HTTPException(501, "refresh requests are not set up on this host")
    try:
        (TRIGGER_DIR / "refresh").touch()
    except OSError as exc:
        raise HTTPException(501, f"refresh request could not be written: {exc}")
    return {"requested": True}


@app.get("/api/templates")
def list_templates():
    """Returns all known templates as JSON."""
    templates = load_templates()
    return {"count": len(templates), "templates": templates}


@app.get("/api/search")
def search_templates(
    name: str | None = Query(None, description="Name to search for"),
    tags: str | None = Query(None, description="Comma-separated tags"),
    top: int = Query(5, ge=1, le=50),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
):
    """Searches templates via fuzzy matching on name and/or tags."""
    if not name and not tags:
        raise HTTPException(400, "Please provide at least 'name' or 'tags'.")

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
    """Downloads the .ova file for the exact given template ID."""
    templates = load_templates()
    match = next((t for t in templates if t.get("id") == template_id), None)
    if not match:
        raise HTTPException(404, f"No template found with id={template_id}.")

    return proxy_download(match)


@app.get("/api/download")
def download_by_query(
    name: str | None = Query(None),
    tags: str | None = Query(None),
    min_score: float = Query(0.3, ge=0.0, le=1.0),
):
    """Finds the best match by name/tags and downloads it directly."""
    if not name and not tags:
        raise HTTPException(400, "Please provide at least 'name' or 'tags'.")

    tags_query = [t.strip() for t in tags.split(",")] if tags else []
    templates = load_templates()
    scored = rank_templates(templates, name, tags_query)

    if not scored or scored[0][0] < min_score:
        raise HTTPException(404, "No sufficiently good match found.")

    best_score, best_template = scored[0]
    log.info("Best match: %s (score=%.2f)", best_template.get("name"), best_score)
    return proxy_download(best_template)


# ---------- yml only (legacy) ----------
@app.get("/{name}")
def get_file(name: str):
    """Returns a raw .yml file by name from /data (legacy)."""
    file = DATA_DIR / f"{name}.yml"
    if not file.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(file, media_type="application/x-yaml")
