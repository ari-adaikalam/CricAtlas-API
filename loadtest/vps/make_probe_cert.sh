#!/bin/sh
# Make a short-lived client certificate for the flood check, signed by the origin CA that already lives on this Mac. The certificate Cloudflare holds is never copied anywhere.
#
#   make_probe_cert.sh PKI_DIR OUT_DIR
#
# PKI_DIR is the folder origin_pki.sh made (it must hold ca.pem and ca.key); OUT_DIR must be new or empty, outside any git work tree. It writes probe.key and probe.pem (mode 0600), valid for one day (the
# shortest openssl offers; the check uses it for minutes and the key is deleted from the server when the check ends). The CA key never leaves this machine. Nothing is uploaded or sent.
set -eu
umask 077
[ $# -eq 2 ] || { echo "usage: $0 PKI_DIR OUT_DIR" >&2; exit 2; }
pki="$1"; out="$2"
[ -r "$pki/ca.pem" ] && [ -r "$pki/ca.key" ] || { echo "$pki must hold ca.pem and ca.key (origin_pki.sh pki)" >&2; exit 2; }
if [ -e "$out" ] && [ -n "$(ls -A "$out" 2>/dev/null)" ]; then echo "$out is not empty" >&2; exit 2; fi
mkdir -p "$out"; out="$(cd "$out" && pwd -P)"
if (cd "$out" && git rev-parse --is-inside-work-tree >/dev/null 2>&1); then rmdir "$out" 2>/dev/null || true; echo "$out is inside a git work tree: keys must stay outside the repository" >&2; exit 2; fi
printf 'basicConstraints = CA:FALSE\nkeyUsage = digitalSignature\nextendedKeyUsage = clientAuth\n' > "$out/.probe.cnf"
openssl ecparam -name prime256v1 -genkey -noout -out "$out/probe.key"
openssl req -new -key "$out/probe.key" -subj "/O=cricket-api origin pull/CN=flood-check-probe" -out "$out/.probe.csr"
openssl x509 -req -in "$out/.probe.csr" -CA "$pki/ca.pem" -CAkey "$pki/ca.key" -CAcreateserial -CAserial "$out/.ca.srl" -days 1 -sha256 -extfile "$out/.probe.cnf" -out "$out/probe.pem" 2>/dev/null
rm -f "$out/.probe.csr" "$out/.probe.cnf" "$out/.ca.srl"
openssl verify -CAfile "$pki/ca.pem" "$out/probe.pem" >/dev/null
echo "made $out/probe.pem and $out/probe.key (valid one day, clientAuth only)"
