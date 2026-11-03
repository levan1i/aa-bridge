#!/bin/bash
# Generate what this repo deliberately does not ship (it comes from the GPL'd aasdk project):
#   gen/    Python protobuf modules for the Android Auto protocol  (needs `protoc`)
#   certs/  the head-unit TLS certificate + key the phones accept   (extracted from aasdk's source)
# Usage: scripts/fetch-assets.sh        (or point AASDK_DIR at an existing aasdk checkout)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
AASDK_URL=${AASDK_URL:-https://github.com/OpenDsh/aasdk}
AASDK_REV=${AASDK_REV:-1bc0fe69d5f5f505c978a0c6e32c860e820fa8f6}
command -v protoc >/dev/null || { echo "protoc not found (apt install protobuf-compiler)"; exit 1; }
if [ -n "${AASDK_DIR:-}" ]; then
  SRC=$AASDK_DIR
else
  TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
  git clone --quiet "$AASDK_URL" "$TMP/aasdk"
  git -C "$TMP/aasdk" checkout --quiet "$AASDK_REV"
  SRC=$TMP/aasdk
fi
mkdir -p "$ROOT/gen" "$ROOT/certs"
protoc -I "$SRC/aasdk_proto" --python_out="$ROOT/gen" "$SRC"/aasdk_proto/*.proto
python3 -I - "$SRC/src/Messenger/Cryptor.cpp" "$ROOT/certs" <<'PY'
import re, sys, os
src, out = open(sys.argv[1]).read(), sys.argv[2]
for var, name in (("cCertificate", "hu_cert.pem"), ("cPrivateKey", "hu_key.pem")):
    m = re.search(r"Cryptor::%s\s*=\s*\"(.*?)\";" % var, src, re.S)
    if not m:
        sys.exit("could not find %s in Cryptor.cpp" % var)
    pem = m.group(1).replace("\\\n", "").replace("\\n", "\n")
    open(os.path.join(out, name), "w").write(pem)
os.chmod(os.path.join(out, "hu_key.pem"), 0o600)
PY
echo "gen/: $(ls "$ROOT/gen" | wc -l) modules, certs/: $(ls "$ROOT/certs" | tr '\n' ' ')"
