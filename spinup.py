#!/usr/bin/env python3
"""Spin up clean Ubuntu VMs in vCenter from a golden image.

Clones the golden image (see README.md for how to build it) and gives every
clone its own cloud-init identity through vSphere guest customization: a fresh
instance-id, hostname = VM name, and DHCP keyed on the MAC address. It powers
each clone on and waits until VMware Tools reports the new hostname and an IP.
Every VM it creates is recorded in a registry file (`spinup-vms.json`), and
`destroy` only ever deletes VMs from that registry.

    ./spinup.py create --source ubuntu-template --count 3 --folder Lab/Test --cluster CL01
    ./spinup.py list
    ./spinup.py destroy --name 'ubuntu-lab-0*'

Credentials: --vcenter/--user, then VCENTER_SERVER / VCENTER_USER /
VCENTER_PASSWORD, then a prompt. The password is never written to disk.
Needs the vCenter /api endpoints (vSphere 7.0U3 or later). Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import getpass
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

EXIT_OK, EXIT_ABORTED, EXIT_ARGS, EXIT_FAILED, EXIT_VCENTER = 0, 1, 2, 4, 6
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
DEFAULT_KEYS = ("~/.ssh/id_ed25519.pub", "~/.ssh/id_ecdsa.pub", "~/.ssh/id_rsa.pub")
ENV_SERVER, ENV_USER, ENV_PASSWORD = "VCENTER_SERVER", "VCENTER_USER", "VCENTER_PASSWORD"
_PRINT = threading.Lock()


class SpinupError(Exception):
    """Bad input or a VM that did not come up: exit 2 or a per-VM failure."""


class VCenterError(Exception):
    """vCenter refused or could not be reached."""


def say(msg: str) -> None:
    with _PRINT:
        print(msg, flush=True)


def q(ident: str) -> str:
    return urllib.parse.quote(ident, safe="")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ vCenter
def resolve_credentials(server: Optional[str], user: Optional[str]) -> tuple:
    server = server or os.environ.get(ENV_SERVER)
    user = user or os.environ.get(ENV_USER)
    password = os.environ.get(ENV_PASSWORD)
    if not server:
        raise SpinupError("no vCenter given; pass --vcenter or set {}".format(ENV_SERVER))
    if not user:
        raise SpinupError("no vCenter user given; pass --user or set {}".format(ENV_USER))
    if not password:
        if not sys.stdin or not sys.stdin.isatty():
            raise SpinupError("no vCenter password; set {} (or run interactively)".format(ENV_PASSWORD))
        password = getpass.getpass("Password for {}@{}: ".format(user, server))
    return server, user, password


class VCenter:
    """The few vCenter REST calls this tool needs. The session lives in memory only."""

    def __init__(self, server: str, user: str, password: str, insecure: bool = False,
                 timeout: int = 1800):
        raw = server.strip().rstrip("/")
        # An explicit http:// is honoured (the test fake); anything else is HTTPS.
        self.scheme = "http" if raw.lower().startswith("http://") else "https"
        self.server = raw.split("://", 1)[-1]
        self._auth = base64.b64encode("{}:{}".format(user, password).encode()).decode()
        self._token = ""
        self.timeout = timeout          # a clone call only returns once the clone is done
        self._ctx = ssl.create_default_context()
        if insecure:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def __enter__(self) -> "VCenter":
        token = self.call("POST", "/session", auth=True)
        self._token, self._auth = str(token), ""
        say("connected to {}".format(self.server))
        return self

    def __exit__(self, *exc) -> None:
        if self._token:
            try:
                self.call("DELETE", "/session")
            except VCenterError:
                pass
            self._token = ""

    def call(self, method: str, path: str, body: Any = None, auth: bool = False) -> Any:
        hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            hdrs["Authorization"] = "Basic " + self._auth
        elif self._token:
            hdrs["vmware-api-session-id"] = self._token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("{}://{}/api{}".format(self.scheme, self.server, path),
                                     method=method, headers=hdrs, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in (401, 403):
                raise VCenterError("vCenter rejected the credentials ({}): {}".format(exc.code, detail)) from exc
            raise VCenterError("{} {} -> HTTP {}: {}".format(method, path, exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            hint = " (self-signed certificate? add --insecure)" \
                if isinstance(reason, ssl.SSLCertVerificationError) else ""
            raise VCenterError("cannot reach {}: {}{}".format(self.server, reason, hint)) from exc
        return json.loads(text) if text.strip() else None

    def get(self, path: str, **params: Any) -> Any:
        if params:
            path += "?" + urllib.parse.urlencode(params, doseq=True)
        return self.call("GET", path)

    # -- lookups
    def vm_detail(self, moref: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get("/vcenter/vm/{}".format(q(moref)))
        except VCenterError:
            return None

    def vm_names(self) -> List[str]:
        return [vm.get("name", "") for vm in self.get("/vcenter/vm") or []]

    def vms_in_folder(self, folder_id: str) -> List[str]:
        return [vm["vm"] for vm in self.get("/vcenter/vm", folders=folder_id) or []]

    def identity(self, moref: str) -> Dict[str, Any]:
        """Hostname and IP as VMware Tools reports them ({} while Tools is not up)."""
        try:
            return self.get("/vcenter/vm/{}/guest/identity".format(q(moref))) or {}
        except VCenterError:
            return {}

    def find_vm(self, ref: str) -> str:
        """A moref, or the moref of the one VM (or template) with this name."""
        if re.fullmatch(r"vm-\d+", ref):
            if self.vm_detail(ref) is None:
                raise SpinupError("no VM {} in vCenter".format(ref))
            return ref
        hits = [vm["vm"] for vm in self.get("/vcenter/vm", names=ref) or []]
        if not hits:
            raise SpinupError("no VM named {!r}. If it is a template the API does not list, "
                              "pass its moref instead (vm-NNN, shown in the vSphere Client URL)".format(ref))
        if len(hits) > 1:
            raise SpinupError("{} VMs are named {!r} ({}); pass the moref".format(len(hits), ref, ", ".join(hits)))
        return hits[0]

    def folder_id(self, path: str) -> str:
        """A VM folder by its path below the datacenter, e.g. Lab/Test."""
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            raise SpinupError("--folder needs a folder path below the datacenter, e.g. Lab/Test")
        hits = []
        for dc in self.get("/vcenter/datacenter") or []:
            parent = (self.get("/vcenter/datacenter/{}".format(q(dc["datacenter"]))) or {}).get("vm_folder")
            for part in parts:
                if not parent:
                    break
                kids = self.get("/vcenter/folder", type="VIRTUAL_MACHINE", parent_folders=parent) or []
                parent = next((k["folder"] for k in kids if k.get("name", "").lower() == part.lower()), None)
            if parent:
                hits.append((dc.get("name", dc["datacenter"]), parent))
        if not hits:
            raise SpinupError("no VM folder {!r}; give the full path from the datacenter down "
                              "(e.g. Migration/Wave3), and create it in vCenter first".format(path))
        if len(hits) > 1:
            raise SpinupError("folder {!r} exists in {} datacenters ({})".format(
                path, len(hits), ", ".join(dc for dc, _ in hits)))
        return hits[0][1]

    def named(self, kind: str, path: str, name: str) -> Dict[str, Any]:
        items = list(self.get(path) or [])
        hits = [it for it in items if it.get("name") == name]
        if not hits:
            raise SpinupError("no {} named {!r} (have: {})".format(
                kind, name, ", ".join(sorted(it.get("name", "") for it in items)) or "none"))
        if len(hits) > 1:
            raise SpinupError("{} {}s are named {!r}".format(len(hits), kind, name))
        return hits[0]


# ------------------------------------------------------------------ registry
class Registry:
    """The VMs this tool created -- the only VMs `destroy` will touch."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.vms: List[Dict[str, Any]] = []
        if path.is_file():
            self.vms = list(json.loads(path.read_text(encoding="utf-8") or "{}").get("vms") or [])

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"vms": self.vms}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self.vms.append(rec)
            self._save()

    def update(self, moref: str, server: str, **fields: Any) -> None:
        with self._lock:
            for rec in self.vms:
                if rec["moref"] == moref and rec["vcenter"] == server:
                    rec.update(fields)
            self._save()

    def remove(self, moref: str, server: str) -> None:
        with self._lock:
            self.vms = [r for r in self.vms if not (r["moref"] == moref and r["vcenter"] == server)]
            self._save()

    def on(self, server: str) -> List[Dict[str, Any]]:
        return [r for r in self.vms if r["vcenter"] == server]


# ------------------------------------------------------------------ cloud-init
def build_metadata(name: str, instance_id: str) -> str:
    """cloud-init metadata. JSON is valid YAML, so no YAML library is needed."""
    return json.dumps({
        # A new instance-id is what makes cloud-init treat the clone as a new
        # machine: SSH host keys regenerated, hostname and network applied.
        "instance-id": instance_id,
        "local-hostname": name,
        "network": {"version": 2, "ethernets": {"nics": {
            "match": {"name": "e*"},
            "dhcp4": True,
            # networkd derives its DHCP client id from /etc/machine-id. Key the
            # lease on the MAC instead, so two clones can never share an address.
            "dhcp-identifier": "mac",
        }}},
    }, indent=2)


def build_userdata(name: str, user: str, keys: List[str], password_hash: str,
                   template: Optional[str]) -> str:
    if template is not None:
        return template.replace("{{hostname}}", name)
    account: Dict[str, Any] = {
        "name": user, "groups": ["sudo"], "shell": "/bin/bash",
        "sudo": "ALL=(ALL) NOPASSWD:ALL", "lock_passwd": not password_hash,
    }
    if keys:
        account["ssh_authorized_keys"] = keys
    if password_hash:
        account["passwd"] = password_hash
    cfg = {
        "hostname": name, "preserve_hostname": False, "manage_etc_hosts": True,
        "users": [account], "ssh_pwauth": bool(password_hash),
    }
    return "#cloud-config\n" + json.dumps(cfg, indent=2) + "\n"


def customization_spec(metadata: str, userdata: str) -> Dict[str, Any]:
    # Cloud-init customization carries the network in the metadata, so the
    # per-adapter and global DNS settings must be present and empty.
    return {"spec": {
        "configuration_spec": {"cloud_config": {
            "type": "CLOUDINIT", "cloudinit": {"metadata": metadata, "userdata": userdata}}},
        "global_DNS_settings": {},
        "interfaces": [],
    }}


# ------------------------------------------------------------------ create
def allocate_names(prefix: str, count: int, taken: List[str]) -> List[str]:
    """`count` new names after the highest `prefix-NN` already in use."""
    pat = re.compile(r"^{}-(\d+)$".format(re.escape(prefix)), re.IGNORECASE)
    used = [int(m.group(1)) for m in (pat.match(t) for t in taken) if m]
    start = max(used, default=0) + 1
    width = max(2, len(str(start + count - 1)))
    return ["{}-{:0{}d}".format(prefix, n, width) for n in range(start, start + count)]


def usable_ip(ip: str) -> bool:
    return bool(ip) and not ip.lower().startswith(("fe80", "169.254.", "127."))


def wait_ready(vc: VCenter, moref: str, name: str, timeout: float, interval: float) -> Dict[str, str]:
    """Wait until the guest reports the new hostname and a routable IP."""
    deadline = time.monotonic() + timeout
    custom, host, ip = "", "", ""
    while True:
        try:
            st = vc.get("/vcenter/vm/{}/guest/customization".format(q(moref))) or {}
            custom = str(st.get("status") or "")
            if custom == "FAILED":
                raise SpinupError("guest customization failed: {}".format(
                    json.dumps(st.get("error") or "no detail")[:300]))
        except VCenterError:
            pass
        ident = vc.identity(moref)
        host = str(ident.get("host_name") or "")
        ip = str(ident.get("ip_address") or "")
        if host.split(".")[0].lower() == name and usable_ip(ip):
            return {"hostname": host, "ip": ip}
        if time.monotonic() > deadline:
            raise SpinupError("not ready after {}s (customization {}, hostname {!r}, ip {!r})".format(
                int(timeout), custom or "unknown", host, ip))
        time.sleep(interval)


def read_keys(paths: List[str]) -> List[str]:
    keys = []
    for p in paths:
        text = Path(os.path.expanduser(p)).read_text(encoding="utf-8")
        keys += [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return keys


def cmd_create(args: argparse.Namespace, vc: VCenter, reg: Registry) -> int:
    prefix = args.prefix.lower()
    if not HOSTNAME_RE.match(prefix) or len(prefix) > 58:
        raise SpinupError("--prefix {!r} must be a hostname: a-z, 0-9 and '-', at most 58 characters"
                          .format(args.prefix))
    if args.count < 1:
        raise SpinupError("--count must be at least 1")

    template = None
    if args.user_data:
        template = Path(args.user_data).read_text(encoding="utf-8")
        if not template.startswith(("#cloud-config", "#!")):
            raise SpinupError("{} must start with '#cloud-config' (or '#!' for a script)".format(args.user_data))
    key_files = args.ssh_key or [p for p in DEFAULT_KEYS if Path(os.path.expanduser(p)).is_file()][:1]
    keys = read_keys(key_files)
    if template is None and not keys and not args.password_hash:
        raise SpinupError("the new VMs' {!r} user would have no way in: no SSH public key at {}. "
                          "Make one (ssh-keygen -t ed25519), or pass --ssh-key FILE, "
                          "--password-hash \"$(openssl passwd -6)\" or --user-data FILE".format(
                              args.user, " or ".join(DEFAULT_KEYS)))
    if key_files and not args.ssh_key:
        say("using SSH key {}".format(key_files[0]))

    source = vc.find_vm(args.source)
    detail = vc.vm_detail(source) or {}
    src_name = detail.get("name") or args.source
    for cd in (detail.get("cdroms") or {}).values():
        if (cd.get("backing") or {}).get("type") == "ISO_FILE":
            say("warning: {} has an ISO attached and every clone inherits it. "
                "Detach it from the golden image.".format(src_name))
            break

    folder = vc.folder_id(args.folder)
    placement: Dict[str, str] = {"folder": folder}
    if args.cluster:
        placement["cluster"] = vc.named("cluster", "/vcenter/cluster", args.cluster)["cluster"]
    if args.datastore:
        placement["datastore"] = vc.named("datastore", "/vcenter/datastore", args.datastore)["datastore"]
    spec_base: Dict[str, Any] = {"source": source, "power_on": False, "placement": placement}
    if args.network:
        nics = sorted((detail.get("nics") or {}).keys())
        if not nics:
            raise SpinupError("{} has no network adapter to attach to {}".format(src_name, args.network))
        net = vc.named("network", "/vcenter/network", args.network)
        spec_base["hardware_customization"] = {"nics": {nics[0]: {
            "backing": {"type": net["type"], "network": net["network"]}, "start_connected": True}}}

    names = allocate_names(prefix, args.count, vc.vm_names() + [r["name"] for r in reg.vms])
    say("creating {} from {} ({}) in {}: {}".format(
        len(names), src_name, source, args.folder, ", ".join(names)))

    def one(name: str) -> Dict[str, Any]:
        instance_id = "{}-{}".format(name, uuid.uuid4().hex[:8])
        moref = ""
        try:
            say("[{}] cloning".format(name))
            moref = str(vc.call("POST", "/vcenter/vm?action=clone", dict(spec_base, name=name)))
            reg.add({"name": name, "moref": moref, "vcenter": vc.server, "source": src_name,
                     "folder": args.folder, "folder_id": folder, "instance_id": instance_id,
                     "created": now(), "state": "cloned", "ip": "", "error": ""})
            meta = build_metadata(name, instance_id)
            user = build_userdata(name, args.user, keys, args.password_hash or "", template)
            vc.call("PUT", "/vcenter/vm/{}/guest/customization".format(q(moref)),
                    customization_spec(meta, user))
            reg.update(moref, vc.server, state="customized")
            vc.call("POST", "/vcenter/vm/{}/power?action=start".format(q(moref)))
            if args.no_wait:
                reg.update(moref, vc.server, state="powered_on")
                say("[{}] {} powered on".format(name, moref))
                return {"name": name, "ok": True}
            say("[{}] {} powered on, waiting for cloud-init".format(name, moref))
            got = wait_ready(vc, moref, name, args.wait_timeout, args.poll_interval)
            reg.update(moref, vc.server, state="ready", ip=got["ip"])
            say("[{}] ready  {}  {}".format(name, got["hostname"], got["ip"]))
            return {"name": name, "ok": True}
        except (VCenterError, SpinupError) as exc:
            if moref:
                reg.update(moref, vc.server, state="failed", error=str(exc)[:500])
            say("[{}] FAILED: {}".format(name, exc))
            return {"name": name, "ok": False}

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        results = list(pool.map(one, names))
    bad = [r["name"] for r in results if not r["ok"]]
    say("\n{} of {} created{}".format(len(results) - len(bad), len(results),
                                      "; failed: " + ", ".join(bad) if bad else ""))
    return EXIT_FAILED if bad else EXIT_OK


# ------------------------------------------------------------------ list / destroy
def live_view(vc: VCenter, rec: Dict[str, Any]) -> Dict[str, str]:
    """What vCenter says about a registry entry now, and whether it is still where we left it."""
    detail = vc.vm_detail(rec["moref"])
    if detail is None:
        return {"power": "missing", "ip": "", "hostname": "", "note": "gone from vCenter"}
    out = {"power": str(detail.get("power_state") or ""), "ip": "", "hostname": "", "note": ""}
    if detail.get("name") != rec["name"]:
        out["note"] = "renamed to {!r}".format(detail.get("name"))
    elif rec.get("folder_id") and rec["moref"] not in vc.vms_in_folder(rec["folder_id"]):
        out["note"] = "moved out of {}".format(rec.get("folder"))
    if out["power"] == "POWERED_ON":
        ident = vc.identity(rec["moref"])
        out["ip"] = str(ident.get("ip_address") or "")
        out["hostname"] = str(ident.get("host_name") or "")
    return out


def cmd_list(args: argparse.Namespace, vc: VCenter, reg: Registry) -> int:
    recs = reg.on(vc.server)
    if not recs:
        say("no VMs recorded for {} in {}".format(vc.server, reg.path))
        return EXIT_OK
    rows = [("name", "moref", "power", "ip", "hostname", "state", "created", "note")]
    for rec in recs:
        live = live_view(vc, rec)
        rows.append((rec["name"], rec["moref"], live["power"], live["ip"] or rec.get("ip", ""),
                     live["hostname"], rec.get("state", ""), rec.get("created", ""),
                     live["note"] or rec.get("error", "")[:60]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for r in rows:
        say("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[-1])
    return EXIT_OK


def confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        say("refusing without --yes in a non-interactive shell")
        return False
    return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")


def cmd_destroy(args: argparse.Namespace, vc: VCenter, reg: Registry) -> int:
    recs = reg.on(vc.server)
    if args.all:
        chosen = recs
    elif args.name or args.failed:
        chosen = [r for r in recs
                  if any(fnmatch.fnmatch(r["name"], pat) for pat in args.name or [])
                  or (args.failed and r.get("state") == "failed")]
    else:
        raise SpinupError("say which: --name GLOB (repeatable), --failed or --all")
    if not chosen:
        say("no recorded VMs match")
        return EXIT_OK

    say("to power off and delete from {}:".format(vc.server))
    for r in chosen:
        say("  {}  {}".format(r["name"], r["moref"]))
    if not confirm("delete {} VM(s)?".format(len(chosen)), args.yes):
        return EXIT_ABORTED

    problems = 0
    for rec in chosen:
        name, moref = rec["name"], rec["moref"]
        try:
            live = live_view(vc, rec)
            if live["power"] == "missing":
                reg.remove(moref, vc.server)
                say("[{}] already gone; dropped from the registry".format(name))
                continue
            if live["note"] and not args.force:
                # Renamed or moved usually means something else owns it now -- a
                # VCF Automation import, for one. Deleting it here would pull the
                # disk out from under that owner.
                say("[{}] skipped: {}; it may belong to something else now "
                    "(--force deletes it anyway)".format(name, live["note"]))
                problems += 1
                continue
            if live["power"] == "POWERED_ON":
                vc.call("POST", "/vcenter/vm/{}/power?action=stop".format(q(moref)))
            vc.call("DELETE", "/vcenter/vm/{}".format(q(moref)))
            reg.remove(moref, vc.server)
            say("[{}] deleted".format(name))
        except (VCenterError, SpinupError) as exc:
            say("[{}] FAILED: {}".format(name, exc))
            problems += 1
    return EXIT_FAILED if problems else EXIT_OK


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="spinup.py", description="Create and clean up cloud-init Ubuntu VMs in vCenter.")
    ap.add_argument("--vcenter", help="vCenter address (or {})".format(ENV_SERVER))
    ap.add_argument("--user", dest="vc_user", help="vCenter user (or {})".format(ENV_USER))
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed vCenter certificate")
    ap.add_argument("--registry", default="spinup-vms.json",
                    help="where created VMs are recorded (default: ./spinup-vms.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="clone, customize and boot new VMs")
    c.add_argument("--source", required=True, help="golden image: VM or template name, or its moref")
    c.add_argument("--folder", required=True, help="VM folder path below the datacenter, e.g. Lab/Test")
    c.add_argument("--count", type=int, default=1)
    c.add_argument("--prefix", default="ubuntu-lab", help="names are PREFIX-01, PREFIX-02, ... (default ubuntu-lab)")
    c.add_argument("--cluster", help="cluster to place the clones in (needed when the source is a template)")
    c.add_argument("--datastore", help="datastore for the clones (default: the source's)")
    c.add_argument("--network", help="portgroup for the first NIC (default: the source's)")
    c.add_argument("--ssh-key", action="append", help="public key file, repeatable (default: ~/.ssh/id_*.pub)")
    c.add_argument("--password-hash", help="crypt hash for the login user, from `openssl passwd -6`")
    c.add_argument("--login", dest="user", default="ubuntu", help="login user to create (default ubuntu)")
    c.add_argument("--user-data", help="your own #cloud-config instead of the generated one; "
                                       "{{hostname}} is replaced per VM")
    c.add_argument("--parallel", type=int, default=2, help="clones in flight at once (default 2)")
    c.add_argument("--no-wait", action="store_true", help="return once powered on")
    c.add_argument("--wait-timeout", type=float, default=600, help="seconds to wait for hostname + IP")
    c.add_argument("--poll-interval", type=float, default=5, help=argparse.SUPPRESS)

    sub.add_parser("list", help="the recorded VMs, with live power state, hostname and IP")

    d = sub.add_parser("destroy", help="power off and delete recorded VMs")
    d.add_argument("--name", action="append", help="name glob, repeatable")
    d.add_argument("--failed", action="store_true", help="the VMs whose create failed")
    d.add_argument("--all", action="store_true", help="every recorded VM on this vCenter")
    d.add_argument("--force", action="store_true", help="delete even a VM that was renamed or moved")
    d.add_argument("-y", "--yes", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    commands: Dict[str, Callable[..., int]] = {
        "create": cmd_create, "list": cmd_list, "destroy": cmd_destroy}
    try:
        server, user, password = resolve_credentials(args.vcenter, args.vc_user)
        reg = Registry(Path(args.registry))
        with VCenter(server, user, password, insecure=args.insecure) as vc:
            return commands[args.cmd](args, vc, reg)
    except SpinupError as exc:
        say("error: {}".format(exc))
        return EXIT_ARGS
    except VCenterError as exc:
        say("vCenter error: {}".format(exc))
        return EXIT_VCENTER
    except (OSError, json.JSONDecodeError) as exc:
        say("error: {}".format(exc))
        return EXIT_ARGS
    except KeyboardInterrupt:
        say("\ninterrupted; VMs created so far are in the registry (spinup.py list)")
        return EXIT_ABORTED


if __name__ == "__main__":
    sys.exit(main())
