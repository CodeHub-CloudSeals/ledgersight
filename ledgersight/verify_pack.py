"""Independent verifier for an exported review pack.

    python -m ledgersight.verify_pack harborline_RUN-xxxx_review_pack.json [trusted_public_key.pem]

Pass the public key you obtained separately (e.g. from the Trust tab or CloudSeals)
to avoid trusting the key embedded in the pack itself.
"""
import json
import sys
from pathlib import Path

from .core.evidence import verify


def main():
    pack = json.loads(Path(sys.argv[1]).read_text())
    key = Path(sys.argv[2]).read_bytes() if len(sys.argv) > 2 else pack["evidence"]["public_key_pem"].encode()
    res = verify(pack["evidence"]["events"], key)
    src = "supplied key file" if len(sys.argv) > 2 else "key embedded in pack (weaker: supply your own copy)"
    print(f"{res['state']}: {res['reason']} ({res['checked']} events, {src})")
    sys.exit(0 if res["state"] == "PASS" else 1)


if __name__ == "__main__":
    main()
