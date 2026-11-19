# Hugyn: the ESXi template service (formerly part of Docker-Server-Files)

run this API: docker compose up -d --build

## Configuration (no defaults)

Nothing here falls back to a built-in address or to some user's home folder; a missing setting is reported and the
program refuses to run.

Where the templates are read from - one of:

- `TEMPLATE_STORE_DIR` (preferred): a directory, e.g. the NFS share mounted on this machine.
- `FILEBROWSER_URL` + `FILEBROWSER_USER` + `FILEBROWSER_PASSWORD`: through Filebrowser.

With neither, the collector exits 1 and `GET /health` answers 503 with: `Neither TEMPLATE_STORE_DIR nor FILEBROWSER_URL +
FILEBROWSER_PASSWORD is set - the template list cannot be read. Set TEMPLATE_STORE_DIR (preferred) or the Filebrowser
variables.`

The collector (`collect_templates.py`, started by `yaml-collector.service`) additionally needs

- `TEMPLATES_OUTPUT_FILE` - the combined `templates.yml` it writes (the API reads it from `DATA_DIR`),
- `TEMPLATES_LOG_FILE` - its rotating log file.

Without them it stops with `TEMPLATES_OUTPUT_FILE is not set - refusing to write into a default home folder.` (or the
same for `TEMPLATES_LOG_FILE`). The unit file in this folder sets both; the passwords belong in `.env`, never in a unit.
