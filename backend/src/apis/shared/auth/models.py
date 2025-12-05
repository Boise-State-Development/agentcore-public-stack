"""Authentication models shared across API projects."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class User:
    """Authenticated user model."""
    email: str
    user_id: str
    name: str
    roles: List[str]
    picture: Optional[str] = None





