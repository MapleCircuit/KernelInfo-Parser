"""webapp/backend/models.py - Pydantic Request Models."""
from __future__ import annotations
from pydantic import BaseModel, Field


class FormatPatchRequest(BaseModel):
    file_path: str = Field(..., description="Relative file path being patched")
    original_content: str = Field(..., description="Pristine file content")
    modified_content: str = Field(..., description="Modified file content")
    commit_subject: str = Field(default="[PATCH] update file", description="Patch commit summary line")
    commit_body: str = Field(default="", description="Patch description (commit message body)")
    author_name: str = Field(default="Kernel Developer", description="Patch author name")
    author_email: str = Field(default="dev@kernel.org", description="Patch author email")
