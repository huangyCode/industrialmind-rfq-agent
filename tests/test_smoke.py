from rfq_agent import config


def test_paths_exist():
    assert config.ROOT.joinpath("pyproject.toml").exists()
    assert config.BIZ.margin_pct == 0.18
