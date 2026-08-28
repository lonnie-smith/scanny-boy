import hashlib

from scanny_boy.hashing import sha256_file


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "a.bin"
    data = b"scanny boy" * 100_000  # bigger than the streaming chunk size
    path.write_bytes(data)

    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_sha256_file_empty_file(tmp_path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")

    assert sha256_file(path) == hashlib.sha256(b"").hexdigest()
