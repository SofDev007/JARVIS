"""Privacy tiers for memory and knowledge (RAG) records (M20, THREAT_MODEL.md §4.8).

Two values, not a classifier: ``LOCAL_ONLY`` (default everywhere a record is
created) and ``CLOUD_OK`` (explicit opt-in, only ever set at a human-facing
write surface — the memory CLI's ``--privacy-tier`` flag, or the confirmed
``ingest_document``/``ingest_text`` action params).

The enforcement filter at prompt-assembly time (``ChatReasoner``) only ever
*admits* ``CLOUD_OK`` content into a cloud-bound prompt — it never promotes
or demotes a tier, so there is no clamp logic to get right the way
``PermissionPolicy`` needs one for its DANGEROUS floor.
"""

from __future__ import annotations

from enum import Enum


class PrivacyTier(Enum):
    LOCAL_ONLY = "local_only"
    CLOUD_OK = "cloud_ok"
