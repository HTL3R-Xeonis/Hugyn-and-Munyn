#!/usr/bin/env python3
"""
Reads all templates from every GNS3 server listed in the config file, and
for Docker templates additionally checks whether the referenced image is
actually present (pulled) on that server.

Each server entry in the config gives an ESXi host, not a GNS3 server IP
directly - the GNS3 VM on that host is looked up fresh via the ESXi API on
every run (name contains "gns" and guest OS is Ubuntu Linux (64-bit)) and
its current IP is used, instead of
relying on a hardcoded, potentially stale IP (GNS3 VMs often get their IP
via DHCP, and a topologybuilder '--fresh-gns3-vm' run replaces the VM
entirely - both make a static IP unreliable). Because of this it also needs
pyvmomi in addition to requests/pyyaml, and an ESXi login for every host.

The login of a host comes from, in this order: its own 'esxi_password' in the config (or the one the app stored for its
profile, see --profiles-dir), otherwise the Operator is asked for it (OPERATOR_URL + OPERATOR_TOKEN, see .env.example):
GET <OPERATOR_URL>/internal/host-credentials?ip=<esxi ip> with a bearer token. There is no process-wide password any more -
a host without a login is reported and skipped, the others still run.

With --profiles-dir the list of servers is not taken from the config file but from the host profiles of
TopologyOperator/Ginnungagap (its settings_profiles folder): one server per profile with an esxi.ip, so a host is
entered in one place only. The ESXi password of such a host is the one stored for it by the app
(<profiles-dir>/.secrets/<profile>.json, key esxi_password), otherwise the Operator is asked. The config file is still
read for output_file, log_file and request_timeout.

Usage:
    ./venv/bin/python read_gns3_templates.py --config gns3_config.yml
    ./venv/bin/python read_gns3_templates.py --config gns3_config.yml --profiles-dir /data/settings_profiles

Install dependencies:
    pip3 install -r requirements.txt
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

DEFAULT_CONFIG_PATH = "gns3_config.yml"
DEFAULT_TIMEOUT = 15
ESXI_SOCKET_TIMEOUT_SECONDS = 60
ESXI_PORT = 443

# Matches the backup-name suffix a topologybuilder '--fresh-gns3-vm' run
# gives the VM it replaces (f"{vm_name}-backup-{timestamp:%Y%m%d%H%M%S}") -
# excluded from auto-detection so a powered-off backup is never mistaken
# for the live GNS3 VM. Same pattern topologybuilder itself uses.
_BACKUP_VM_NAME_PATTERN = re.compile(r"-backup-\d{14}")

# Auto-detection of the GNS3 VM requires its name to contain this
# (case-insensitive) - "gns" rather than "gns3", since the real VM is
# called e.g. "GNS-VM-1" - AND its guest OS to be Ubuntu Linux (64-bit),
# which is how ESXi reports guestId "ubuntu64Guest". The OS requirement
# keeps an unrelated VM that merely has "gns" in its name from being
# picked up. Only applies to auto-detection: an explicit gns3_vm_name in
# the config always wins and is not checked against the guest OS.
_GNS3_VM_NAME_FRAGMENT = "gns"
_GNS3_VM_GUEST_ID = "ubuntu64Guest"


def _is_gns3_vm_candidate(vm: vim.VirtualMachine) -> bool:
    """True if vm's name contains "gns" (case-insensitive), it isn't a
    backup VM, and ESXi reports its guest OS as Ubuntu Linux (64-bit)."""
    if _GNS3_VM_NAME_FRAGMENT not in vm.name.lower():
        return False
    if _BACKUP_VM_NAME_PATTERN.search(vm.name):
        return False
    config = vm.config  # None for VMs whose config can't be read (e.g. orphaned)
    return config is not None and config.guestId == _GNS3_VM_GUEST_ID


def setup_logging(log_file: str | None) -> logging.Logger:
    """Sets up logging to console + optionally a rotating log file."""
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


def _stored_esxi_password(profiles_dir: Path, profile_name: str) -> str | None:
    """The ESXi password the app stored for this profile, if any (never logged)."""
    try:
        data = json.loads(
            (profiles_dir / ".secrets" / f"{profile_name}.json").read_text()
        )
    except (OSError, ValueError):
        return None
    password = data.get("esxi_password") if isinstance(data, dict) else None
    return password if isinstance(password, str) and password else None


class OperatorLoginError(Exception):
    """The Operator could not give a host's login; the message is ready to log (never contains a secret)."""


def fetch_operator_login(
    operator_url: str, token: str, name: str, ip: str, timeout: float
) -> tuple[str, str]:
    """(username, password) of the ESXi host with this address, asked from the Operator's gateway."""
    url = operator_url.rstrip("/") + "/internal/host-credentials"
    try:
        response = requests.get(
            url,
            params={"ip": ip},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise OperatorLoginError(
            f"Server {name}: the Operator at {operator_url} could not be reached "
            f"({type(exc).__name__}) - no ESXi login."
        ) from exc
    status = response.status_code
    if status == 401:
        raise OperatorLoginError(
            f"Server {name}: the Operator refused the service token (HTTP 401) - "
            "check OPERATOR_TOKEN in .env on this machine and XEONIS_COLLECTOR_TOKEN on the Operator."
        )
    if status == 503:
        raise OperatorLoginError(
            f"Server {name}: the credentials endpoint is switched off on the Operator "
            "(XEONIS_COLLECTOR_TOKEN is not set there or is shorter than 32 characters)."
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if status != 200:
        detail = body.get("detail") if isinstance(body, dict) else None
        detail = str(detail or response.reason or "no detail")
        if token:
            detail = detail.replace(token, "[redacted]")
        advice = " - add the host in the Operator" if status == 404 else ""
        raise OperatorLoginError(
            f"Server {name}: the Operator answered HTTP {status} for {ip} ({detail}){advice}."
        )
    username = body.get("username") if isinstance(body, dict) else None
    password = body.get("password") if isinstance(body, dict) else None
    if not isinstance(username, str) or not isinstance(password, str) or not password:
        raise OperatorLoginError(
            f"Server {name}: the Operator answered HTTP 200 for {ip} without a user name and password - "
            f"add the host in the Operator."
        )
    return username, password


NO_LOGIN_TEXT = (
    "Server {name}: no ESXi login - set esxi_password in the config or "
    "OPERATOR_URL + OPERATOR_TOKEN so the Operator can be asked."
)


def servers_from_profiles(profiles_dir: str, log: logging.Logger) -> list[dict]:
    """
    One server entry per host profile (*.yml/*.yaml) that names an esxi.ip, in the shape the config file's 'servers' use.
    A host that two profiles share (same ESXi IP and GNS3 VM name) appears once. Unreadable or incomplete profiles are
    skipped with a warning. The entry carries the stored ESXi password in 'esxi_password' when there is one.
    """
    root = Path(profiles_dir)
    servers: list[dict] = []
    seen: set[tuple] = set()
    for path in sorted([*root.glob("*.yml"), *root.glob("*.yaml")]):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("Profile %s skipped, could not be read: %s", path.name, e)
            continue
        esxi = data.get("esxi") if isinstance(data, dict) else None
        if not isinstance(esxi, dict) or not esxi.get("ip"):
            log.warning("Profile %s skipped: no esxi.ip", path.name)
            continue
        key = (str(esxi["ip"]), esxi.get("gns3_vm_name"))
        if key in seen:
            continue
        seen.add(key)
        gns3 = data.get("gns3") if isinstance(data.get("gns3"), dict) else {}
        server = {
            "name": path.stem,
            "esxi_host": str(esxi["ip"]),
            "esxi_username": esxi.get("username") or "root",
            "port": gns3.get("port", 80),
        }
        if esxi.get("gns3_vm_name"):
            server["gns3_vm_name"] = esxi["gns3_vm_name"]
        stored = _stored_esxi_password(root, path.name)
        if stored:
            server["esxi_password"] = stored
        servers.append(server)
    return servers


def load_config(config_path: str, need_servers: bool = True) -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"ERROR: config file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config = config or {}
    if not need_servers:
        config.setdefault("servers", [])
        return config

    if "servers" not in config or not config["servers"]:
        print("ERROR: config contains no servers under 'servers'.", file=sys.stderr)
        sys.exit(1)

    for server_cfg in config["servers"]:
        if "esxi_host" not in server_cfg:
            print(
                f"ERROR: server entry {server_cfg!r} has no 'esxi_host'. "
                f"A direct GNS3 'host:' entry is no longer supported - see "
                f"the comments in gns3_config.yml.",
                file=sys.stderr,
            )
            sys.exit(1)

    return config


def get_templates(
    base_url: str, auth: tuple | None, timeout: int, log: logging.Logger
) -> list[dict]:
    """Fetches ALL templates from a GNS3 server (unfiltered, including builtins)."""
    url = f"{base_url}/v2/templates"
    resp = requests.get(url, auth=auth, timeout=timeout)
    resp.raise_for_status()
    templates = resp.json()
    log.info("  %d template(s) found.", len(templates))
    return templates


def get_docker_images(
    base_url: str, auth: tuple | None, timeout: int, log: logging.Logger
) -> list[dict]:
    """Fetches the list of Docker images actually present on the compute."""
    url = f"{base_url}/v2/compute/docker/images"
    try:
        resp = requests.get(url, auth=auth, timeout=timeout)
        resp.raise_for_status()
        images = resp.json()
        log.info("  %d Docker image(s) found on the compute.", len(images))
        return images
    except requests.RequestException as e:
        log.warning("  Could not fetch Docker images (maybe no Docker compute): %s", e)
        return []


def extract_available_image_names(images_data: list[dict]) -> set[str]:
    """Extracts all known image names/tags from the Docker images response,
    regardless of the exact response format (different GNS3 versions
    sometimes return slightly different fields)."""
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
    """Adds an 'image_available' field (True/False/None) to Docker templates."""
    for template in templates:
        if template.get("template_type") == "docker":
            image_name = template.get("image")
            template["image_available"] = (
                image_name in available_images if image_name else None
            )


def connect_esxi(host: str, username: str, password: str) -> vim.ServiceInstance:
    """
    Opens a connection to the ESXi host (direct, not via vCenter) - ported
    from topologybuilder's ESXiConnection.connect(), same TLS handling
    (self-signed ESXi host certificates are accepted, intentionally, not
    an oversight).
    """
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return SmartConnect(
        host=host, user=username, pwd=password, port=ESXI_PORT, sslContext=ssl_context
    )


def find_gns3_vm(
    instance: vim.ServiceInstance, vm_name: str | None, log: logging.Logger
) -> vim.VirtualMachine | None:
    """
    Looks up the GNS3 VM on the given ESXi host - None if no
    unambiguous match is found. Split from IP extraction (see
    extract_vm_ipv4) so callers can tell "no matching VM at all" apart
    from "found it, but it has no reachable IP yet" (e.g. powered off) -
    collapsing both into one None used to make read_server() report the
    same generic "gns3_vm_not_found" status either way, which is
    genuinely misleading when the VM exists and is simply off.
    :param vm_name: exact VM name, if given (gns3_vm_name in the config) -
        otherwise auto-detection: name contains "gns" (case-insensitive),
        guest OS is Ubuntu Linux (64-bit), backup VMs (name ends in
        "-backup-<timestamp>") are excluded, and a guess is only made if
        exactly one candidate remains.
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
            log.warning("  GNS3 VM '%s' not found.", vm_name)
            return None
    else:
        matches = [vm for vm in all_vms if _is_gns3_vm_candidate(vm)]
        if len(matches) > 1:
            log.warning(
                "  Multiple GNS3 VMs found (%s) - skipping, set gns3_vm_name "
                "in the config to disambiguate.",
                [vm.name for vm in matches],
            )
            return None
        if not matches:
            log.warning(
                "  No GNS3 VM found on this ESXi host (looked for a non-backup "
                "VM with '%s' in its name and guest OS Ubuntu Linux (64-bit)).",
                _GNS3_VM_NAME_FRAGMENT,
            )
            return None

    return matches[0]


def extract_vm_ipv4(vm: vim.VirtualMachine, log: logging.Logger) -> str | None:
    """Returns the first usable IPv4 address VMware Tools reports for
    vm (skipping loopback/link-local/multicast), or None if it hasn't
    reported one yet - normal right after boot, or permanent if the VM
    is powered off."""
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
            log.info("  GNS3 VM '%s' found, IP: %s", vm.name, address)
            return address
    return None


def read_server(
    server_cfg: dict, timeout: int, log: logging.Logger, esxi_password: str
) -> dict:
    name = server_cfg.get("name", server_cfg["esxi_host"])
    esxi_host = server_cfg["esxi_host"]
    esxi_username = server_cfg.get("esxi_username", "root")
    gns3_vm_name = server_cfg.get("gns3_vm_name")
    port = server_cfg.get("port", 80)

    log.info("Server '%s': resolving GNS3 VM via ESXi host %s ...", name, esxi_host)

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
        log.error("  Could not connect to ESXi host %s: %s", esxi_host, e.msg)
        result["status"] = "esxi_unreachable"
        result["error"] = e.msg
        return result
    except Exception as e:  # noqa: BLE001 - deliberately broad, see below
        # Broader than just vim.fault.VimFault (login errors): SmartConnect
        # can also fail with other exception types on network problems
        # (host down, DNS, TLS handshake - socket/ssl level). A single
        # unreachable ESXi host shouldn't abort the whole run and lose the
        # results for the other servers.
        log.error("  Could not connect to ESXi host %s: %s", esxi_host, e)
        result["status"] = "esxi_unreachable"
        result["error"] = str(e)
        return result

    try:
        vm = find_gns3_vm(instance, gns3_vm_name, log)
        host = None
        vm_name = None
        power_state = None
        if vm is not None:
            # Grabbed here, still inside the connected session - vm's
            # properties (name, runtime.powerState) are fetched lazily
            # over that session, so reading them after Disconnect()
            # below raises vim.fault.NotAuthenticated.
            vm_name = vm.name
            host = extract_vm_ipv4(vm, log)
            if host is None:
                power_state = str(vm.runtime.powerState)
    finally:
        Disconnect(instance)

    if vm is None:
        result["status"] = "gns3_vm_not_found"
        return result

    if host is None:
        log.warning(
            "  GNS3 VM '%s' found, but no IP reported yet (power state: %s).",
            vm_name,
            power_state,
        )
        result["status"] = "gns3_vm_no_ip"
        result["vm_name"] = vm_name
        result["vm_power_state"] = power_state
        result["error"] = (
            f"GNS3 VM '{vm_name}' was found but has no reported IPv4 address yet "
            f"(power state: {power_state}) - if it's powered off, start it; "
            f"if it's booting, wait for VMware Tools to report an IP."
        )
        return result

    result["host"] = host
    base_url = f"http://{host}:{port}"

    username = server_cfg.get("username")
    password = server_cfg.get("password")
    auth = (username, password) if username and password else None

    try:
        templates = get_templates(base_url, auth, timeout, log)
    except requests.RequestException as e:
        log.error("  Could not fetch templates: %s", e)
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
        description="Reads templates from multiple GNS3 servers (via config)."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--profiles-dir",
        default=os.getenv("HOST_PROFILES_DIR"),
        help="Take the servers from the host profiles in this folder instead of the config file's 'servers' "
        "(default: the HOST_PROFILES_DIR environment variable, if set)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config, need_servers=not args.profiles_dir)

    log = setup_logging(config.get("log_file"))
    timeout = config.get("request_timeout", DEFAULT_TIMEOUT)
    # pyVmomi has no timeout of its own: an ESXi host that accepts the connection and then stops answering
    # (it does when it is going down) would block this run forever, and the timer never starts a second one.
    socket.setdefaulttimeout(max(timeout, ESXI_SOCKET_TIMEOUT_SECONDS))

    if args.profiles_dir:
        if Path(args.profiles_dir).is_dir():
            config["servers"] = servers_from_profiles(args.profiles_dir, log)
        else:
            # The app creates this folder when the first host profile is saved: until then there are simply no hosts, and
            # an empty list is written so the template API is healthy instead of waiting for a host.
            log.warning(
                "The profiles folder %s does not exist (yet) - no hosts.",
                args.profiles_dir,
            )
            config["servers"] = []
        log.info(
            "Using %d host profile(s) from %s",
            len(config["servers"]),
            args.profiles_dir,
        )

    operator_url = os.getenv("OPERATOR_URL", "").strip()
    operator_token = os.getenv("OPERATOR_TOKEN", "").strip()

    log.info("=" * 60)
    log.info(
        "Starting GNS3 template collection for %d server(s)", len(config["servers"])
    )

    all_results = []
    for server_cfg in config["servers"]:
        name = server_cfg.get("name", server_cfg["esxi_host"])
        password = server_cfg.get("esxi_password")
        problem = None
        if not password:
            if operator_url and operator_token:
                try:
                    username, password = fetch_operator_login(
                        operator_url,
                        operator_token,
                        name,
                        server_cfg["esxi_host"],
                        timeout,
                    )
                    server_cfg = {**server_cfg, "esxi_username": username}
                except OperatorLoginError as exc:
                    problem = str(exc)
            else:
                problem = NO_LOGIN_TEXT.format(name=name)
        if problem:
            # Not worth aborting the other hosts for.
            log.error(problem)
            all_results.append(
                {
                    "name": name,
                    "esxi_host": server_cfg["esxi_host"],
                    "port": server_cfg.get("port", 80),
                    "status": "esxi_unreachable",
                    "error": "no ESXi login for this host",
                    "templates": [],
                }
            )
            continue
        all_results.append(read_server(server_cfg, timeout, log, password))

    output = {
        "version": 1,
        "servers": all_results,
    }

    output_file = config.get("output_file", "gns3_templates.yml")
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Nothing could be read (the Operator or every ESXi host was unreachable for a moment): an empty list must not
    # replace the last good one - the template API would serve nothing until the next successful run.
    nothing_read = bool(all_results) and not any(
        r["status"] == "ok" for r in all_results
    )
    if nothing_read and output_path.exists():
        log.error(
            "No server could be read - keeping the previous %s instead of writing an empty list.",
            output_path,
        )
        return 1

    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(
            output, f, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
    tmp_path.replace(output_path)

    log.info("-" * 60)
    total_templates = sum(len(r["templates"]) for r in all_results)
    reachable = sum(1 for r in all_results if r["status"] == "ok")
    log.info("Servers reachable:     %d / %d", reachable, len(all_results))
    log.info("Templates total:       %d", total_templates)
    log.info("Written to:            %s", output_path)
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
