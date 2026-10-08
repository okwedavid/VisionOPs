"""VisionOps — production-grade computer-vision operations platform.

V0.1 slice: restricted-zone intelligence (detect → track → zone → event).
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Version of the ``visionops.vision`` public interface (docs/TECHNICAL_DECISIONS.md).
#: Bumped when a ``Protocol`` in :mod:`visionops.vision.contracts` changes shape.
VISION_API_VERSION = 1

__all__ = ["VISION_API_VERSION", "__version__"]
