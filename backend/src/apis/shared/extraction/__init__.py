"""Typed structured extraction: a named field set over a document.

``docs/specs/structured-extraction.md``. Two pure layers:

* :mod:`field_sets` — the field model and the markdown parser. Standard
  library only.
* :mod:`schema` — the Pydantic model one field set becomes, which is the tool
  spec sent, the validator applied on return, and the shape the writers
  iterate.

Neither reads DynamoDB, S3 or a model. Resolution and the extraction call live
in the Runtime, which is the only place that has a turn's skill records.
"""

from .field_sets import (
    FIELD_TYPES,
    JUDGEMENT_TYPES,
    MAX_FIELDS,
    Field,
    FieldSet,
    FieldSetError,
    field_set_name_for,
    parse_field_set,
)
from .schema import build_model, field_set_fingerprint, model_for_field_set

__all__ = [
    "FIELD_TYPES",
    "JUDGEMENT_TYPES",
    "MAX_FIELDS",
    "Field",
    "FieldSet",
    "FieldSetError",
    "build_model",
    "field_set_fingerprint",
    "field_set_name_for",
    "model_for_field_set",
    "parse_field_set",
]
