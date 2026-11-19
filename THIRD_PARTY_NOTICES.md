# Third-party notices - Hugyn and Munyn (formerly Docker-Server-Files)

This project is licensed under the GNU GPL v3 (see `LICENSE`). No third-party source code is copied into the repository.
Licenses were read from the installed packages (`pip-licenses`, November 2026) and the image names in the compose files.

## Python dependencies

FastAPI (MIT), uvicorn (BSD-3-Clause), PyYAML (MIT), requests (Apache-2.0), pyvmomi (Apache-2.0). All are compatible
with GPL-3.0.

## Container images (pulled by the user, not distributed here)

| Image | Used by | License |
| --- | --- | --- |
| `python:3.12-slim` | the API/collector images | PSF license plus the Debian packages' own licenses |
| `filebrowser/filebrowser` | `downloadserver/` | Apache-2.0 |
| `prom/prometheus`, `prom/blackbox-exporter`, `prom/graphite-exporter`, `pryorda/vmware_exporter` | `monitoring/` | Apache-2.0 (as published by each project; check the image you pull) |
| `grafana/grafana` | `monitoring/` | AGPL-3.0 (a separate container; this repository only contains a dashboard JSON and a compose file) |

`monitoring/` is a lab convenience and not part of the appliance.
