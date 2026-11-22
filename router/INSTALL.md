# OpenClash hook

Copy `openclash-hook.sh` to a protected path on the OpenWrt router and call it from the existing OpenClash custom overwrite script after `CONFIG_FILE` is set:

```sh
WLOC_REMOTE_SERVER=192.168.50.2
WLOC_ROUTER_IP=192.168.50.1
export WLOC_REMOTE_SERVER WLOC_ROUTER_IP
/bin/sh /etc/openclash/custom/wloc-remote-hook.sh "$CONFIG_FILE" "$LOG_FILE"
```

Back up the overwrite script before editing it. The hook is idempotent and atomically rewrites the generated YAML.
