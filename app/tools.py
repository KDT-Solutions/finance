"""Small CLI helpers: python -m app.tools hash-password | totp-secret | fernet-key"""

from __future__ import annotations

import getpass
import sys


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "hash-password":
        from argon2 import PasswordHasher

        import base64

        pw = getpass.getpass("Passwort: ")
        h = PasswordHasher().hash(pw)
        print("ADMIN_PASSWORD_HASH_B64=" + base64.b64encode(h.encode()).decode())
        return 0
    if cmd == "totp-secret":
        import pyotp

        secret = pyotp.random_base32()
        print(secret)
        print("otpauth-URI:", pyotp.TOTP(secret).provisioning_uri(name="admin", issuer_name="finance"))
        return 0
    if cmd == "fernet-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
