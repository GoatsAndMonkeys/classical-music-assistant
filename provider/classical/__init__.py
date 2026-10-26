"""Classical: a classical-music index and browse tree for Music Assistant (PLAN.md §6, §7).

Only stable names are imported at module level. `setup()` checks the running MA version against the
versions this release was tested on, then imports the glue. The core package (`classical_core`) is
installed by MA from the manifest requirement; it has no install dependencies of its own.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from music_assistant_models.errors import SetupFailedError

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# MA versions whose image digests passed the provider tests (docker/compose.dev.yml pins them)
TESTED_VERSIONS = frozenset({"2.10.8", "2.11.0.dev2026102604"})
ENV_ALLOW_UNTESTED = "CLASSICAL_ALLOW_UNTESTED"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider after the version guard."""
    version = str(getattr(mass, "version", "") or "")
    if version not in TESTED_VERSIONS and os.environ.get(ENV_ALLOW_UNTESTED) != "1":
        msg = (
            f"Classical has not been tested with Music Assistant {version or '(unknown)'}; tested: "
            f"{', '.join(sorted(TESTED_VERSIONS))}. Set {ENV_ALLOW_UNTESTED}=1 to load it anyway."
        )
        raise SetupFailedError(msg)
    from .glue import SUPPORTED_FEATURES, ClassicalProvider  # noqa: PLC0415 - version-specific glue

    return ClassicalProvider(mass, manifest, config, SUPPORTED_FEATURES)
