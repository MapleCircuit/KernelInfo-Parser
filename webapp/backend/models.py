"""webapp/backend/models.py - Pydantic Request & Response Models."""
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field


class AutoSolveRequest(BaseModel):
    target_symbol: str = Field(..., description="Target Kconfig symbol name to enable")
    current_values: dict[str, str] = Field(default_factory=dict, description="Active symbol values")


class DiffConfigRequest(BaseModel):
    active_config: dict[str, str] = Field(..., description="Base configuration")
    custom_config: dict[str, str] = Field(..., description="Target configuration to compare against")


class FormatPatchRequest(BaseModel):
    file_path: str = Field(..., description="Relative file path being patched")
    original_content: str = Field(..., description="Pristine file content")
    modified_content: str = Field(..., description="Modified file content")
    commit_subject: str = Field(default="[PATCH] update file", description="Patch commit summary line")
    author_name: str = Field(default="Kernel Developer", description="Patch author name")
    author_email: str = Field(default="dev@kernel.org", description="Patch author email")


class PatchReviewRequest(BaseModel):
    patch_text: str = Field(default="", description="Unified diff or patch email text")
