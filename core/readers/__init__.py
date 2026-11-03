"""Per-client readers. Add a client = add a reader here, nothing else."""
from __future__ import annotations

from .base import Reader, ReadResult
from .geeta import GeetaReader
from .friendship import FriendshipReader
from .goldcoin import GoldcoinReader
from .eurochemco import EurochemcoReader

# Registry keyed by ClientConfig.reader.
READERS: dict[str, type[Reader]] = {
    "geeta": GeetaReader,
    "friendship": FriendshipReader,
    "goldcoin": GoldcoinReader,
    "eurochemco": EurochemcoReader,
}


def get_reader(name: str) -> Reader:
    try:
        return READERS[name]()
    except KeyError as exc:
        raise KeyError(
            f"Unknown reader {name!r}. Known readers: {sorted(READERS)}"
        ) from exc


__all__ = [
    "Reader",
    "ReadResult",
    "GeetaReader",
    "FriendshipReader",
    "GoldcoinReader",
    "EurochemcoReader",
    "READERS",
    "get_reader",
]
