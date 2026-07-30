"""genesis_agent.cryptography_utils — the only security-critical module in
the codebase (RSA signing of skills), previously with zero coverage. Every
test redirects PRIVATE_KEY_PATH/PUBLIC_KEY_PATH to tmp_path — this module
writes real key files to ~/.genesis by default and must never touch it."""
from __future__ import annotations

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from genesis_agent import cryptography_utils as cu


@pytest.fixture(autouse=True)
def _isolated_key_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cu, "KEY_DIR", tmp_path)
    monkeypatch.setattr(cu, "PRIVATE_KEY_PATH", tmp_path / "private_key.pem")
    monkeypatch.setattr(cu, "PUBLIC_KEY_PATH", tmp_path / "public_key.pem")
    yield


class TestGenerateKeys:
    def test_writes_both_pem_files(self) -> None:
        cu.generate_keys()
        assert cu.PRIVATE_KEY_PATH.exists()
        assert cu.PUBLIC_KEY_PATH.exists()

    def test_private_key_is_a_loadable_2048_bit_rsa_key(self) -> None:
        cu.generate_keys()
        key = serialization.load_pem_private_key(cu.PRIVATE_KEY_PATH.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        assert key.key_size == 2048

    def test_public_key_matches_the_private_key(self) -> None:
        private_key = cu.generate_keys()
        pub_from_file = serialization.load_pem_public_key(cu.PUBLIC_KEY_PATH.read_bytes())
        expected = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        actual = pub_from_file.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert actual == expected

    def test_private_key_is_unencrypted_on_disk(self) -> None:
        """NoEncryption() is deliberate (no passphrase prompt for an unattended
        agent) — loading with password=None must succeed."""
        cu.generate_keys()
        serialization.load_pem_private_key(cu.PRIVATE_KEY_PATH.read_bytes(), password=None)


class TestSignAndVerifyRoundTrip:
    def test_valid_signature_verifies(self) -> None:
        sig = cu.sign_code("print('hello')")
        assert cu.verify_signature("print('hello')", sig) is True

    def test_tampered_code_fails_verification(self) -> None:
        sig = cu.sign_code("print('hello')")
        assert cu.verify_signature("print('goodbye')", sig) is False

    def test_sign_code_auto_generates_keys_when_missing(self) -> None:
        assert not cu.PRIVATE_KEY_PATH.exists()
        cu.sign_code("some code")
        assert cu.PRIVATE_KEY_PATH.exists()
        assert cu.PUBLIC_KEY_PATH.exists()

    def test_signature_is_hex(self) -> None:
        sig = cu.sign_code("print(1)")
        bytes.fromhex(sig)  # raises ValueError if not valid hex

    def test_two_signatures_of_the_same_code_both_verify(self) -> None:
        """PSS padding is randomized (salt) — signatures need not be byte-identical,
        but both must still verify against the same code."""
        sig1 = cu.sign_code("print('hello')")
        sig2 = cu.sign_code("print('hello')")
        assert cu.verify_signature("print('hello')", sig1) is True
        assert cu.verify_signature("print('hello')", sig2) is True


class TestVerifySignatureFailureModes:
    def test_missing_public_key_returns_false_not_an_exception(self) -> None:
        assert cu.verify_signature("anything", "aa") is False

    def test_garbage_signature_hex_returns_false(self) -> None:
        cu.generate_keys()
        assert cu.verify_signature("print(1)", "not-valid-hex!!") is False

    def test_well_formed_but_wrong_signature_returns_false(self) -> None:
        cu.generate_keys()
        assert cu.verify_signature("print(1)", "aa" * 256) is False

    def test_signature_from_a_different_keypair_fails(self) -> None:
        sig = cu.sign_code("print(1)")
        # Rotate to a brand-new keypair — old signature must no longer verify.
        cu.generate_keys()
        assert cu.verify_signature("print(1)", sig) is False


class TestNonRsaKeyGuard:
    def test_sign_code_rejects_a_non_rsa_private_key(self) -> None:
        ec_key = ec.generate_private_key(ec.SECP256R1())
        cu.PRIVATE_KEY_PATH.write_bytes(ec_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        with pytest.raises(TypeError, match="RSA"):
            cu.sign_code("print(1)")

    def test_verify_signature_rejects_a_non_rsa_public_key(self) -> None:
        ec_key = ec.generate_private_key(ec.SECP256R1())
        cu.PUBLIC_KEY_PATH.write_bytes(ec_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        assert cu.verify_signature("print(1)", "aa") is False
