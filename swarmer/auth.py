from pathlib import Path

import argon2

_ph = argon2.PasswordHasher()


def load_hash(hash_file: Path) -> str:
    """Read the stored argon2 hash from disk."""
    return hash_file.read_text().strip()


def verify_password(plain: str, stored_hash: str) -> bool:
    try:
        return _ph.verify(stored_hash, plain)
    except argon2.exceptions.VerifyMismatchError:
        return False
    except argon2.exceptions.VerificationError:
        return False


def hash_password(plain: str) -> str:
    return _ph.hash(plain)
