# vcf-spinup-vms

Spin up clean Ubuntu VMs in vCenter from a golden image, as many as you need,
and tear them down again.

Each clone gets its own cloud-init identity through vSphere guest
customization:
- a new instance-id, so cloud-init treats it as a new machine and regenerates
  the SSH host keys,
- the VM name as its hostname,
- DHCP keyed on its MAC address, so clones never fight over one lease.

`spinup.py` powers each clone on and waits until VMware Tools reports the new
hostname and an IP.

One file, Python 3.8+ standard library only, no install. It needs the vCenter
`/api` REST endpoints (vSphere 7.0U3 or later).

```bash
export VCENTER_SERVER=vcenter.lab.local VCENTER_USER=administrator@vsphere.local
./spinup.py --insecure create --source ubuntu-template --count 3 \
    --folder Lab/Test --cluster CL01 --network mgmt-tools
./spinup.py --insecure list
./spinup.py --insecure destroy --name 'ubuntu-lab-0*'
```

`--insecure` accepts a self-signed vCenter certificate. The password is
prompted for, or read from `VCENTER_PASSWORD`, and never written to disk.

---

## 1. Build the golden image once

Either base works:

- **The Ubuntu cloud image OVA**
  (`https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.ova`).
  It ships with cloud-init and open-vm-tools and has no installer leftovers.
- **A finished Ubuntu Server ISO install.** The installer leaves files behind
  that turn cloud-init off or pin the install-time network. Step 2 removes
  them.

Every command below is a single line, so pasting with or without indentation
works.

**1. Prepare the VM.** Boot it on the network the clones should use, log in,
and install anything every clone should have.

**2. Hand cloud-init over to vSphere customization.** Inside the guest:

```bash
sudo apt-get install -y cloud-init open-vm-tools
printf 'disable_vmware_customization: false\ndatasource_list: [ VMware, OVF, None ]\n' | sudo tee /etc/cloud/cloud.cfg.d/99-vsphere.cfg
# left by an ISO install: cloud-init switched off, installer user-data, installer network
sudo rm -f /etc/cloud/cloud-init.disabled /etc/cloud/cloud.cfg.d/99-installer.cfg /etc/cloud/cloud.cfg.d/90-installer-network.cfg /etc/cloud/cloud.cfg.d/*subiquity-disable-cloudinit-networking.cfg
```

**Check before going further:**

```bash
cat /etc/cloud/cloud.cfg.d/99-vsphere.cfg
ls /etc/cloud/cloud.cfg.d/ /etc/netplan/
systemctl is-active open-vm-tools
```

- `99-vsphere.cfg` holds exactly the two lines above.
- `cloud.cfg.d` lists none of the removed files. Installers prefix some of them,
  for example `00-subiquity-disable-cloudinit-networking.cfg`; that file would leave
  every clone without network config.
- open-vm-tools is `active`.
- `20-disable-cc-dpkg-grub.cfg` and `curtin-preserve-sources.cfg` are harmless
  and can stay.

**3. Remove everything that identifies this machine, and shut down.** Do this
last: until cloud-init writes a new network config on the next boot, the VM
has none.

```bash
sudo rm -f /etc/netplan/*.yaml /etc/ssh/ssh_host_*
sudo cloud-init clean --logs --seed
sudo truncate -s 0 /etc/machine-id
sudo shutdown -h now
```

**4. In vCenter:**
- Disconnect the install ISO from the CD-ROM. `create` warns if the source
  still has one attached.
- Turn off **vApp Options**. An OVA's own hostname and user-data properties
  would otherwise reach every clone.
- Convert the VM to a template, or leave it powered off.

If `create` reports that a template cannot be cloned, convert it back to a VM.
A powered-off VM works as a source in exactly the same way.

Don't boot the golden image again. A boot runs cloud-init and gives the image
an identity again. To change the image, make your changes and repeat steps 2
to 4.

---

## 2. Make VMs

```bash
./spinup.py create --source ubuntu-template --count 3 --folder Lab/Test --cluster CL01
```

| option | |
|---|---|
| `--source` | the golden image: VM or template name, or its moref (`vm-123`) |
| `--folder` | **required.** The VM folder path from the datacenter down, e.g. `Migration/Wave3` |
| `--count` | how many (default 1) |
| `--prefix` | names are `PREFIX-01`, `-02`, … (default `ubuntu-lab`), continuing after the highest number already in vCenter; the hostname is the VM name |
| `--cluster` | cluster to place the clones in; needed when the source is a template |
| `--datastore` | default: the source's |
| `--network` | portgroup for the first NIC; default: the source's |
| `--parallel` | clones in flight at once (default 2) |
| `--no-wait` | return once powered on instead of waiting for hostname and IP |

**Logging in to the clones.** The tool creates a sudo user, `ubuntu` by
default (`--login NAME` changes it). It is reachable by:
- your SSH key: `~/.ssh/id_ed25519.pub` (or `id_ecdsa`/`id_rsa`) by default,
  or `--ssh-key FILE`, repeatable. No key yet? `ssh-keygen -t ed25519`.
- or a password: `--password-hash "$(openssl passwd -6)"`.

`create` refuses to run with neither. Users already in the golden image, such
as the one you made during an ISO install, keep their passwords. Don't point
`--login` at one of them without `--password-hash`: cloud-init would lock its
password.

**Your own cloud-config.** `--user-data FILE` replaces the generated
cloud-config entirely; `{{hostname}}` in it becomes each VM's name. The file
must start with `#cloud-config`.

## 3. List and destroy

Every VM `create` makes is recorded in `./spinup-vms.json`, so always run the
tool from the same directory (`--registry` points elsewhere).

```bash
./spinup.py list                                  # live power state, hostname, IP
./spinup.py destroy --name 'ubuntu-lab-0*'        # name globs, repeatable
./spinup.py destroy --failed                      # VMs whose create failed
./spinup.py destroy --all -y
```

`destroy` deletes only VMs in the registry. It also skips a VM that has been
renamed or moved out of its folder since it was made. That usually means
something else owns it now; an import into VCF Automation does both.
`--force` deletes it anyway.

---

## Troubleshooting

| symptom | cause |
|---|---|
| `the new VMs' 'ubuntu' user would have no way in` | no SSH key at `~/.ssh/id_*.pub` on the machine running the tool; make one, or pass `--ssh-key` / `--password-hash` |
| `no VM folder 'X'` | `--folder` takes the full path from the datacenter down, not just the folder's name |
| `no VM named 'X'` for a template | pass the template's moref (`vm-NNN`, in the vSphere Client URL) |
| clone comes up with the template's hostname, or no IP | cloud-init didn't take the vSphere data: check step 2 in the golden image. In the clone: `sudo cat /var/log/cloud-init.log`, `cloud-id` (should print `vmware`) |
| clones get the same IP | the golden image still had a machine-id or netplan file; redo step 3 |
| `guest customization failed` | vCenter's own log is in the clone at `/var/log/vmware-imc/toolsDeployPkg.log` |

Exit codes: `0` ok · `1` aborted · `2` bad arguments · `4` some VMs failed ·
`6` vCenter error.

## Tests

```bash
python3 -m unittest discover tests
```

`tests/fake_vcenter.py` stands in for the vCenter REST API: clone, cloud-init
customization, power and delete. Nothing real is contacted.
