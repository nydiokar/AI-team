#!/usr/bin/env python
"""Generate a VAPID keypair for Web Push (#21), printed in the base64url form the
browser (applicationServerKey) and backend (pywebpush) both expect.

Usage:
    pip install -e ".[push]"          # brings in pywebpush + py-vapid + cryptography
    python scripts/gen_vapid_keys.py  # prints VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY

Then set these in your environment (never commit the private key):
    VAPID_PUBLIC_KEY=<printed public>
    VAPID_PRIVATE_KEY=<printed private>
    VAPID_SUBJECT=mailto:you@example.com   # or https://your.site

The public key is safe to expose (the SW subscribes with it). The private key is
a secret. VAPID_SUBJECT is a contact URI the push service can use to reach you.
"""
import base64
import sys


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> int:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception:
        print(
            "cryptography is not installed. Run:  pip install -e \".[push]\"",
            file=sys.stderr,
        )
        return 1

    private_key = ec.generate_private_key(ec.SECP256R1())

    # Private key: raw 32-byte scalar, base64url (the form py-vapid/pywebpush wants).
    priv_int = private_key.private_numbers().private_value
    priv_raw = priv_int.to_bytes(32, "big")

    # Public key: uncompressed EC point (0x04 || X || Y), 65 bytes, base64url —
    # exactly what the browser expects as applicationServerKey.
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    print("VAPID_PUBLIC_KEY=" + _b64url(pub_raw))
    print("VAPID_PRIVATE_KEY=" + _b64url(priv_raw))
    print("VAPID_SUBJECT=mailto:you@example.com   # <- edit to your real contact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
