import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import secret_box
from app.secret_box import is_protected, protect_secret, unprotect_secret


def test_secret_box_round_trip_or_compatible_plaintext():
    protected = protect_secret("secret-value")

    assert unprotect_secret(protected) == "secret-value"
    assert protected == "secret-value" or is_protected(protected)


def test_secret_box_supports_fernet_backend(monkeypatch):
    class FakeFernet:
        def __init__(self, key):
            self.key = key

        def encrypt(self, value):
            return b"enc:" + value

        def decrypt(self, value):
            assert value.startswith(b"enc:")
            return value[4:]

    monkeypatch.setattr(secret_box, "_load_fernet", lambda: (FakeFernet, b"k"))

    protected = secret_box.protect_secret("docker-secret")

    assert protected.startswith("fernet:v1:")
    assert secret_box.unprotect_secret(protected) == "docker-secret"
