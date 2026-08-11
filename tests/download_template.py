#!/usr/bin/env python3
"""
Sucht Templates in templates.yml per Name und/oder Tags (Fuzzy-Matching) und
lädt die passende .ova-Datei vom NFS-Share über die Filebrowser-API herunter.

Voraussetzung: templates.yml wurde vorher mit collect_templates.py erzeugt
(dieses Script muss die erweiterte Version mit '_source_dir' pro Template sein).

Beispiele:
    # Interaktiv: zeigt Top-Treffer, fragt nach, lädt dann herunter
    ./venv/bin/python download_template.py --name mint

    # Nach Tags suchen
    ./venv/bin/python download_template.py --tags linux,gui

    # Kombiniert, nur die Treffer anzeigen, nicht herunterladen
    ./venv/bin/python download_template.py --name mint --tags host --list-only

    # Automatisch den besten Treffer ohne Rückfrage herunterladen
    ./venv/bin/python download_template.py --name mint21 --yes

Abhängigkeiten installieren:
    pip3 install requests pyyaml
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from difflib import SequenceMatcher
from pathlib import Path

import requests
import yaml

# ---------- Konfiguration ----------
FILEBROWSER_URL = os.environ.get("FILEBROWSER_URL", "http://10.20.20.171:4000")
USERNAME = os.environ.get("FILEBROWSER_USER", "admin")
PASSWORD = os.environ.get("FILEBROWSER_PASSWORD", "")

TEMPLATES_FILE = Path(
    os.environ.get(
        "TEMPLATES_FILE", "/home/junioradmin/yaml-api/data/templates.yml"
    )
)
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/home/junioradmin/downloads"))

REQUEST_TIMEOUT = 15
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB pro Chunk beim Download

# Gewichtung, wenn sowohl Name als auch Tags angegeben werden
NAME_WEIGHT = 0.6
TAGS_WEIGHT = 0.4
# ------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("template_downloader")


def load_templates() -> list[dict]:
    if not TEMPLATES_FILE.exists():
        log.error("templates.yml nicht gefunden unter %s", TEMPLATES_FILE)
        sys.exit(1)

    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    templates = data.get("templates", []) if isinstance(data, dict) else []
    if not templates:
        log.error("Keine Templates in %s gefunden.", TEMPLATES_FILE)
        sys.exit(1)

    return templates


def name_similarity(query: str, candidate: str) -> float:
    """Ähnlichkeit zwischen 0 und 1 via SequenceMatcher, case-insensitiv."""
    return SequenceMatcher(None, query.lower().strip(), candidate.lower().strip()).ratio()


def tag_similarity(query_tags: list[str], candidate_tags: list[str]) -> float:
    """Jaccard-Ähnlichkeit (Schnittmenge / Vereinigungsmenge) zwischen 0 und 1."""
    if not query_tags:
        return 0.0
    q = {t.lower().strip() for t in query_tags}
    c = {t.lower().strip() for t in (candidate_tags or [])}
    if not q or not c:
        return 0.0
    intersection = q & c
    union = q | c
    return len(intersection) / len(union) if union else 0.0


def score_template(template: dict, name_query: str | None, tags_query: list[str]) -> float:
    """Berechnet einen kombinierten Score 0..1 für ein Template."""
    has_name = bool(name_query)
    has_tags = bool(tags_query)

    name_score = name_similarity(name_query, template.get("name", "")) if has_name else 0.0
    tags_score = tag_similarity(tags_query, template.get("tags", [])) if has_tags else 0.0

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


def print_matches(scored: list[tuple[float, dict]], top_n: int) -> None:
    print(f"\nTop {min(top_n, len(scored))} Treffer:\n")
    for i, (score, t) in enumerate(scored[:top_n], start=1):
        tags = ", ".join(t.get("tags", []))
        print(
            f"  [{i}] {score * 100:5.1f}%  {t.get('name', '?'):<20} "
            f"file={t.get('file', '?')}  tags=[{tags}]"
        )
    print()


def login() -> str:
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


def download_file(token: str, remote_path: str, local_path: Path) -> None:
    url = f"{FILEBROWSER_URL}/api/raw/{remote_path}"
    log.info("Lade herunter: %s", remote_path)
    log.info("Ziel: %s", local_path)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_suffix(local_path.suffix + ".part")

    with requests.get(
        url, headers={"X-Auth": token}, stream=True, timeout=REQUEST_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        last_logged_percent = -1

        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                if total_size:
                    percent = int(downloaded * 100 / total_size)
                    if percent >= last_logged_percent + 5:
                        log.info(
                            "  %3d%%  (%.2f / %.2f GB)",
                            percent,
                            downloaded / (1024**3),
                            total_size / (1024**3),
                        )
                        last_logged_percent = percent
                else:
                    # Keine Content-Length vorhanden: nur MB-Fortschritt loggen
                    if downloaded % (200 * 1024 * 1024) < CHUNK_SIZE:
                        log.info("  %.0f MB heruntergeladen...", downloaded / (1024**2))

    tmp_path.replace(local_path)
    log.info("Fertig: %s (%.2f GB)", local_path, local_path.stat().st_size / (1024**3))


def build_remote_path(template: dict) -> str | None:
    source_dir = template.get("_source_dir")
    file_name = template.get("file")

    if not file_name:
        log.error("Template hat kein 'file'-Feld, kann nicht heruntergeladen werden.")
        return None

    if not source_dir:
        log.warning(
            "Kein '_source_dir' im Template vorhanden — templates.yml wurde "
            "vermutlich mit einer älteren Collector-Version erzeugt. "
            "Bitte collect_templates.py erneut ausführen."
        )
        return file_name

    return f"{source_dir}/{file_name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sucht und lädt Templates per Name/Tags via Fuzzy-Matching herunter."
    )
    parser.add_argument("--name", help="Gesuchter Name (z.B. 'mint' oder 'mint21')")
    parser.add_argument(
        "--tags", help="Kommagetrennte Tags (z.B. 'linux,gui,host')"
    )
    parser.add_argument(
        "--top", type=int, default=5, help="Anzahl angezeigter Top-Treffer (Standard: 5)"
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.3,
        help="Minimaler Score (0-1), unterhalb dessen kein Treffer akzeptiert wird (Standard: 0.3)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Nur Treffer anzeigen, nichts herunterladen",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Besten Treffer ohne Rückfrage herunterladen",
    )
    parser.add_argument(
        "--download-dir",
        default=str(DOWNLOAD_DIR),
        help=f"Zielordner für den Download (Standard: {DOWNLOAD_DIR})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.name and not args.tags:
        log.error("Bitte mindestens --name oder --tags angeben.")
        return 1

    tags_query = [t.strip() for t in args.tags.split(",")] if args.tags else []

    templates = load_templates()
    scored = rank_templates(templates, args.name, tags_query)

    if not scored or scored[0][0] < args.min_score:
        log.error(
            "Kein Treffer mit ausreichendem Score gefunden (min. %.0f%% erforderlich).",
            args.min_score * 100,
        )
        if scored:
            print_matches(scored, args.top)
        return 1

    print_matches(scored, args.top)

    if args.list_only:
        return 0

    if args.yes:
        chosen = scored[0][1]
    else:
        choice = input(
            f"Welches Template herunterladen? [1-{min(args.top, len(scored))}] "
            f"oder Enter für Nr. 1, 'q' zum Abbrechen: "
        ).strip()

        if choice.lower() == "q":
            print("Abgebrochen.")
            return 0

        index = int(choice) - 1 if choice else 0
        if index < 0 or index >= min(args.top, len(scored)):
            log.error("Ungültige Auswahl.")
            return 1

        chosen = scored[index][1]

    if not PASSWORD:
        log.error("FILEBROWSER_PASSWORD ist nicht gesetzt. Abbruch.")
        return 1

    remote_path = build_remote_path(chosen)
    if not remote_path:
        return 1

    local_path = Path(args.download_dir) / chosen["file"]

    try:
        token = login()
        download_file(token, remote_path, local_path)
    except requests.RequestException as e:
        log.error("Download fehlgeschlagen: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
