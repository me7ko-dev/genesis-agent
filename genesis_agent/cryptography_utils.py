#!/usr/bin/env python3
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# Key storage. Follows GENESIS_HOME — the ONE variable that relocates Genesis's
# configuration — with GENESIS_KEY_DIR kept as an explicit per-purpose override.
#
# This used to default to `Path.home() / ".genesis"` directly, ignoring
# GENESIS_HOME (bug found end-to-end, 2026-08-12). Nothing depended on the
# signing keys until skills started being signed, and then it broke exactly the
# setup GENESIS_HOME exists for: run Genesis from WSL and from Windows against
# the same checkout, and each side silently generated its OWN keypair in its own
# home. A skill written on one side then failed verification on the other and
# was refused with "кодът е бил променен след подписването" — a tampering
# accusation for two environments that were both behaving correctly.
#
# When GENESIS_HOME is unset this resolves to `~/.genesis`, exactly as before,
# so a single-environment install sees no change and needs no migration.
from genesis_agent.paths import GENESIS_HOME as _GENESIS_HOME

KEY_DIR = Path(os.environ.get("GENESIS_KEY_DIR", _GENESIS_HOME))
PRIVATE_KEY_PATH = KEY_DIR / "private_key.pem"
PUBLIC_KEY_PATH = KEY_DIR / "public_key.pem"

def generate_keys(*, exclusive: bool = False):
    """Generate this installation's RSA key pair for signing skills.

    exclusive=True (the implicit first-use path): an existing key wins and is
    returned instead of being replaced."""
    print(f"[DNA] Generating secure keys in {KEY_DIR}...")
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir's `mode` only applies when this call actually creates the
    # directory — if KEY_DIR (typically ~/.genesis, shared with API keys)
    # already existed with looser permissions, force it here too.
    try:
        os.chmod(KEY_DIR, 0o700)
    except OSError:
        pass

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    # Two processes on a fresh install used to overwrite each other's key, so
    # a skill one of them had just signed failed its check as "tampered"
    # (audit 2026-10-07). The key is written to a private temp file (0o600
    # from creation — never readable by group/other, design note 2026-08-12)
    # and published with os.link, which refuses an existing target: the first
    # process wins and every other one loads the winner's key.
    tmp = KEY_DIR / f".private_key.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        try:
            if exclusive:
                os.link(tmp, PRIVATE_KEY_PATH)
            else:
                os.replace(tmp, PRIVATE_KEY_PATH)
        except FileExistsError:
            private_key = _load_private_key()
        except OSError:  # a filesystem without hard links
            if not PRIVATE_KEY_PATH.exists():
                os.replace(tmp, PRIVATE_KEY_PATH)
            else:
                private_key = _load_private_key()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        os.chmod(PRIVATE_KEY_PATH, 0o600)
    except OSError:
        pass
    _write_public_key(private_key)
    print("[DNA] Keys generated successfully. Keep the private key safe!")
    return private_key


def _load_private_key():
    with open(PRIVATE_KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"{PRIVATE_KEY_PATH} does not hold an RSA key (this "
                        f"module only generates/expects RSA keys)")
    return key


def _write_public_key(private_key) -> None:
    data = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    tmp = KEY_DIR / f".public_key.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, PUBLIC_KEY_PATH)


def _load_public_key():
    """The public key; derived from the private one when public_key.pem is
    missing (deleting it must not switch the signature check off — audit
    2026-10-07). None when this installation has no keys at all."""
    if PUBLIC_KEY_PATH.exists():
        with open(PUBLIC_KEY_PATH, "rb") as f:
            key = serialization.load_pem_public_key(f.read())
        return key if isinstance(key, rsa.RSAPublicKey) else None
    if PRIVATE_KEY_PATH.exists():
        return _load_private_key().public_key()
    return None


def have_keys() -> bool:
    return PUBLIC_KEY_PATH.exists() or PRIVATE_KEY_PATH.exists()


def sign_code(code_text: str) -> str:
    """Sign skill code with the private key."""
    private_key = generate_keys(exclusive=True) if not PRIVATE_KEY_PATH.exists() else _load_private_key()

    signature = private_key.sign(
        code_text.encode('utf-8'),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return signature.hex()

def verify_signature(code_text: str, signature_hex: str) -> bool:
    """Verify skill code against its signature using the public key."""
    try:
        public_key = _load_public_key()
    except (OSError, ValueError, TypeError):
        return False
    if public_key is None:
        return False

    try:
        public_key.verify(
            bytes.fromhex(signature_hex),
            code_text.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return True
    except Exception:
        return False

if __name__ == "__main__":
    if not PRIVATE_KEY_PATH.exists():
        generate_keys()
    else:
        print("[DNA] Sovereignty keys already exist.")
