"""Generate a P-256 VAPID key pair for KERYNTIX MARKET DESK Web Push.

Writes the private PEM to a local file with mode 0600 and prints only the public key.
Never commit the generated private key file.
"""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-out", default="vapid_private_key.pem", help="Ruta local para la clave privada PEM")
    args = parser.parse_args()

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_key = base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")
    path = Path(args.private_out).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(private_pem)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f"VAPID_PRIVATE_KEY_PATH={path}")
    print("VAPID_SUBJECT=mailto:admin@your-domain.example")
    print("La clave privada no se imprime. Guárdala fuera del repositorio y no la compartas.")


if __name__ == "__main__":
    main()
