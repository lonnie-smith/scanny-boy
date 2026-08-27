from scanny_boy.core.scanner import scan


def test_scan_returns_ok_for_given_path():
    result = scan("/tmp/example")

    assert result.path == "/tmp/example"
    assert result.ok is True
