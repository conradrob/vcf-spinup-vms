"""Tests for spinup.py, against tests/fake_vcenter.py. Standard library only:

    python -m unittest discover tests
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fake_vcenter  # noqa: E402
import spinup  # noqa: E402
from fake_vcenter import Fake  # noqa: E402


class CloudInitData(unittest.TestCase):
    def test_names_continue_after_the_highest_in_use(self):
        self.assertEqual(spinup.allocate_names("ubuntu-lab", 2, ["ubuntu-lab-07", "UBUNTU-LAB-09", "x-10"]),
                         ["ubuntu-lab-10", "ubuntu-lab-11"])
        self.assertEqual(spinup.allocate_names("t", 1, []), ["t-01"])

    def test_metadata_gives_each_clone_its_own_identity(self):
        meta = json.loads(spinup.build_metadata("ubuntu-lab-01", "ubuntu-lab-01-abcd"))
        self.assertEqual(meta["instance-id"], "ubuntu-lab-01-abcd")
        self.assertEqual(meta["local-hostname"], "ubuntu-lab-01")
        # clones share the template's machine-id until first boot
        self.assertEqual(meta["network"]["ethernets"]["nics"]["dhcp-identifier"], "mac")

    def test_userdata(self):
        user = spinup.build_userdata("h1", "ubuntu", ["ssh-ed25519 AAAA k"], "", None)
        self.assertTrue(user.startswith("#cloud-config\n"))
        cfg = json.loads(user.split("\n", 1)[1])
        self.assertEqual(cfg["hostname"], "h1")
        self.assertEqual(cfg["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 AAAA k"])
        self.assertFalse(cfg["ssh_pwauth"])
        self.assertEqual(spinup.build_userdata("h2", "u", [], "", "#cloud-config\nfqdn: {{hostname}}.lab\n"),
                         "#cloud-config\nfqdn: h2.lab\n")


class AgainstFakeVCenter(unittest.TestCase):
    def setUp(self):
        self.server, url = fake_vcenter.serve()
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Path(self.tmp.name, "k.pub")
        self.key.write_text("ssh-ed25519 AAAAtest lab\n", encoding="utf-8")
        self.reg_path = str(Path(self.tmp.name, "reg.json"))
        env = mock.patch.dict(os.environ, {
            "VCENTER_SERVER": url, "VCENTER_USER": fake_vcenter.USER,
            "VCENTER_PASSWORD": fake_vcenter.PASSWORD, "HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_tool(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = spinup.main(["--registry", self.reg_path] + list(argv))
        return rc, buf.getvalue()

    def registry(self):
        return {r["name"]: r for r in json.loads(Path(self.reg_path).read_text())["vms"]}

    def create(self, *extra):
        return self.run_tool("create", "--source", "ubuntu-template", "--folder", "/lab/test/",
                             "--cluster", "m01-dc01", "--network", "mgmt-tools", "--ssh-key", str(self.key),
                             "--poll-interval", "0.02", *extra)

    def test_create_list_destroy(self):
        rc, out = self.create("--count", "3")
        self.assertEqual(rc, 0, out)
        self.assertIn("has an ISO attached", out)
        recs = self.registry()
        self.assertEqual(sorted(recs), ["ubuntu-lab-01", "ubuntu-lab-02", "ubuntu-lab-03"])
        for name, rec in recs.items():
            vm = Fake.vms[rec["moref"]]
            self.assertEqual((vm["hostname"], vm["power_state"], vm["folder"]), (name, "POWERED_ON", "group-v11"))
            self.assertEqual(vm["nics"]["4000"]["backing"]["network"], "dvportgroup-100")
            self.assertEqual(rec["state"], "ready")
            self.assertTrue(rec["ip"].startswith("10.10.11."))
        self.assertEqual(len({r["instance_id"] for r in recs.values()}), 3)

        rc, out = self.create("--count", "1")
        self.assertEqual((rc, max(self.registry())), (0, "ubuntu-lab-04"), out)

        rc, out = self.run_tool("list")
        self.assertEqual(rc, 0, out)
        self.assertIn("ubuntu-lab-04", out)

        rc, out = self.run_tool("destroy", "--all")
        self.assertEqual(rc, 1, "refuses without --yes when nobody can answer")
        rc, out = self.run_tool("destroy", "--name", "ubuntu-lab-0[12]", "-y")
        self.assertEqual((rc, sorted(self.registry())), (0, ["ubuntu-lab-03", "ubuntu-lab-04"]), out)
        self.assertNotIn("ubuntu-lab-01", {vm["name"] for vm in Fake.vms.values()})
        self.assertIn("ubuntu-template", {vm["name"] for vm in Fake.vms.values()}, "the source is never touched")

    def test_failed_customization_is_recorded_and_cleanable(self):
        Fake.fail_customization = {"ubuntu-lab-01"}
        rc, out = self.create()
        self.assertEqual((rc, self.registry()["ubuntu-lab-01"]["state"]), (4, "failed"), out)
        self.assertIn("guest customization failed", out)
        rc, out = self.run_tool("destroy", "--failed", "-y")
        self.assertEqual((rc, self.registry()), (0, {}), out)

    def test_destroy_leaves_moved_or_renamed_vms_alone(self):
        self.create("--count", "2")
        recs = self.registry()
        Fake.vms[recs["ubuntu-lab-01"]["moref"]]["name"] = "vm-a1b2c3d4"     # what an import looks like
        Fake.vms[recs["ubuntu-lab-02"]["moref"]]["folder"] = "group-v20"
        rc, out = self.run_tool("destroy", "--all", "-y")
        self.assertEqual((rc, len(self.registry())), (4, 2), out)
        self.assertIn("renamed to 'vm-a1b2c3d4'", out)
        self.assertIn("moved out of /lab/test/", out)
        rc, out = self.run_tool("destroy", "--all", "--force", "-y")
        self.assertEqual((rc, self.registry()), (0, {}), out)

    def test_refuses_before_cloning_when_nobody_could_log_in(self):
        rc, out = self.run_tool("create", "--source", "ubuntu-template", "--folder", "Lab/Test")
        self.assertEqual(rc, 2, out)
        self.assertIn("would have no way in", out)
        self.assertEqual(list(Fake.vms), ["vm-100"])

    def test_clear_errors_for_bad_placement(self):
        rc, out = self.create("--folder", "Test")     # not the full path
        self.assertEqual(rc, 2)
        self.assertIn("full path from the datacenter down", out)
        rc, out = self.run_tool("create", "--source", "nope", "--folder", "Lab/Test", "--ssh-key", str(self.key))
        self.assertEqual(rc, 2)
        self.assertIn("no VM named 'nope'", out)

    def test_wrong_password(self):
        with mock.patch.dict(os.environ, {"VCENTER_PASSWORD": "wrong"}):
            rc, out = self.run_tool("list")
        self.assertEqual(rc, 6)
        self.assertIn("rejected the credentials", out)
        self.assertNotIn("wrong", out.replace("rejected", ""))


if __name__ == "__main__":
    unittest.main()
