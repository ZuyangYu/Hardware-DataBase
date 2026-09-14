#!/usr/bin/env bash
set -euo pipefail

# Route the published observability ports through the normal host route instead
# of the FlClash TUN route.  The 0x6080 mark is already used by this host's
# policy rule (priority 100) to select the main routing table.

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root: sudo bash $0 [--persist]" >&2
  exit 1
fi

readonly chain='HDB_OBSERVABILITY_BYPASS'
readonly mark='0x6080/0xffff'

iptables -t mangle -N "${chain}" 2>/dev/null || true

if ! iptables -t mangle -C PREROUTING -j "${chain}" 2>/dev/null; then
  iptables -t mangle -I PREROUTING 1 -j "${chain}"
fi

# Restore the connection mark for packets after the initial SYN.
if ! iptables -t mangle -C "${chain}" \
  -m conntrack --ctstate RELATED,ESTABLISHED \
  -j CONNMARK --restore-mark --nfmask 0xffffffff --ctmask 0xffffffff 2>/dev/null; then
  iptables -t mangle -A "${chain}" \
    -m conntrack --ctstate RELATED,ESTABLISHED \
    -j CONNMARK --restore-mark --nfmask 0xffffffff --ctmask 0xffffffff
fi

# Mark new connections before Docker DNAT.  Normally traffic arrives through
# eth0; when FlClash TUN injects a packet locally, the ingress interface is
# FlClash instead.  Both paths must use the main table so the reply does not
# get sent back through the proxy.
for ingress in eth0 FlClash; do
  while iptables -t mangle -C "${chain}" \
    -i "${ingress}" -p tcp -m multiport --dports 3000,6006,6080 \
    -m conntrack --ctstate NEW \
    -j CONNMARK --set-xmark "${mark}" 2>/dev/null; do
    iptables -t mangle -D "${chain}" \
      -i "${ingress}" -p tcp -m multiport --dports 3000,6006,6080 \
      -m conntrack --ctstate NEW \
      -j CONNMARK --set-xmark "${mark}"
  done
  iptables -t mangle -I "${chain}" 2 \
    -i "${ingress}" -p tcp -m multiport --dports 3000,6006,6080 \
    -m conntrack --ctstate NEW \
    -j CONNMARK --set-xmark "${mark}"
done

if ! iptables -t mangle -C "${chain}" \
  -m connmark --mark "${mark}" \
  -j MARK --set-xmark "${mark}" 2>/dev/null; then
  iptables -t mangle -A "${chain}" \
    -m connmark --mark "${mark}" \
    -j MARK --set-xmark "${mark}"
fi

# FlClash normally creates this rule while TUN is enabled.  Recreate it if
# TUN was restarted and the rule is absent.
if ! ip -4 rule show | grep -Eq '^[[:space:]]*100:.*fwmark 0x6080/0xffff lookup main'; then
  ip -4 rule add pref 100 fwmark "${mark}" lookup main
fi

if [[ ${1:-} == '--persist' ]]; then
  install -d -m 0755 /etc/iptables
  tmp_file=$(mktemp /etc/iptables/rules.v4.observability.XXXXXX)
  iptables-save > "${tmp_file}"
  chmod 0644 "${tmp_file}"
  mv "${tmp_file}" /etc/iptables/rules.v4
  echo 'iptables rules saved to /etc/iptables/rules.v4'
fi

iptables -t mangle -L "${chain}" -n -v --line-numbers
ip -4 rule show | sed -n '1,8p'
