"""The arrangement of one list page, as stored and as sent.

``docs/specs/agent-project-organization.md`` §5. An arrangement is a view
preference: which sort the page uses, the user's custom order, and the ids they
hid. It grants nothing and revokes nothing, so the ids are checked for shape,
never for access. An id the user can no longer reach is kept (spec D3): the page
ignores it when it renders, and it comes back in place if access does.
"""

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ArrangementSurface = Literal["agents", "projects"]
ArrangementSort = Literal["recent", "name", "custom"]

# Past this a page is not being arranged any more. The SPA drops ids it cannot
# see (the only ones that can go) before it would send more.
MAX_ARRANGEMENT_IDS = 500

# Agent ids are ``ast-<hex>`` and project ids ``prj_<hex>`` today; the pattern is
# the shape both share rather than either prefix, so an older id format still
# arranges.
ARRANGEMENT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

ArrangementId = Annotated[str, Field(pattern=ARRANGEMENT_ID_PATTERN)]


class Arrangement(BaseModel):
    """One surface's arrangement. No stored item reads as the defaults."""

    model_config = ConfigDict(populate_by_name=True)

    sort: ArrangementSort = "recent"
    order: List[str] = Field(default_factory=list)
    hidden: List[str] = Field(default_factory=list)
    updated_at: Optional[str] = Field(None, alias="updatedAt")


class ArrangementUpdate(BaseModel):
    """A whole arrangement, replacing the stored one.

    An id may be in both ``order`` and ``hidden``: a hidden item keeps its place,
    so unhiding it puts it back where it was.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    sort: ArrangementSort
    order: Annotated[List[ArrangementId], Field(max_length=MAX_ARRANGEMENT_IDS)]
    hidden: Annotated[List[ArrangementId], Field(max_length=MAX_ARRANGEMENT_IDS)]

    @field_validator("order", "hidden")
    @classmethod
    def _unique(cls, value: List[str]) -> List[str]:
        if len(set(value)) != len(value):
            raise ValueError("ids must be unique")
        return value
