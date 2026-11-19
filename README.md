# Hugyn and Munyn (formerly Docker-Server-Files)

**Hugyn and Munyn are the template services of [Ginnungagap](https://github.com/HTL3R-Xeonis/Ginnungagap-Appliance):** the small helper
services that run next to the web app ([Igdrasyl, formerly TopologyOperator](https://github.com/HTL3R-Xeonis/Igdrasyl)) and tell it, and
[Mjonyr (formerly TopologyBuilder)](https://github.com/HTL3R-Xeonis/Mjonyr), which device images exist. "Ginnungagap" is the name of the
whole product; Hugyn and Munyn are code names of components and are always written together as "Hugyn and Munyn". The repository used to be
called Docker-Server-Files. The folder names, the unit names (`yaml-collector`, `gns3-collector`), the ports and the
`template-esxi` / `template-gns3` service names of the appliance keep their names on purpose.

## Hugyn: the ESXi side

- `yaml-api/` - the collector and HTTP API for the ESXi/OVA template list (`yaml-api/README.md`). In the appliance these are the
  services `template-esxi` and `collector-esxi`.

## Munyn: the GNS3 side

- `gns3-reader/` - the collector that reads the GNS3 templates into a YAML file (it finds the GNS3 VM through the ESXi API).
- `gns3-template-api/` - the HTTP API that serves the GNS3 template list. In the appliance these are the services `template-gns3` and
  `collector-gns3`.

## Shared

- `downloadserver/` - a file browser in front of the template store.
- `monitoring/` - an optional Prometheus/Grafana setup used in the lab. It is not part of the appliance.

The appliance repository ([Ginnungagap-Appliance](https://github.com/HTL3R-Xeonis/Ginnungagap-Appliance)) builds and runs these services
as containers. Each service reads its settings from environment variables or its `.env` file (copy `.env.example`); no password
belongs in a file that is committed.

Run the tests with `pytest tests`.

## License

GNU General Public License v3.0 - see [LICENSE](LICENSE). Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
