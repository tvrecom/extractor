import pytest

from tmdb_extractor.cli import main
from tmdb_extractor.config import Settings
from tmdb_extractor.errors import ConfigError


def test_missing_key_raises_before_any_io(tmp_path):
    data_dir = tmp_path / "never_created"
    with pytest.raises(ConfigError) as err:
        Settings.from_env({"TMDB_DATA_DIR": str(data_dir)})
    assert "TMDB_API_KEY" in str(err.value)
    assert not data_dir.exists()


def test_main_returns_2_without_key_and_creates_nothing(tmp_path, capsys):
    data_dir = tmp_path / "d"
    assert main({"TMDB_DATA_DIR": str(data_dir)}) == 2
    assert "TMDB_API_KEY" in capsys.readouterr().err
    assert not data_dir.exists()


def test_all_problems_reported_together():
    with pytest.raises(ConfigError) as err:
        Settings.from_env({"TMDB_CONCURRENCY": "x", "TMDB_START_YEAR": "2030",
                           "TMDB_END_YEAR": "2020"})
    assert len(err.value.problems) == 3  # key, concurrency, year order


def test_popularity_pages_clamped_and_key_hidden_in_repr():
    s = Settings.from_env({"TMDB_API_KEY": "abc", "TMDB_POPULARITY_PAGES": "9999"})
    assert s.popularity_pages == 500
    assert "abc" not in repr(s)
