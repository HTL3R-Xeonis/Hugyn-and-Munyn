# Docker-Server-Files

The small helper services that the Ginnungagap appliance runs next to the application:

- `gns3-reader/` - the collector that reads the GNS3 templates into a YAML file (it finds the GNS3 VM through the ESXi API).
- `gns3-template-api/` - the HTTP API that serves the GNS3 template list.
- `yaml-api/` - the collector and HTTP API for the ESXi/OVA template list (`yaml-api/README.md`).
- `downloadserver/` - a file browser in front of the template store.
- `monitoring/` - an optional Prometheus/Grafana setup used in the lab. It is not part of the appliance.

The appliance repository ([Ginnungagap-Appliance](https://github.com/HTL3R-Xeonis/Ginnungagap-Appliance)) builds and runs these services
as containers. Each service reads its settings from environment variables or its `.env` file (copy `.env.example`); no password
belongs in a file that is committed.

Run the tests with `pytest tests`.

## License

GNU General Public License v3.0 - see [LICENSE](LICENSE). Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
