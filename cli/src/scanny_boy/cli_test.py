from scanny_boy.cli import main


def test_main_with_no_arguments_returns_success():
    assert main([]) == 0
