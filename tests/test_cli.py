from gate_keeper.cli import main


def test_help_exits_successfully(capsys):
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "Compile natural-language rules" in captured.out
