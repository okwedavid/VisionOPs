"""Secret hashing and envelope encryption (ADR-017).

Two distinct jobs with two distinct rules:

* **API keys** are 256-bit random values, not user-chosen passwords, so they are
  hashed with a fast salted SHA-256: a slow KDF buys nothing against a
  brute-force attack on random material and would make every request O(1) in
  only one direction. User passwords (Phase 6) live in a different table with a
  different rule.
* **Camera credentials** must be *recoverable*, so they are encrypted with
  AES-256-GCM. The row id is bound as associated data, which makes ciphertext
  swapping between rows fail authentication.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Envelope format version. Bump to change the algorithm; ``key_id`` allows
#: overlapping keys during a rotation.
ENVELOPE_VERSION = "v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_SALT_BYTES = 16
#: Key-derivation iterations for the *master* key when a passphrase is supplied.
#: A raw 32-byte key needs no derivation; see ``derive_key_from_passphrase``.
_PASSPHRASE_ITERATIONS = 200_000


class DecryptionError(RuntimeError):
    """Raised when a secret cannot be decrypted (wrong key, tampering, no key)."""


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """A freshly minted API key. The secret is shown exactly once."""

    key_id: str
    secret: str
    key_prefix: str
    salt: str

    @property
    def full_key(self) -> str:
        return f"vo_{self.key_id}_{self.secret}"


def generate_api_key() -> GeneratedKey:
    """Mint a new API key: 12-char id + 43-char 256-bit secret."""
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    salt = secrets.token_hex(_SALT_BYTES)
    return GeneratedKey(
        key_id=key_id,
        secret=secret,
        key_prefix=f"vo_{key_id}",
        salt=salt,
    )


def parse_api_key(candidate: str) -> tuple[str, str] | None:
    """Split ``vo_<id>_<secret>`` into its id and secret, or ``None`` if malformed."""
    if not candidate or not candidate.startswith("vo_"):
        return None
    parts = candidate.split("_", 2)
    if len(parts) != 3:
        return None
    _, key_id, secret = parts
    if len(key_id) != 12 or len(secret) < 32:
        return None
    return key_id, secret


def hash_secret(secret: str, salt: str) -> str:
    """Salted SHA-256 of a key secret (hex)."""
    return hashlib.sha256(f"{salt}:{secret}".encode()).hexdigest()


def verify_secret(candidate_secret: str, salt: str, expected_hash: str) -> bool:
    """Constant-time secret comparison."""
    return hmac.compare_digest(hash_secret(candidate_secret, salt), expected_hash)


def fingerprint(candidate: str) -> str:
    """A short, non-reversible id for rate-limit bucketing and logs."""
    return hashlib.sha256(candidate.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Master key handling
# ---------------------------------------------------------------------------


def generate_master_key() -> str:
    """Generate a new base64 master key (32 bytes)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode()


def derive_key_from_passphrase(passphrase: str, salt: bytes | None = None) -> str:
    """Derive a base64 key from a human passphrase (PBKDF2-HMAC-SHA256).

    Only for deployments that prefer a memorable key; a random key from
    :func:`generate_master_key` is stronger.
    """
    used_salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode(), used_salt, _PASSPHRASE_ITERATIONS, _KEY_BYTES
    )
    salt_b64 = base64.urlsafe_b64encode(used_salt).decode()
    digest_b64 = base64.urlsafe_b64encode(digest).decode()
    return f"pbkdf2${salt_b64}${digest_b64}"


def load_master_key(value: str) -> bytes:
    """Load a master key from a base64 or ``pbkdf2$`` encoded string."""
    if value.startswith("pbkdf2$"):
        try:
            _, _salt_b64, digest_b64 = value.split("$", 2)
            key = base64.urlsafe_b64decode(digest_b64)
        except (ValueError, TypeError) as exc:
            raise DecryptionError("master key is malformed") from exc
        if len(key) != _KEY_BYTES:
            raise DecryptionError("derived master key has the wrong length")
        return key
    try:
        raw = base64.urlsafe_b64decode(value.encode())
    except (ValueError, TypeError) as exc:
        raise DecryptionError("master key is not valid base64") from exc
    if len(raw) != _KEY_BYTES:
        raise DecryptionError("master key must decode to 32 bytes")
    return raw


# ---------------------------------------------------------------------------
# Envelope encryption
# ---------------------------------------------------------------------------


class SecretBox:
    """AES-256-GCM envelope encryption bound to a master key."""

    def __init__(self, master_key: str | bytes, *, key_id: str = "k1") -> None:
        self._key = load_master_key(master_key) if isinstance(master_key, str) else master_key
        if len(self._key) != _KEY_BYTES:
            raise DecryptionError("master key must be 32 bytes")
        self._key_id = key_id
        self._aead = AESGCM(self._key)

    @property
    def key_id(self) -> str:
        return self._key_id

    def encrypt(self, plaintext: str, *, aad: str) -> str:
        """Encrypt ``plaintext``, binding it to ``aad`` (normally the row id)."""
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode(), aad.encode())
        blob = base64.urlsafe_b64encode(nonce + ciphertext).decode()
        return f"{ENVELOPE_VERSION}:{self._key_id}:{blob}"

    def decrypt(self, envelope: str, *, aad: str) -> str:
        """Decrypt an envelope produced by :meth:`encrypt`."""
        try:
            version, key_id, blob = envelope.split(":", 2)
        except (ValueError, AttributeError) as exc:
            raise DecryptionError("encrypted value is malformed") from exc
        if version != ENVELOPE_VERSION:
            raise DecryptionError(f"unsupported envelope version {version!r}")
        if key_id != self._key_id:
            raise DecryptionError(
                f"value was encrypted with key {key_id!r}, but {self._key_id!r} is active"
            )
        try:
            raw = base64.urlsafe_b64decode(blob.encode())
        except (ValueError, TypeError) as exc:
            raise DecryptionError("encrypted payload is not valid base64") from exc
        if len(raw) <= _NONCE_BYTES:
            raise DecryptionError("encrypted payload is truncated")
        try:
            plaintext = self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], aad.encode())
        except InvalidTag as exc:
            # Covers a wrong key, a wrong row (AAD mismatch) and tampering.
            raise DecryptionError("encrypted value failed authentication") from exc
        return plaintext.decode()


# ---------------------------------------------------------------------------
# Filesystem safety
# ---------------------------------------------------------------------------


def secure_write(path: Path, content: str) -> None:
    """Write a secret to disk with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` and assert it stays inside ``root``.

    Defeats path traversal (``../../etc/passwd``), absolute paths and symlink
    escapes. ``docs/SECURITY_PRIVACY.md`` threat T-04.
    """
    root_resolved = root.resolve()
    target = Path(candidate)
    if not target.is_absolute():
        target = root_resolved / target
    resolved = target.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"path {candidate!r} resolves outside the allowed root {root_resolved}")
    return resolved


__all__ = [
    "ENVELOPE_VERSION",
    "DecryptionError",
    "GeneratedKey",
    "SecretBox",
    "derive_key_from_passphrase",
    "encrypt_with",
    "fingerprint",
    "generate_api_key",
    "generate_master_key",
    "hash_secret",
    "load_master_key",
    "parse_api_key",
    "resolve_within",
    "secure_write",
    "verify_secret",
]


def encrypt_with(master_key: str, plaintext: str, *, aad: str) -> str:
    """One-shot helper for callers that do not hold a :class:`SecretBox`."""
    return SecretBox(master_key).encrypt(plaintext, aad=aad)
