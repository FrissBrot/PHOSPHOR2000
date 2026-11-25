#!/bin/bash
# Root oneshot phosphor-gateway@on|off: this Pi shares its cable internet with the cluster
# (IP forwarding + masquerading out of the Ethernet interface). Separate nft table, nothing else touched.
set -euo pipefail
ACTION=${1:-}
ETH=${PHOS_ETH:-eth0}
case $ACTION in
  on)
    sysctl -q -w net.ipv4.ip_forward=1
    nft -f - <<NFT
table ip phosphor_gw
delete table ip phosphor_gw
table ip phosphor_gw {
  chain postrouting { type nat hook postrouting priority srcnat; policy accept;
    oifname "$ETH" masquerade }
  chain forward { type filter hook forward priority 0; policy accept;
    oifname "$ETH" accept
    iifname "$ETH" ct state established,related accept }
}
NFT
    echo "sharing internet of $ETH with the cluster" ;;
  off)
    nft delete table ip phosphor_gw 2>/dev/null || true
    echo "internet sharing off" ;;
  *) echo "usage: $0 on|off" >&2; exit 2 ;;
esac
