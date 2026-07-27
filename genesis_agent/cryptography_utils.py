#!/usr/bin/env python3
import os
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

# Key storage — the current user's ~/.genesis, overridable via GENESIS_KEY_DIR.
KEY_DIR = Path(os.environ.get("GENESIS_KEY_DIR", Path.home() / ".genesis"))
PRIVATE_KEY_PATH = KEY_DIR / "private_key.pem"
PUBLIC_KEY_PATH = KEY_DIR / "public_key.pem"

def generate_keys():
    """Generate this installation's RSA key pair for signing skills."""
    print(f"[DNA] Generating secure keys in {KEY_DIR}...")
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    
    # Save Private Key
    with open(PRIVATE_KEY_PATH, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
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
