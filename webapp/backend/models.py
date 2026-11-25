"""webapp/backend/models.py - Pydantic Request Models."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_PATCH_TEXT_CHARS = 2_000_000
MAX_CONTENT_CHARS = 5_000_000
MAX_KCONFIG_ASSIGNMENTS = 25_000
MAX_WATCH_ITEMS = 200


class FormatPatchRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=1024, description="Relative file path being patched")
    original_content: str = Field(..., max_length=MAX_CONTENT_CHARS, description="Pristine file content")
    modified_content: str = Field(..., max_length=MAX_CONTENT_CHARS, description="Modified file content")
    commit_subject: str = Field(default="[PATCH] update file", max_length=500, description="Patch commit summary line")
    commit_body: str = Field(default="", max_length=100_000, description="Patch description (commit message body)")
    author_name: str = Field(default="Kernel Developer", max_length=256, description="Patch author name")
    author_email: str = Field(default="dev@kernel.org", max_length=320, description="Patch author email")


class KconfigResolveRequest(BaseModel):
    arch: str = Field(default="x86_64", max_length=64)
    assignments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("assignments")
    @classmethod
    def _cap_assignments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_KCONFIG_ASSIGNMENTS:
            raise ValueError(f"assignments exceeds maximum of {MAX_KCONFIG_ASSIGNMENTS} entries")
        return value


class PatchApplyRequest(BaseModel):
    patch_text: str = Field(..., max_length=MAX_PATCH_TEXT_CHARS)
    selected_hunks: dict[str, list[int]] | None = None

    @field_validator("selected_hunks")
    @classmethod
    def _validate_selected_hunks(cls, value: dict[str, list[int]] | None) -> dict[str, list[int]] | None:
        if value is None:
            return None
        if len(value) > 5000:
            raise ValueError("selected_hunks exceeds 5000 file entries")
        for path, indices in value.items():
            if len(indices) > 5000:
                raise ValueError(f"selected_hunks for {path!r} exceeds 5000 hunks")
            if any(idx < 0 for idx in indices):
                raise ValueError(f"selected_hunks for {path!r} contains negative hunk index")
        return value


class PatchSeriesInspectRequest(BaseModel):
    raw_text: str = Field(..., max_length=MAX_PATCH_TEXT_CHARS)


class WatchChangesRequest(BaseModel):
    from_rev: str = Field(..., alias="from", min_length=1, max_length=256)
    to_rev: str = Field(..., alias="to", min_length=1, max_length=256)
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_WATCH_ITEMS)
