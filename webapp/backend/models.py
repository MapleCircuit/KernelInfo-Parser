"""webapp/backend/models.py - Pydantic Request Models."""
from __future__ import annotations

from pydantic import BaseModel, Field

MAX_CONTENT_CHARS = 5_000_000


class FormatPatchRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=1024, description="Relative file path being patched")
    original_content: str = Field(..., max_length=MAX_CONTENT_CHARS, description="Pristine file content")
    modified_content: str = Field(..., max_length=MAX_CONTENT_CHARS, description="Modified file content")
    commit_subject: str = Field(default="[PATCH] update file", max_length=500, description="Patch commit summary line")
    commit_body: str = Field(default="", max_length=100_000, description="Patch description (commit message body)")
    author_name: str = Field(default="Kernel Developer", max_length=256, description="Patch author name")
    author_email: str = Field(default="dev@kernel.org", max_length=320, description="Patch author email")
