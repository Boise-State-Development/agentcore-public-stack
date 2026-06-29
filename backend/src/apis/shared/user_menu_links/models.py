"""Models for admin-managed user-menu links.

A link is either:
  - ``external``: opens ``url`` in a new tab
  - ``modal``:    opens an in-app modal that renders ``body_markdown``

Storage uses single-table design with a fixed partition (``USER_MENU_LINKS``)
since the data is global / single-tenant. When per-org scoping is needed, the
PK becomes ``USER_MENU_LINKS#<org_id>`` without touching the SK shape.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

LinkKind = Literal["external", "modal"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _validate_http_url(value: Optional[str]) -> Optional[str]:
    """Reject non-http(s) URLs. Defense-in-depth — Angular's DomSanitizer
    also strips ``javascript:`` URLs from ``[href]``, but anyone hitting the
    API directly (curl, scripts, a malicious admin) bypasses the SPA form."""
    if value is None or value == "":
        return value
    lowered = value.strip().lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    return value


@dataclass
class UserMenuLink:
    """Admin-managed user-menu link stored in DynamoDB."""

    link_id: str
    label: str
    kind: LinkKind
    created_at: str
    updated_at: str
    enabled: bool = True
    order: int = 0
    url: Optional[str] = None  # external kind only
    body_markdown: Optional[str] = None  # modal kind only
    created_by: Optional[str] = None

    def to_dynamo_item(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "PK": "USER_MENU_LINKS",
            "SK": f"LINK#{self.link_id}",
            "linkId": self.link_id,
            "label": self.label,
            "kind": self.kind,
            "enabled": self.enabled,
            "order": self.order,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.url:
            item["url"] = self.url
        if self.body_markdown:
            item["bodyMarkdown"] = self.body_markdown
        if self.created_by:
            item["createdBy"] = self.created_by
        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "UserMenuLink":
        try:
            created_at = item["createdAt"]
            updated_at = item["updatedAt"]
        except KeyError as e:
            raise ValueError(
                f"User-menu link item {item.get('SK', '?')} is missing required "
                f"timestamp field: {e.args[0]}"
            ) from e
        return cls(
            link_id=item["linkId"],
            label=item["label"],
            kind=item["kind"],
            enabled=item.get("enabled", True),
            order=int(item.get("order", 0)),
            url=item.get("url"),
            body_markdown=item.get("bodyMarkdown"),
            created_at=created_at,
            updated_at=updated_at,
            created_by=item.get("createdBy"),
        )


# =============================================================================
# Pydantic request/response models
# =============================================================================


class _LinkFieldsMixin(BaseModel):
    """Field validation shared by Create + Update."""

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "_LinkFieldsMixin":
        kind = getattr(self, "kind", None)
        url = getattr(self, "url", None)
        body = getattr(self, "body_markdown", None)
        if kind == "external" and not url:
            raise ValueError("external links require url")
        if kind == "modal" and not body:
            raise ValueError("modal links require body_markdown")
        # On a partial update, kind may be None — that's fine; downstream
        # service merges with existing values before persisting.
        return self


class UserMenuLinkCreate(_LinkFieldsMixin):
    label: str = Field(..., min_length=1, max_length=64)
    kind: LinkKind
    enabled: bool = True
    order: int = Field(default=0, ge=0, le=10_000)
    url: Optional[str] = Field(None, max_length=2048)
    body_markdown: Optional[str] = Field(None, max_length=50_000)

    @field_validator("url")
    @classmethod
    def _check_url_scheme(cls, v: Optional[str]) -> Optional[str]:
        return _validate_http_url(v)


class UserMenuLinkUpdate(BaseModel):
    """Partial update — all fields optional. Validation runs after merge."""

    label: Optional[str] = Field(None, min_length=1, max_length=64)
    kind: Optional[LinkKind] = None
    enabled: Optional[bool] = None
    order: Optional[int] = Field(None, ge=0, le=10_000)
    url: Optional[str] = Field(None, max_length=2048)
    body_markdown: Optional[str] = Field(None, max_length=50_000)

    @field_validator("url")
    @classmethod
    def _check_url_scheme(cls, v: Optional[str]) -> Optional[str]:
        return _validate_http_url(v)


class UserMenuLinkResponse(BaseModel):
    """API response shape (camelCase via field aliases on read sites)."""

    link_id: str
    label: str
    kind: LinkKind
    enabled: bool
    order: int
    url: Optional[str] = None
    body_markdown: Optional[str] = None
    created_at: str
    updated_at: str
    created_by: Optional[str] = None

    @classmethod
    def from_link(cls, link: UserMenuLink) -> "UserMenuLinkResponse":
        return cls(
            link_id=link.link_id,
            label=link.label,
            kind=link.kind,
            enabled=link.enabled,
            order=link.order,
            url=link.url,
            body_markdown=link.body_markdown,
            created_at=link.created_at,
            updated_at=link.updated_at,
            created_by=link.created_by,
        )


class UserMenuLinkListResponse(BaseModel):
    links: List[UserMenuLinkResponse]
    total: int
