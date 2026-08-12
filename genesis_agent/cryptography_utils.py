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

def generate_keys():
    """Generate this installation's RSA key pair for signing skills."""
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

    # Save Private Key. `open(..., "wb")` alone leaves it at the process
    # umask (0o644 on a typical Linux default: group/other CAN read an
    # unencrypted RSA private key) — this directory holds the same class of
    # secret as ~/.genesis/.env (paths.ensure_genesis_home already locks that
    # one to 0o700); the key file itself needs the same treatment, design
    # note 2026-08-12.
    with open(PRIVATE_KEY_PATH, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    try:
        os.chmod(PRIVATE_KEY_PATH, 0o600)
    except OSError:
        pass

    # Save Public Key
    public_key = private_key.public_key()
    with open(PUBLIC_KEY_PATH, "wb") as f:
        f.write(public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ))

    print("[DNA] Keys generated successfully. Keep the private key safe!")
    return private_key

def sign_code(code_text: str) -> str:
    """Sign skill code with the private key."""
    if not PRIVATE_KEY_PATH.exists():
        generate_keys()
        
    with open(PRIVATE_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(
            f.read(),
            password=None
        )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError(f"{PRIVATE_KEY_PATH} does not hold an RSA key (this "
                         f"module only generates/expects RSA keys)")

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
    if not PUBLIC_KEY_PATH.exists():
        return False
        
    with open(PUBLIC_KEY_PATH, "rb") as f:
        public_key = serialization.load_pem_public_key(f.read())
    if not isinstance(public_key, rsa.RSAPublicKey):
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
