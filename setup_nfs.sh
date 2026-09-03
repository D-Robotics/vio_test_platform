#!/usr/bin/env bash
# One-time host setup: install & configure the NFS server exporting DATA_ROOT.
# Run with sudo:  sudo bash vio_test_platform/setup_nfs.sh
set -e
DATA_ROOT="${DATA_ROOT:-/home/hobot/work/cc_ws/tros_ws}"
if ! dpkg -s nfs-kernel-server >/dev/null 2>&1; then
    echo "installing nfs-kernel-server ..."
    apt-get update && apt-get install -y nfs-kernel-server
fi
ENTRY="${DATA_ROOT} *(rw,sync,no_subtree_check,insecure)"
if grep -qF "$DATA_ROOT" /etc/exports; then
    echo "export entry for ${DATA_ROOT} already present"
else
    echo "$ENTRY" >> /etc/exports
    echo "added: $ENTRY"
fi
exportfs -ra
echo "done. boards can now mount: mount -t nfs <host_ip>:${DATA_ROOT} /mnt/vio_datasets"
