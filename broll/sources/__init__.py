"""Adapter registry. Adding a source is one file plus one line here.

`build_adapters` returns only the adapters that can actually run: keyed sources
are skipped when their key is missing.
"""

from __future__ import annotations

from ..config import Config
from .base import Adapter
from .dvids import DVIDS
from .internet_archive import InternetArchive
from .loc import LOC
from .met import Met
from .nasa import NASA
from .openverse import Openverse
from .pexels import Pexels
from .pixabay import Pixabay
from .rijksmuseum import Rijksmuseum
from .smithsonian import Smithsonian
from .wikimedia import Wikimedia

__all__ = ["Adapter", "build_adapters", "all_adapter_names"]


def build_adapters(cfg: Config) -> list[Adapter]:
    adapters: list[Adapter] = [
        # no-key sources — always on
        Wikimedia(),
        Met(),
        # LOC(),  # disabled: loc.gov sits behind a Cloudflare bot challenge that
                  # 429s every automated request. The adapter still works — re-add
                  # this line to retry from a network Cloudflare doesn't challenge.
        InternetArchive(),
        NASA(),
        Openverse(),
    ]
    # keyed sources — only if the key exists
    if cfg.pexels_key:
        adapters.append(Pexels(cfg.pexels_key))
    if cfg.pixabay_key:
        adapters.append(Pixabay(cfg.pixabay_key))
    if cfg.smithsonian_key:
        adapters.append(Smithsonian(cfg.smithsonian_key))
    if cfg.rijksmuseum_key:
        adapters.append(Rijksmuseum(cfg.rijksmuseum_key))
    if cfg.dvids_key:
        adapters.append(DVIDS(cfg.dvids_key))
    return adapters


def all_adapter_names() -> list[str]:
    return [
        "wikimedia", "met", "loc", "internetarchive", "nasa", "openverse",
        "pexels", "pixabay", "smithsonian", "rijksmuseum", "dvids",
    ]
