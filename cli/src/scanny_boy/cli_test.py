import json

from scanny_boy.cli import main


def test_scan_command_prints_json_result(capsys):
    exit_code = main(["scan", "/tmp/example"])

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out == {"path": "/tmp/example", "ok": True}
