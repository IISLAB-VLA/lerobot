"""Codec preference helpers for the dashboard WebRTC stack.

aiortc 1.14.0 publishes VP8, two H264 baseline profiles, and RTX on the
sender side — VP9 is **not** included. We keep ``video/VP9`` at the head of
:data:`CODEC_PREFERENCE` so a future aiortc release upgrades us without a
code change; on today's stack the effective order is H264 > VP8.
"""

from __future__ import annotations

import logging
from typing import Sequence

from aiortc import RTCRtpSender
from aiortc.rtcrtpsender import RTCRtpSender as _RTCRtpSender

logger = logging.getLogger(__name__)

CODEC_PREFERENCE: tuple[str, ...] = ("video/VP9", "video/H264", "video/VP8")
"""Scanned in order when narrowing a sender's codecs."""


def apply_codec_preference(
    sender: RTCRtpSender,
    preference: Sequence[str] = CODEC_PREFERENCE,
) -> list[str]:
    """Narrow ``sender``'s offered codecs to those listed in ``preference``.

    Returns the mimeTypes that were actually applied, in priority order.
    When none of the preferred codecs are available we return an empty list
    and leave the sender untouched — the browser's default negotiation then
    picks whatever aiortc advertises.
    """
    capabilities = _RTCRtpSender.getCapabilities("video")
    if capabilities is None:
        return []

    by_mime: dict[str, list] = {}
    for codec in capabilities.codecs:
        by_mime.setdefault(codec.mimeType, []).append(codec)

    ordered = [codec for mime in preference for codec in by_mime.get(mime, [])]
    if not ordered:
        logger.warning(
            "no preferred codecs available; falling back to aiortc defaults (have=%s)",
            sorted(by_mime),
        )
        return []

    transceiver = getattr(sender, "_transceiver", None) or getattr(sender, "transceiver", None)
    if transceiver is None or not hasattr(transceiver, "setCodecPreferences"):
        logger.debug("sender has no transceiver yet; preference will apply at negotiation time")
        return [codec.mimeType for codec in ordered]

    transceiver.setCodecPreferences(ordered)
    return [codec.mimeType for codec in ordered]
