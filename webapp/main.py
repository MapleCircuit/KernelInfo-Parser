"""webapp/main.py - KernelInfo-Parser Developer Web Application Entrypoint.

Starts the FastAPI server and exposes clean domain services.
To run locally:
    uvicorn webapp.main:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations
import sys
from pathlib import Path

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from webapp.backend.app import create_app
from webapp.backend.services import (
    FilesystemService,
    SymbolService,
    KconfigService,
    MaintainerService,
    GitService,
    PaholeService,
    CallgraphService,
    DiffService,
)

# Initialize application instance
app = create_app()

# Service singletons
fs_service = FilesystemService()
symbol_service = SymbolService()
kconfig_service = KconfigService()
maintainer_service = MaintainerService()
git_service = GitService()
pahole_service = PaholeService()
callgraph_service = CallgraphService()
diff_service = DiffService()

# Models
from webapp.backend.models import (
    AutoSolveRequest,
    DiffConfigRequest,
    FormatPatchRequest,
    PatchReviewRequest,
)
from webapp.backend.services.git_service import _compute_structured_diff
from webapp.backend.database.pool import db, DatabaseManager

# Exported service delegate functions for direct script/test execution
get_tree = fs_service.get_tree
get_file = fs_service.get_file
browse_path = fs_service.browse_path
get_file_by_id = fs_service.get_file_by_id
get_file_references_internal = lambda cnx, vid, target_fnid: fs_service.get_file_references(str(vid), target_fnid)
export_compile_commands = fs_service.export_compile_commands
get_codebase_treemap = fs_service.get_codebase_treemap

search_symbols = symbol_service.search_symbols
lookup_symbols = symbol_service.lookup_symbols
get_symbol_detail = symbol_service.get_symbol_detail
get_symbol_xref = symbol_service.get_symbol_xref
get_ast_container_tree = lambda ast_id, version_name="v3.0", depth=3: symbol_service.get_ast_tree(ast_id, depth, version_name)
get_tag_by_id = lambda tag_id: symbol_service.get_tag_timeline(tag_id)
get_tag_timeline = symbol_service.get_tag_timeline
get_include_symbols = symbol_service.get_include_symbols

get_kconfig_defconfigs = kconfig_service.get_defconfigs
get_kconfig_defconfig_content = kconfig_service.get_defconfig_content
get_kconfig_tree = kconfig_service.get_tree
get_kconfig_symbol_detail = kconfig_service.get_symbol_detail
get_kconfig_graph = kconfig_service.get_graph
get_kconfig_env_presets = kconfig_service.get_env_presets
export_kconfig_file = kconfig_service.export_file
import_kconfig_file = kconfig_service.import_file
search_kconfig_symbols = kconfig_service.search_symbols
validate_kconfig_assignments = kconfig_service.validate_assignments
autosolve_kconfig = kconfig_service.autosolve
diff_kconfig_configurations = kconfig_service.diff_configurations

get_maintainers_overview = maintainer_service.get_overview
get_maintainer_section_detail = maintainer_service.get_section_detail
get_person_profile = maintainer_service.get_person_profile
get_credits_overview = maintainer_service.get_credits
match_patch_maintainers = maintainer_service.match_maintainers

get_blame = git_service.get_blame
get_file_blame = git_service.get_file_blame
get_commits = git_service.get_commits
get_version_commits = git_service.get_commits
get_commit_detail = git_service.get_commit_detail
get_commit_timeline = git_service.get_commit_timeline
generate_formatted_patch = git_service.format_patch

get_struct_layout = pahole_service.get_struct_layout
get_function_callgraph = callgraph_service.get_function_callgraph
get_versions_diff = diff_service.get_versions_diff
get_kconfig_diff = diff_service.get_kconfig_diff


if __name__ == "__main__":
    import uvicorn
    from webapp.backend.config import get_backend_server_config

    cfg = get_backend_server_config()
    uvicorn.run(
        "webapp.main:app",
        host=cfg.get("host", "0.0.0.0"),
        port=int(cfg.get("port", 8000)),
        reload=bool(cfg.get("reload", True)),
    )
