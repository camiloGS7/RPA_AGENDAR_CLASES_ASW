"""Token stores persist the token and never reveal it."""

import os
import stat
from pathlib import Path

import pytest

from asw_bot.token_store import FileTokenStore, MemoryTokenStore, build_token_store
from helpers import NEW_TOKEN, OLD_TOKEN, make_settings


def test_file_store_round_trip(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "nested" / "token")

    assert store.load() is None
    store.save(OLD_TOKEN)
    assert store.load() == OLD_TOKEN
    store.save(NEW_TOKEN)
    assert store.load() == NEW_TOKEN
    assert not (tmp_path / "nested" / "token.tmp").exists()

    store.clear()
    assert store.load() is None
    store.clear()  # clearing twice is not an error


def test_file_store_treats_an_empty_file_as_no_token(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("  \n", encoding="utf-8")
    assert FileTokenStore(path).load() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not exist on Windows")
def test_file_store_is_private_to_the_user(tmp_path: Path) -> None:
    path = tmp_path / "token"
    FileTokenStore(path).save(OLD_TOKEN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_stores_do_not_reveal_the_token_in_repr(tmp_path: Path) -> None:
    file_store = FileTokenStore(tmp_path / "token")
    file_store.save(OLD_TOKEN)
    memory_store = MemoryTokenStore(OLD_TOKEN)

    assert OLD_TOKEN not in repr(file_store) + str(file_store)
    assert OLD_TOKEN not in repr(memory_store) + str(memory_store)
    assert "has_token=True" in repr(memory_store)


def test_memory_store_round_trip() -> None:
    store = MemoryTokenStore()
    assert store.load() is None
    store.save(OLD_TOKEN)
    assert store.load() == OLD_TOKEN
    store.clear()
    assert store.load() is None


def test_builder_prefers_the_environment_token(tmp_path: Path) -> None:
    from_env = build_token_store(make_settings(asw_token=OLD_TOKEN, token_file=tmp_path / "t"))
    from_file = build_token_store(make_settings(token_file=tmp_path / "t"))

    assert isinstance(from_env, MemoryTokenStore)
    assert from_env.load() == OLD_TOKEN
    assert isinstance(from_file, FileTokenStore)
    assert from_file.load() is None
