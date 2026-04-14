import base64
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet

_fernet: Fernet | None = None
_FALLBACK = "SWARMER_NOT_CONFIGURED_RUN_MAKE_SETUP_AUTH"


def _read_hash_bytes(hash_file: Path) -> bytes:
    """Read the hash file; return a fallback constant if it doesn't exist yet."""
    try:
        return hash_file.read_bytes()
    except FileNotFoundError:
        return _FALLBACK.encode()


def init_crypto(hash_file: Path) -> None:
    """
    Derive a Fernet key from the contents of the auth hash file.
    Called once during app lifespan startup before any DB access.

    SHA-256(b"fernet:" + hash_file_contents) → 32 bytes → base64url → Fernet key
    A separate SHA-256 (with prefix b"session:") is used for cookie signing.
    """
    global _fernet
    raw = _read_hash_bytes(hash_file)
    key_bytes = hashlib.sha256(b"fernet:" + raw).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    _fernet = Fernet(fernet_key)


def derive_session_secret(hash_file: Path) -> str:
    """Return a hex string for use as the Starlette session cookie secret.

    Safe to call at import time — falls back to a deterministic constant if
    the hash file does not yet exist (user hasn't run make setup-auth).
    """
    raw = _read_hash_bytes(hash_file)
    return hashlib.sha256(b"session:" + raw).hexdigest()


def encrypt(plaintext: str) -> str:
    if _fernet is None:
        raise RuntimeError("crypto not initialised — call init_crypto() first")
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if _fernet is None:
        raise RuntimeError("crypto not initialised — call init_crypto() first")
    return _fernet.decrypt(ciphertext.encode()).decode()
