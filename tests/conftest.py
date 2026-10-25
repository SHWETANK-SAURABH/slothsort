import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings  # noqa: E402
from core.audit_log import Journal  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "state" / "journal.sqlite3", max_retries=2, batch_size=4)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "messy"
    r.mkdir()
    return r.resolve()


@pytest.fixture
def journal(settings: Settings) -> Journal:
    return Journal(settings.db_path)


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """Map of every path under ``folder`` to (size, mtime_ns)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.lstat()
            out[p.relative_to(folder).as_posix()] = (st.st_size if p.is_file() else -1, st.st_mtime_ns)
    return out


def can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "_link_probe").symlink_to(tmp_path)
        (tmp_path / "_link_probe").unlink()
        return True
    except (OSError, NotImplementedError):
        return False
