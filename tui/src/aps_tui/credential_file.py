"""An encrypted file that stands in for the OS keyring where there is none.

Headless Linux (containers, VMs, SSH boxes) usually has no Secret Service, so
``keyring`` selects its failing backend and nothing can be stored. Rather than
asking the user to install and unlock gnome-keyring, which needs root, a D-Bus
session bus and a daemon started with exactly the right flags, the client keeps
credentials in a file of its own, encrypted under a passphrase.

This is the same protection gnome-keyring gives on such a host: its
``login.keyring`` is also a passphrase-encrypted file on disk. What matters is
that the passphrase is never stored next to what it encrypts, so it is typed
once per launch and held only in memory.

Format, one JSON document::

    {"version": 1, "kdf": "scrypt", "n": ..., "r": ..., "p": ...,
     "salt": b64, "nonce": b64, "ciphertext": b64}

The plaintext is ``{service: {account: secret}}`` as JSON. AES-256-GCM
authenticates it, so a wrong passphrase and a tampered file are both detected
(and cannot be told apart, which is fine: neither can be opened). Every write
uses a fresh nonce; the salt, and so the derived key, lasts as long as the file.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
from pathlib import Path

from .errors import ConfigError

FORMAT_VERSION = 1

#: scrypt cost. 2**15 * r=8 is 32 MiB and roughly 0.1 s per unlock: cheap for
#: one prompt per launch, expensive for anyone guessing offline.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1

#: Binds the ciphertext to this format so it cannot be replayed as another.
_AAD = b"aps-tui credentials v1"


class WrongPassphraseError(ConfigError):
    """The passphrase does not open the file (or the file was altered)."""


def _derive_key(passphrase: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class CredentialFile:
    """An unlocked credential file: the derived key and the decrypted secrets.

    Construct with :meth:`create` or :meth:`unlock`. A created file is not
    written until the first :meth:`set`, so choosing a passphrase and then
    abandoning sign-in leaves nothing behind.
    """

    def __init__(self, path: Path, *, key: bytes, salt: bytes, n: int, r: int, p: int, data: dict[str, dict[str, str]]) -> None:
        self.path = path
        self._key = key
        self._salt = salt
        self._params = (n, r, p)
        self._data = data

    def __repr__(self) -> str:  # never show secrets, even in a traceback
        return f"CredentialFile({str(self.path)!r})"

    # -- opening -------------------------------------------------------------

    @classmethod
    def create(cls, path: Path, passphrase: str) -> CredentialFile:
        if not passphrase:
            raise ConfigError("An empty passphrase would leave the credential file unprotected.")
        salt = secrets.token_bytes(16)
        key = _derive_key(passphrase, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
        return cls(path, key=key, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, data={})

    @classmethod
    def unlock(cls, path: Path, passphrase: str) -> CredentialFile:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("version") != FORMAT_VERSION or envelope.get("kdf") != "scrypt":
                raise ValueError(f"unsupported format {envelope.get('version')!r}")
            n, r, p = int(envelope["n"]), int(envelope["r"]), int(envelope["p"])
            salt = base64.b64decode(envelope["salt"])
            nonce = base64.b64decode(envelope["nonce"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(
                f"Could not read the credential file {path}: {exc}",
                hint=f"Delete {path} and sign in again.",
            ) from exc

        key = _derive_key(passphrase, salt, n=n, r=r, p=p)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
        except InvalidTag as exc:
            raise WrongPassphraseError("That passphrase does not open the credential file.") from exc

        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise ConfigError(f"The credential file {path} is malformed.", hint=f"Delete {path} and sign in again.")
        return cls(path, key=key, salt=salt, n=n, r=r, p=p, data=data)

    # -- the keyring operations ----------------------------------------------

    def get(self, service: str, account: str) -> str | None:
        return self._data.get(service, {}).get(account)

    def set(self, service: str, account: str, secret: str) -> None:
        self._data.setdefault(service, {})[account] = secret
        self._write()

    def delete(self, service: str, account: str) -> bool:
        accounts = self._data.get(service, {})
        if account not in accounts:
            return False
        del accounts[account]
        if not accounts:
            del self._data[service]
        self._write()
        return True

    # -- persistence ---------------------------------------------------------

    def _write(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        n, r, p = self._params
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, json.dumps(self._data).encode("utf-8"), _AAD)
        envelope = {
            "version": FORMAT_VERSION,
            "kdf": "scrypt",
            "n": n,
            "r": r,
            "p": p,
            "salt": _b64(self._salt),
            "nonce": _b64(nonce),
            "ciphertext": _b64(ciphertext),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename over it, so an interrupted write
        # cannot leave half a file and lose every stored credential.
        # mkstemp creates the file 0600 on POSIX before anything is written.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".credentials-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
