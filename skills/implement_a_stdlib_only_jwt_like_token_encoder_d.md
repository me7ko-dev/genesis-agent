---
name: 'implement_a_stdlib_only_jwt_like_token_encoder_d'
category: 'autonomous'
description: 'Implement a stdlib-only JWT-like token encoder/decoder using hmac (HS256) with type hints, docstring, and an assert-based self-test that prints OK.'
triggers: ["implement a stdlib only jwt like token encoder d"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:53:13.677343+00:00'
---

## Описание
Implement a stdlib-only JWT-like token encoder/decoder using hmac (HS256) with type hints, docstring, and an assert-based self-test that prints OK.

## Python Код
```python
import json
import hmac
import hashlib
import base64
from typing import Dict


def base64_url_encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def base64_url_decode(s: str) -> bytes:
    """Decode URL-safe base64 string (with optional padding) to bytes."""
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def create_jwt(payload: Dict, secret_key: str) -> str:
    """
    Create a JWT-like token signed with HS256.

    Args:
        payload: Dictionary to be encoded as the token payload.
        secret_key: Secret key used for HMAC signing.

    Returns:
        Token string in the form header.payload.signature.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = base64_url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = base64_url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    signature_b64 = base64_url_encode(signature)

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode(token: str, secret: str) -> Dict:
    """
    Decode and verify a JWT-like token signed with HS256.

    Args:
        token: Token string in the form header.payload.signature.
        secret: Secret key used for HMAC verification.

    Returns:
        The payload as a dictionary.

    Raises:
        ValueError: If the token structure is invalid or the signature does not match.
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise ValueError("Token must have exactly three parts") from exc

    # Verify signature
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    expected_sig_b64 = base64_url_encode(expected_sig)

    if not hmac.compare_digest(expected_sig_b64, signature_b64):
        raise ValueError("Invalid token signature")

    # Decode payload
    payload_bytes = base64_url_decode(payload_b64)
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Payload is not valid JSON") from exc

    return payload


if __name__ == "__main__":
    SECRET = "my_secure_secret_key"
    payload = {"user_id": 12345, "role": "admin"}

    # Encode
    token = create_jwt(payload, SECRET)

    # Decode – should succeed
    decoded = decode(token, SECRET)
    assert decoded == payload

    # Manipulate token – should raise
    bad_token = token.rsplit(".", 1)[0] + ".invalidsignature"
    try:
        decode(bad_token, SECRET)
        raise AssertionError("Manipulated token was not rejected")
    except ValueError:
        pass

    print("OK")
```

## Pitfalls
- OK

