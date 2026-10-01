"""A stand-in vCenter REST API, just big enough for spinup.py.

Serves plain HTTP on loopback; point the tool at it with an http:// address.
One golden image (`ubuntu-template`, with an ISO attached), a small folder
tree, a cluster, two networks and two datastores. Clones boot instantly: on
power-on, a pending cloud-init customization sets the guest hostname, or
reports FAILED for names listed in `Fake.fail_customization`.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

TOKEN = "fake-session-token"
USER = "administrator@vsphere.local"
PASSWORD = "hunter2"

# folder id -> (parent id, name); group-v1 is the datacenter's hidden root
FOLDERS = {
    "group-v1": (None, ""),
    "group-v10": ("group-v1", "Lab"),
    "group-v11": ("group-v10", "Test"),
    "group-v20": ("group-v1", "Templates"),
}
NETWORKS = [{"network": "dvportgroup-100", "name": "mgmt-tools", "type": "DISTRIBUTED_PORTGROUP"},
            {"network": "network-200", "name": "VM Network", "type": "STANDARD_PORTGROUP"}]
DATASTORES = [{"datastore": "datastore-10", "name": "vsanDatastore"},
              {"datastore": "datastore-11", "name": "nfs-lab"}]
CLUSTERS = [{"cluster": "domain-c8", "name": "m01-dc01"}]


class Fake:
    vms: Dict[str, Dict[str, Any]] = {}
    fail_customization: set = set()

    @classmethod
    def reset(cls) -> None:
        cls.fail_customization = set()
        cls.vms = {"vm-100": {
            "vm": "vm-100", "name": "ubuntu-template", "power_state": "POWERED_OFF",
            "folder": "group-v20", "hostname": "ubuntu-template", "ip": "", "custom": None,
            "custom_status": "IDLE",
            "nics": {"4000": {"backing": {"type": "STANDARD_PORTGROUP", "network": "network-200"}}},
            "cdroms": {"3000": {"backing": {"type": "ISO_FILE"}}},
        }}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, payload: Any = None) -> None:
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _body(self) -> Any:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def _vm(self, path: str):
        return Fake.vms.get(path.split("/")[4]) if path.startswith("/api/vcenter/vm/") else None

    def _authed(self) -> bool:
        if self.headers.get("vmware-api-session-id") == TOKEN:
            return True
        self._send(401, {"error_type": "UNAUTHENTICATED"})
        return False

    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path, query = self._route()
        if path == "/api/session":
            auth = self.headers.get("Authorization", "")
            ok = auth.startswith("Basic ") and \
                base64.b64decode(auth[6:]).decode() == "{}:{}".format(USER, PASSWORD)
            self._send(201, TOKEN) if ok else self._send(401, {"error_type": "UNAUTHENTICATED"})
            return
        if not self._authed():
            return
        action = query.get("action", [""])[0]
        if path == "/api/vcenter/vm" and action == "clone":
            spec = self._body()
            src = Fake.vms.get(spec.get("source"))
            if not src:
                self._send(404, {"error_type": "NOT_FOUND"})
                return
            if any(vm["name"] == spec["name"] for vm in Fake.vms.values()):
                self._send(400, {"error_type": "ALREADY_EXISTS"})
                return
            moref = "vm-{}".format(9000 + len(Fake.vms))
            clone = json.loads(json.dumps(src))
            clone.update(vm=moref, name=spec["name"], power_state="POWERED_OFF",
                         folder=spec["placement"]["folder"], custom=None, custom_status="IDLE",
                         ip="10.10.11.{}".format(100 + len(Fake.vms)))
            for key, upd in ((spec.get("hardware_customization") or {}).get("nics") or {}).items():
                clone["nics"][key]["backing"] = upd["backing"]
            Fake.vms[moref] = clone
            self._send(200, moref)
            return
        vm = self._vm(path)
        if vm and path.endswith("/power"):
            if action == "start":
                vm["power_state"] = "POWERED_ON"
                if vm["custom"]:
                    if vm["name"] in Fake.fail_customization:
                        vm["custom_status"] = "FAILED"
                    else:
                        vm["hostname"] = json.loads(vm["custom"]["metadata"])["local-hostname"]
                        vm["custom_status"] = "SUCCEEDED"
                    vm["custom"] = None
            else:
                vm["power_state"] = "POWERED_OFF"
            self._send(204)
            return
        self._send(404, {"error_type": "NOT_FOUND"})

    def do_PUT(self) -> None:  # noqa: N802
        path, _ = self._route()
        if not self._authed():
            return
        vm = self._vm(path)
        if vm and path.endswith("/guest/customization"):
            if vm["power_state"] != "POWERED_OFF":
                self._send(400, {"error_type": "NOT_ALLOWED_IN_CURRENT_STATE"})
                return
            spec = self._body()["spec"]
            if spec.get("interfaces") != [] or spec.get("global_DNS_settings") != {}:
                self._send(400, {"error_type": "INVALID_ARGUMENT",
                                 "messages": ["cloud-init needs empty interfaces and DNS settings"]})
                return
            vm["custom"] = spec["configuration_spec"]["cloud_config"]["cloudinit"]
            vm["custom_status"] = "PENDING"
            self._send(204)
            return
        self._send(404, {"error_type": "NOT_FOUND"})

    def do_DELETE(self) -> None:  # noqa: N802
        path, _ = self._route()
        if path == "/api/session":
            self._send(204)
            return
        if not self._authed():
            return
        vm = self._vm(path)
        if not vm:
            self._send(404, {"error_type": "NOT_FOUND"})
        elif vm["power_state"] == "POWERED_ON":
            self._send(400, {"error_type": "NOT_ALLOWED_IN_CURRENT_STATE"})
        else:
            del Fake.vms[vm["vm"]]
            self._send(204)

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._route()
        if not self._authed():
            return
        if path == "/api/vcenter/vm":
            items = list(Fake.vms.values())
            if "names" in query:
                items = [v for v in items if v["name"] in query["names"]]
            if "folders" in query:
                items = [v for v in items if v["folder"] in query["folders"]]
            self._send(200, [{"vm": v["vm"], "name": v["name"], "power_state": v["power_state"]}
                             for v in items])
            return
        vm = self._vm(path)
        if vm and path.endswith("/guest/identity"):
            if vm["power_state"] != "POWERED_ON":
                self._send(503, {"error_type": "SERVICE_UNAVAILABLE"})
            else:
                self._send(200, {"host_name": vm["hostname"], "ip_address": vm["ip"]})
            return
        if vm and path.endswith("/guest/customization"):
            self._send(200, {"status": vm["custom_status"]})
            return
        if vm:
            self._send(200, {k: vm[k] for k in ("name", "power_state", "nics", "cdroms")})
            return
        if path == "/api/vcenter/folder":
            parents = query.get("parent_folders")
            self._send(200, [{"folder": fid, "name": name, "type": "VIRTUAL_MACHINE"}
                             for fid, (parent, name) in FOLDERS.items()
                             if parent and (not parents or parent in parents)])
            return
        fixed = {"/api/vcenter/datacenter": [{"datacenter": "datacenter-1", "name": "dc01"}],
                 "/api/vcenter/datacenter/datacenter-1": {"name": "dc01", "vm_folder": "group-v1"},
                 "/api/vcenter/cluster": CLUSTERS, "/api/vcenter/network": NETWORKS,
                 "/api/vcenter/datastore": DATASTORES}
        if path in fixed:
            self._send(200, fixed[path])
            return
        self._send(404, {"error_type": "NOT_FOUND"})


def serve() -> tuple:
    """Start on a free loopback port: (server, base URL)."""
    Fake.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:{}".format(server.server_address[1])
