"""Make the vcsim simulator more interesting for demos: creates snapshots on two VMs and powers one VM off.
Run AFTER starting vcsim:   python scripts/vcsim_demo_setup.py
"""
import sys
import time

from pyVim.connect import SmartConnect
from pyVmomi import vim

HOST, PORT, USER, PWD = "127.0.0.1", 8989, "user", "pass"


def wait(task):
    while task.info.state in ("queued", "running"):
        time.sleep(0.3)
    if task.info.state == "error":
        raise RuntimeError(task.info.error.msg)


def main():
    si = SmartConnect(host=HOST, port=PORT, user=USER, pwd=PWD, disableSslCertValidation=True)
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    vms = list(view.view)
    if len(vms) < 3:
        sys.exit("Need at least 3 VMs in the simulator")
    for vm, name in ((vms[0], "before-patching"), (vms[1], "test-snapshot")):
        wait(vm.CreateSnapshot_Task(name=name, description="demo", memory=False, quiesce=False))
        print(f"snapshot '{name}' created on {vm.name}")
    wait(vms[2].PowerOffVM_Task())
    print(f"{vms[2].name} powered off")


if __name__ == "__main__":
    main()
