"""SkillProfile — per-domain operator proficiency (Phase 4 Skill Inferer).

One row per domain. Rows accumulate passively from chat (``source=inferred``)
or are pinned by the operator (``source=manual``); an inferred update never
overwrites a manual row. Phase 6's trust gradient reads these as *hints* for
per-domain defaults — never as authorization by itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import SkillLevel, SkillSource


class SkillProfile(Base):
    __tablename__ = "skill_profile"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    domain: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    level: Mapped[SkillLevel] = mapped_column(SAEnum(SkillLevel), default=SkillLevel.NOVICE)
    source: Mapped[SkillSource] = mapped_column(SAEnum(SkillSource), default=SkillSource.INFERRED)

    # Weighted evidence accumulated from chat; drives level for inferred rows.
    score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
