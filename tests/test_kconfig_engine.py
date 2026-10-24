"""tests/test_kconfig_engine.py - Kconfig model, evaluator and web engine fidelity tests.

Synthetic Kconfig trees pin down individual kconfig rules (menu_finalize propagation, choices,
select/imply, ranges, modules, .config input/output). The fidelity tests compare against the
output of the kernel's own scripts/kconfig/conf for Linux v3.0, stored gzipped in
tests/fixtures/kconfig/ and produced with `make ARCH=<arch> O=<dir> <target>` (and
`make savedefconfig`) on a pristine `git archive v3.0` tree. Both the Python evaluator and the
client-side JavaScript engine must reproduce them byte for byte.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser.kconfig_ast.kconfig_eval import KconfigEvaluator, solve
from parser.kconfig_ast.kconfig_lexer import KconfigLexer, TokenType
from parser.kconfig_ast.kconfig_model import (
    KconfigEnv,
    KconfigModelBuilder,
    arch_aliases,
    compact_model,
    resolve_kbuild_defconfig,
)
from parser.kconfig_ast.kconfig_parser import ExprOp, KconfigParser
from parser.kconfig_ast.kconfig_preprocess import KconfigPreprocessor
from parser.kconfig_ast.kconfig_toolchain import ToolchainProfile, ToolchainShell
from parser.kbuild_parser import KbuildParser

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "kconfig")
ENGINE_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp", "frontend", "js", "views", "kconfig", "kconfig_engine.js")


def build(files: dict[str, str], arch: str = "x86_64", srcarch: str = "x86") -> dict:
    return KconfigModelBuilder(files.get, KconfigEnv(ARCH=arch, SRCARCH=srcarch, KERNELVERSION="9.9.9")).build("Kconfig")


def evaluator(files: dict[str, str], config: str = "", **kw) -> KconfigEvaluator:
    ev = KconfigEvaluator(build(files, **kw))
    ev.load_config(config)
    ev.set_all_new_symbols("default")
    return ev


BASE = """mainmenu "Test/$ARCH $KERNELVERSION"
config ARCH
\tstring
\toption env="ARCH"
config MODULES
\tbool "Modules"
\tdefault y
"""


class TestKconfigSyntax(unittest.TestCase):
    """Lexer/parser features required by the semantic model."""

    def test_keywords_are_case_sensitive(self) -> None:
        toks = KconfigLexer("config Bool\n\tbool \"x\"\n").tokenize()
        self.assertEqual(toks[1].type, TokenType.SYMBOL)
        self.assertEqual(toks[3].type, TokenType.BOOL)

    def test_relational_operators_and_macros(self) -> None:
        src = 'config A\n\tint "a" if B >= 3 && C < 0x10\n\tdefault $(shell,echo 1)\n'
        cfg = KconfigParser(KconfigLexer(src).tokenize(), src).parse()[0]
        self.assertEqual(cfg.prompt_cond.op, ExprOp.AND)
        self.assertEqual(cfg.prompt_cond.left.op, ExprOp.GREATER_EQUAL)
        self.assertEqual(cfg.prompt_cond.right.op, ExprOp.LESS)
        self.assertEqual(cfg.defaults[0][0].op, ExprOp.CONSTANT)
        self.assertEqual(cfg.defaults[0][0].value, "$(shell,echo 1)")

    def test_options_ranges_and_unknown_properties(self) -> None:
        src = 'config A\n\tstring\n\toption env="SRCARCH"\nconfig B\n\tint "b"\n\trange 1 MAX if C\n\tfrobnicate if D\nconfig E\n\tbool\n'
        items = KconfigParser(KconfigLexer(src).tokenize(), src).parse()
        self.assertEqual(items[0].env, "SRCARCH")
        lo, hi, cond = items[1].ranges[0]
        self.assertEqual((lo.value, hi.value, cond.value), ("1", "MAX", "C"))
        # The unknown property line must not be read as an 'if' block swallowing config E
        self.assertEqual([i.name for i in items], ["A", "B", "E"])

    def test_untyped_choice_takes_member_type(self) -> None:
        files = {"Kconfig": BASE + 'choice\n\tprompt "pick"\nconfig X\n\ttristate "x"\nconfig Y\n\ttristate "y"\nendchoice\n'}
        model = build(files)
        choice = next(s for n, s in model["symbols"].items() if s.get("is_choice"))
        self.assertEqual(choice["type"], "tristate")
        self.assertEqual(choice["members"], ["X", "Y"])


class TestKconfigModelSemantics(unittest.TestCase):
    """menu_finalize rules and value computation on small trees."""

    def test_env_mainmenu_and_source_expansion(self) -> None:
        files = {
            "Kconfig": BASE + 'source "arch/$ARCH/Kconfig"\n',
            "arch/x86_64/Kconfig": "config FROM_ARCH\n\tdef_bool y\n",
        }
        ev = evaluator(files)
        self.assertEqual(ev.model["mainmenu"], "Test/x86_64 9.9.9")
        self.assertEqual(ev.value("ARCH"), "x86_64")
        self.assertEqual(ev.value("FROM_ARCH"), "y")
        self.assertNotIn("CONFIG_ARCH", ev.write_config())  # env symbols are never written

    def test_dependencies_propagate_through_menus_and_ifs(self) -> None:
        files = {"Kconfig": BASE + 'menu "M"\n\tdepends on A\nif B\nconfig C\n\tbool "c"\n\tdefault y\nendif\nendmenu\nconfig A\n\tbool "a"\nconfig B\n\tbool "b"\n\tdefault y\n'}
        ev = evaluator(files)
        self.assertEqual(ev.value("C"), "n")
        self.assertTrue(ev.set_value("A", "y"))
        self.assertEqual(ev.value("C"), "y")
        self.assertTrue(ev.set_value("B", "n"))
        self.assertEqual(ev.value("C"), "n")

    def test_implicit_submenu_and_if_hoisting(self) -> None:
        files = {"Kconfig": BASE + 'config FOO\n\tbool "foo"\nconfig FOO_A\n\tbool "a"\n\tdepends on FOO\nif FOO\nconfig FOO_B\n\tbool "b"\nendif\nconfig OTHER\n\tbool "other"\n'}
        model = build(files)
        nodes = model["nodes"]
        foo = next(n for n in nodes if n["symbol_name"] == "FOO")
        # The emptied if-block stays in the list as a prompt-less entry, as in menu_finalize
        children = [nodes[c]["symbol_name"] for c in foo["children"] if nodes[c]["prompt"]]
        self.assertEqual(children, ["FOO_A", "FOO_B"])
        other = next(n for n in nodes if n["symbol_name"] == "OTHER")
        self.assertEqual(other["parent_id"], 0)

    def test_select_forces_value_and_blocks_lowering(self) -> None:
        files = {"Kconfig": BASE + 'config S\n\ttristate "s"\n\tselect T\nconfig T\n\ttristate "t"\n\tdepends on NEVER\n'}
        ev = evaluator(files)
        self.assertEqual(ev.value("T"), "n")
        self.assertTrue(ev.set_value("S", "m"))
        self.assertEqual(ev.value("T"), "m")
        self.assertTrue(ev.set_value("S", "y"))
        self.assertEqual(ev.value("T"), "y")
        # T is selected despite its unmet dependency (kconfig warns but forces it)
        self.assertEqual(ev.dir_dep("T"), 0)
        self.assertFalse(ev.set_value("T", "n"))

    def test_modules_off_degrades_tristate_and_m_dependencies(self) -> None:
        files = {"Kconfig": BASE + 'config D\n\ttristate "d"\n\tdefault m\nconfig E\n\tbool "e"\n\tdepends on m\n'}
        ev = evaluator(files)
        self.assertEqual(ev.value("D"), "m")
        self.assertEqual(ev.visibility("E"), 2)  # 'm' is rewritten to 'm && MODULES'
        self.assertTrue(ev.set_value("MODULES", "n"))
        self.assertEqual(ev.value("D"), "y")
        self.assertEqual(ev.visibility("E"), 0)
        self.assertFalse(ev.set_value("D", "m"))

    def test_choice_default_user_selection_and_optional(self) -> None:
        files = {"Kconfig": BASE + 'choice\n\tprompt "alloc"\n\tdefault B2\nconfig B1\n\tbool "1"\nconfig B2\n\tbool "2"\nendchoice\n'
                 'choice\n\tprompt "opt"\n\toptional\nconfig O1\n\tbool "o1"\nendchoice\n'}
        ev = evaluator(files)
        self.assertEqual((ev.value("B1"), ev.value("B2")), ("n", "y"))
        self.assertEqual(ev.value("O1"), "n")
        self.assertTrue(ev.set_value("B1", "y"))
        self.assertEqual((ev.value("B1"), ev.value("B2")), ("y", "n"))
        self.assertFalse(ev.set_value("B1", "n"))  # deselect only by choosing another member
        ev.load_config("CONFIG_B1=y\n")
        self.assertEqual(ev.value("B1"), "y")

    def test_ranges_clamp_defaults_and_refuse_user_values(self) -> None:
        files = {"Kconfig": BASE + 'config N\n\tint "n"\n\trange 2 8\nconfig H\n\thex "h"\n\trange 0x10 0x20\n\tdefault 0x40\n'}
        ev = evaluator(files)
        self.assertEqual(ev.value("N"), "2")  # no default: clamped to the range minimum
        self.assertEqual(ev.value("H"), "0x20")
        self.assertTrue(ev.set_value("N", "5"))
        self.assertFalse(ev.set_value("N", "9"))
        self.assertFalse(ev.set_value("N", "abc"))
        self.assertTrue(ev.set_value("H", "18"))
        self.assertEqual(ev.value("H"), "0x18")

    def test_imply_is_a_weak_default(self) -> None:
        files = {"Kconfig": BASE + 'config I\n\ttristate "i"\n\timply J\nconfig J\n\ttristate "j"\n'}
        ev = evaluator(files)
        ev.set_value("I", "y")
        self.assertEqual(ev.value("J"), "y")
        self.assertTrue(ev.set_value("J", "n"))  # unlike select, the user can still say n
        self.assertEqual(ev.value("J"), "n")

    def test_string_round_trip_and_unknown_symbols(self) -> None:
        files = {"Kconfig": BASE + 'config STR\n\tstring "s"\n\tdefault "x"\n'}
        ev = KconfigEvaluator(build(files))
        report = ev.load_config('CONFIG_STR="a \\"q\\" \\\\b"\nCONFIG_GONE=y\nCONFIG_MODULES=m\n')
        self.assertEqual(ev.value("STR"), 'a "q" \\b')
        self.assertEqual(report["unknown"], ["GONE"])
        self.assertEqual(report["invalid"], [{"symbol": "MODULES", "value": "m"}])
        out = ev.write_config()
        self.assertIn('CONFIG_STR="a \\"q\\" \\\\b"', out)
        again = KconfigEvaluator(ev.model)
        again.load_config(out)
        self.assertEqual(again.write_config(), out)

    def test_savedefconfig_is_minimal(self) -> None:
        files = {"Kconfig": BASE + 'config A\n\tbool "a"\n\tdefault y\nconfig B\n\tbool "b"\n'}
        ev = evaluator(files, "CONFIG_B=y\n")
        self.assertEqual(ev.write_defconfig(), "CONFIG_B=y\n")
        ev.set_value("A", "n")
        self.assertEqual(ev.write_defconfig(), "# CONFIG_A is not set\nCONFIG_B=y\n")

    def test_solver_enables_prerequisites_legally(self) -> None:
        files = {"Kconfig": BASE + 'config BUS\n\tbool "bus"\nconfig DRV\n\ttristate "drv"\n\tdepends on BUS && !BROKEN\nconfig BROKEN\n\tbool\n'}
        ev = evaluator(files)
        res = solve(ev, "DRV")
        self.assertTrue(res["solution_found"])
        self.assertEqual([t["symbol"] for t in res["toggles_needed"]], ["BUS", "DRV"])
        files["Kconfig"] = files["Kconfig"].replace("config BROKEN\n\tbool\n", "config BROKEN\n\tdef_bool y\n")
        res = solve(evaluator(files), "DRV")
        self.assertFalse(res["solution_found"])
        self.assertTrue(res["blocked_by"])

    def test_makefile_helpers(self) -> None:
        mk = "SRCARCH := $(ARCH)\nifeq ($(ARCH),i386)\n        SRCARCH := x86\nendif\nifeq ($(ARCH),sparc64)\n       SRCARCH := sparc\nendif\n"
        self.assertEqual(arch_aliases(mk), {"i386": "x86", "sparc64": "sparc"})
        arch_mk = "ifeq ($(ARCH),x86)\n        KBUILD_DEFCONFIG := i386_defconfig\nelse\n        KBUILD_DEFCONFIG := $(ARCH)_defconfig\nendif\n"
        self.assertEqual(resolve_kbuild_defconfig(arch_mk, "x86"), "i386_defconfig")
        self.assertEqual(resolve_kbuild_defconfig(arch_mk, "x86_64"), "x86_64_defconfig")

    def test_kbuild_conditional_blocks_and_goal_lists(self) -> None:
        mk = "ifeq ($(CONFIG_X86_32),y)\nobj-y += a.o\nelse\nobj-$(CONFIG_B) += sub/\nendif\nlib-$(CONFIG_C) += c.o\n"
        bindings = KbuildParser().parse_makefile_content(mk, "arch/x86/lib")
        by_file = {b.source_file_rel: b for b in bindings}
        self.assertEqual(by_file["arch/x86/lib/a.c"].cond_symbols, ["X86_32"])
        self.assertTrue(by_file["arch/x86/lib/sub/Makefile"].is_directory)
        self.assertEqual(by_file["arch/x86/lib/c.c"].symbol_name, "C")


class TestKconfigMacroLanguage(unittest.TestCase):
    """scripts/kconfig/preprocess.c semantics (Linux 4.18+)."""

    def pp(self, env=None, shell=None) -> KconfigPreprocessor:
        return KconfigPreprocessor(env or {}, shell)

    def test_variable_flavors_and_arguments(self) -> None:
        pp = self.pp({"CC": "gcc"})
        pp.add_variable("simple", "$(CC)-x", ":=")
        pp.add_variable("lazy", "$(CC)-$(1)", "=")
        pp.add_variable("lazy", "more", "+=")
        pp.env["CC"] = "clang"
        self.assertEqual(pp.expand_string("$(simple)"), "gcc-x")  # := expands at definition
        self.assertEqual(pp.expand_string("$(lazy,a)"), "clang-a more")  # = expands at use, += keeps flavor
        self.assertEqual(pp.expand_string("$(UNDEFINED)"), "")
        self.assertIn("UNDEFINED", pp.env_used)

    def test_builtin_functions(self) -> None:
        calls = []
        pp = self.pp(shell=lambda cmd: (calls.append(cmd), "a\nb\n")[1])
        self.assertEqual(pp.expand_string("$(shell,echo hi)"), "a b")  # newlines -> spaces, trailing dropped
        self.assertEqual(calls, ["echo hi"])
        pp.expand_string("$(error-if,y,boom)$(warning-if,n,quiet)$(info,note)")
        self.assertEqual([(m["level"], m["text"]) for m in pp.messages], [("error", "boom"), ("info", "note")])

    def test_lexer_expansion_and_parse_order(self) -> None:
        files = {
            "Kconfig": 'mainmenu "M $(ARCH)"\nsource "defs"\nconfig A\n\tdef_bool $(answer)\nconfig S\n\tstring\n\tdefault "$(greet,world)"\n',
            "defs": 'answer := y\ngreet = hello $(1)\n$(warning-if,y,from defs)\n',
        }
        pp = KconfigPreprocessor({"ARCH": "x86_64"})
        model = KconfigModelBuilder(files.get, KconfigEnv("x86_64", "x86"), pp).build("Kconfig")
        ev = KconfigEvaluator(model)
        ev.set_all_new_symbols("default")
        self.assertEqual(model["mainmenu"], "M x86_64")
        self.assertEqual(ev.value("A"), "y")  # defined in a sourced file, used after the source line
        self.assertEqual(ev.value("S"), "hello world")
        self.assertEqual(pp.messages[0]["text"], "from defs")


class TestKconfigToolchain(unittest.TestCase):
    """Toolchain emulation of $(shell,...) answers."""

    INCLUDE = {
        "scripts/cc-version.sh": "get_canonical_version()\n GCC\n Clang\n",
        "scripts/ld-version.sh": 'name=BFD\n',
        "scripts/min-tool-version.sh": "case \"$1\" in\nbinutils)\n\techo 2.25.0\n\t;;\ngcc)\n\techo 5.1.0\n\t;;\nllvm)\n\techo 11.0.0\n\t;;\nesac\n",
    }

    def shell(self, **profile) -> ToolchainShell:
        prof = ToolchainProfile.from_dict(profile, "6.1.0")
        return ToolchainShell(prof, prof.environment("x86_64", "x86", "6.1.0"), "x86", self.INCLUDE.get)

    def test_version_scripts(self) -> None:
        sh = self.shell(cc="gcc", cc_version="12.2.0", binutils_version="2.39")
        self.assertEqual(sh("./scripts/cc-version.sh gcc").strip(), "GCC 120200")
        self.assertEqual(sh("./scripts/ld-version.sh ld").strip(), "BFD 23900")
        self.assertEqual(sh("set -- GCC 120200 && echo $2").strip(), "120200")
        clang = self.shell(cc="clang", cc_version="15.0.7")
        self.assertEqual(clang("./scripts/cc-version.sh clang").strip(), "Clang 150007")
        self.assertEqual(clang("./scripts/ld-version.sh ld.lld").strip(), "LLD 150007")
        self.assertEqual(self.shell(cc="gcc", cc_version="4.9.0")("./scripts/cc-version.sh gcc"), "")  # below minimum

    def test_probes_knowledge_and_overrides(self) -> None:
        wrap = '{{ {0}; }} >/dev/null 2>&1 && echo "y" || echo "n"'
        kcfi = wrap.format("gcc -Werror -fsanitize=kcfi -c -x c /dev/null -o /dev/null")
        self.assertEqual(self.shell(cc="gcc")(kcfi).strip(), "n")
        self.assertEqual(self.shell(cc="clang", cc_version="16.0.0")(kcfi.replace("gcc", "clang")).strip(), "y")
        forced = self.shell(cc="gcc", probes={"cc-option:-fsanitize=kcfi": "y"})
        self.assertEqual(forced(kcfi).strip(), "y")
        self.assertEqual(forced.probes["cc-option:-fsanitize=kcfi"]["source"], "override")
        unknown = self.shell(cc="gcc")
        unknown(wrap.format("gcc -Werror -fbrand-new-flag -c -x c /dev/null -o /dev/null"))
        self.assertEqual(unknown.probes["cc-option:-fbrand-new-flag"]["source"], "assumed")
        self.assertEqual(self.shell()(wrap.format('test "GCC" = GCC')).strip(), "y")

    def test_environment_overrides(self) -> None:
        prof = ToolchainProfile.from_dict({"cc": "clang", "env": {"CC_VERSION_TEXT": "custom"}}, "6.1.0")
        env = prof.environment("arm64", "arm64", "6.1.0")
        self.assertEqual((env["CC"], env["LD"], env["CC_VERSION_TEXT"]), ("clang", "ld.lld", "custom"))
        self.assertEqual(ToolchainProfile.from_dict({}, "6.1.0").cc_version, "12.2.0")


class TestKconfigKernelFidelity(unittest.TestCase):
    """Linux v3.0 models evaluated by Python and JavaScript must match real `conf` output."""

    CASES = [
        # (ARCH, target, fixture)
        ("x86_64", "x86_64_defconfig", "v3.0_x86_64_x86_64_defconfig.config.gz"),
        ("i386", "i386_defconfig", "v3.0_i386_i386_defconfig.config.gz"),
        ("arm", "versatile_defconfig", "v3.0_arm_versatile_defconfig.config.gz"),
        ("sparc", "sparc64_defconfig", "v3.0_sparc_sparc64_defconfig.config.gz"),
        ("mips", "malta_defconfig", "v3.0_mips_malta_defconfig.config.gz"),
        ("x86_64", "allnoconfig", "v3.0_x86_64_allnoconfig.config.gz"),
        ("x86_64", "allmodconfig", "v3.0_x86_64_allmodconfig.config.gz"),
    ]
    MODES = {"allnoconfig": "no", "allyesconfig": "yes", "allmodconfig": "mod"}

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from webapp.backend.services.kconfig_service import KconfigService
            cls.svc = KconfigService()
            cls.svc.get_model("v3.0", "x86_64")
        except Exception as exc:
            raise unittest.SkipTest(f"Linux v3.0 not readable from the configured repository: {exc}")

    @staticmethod
    def fixture(name: str) -> str:
        with gzip.open(os.path.join(FIXTURES, name), "rt") as fh:
            return fh.read()

    def defconfig_text(self, arch: str, target: str) -> str:
        return self.svc.get_defconfig_content("v3.0", target, arch)["content"]

    def run_python(self, arch: str, target: str) -> KconfigEvaluator:
        ev = self.svc.get_model("v3.0", arch).evaluator()
        if target in self.MODES:
            ev.set_all_new_symbols(self.MODES[target])
        else:
            ev.load_config(self.defconfig_text(arch, target))
            ev.set_all_new_symbols("default")
        return ev

    def test_python_evaluator_matches_conf(self) -> None:
        for arch, target, fixture in self.CASES:
            with self.subTest(arch=arch, target=target):
                self.assertEqual(self.run_python(arch, target).write_config(), self.fixture(fixture))

    def test_python_savedefconfig_matches_conf(self) -> None:
        ev = self.KconfigEvaluator_from_config("x86_64", self.fixture("v3.0_x86_64_x86_64_defconfig.config.gz"))
        self.assertEqual(ev.write_defconfig(), self.fixture("v3.0_x86_64_x86_64_defconfig.savedefconfig.gz"))

    def KconfigEvaluator_from_config(self, arch: str, text: str) -> KconfigEvaluator:
        ev = self.svc.get_model("v3.0", arch).evaluator()
        ev.load_config(text)
        return ev

    def test_written_config_is_a_fixed_point(self) -> None:
        text = self.fixture("v3.0_x86_64_x86_64_defconfig.config.gz")
        ev = self.KconfigEvaluator_from_config("x86_64", text)
        self.assertEqual(ev.write_config(), text)

    def test_same_symbol_resolves_per_arch(self) -> None:
        x86 = self.svc.get_symbol_detail("v3.0", "64BIT", "x86_64")
        sparc = self.svc.get_symbol_detail("v3.0", "64BIT", "sparc64")
        arm = self.svc.get_symbol_detail("v3.0", "64BIT", "arm")
        self.assertEqual(x86["file_path"], "arch/x86/Kconfig")
        self.assertEqual(sparc["file_path"], "arch/sparc/Kconfig")
        self.assertFalse(arm["defined_for_arch"])
        self.assertTrue(arm["declarations"])
        self.assertEqual(self.run_python("i386", "i386_defconfig").value("64BIT"), "n")

    def test_symbol_provenance_and_built_files(self) -> None:
        ext4 = self.svc.get_symbol_detail("v3.0", "EXT4_FS", "x86_64")
        self.assertEqual(ext4["file_path"], "fs/ext4/Kconfig")
        files = {f["file_path"] for f in ext4["compiled_files"]}
        self.assertIn("fs/ext4/super.c", files)
        usage = self.svc.get_symbol_usage("v3.0", "EXT4_FS", "x86_64")
        self.assertEqual(usage["source_ref_count"], 0)  # v3.0 C code never tests CONFIG_EXT4_FS itself
        self.assertTrue(any(r["by"] == "EXT4_FS_XATTR" for r in usage["kconfig_refs"]))
        smp = self.svc.get_symbol_usage("v3.0", "SMP", "x86_64")
        self.assertGreater(smp["source_file_count"], 100)
        self.assertTrue(any(r["other_arch"] for r in smp["source_refs"]))

    def test_presets_follow_the_top_makefile(self) -> None:
        presets = {p["id"]: p for p in self.svc.get_env_presets("v3.0")["targets"]}
        self.assertEqual(presets["x86_64"]["env"]["SRCARCH"], "x86")
        self.assertEqual(presets["x86_64"]["canonical_defconfig"], "x86_64_defconfig")
        self.assertEqual(presets["x86"]["canonical_defconfig"], "i386_defconfig")
        self.assertEqual(presets["sparc64"]["env"]["SRCARCH"], "sparc")
        self.assertEqual(presets["arm"]["canonical_defconfig"], "versatile_defconfig")

    def test_resolved_export_and_validation(self) -> None:
        exported = self.svc.export_file("v3.0", {"arch": "x86_64", "symbols": {"IKCONFIG_PROC": "y", "SMP": "n"}})
        self.assertIn("# CONFIG_SMP is not set", exported["content"])
        self.assertNotIn("CONFIG_IKCONFIG_PROC=y", exported["content"])
        report = self.svc.validate_assignments("v3.0", {"arch": "x86_64", "symbols": {"IKCONFIG_PROC": "y"}})
        self.assertEqual(report["adjusted_symbols"].get("IKCONFIG_PROC"), "n")

    def test_javascript_engine_matches_conf(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        tmp = tempfile.mkdtemp(prefix="kconfig-js-")
        try:
            script = os.path.join(tmp, "run.mjs")
            with open(script, "w") as fh:
                fh.write(
                    "import fs from 'fs';\n"
                    f"const {{ KconfigEngine }} = await import({json.dumps(ENGINE_JS)});\n"
                    "const [modelF, mode, cfgF, outF, kind] = process.argv.slice(2);\n"
                    "const e = new KconfigEngine(JSON.parse(fs.readFileSync(modelF, 'utf8')));\n"
                    "if (mode === 'load') { e.loadConfig(fs.readFileSync(cfgF, 'utf8')); e.setAllNewSymbols('default'); }\n"
                    "else if (mode === 'raw') { e.loadConfig(fs.readFileSync(cfgF, 'utf8')); }\n"
                    "else e.setAllNewSymbols(mode);\n"
                    "fs.writeFileSync(outF, kind === 'defconfig' ? e.writeDefconfig() : e.writeConfig());\n"
                )
            models: dict[str, str] = {}
            cases = list(self.CASES) + [("x86_64", "savedefconfig", "v3.0_x86_64_x86_64_defconfig.savedefconfig.gz")]
            try:
                self.svc.resolve_arch("v6.8", "x86_64")
                for name, spec in self.MODERN:
                    key = f"v6.8:{name}"
                    models[key] = os.path.join(tmp, f"{name}.json")
                    with open(models[key], "w") as fh:
                        json.dump(self.svc.get_tree("v6.8", "x86_64", toolchain=spec), fh)
                    cases.append((key, "x86_64_defconfig@v6.8", f"v6.8_x86_64_x86_64_defconfig.{name}.config.gz"))
            except Exception:
                pass
            for arch, target, fixture in cases:
                with self.subTest(arch=arch, target=target):
                    if arch not in models:
                        models[arch] = os.path.join(tmp, f"{arch}.json")
                        with open(models[arch], "w") as fh:
                            json.dump(self.svc.get_tree("v3.0", arch), fh)
                    cfg = os.path.join(tmp, "in.config")
                    out = os.path.join(tmp, "out.config")
                    kind = "config"
                    if target == "savedefconfig":
                        mode, kind = "raw", "defconfig"
                        with open(cfg, "w") as fh:
                            fh.write(self.fixture("v3.0_x86_64_x86_64_defconfig.config.gz"))
                    elif target in self.MODES:
                        mode = self.MODES[target]
                    elif target.endswith("@v6.8"):
                        mode = "load"
                        with open(cfg, "w") as fh:
                            fh.write(self.svc.get_defconfig_content("v6.8", "x86_64_defconfig", "x86_64")["content"])
                    else:
                        mode = "load"
                        with open(cfg, "w") as fh:
                            fh.write(self.defconfig_text(arch, target))
                    subprocess.run([node, script, models[arch], mode, cfg, out, kind], check=True, timeout=120)
                    with open(out) as fh:
                        self.assertEqual(fh.read(), self.fixture(fixture))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # Linux v6.8 x86_64_defconfig through the real conf (Ubuntu linux-headers-6.8.0 build) with the
    # real gcc 13.3.0 / binutils 2.42 and clang 18.1.3 toolchains, reproduced from toolchain profiles
    MODERN = [
        ("gcc-13.3.0", {"cc": "gcc", "cc_version": "13.3.0", "binutils_version": "2.42", "pahole_version": "none",
                        "env": {"CC_VERSION_TEXT": "gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0"}}),
        ("clang-18.1.3", {"cc": "clang", "cc_version": "18.1.3", "ld": "bfd", "ld_version": "2.42", "binutils_version": "2.42",
                          "pahole_version": "none", "llvm_utils": False, "env": {"CC_VERSION_TEXT": "Ubuntu clang version 18.1.3 (1ubuntu1)"}}),
    ]

    def modern_or_skip(self):
        try:
            self.svc.resolve_arch("v6.8", "x86_64")
        except Exception:
            self.skipTest("Linux v6.8 is not in the configured repository")

    def test_macro_language_toolchains_match_conf(self) -> None:
        self.modern_or_skip()
        defconfig = self.svc.get_defconfig_content("v6.8", "x86_64_defconfig", "x86_64")["content"]
        for name, spec in self.MODERN:
            with self.subTest(toolchain=name):
                ev = self.svc.get_model("v6.8", "x86_64", spec).evaluator()
                ev.load_config(defconfig)
                ev.set_all_new_symbols("default")
                self.assertEqual(ev.write_config(), self.fixture(f"v6.8_x86_64_x86_64_defconfig.{name}.config.gz"))
        report = self.svc.get_model("v6.8", "x86_64", self.MODERN[1][1]).model["toolchain"]
        self.assertTrue(report["macros"])
        self.assertTrue(any(p["key"] == "script:cc-version.sh" and p["result"] == "Clang 180103" for p in report["probes"]))

    def test_compact_model_round_trip_shape(self) -> None:
        model = self.svc.get_model("v3.0", "x86_64").model
        compact = compact_model(model)
        self.assertEqual(len(compact["menus"]), len(model["nodes"]))
        self.assertEqual(set(compact["symbols"]), set(model["symbols"]))


if __name__ == "__main__":
    unittest.main()
