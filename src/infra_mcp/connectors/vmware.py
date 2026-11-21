"""VMware connector. Two modes, same tool names:

  mode: soap   (default, recommended) - pyvmomi. Works with vCenter, standalone ESXi and the vcsim simulator.
                                         Has everything: VMs, hosts, host hardware sensors, alarms, snapshots.
  mode: rest   - vSphere Automation REST API (vCenter 7.0U2+/8.x only). Basic inventory tools only.
"""
NAME = "vmware"
ENABLED_BY_DEFAULT = False


def register(mcp, guard, cfg: dict) -> None:
    mode = cfg.get("mode", "soap").lower()
    if mode == "rest":
        from ._vmware_rest import register_rest
        register_rest(mcp, guard, cfg)
    elif mode == "soap":
        from ._vmware_soap import register_soap
        register_soap(mcp, guard, cfg)
    else:
        raise ValueError("vmware.mode must be 'soap' or 'rest'")
