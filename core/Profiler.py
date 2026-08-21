from dataclasses import dataclass, field
import time
from core.globalstuff import COLOR


@dataclass
class PipelineProfiler:
    """Tracks sub-millisecond durations for every core stage in the parsing pipeline."""

    file_path: str = ""
    clang_parse_tu_s: float = 0.0
    clang_tokenize_s: float = 0.0
    token_processing_s: float = 0.0
    ast_extraction_s: float = 0.0
    cs_execute_s: float = 0.0
    te_db_commit_s: float = 0.0
    total_elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, float | str]:
        """Convert profiler metrics to a serializable dictionary."""
        return {
            "file_path": self.file_path,
            "clang_parse_tu_s": self.clang_parse_tu_s,
            "clang_tokenize_s": self.clang_tokenize_s,
            "token_processing_s": self.token_processing_s,
            "ast_extraction_s": self.ast_extraction_s,
            "cs_execute_s": self.cs_execute_s,
            "te_db_commit_s": self.te_db_commit_s,
            "total_elapsed_s": self.total_elapsed_s or (
                self.clang_parse_tu_s
                + self.clang_tokenize_s
                + self.token_processing_s
                + self.ast_extraction_s
                + self.cs_execute_s
                + self.te_db_commit_s
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, float | str]) -> "PipelineProfiler":
        """Recreate profiler instance from a serialized dictionary."""
        return cls(
            file_path=str(data.get("file_path", "")),
            clang_parse_tu_s=float(data.get("clang_parse_tu_s", 0.0)),
            clang_tokenize_s=float(data.get("clang_tokenize_s", 0.0)),
            token_processing_s=float(data.get("token_processing_s", 0.0)),
            ast_extraction_s=float(data.get("ast_extraction_s", 0.0)),
            cs_execute_s=float(data.get("cs_execute_s", 0.0)),
            te_db_commit_s=float(data.get("te_db_commit_s", 0.0)),
            total_elapsed_s=float(data.get("total_elapsed_s", 0.0)),
        )

    def add(self, other: "PipelineProfiler") -> None:
        """Accumulate metrics from another profiler instance."""
        self.clang_parse_tu_s += other.clang_parse_tu_s
        self.clang_tokenize_s += other.clang_tokenize_s
        self.token_processing_s += other.token_processing_s
        self.ast_extraction_s += other.ast_extraction_s
        self.cs_execute_s += other.cs_execute_s
        self.te_db_commit_s += other.te_db_commit_s
        self.total_elapsed_s += other.total_elapsed_s


def _format_ms(val_s: float) -> str:
    """Format seconds into readable milliseconds string."""
    ms = val_s * 1000.0
    if ms >= 1000.0:
        return f"{val_s:5.2f}s"
    elif ms >= 10.0:
        return f"{ms:5.1f}ms"
    else:
        return f"{ms:5.2f}ms"


def _format_pct(val_s: float, total_s: float) -> str:
    """Format percentage of total with threshold-based ANSI highlighting."""
    if total_s <= 0.0:
        return "  0.0%"
    pct = (val_s / total_s) * 100.0
    formatted = f"{pct:5.1f}%"
    if pct >= 50.0:
        return COLOR.red(formatted)
    elif pct >= 25.0:
        return COLOR.yellow(formatted)
    return formatted


def format_profiling_report(
    profilers: list[PipelineProfiler],
    title: str = "PIPELINE STAGE TIMING BREAKDOWN (PROFILER)",
) -> str:
    """Render a structured, colorized terminal report of pipeline stage timings."""
    if not profilers:
        return "No profiler data available."

    lines = []
    w_file = 38
    sep_len = w_file + 7 * 11 + 3
    border = "=" * sep_len

    lines.append("\n" + border)
    lines.append(COLOR.cyan(f"{title:^{sep_len}}"))
    lines.append(border)
    header = (
        f"{'Target File':<{w_file}} | "
        f"{'Clang TU':>9} | "
        f"{'Tokenize':>9} | "
        f"{'Process':>9} | "
        f"{'AST Gen':>9} | "
        f"{'CS.exec':>9} | "
        f"{'Commit':>9} | "
        f"{'Total':>9}"
    )
    lines.append(header)
    lines.append("-" * sep_len)

    tot = PipelineProfiler(file_path="TOTAL AGGREGATE")
    for p in profilers:
        tot.add(p)
        f_display = p.file_path
        if len(f_display) > w_file:
            f_display = "..." + f_display[-(w_file - 3):]

        p_total = p.total_elapsed_s or (
            p.clang_parse_tu_s
            + p.clang_tokenize_s
            + p.token_processing_s
            + p.ast_extraction_s
            + p.cs_execute_s
            + p.te_db_commit_s
        )

        row = (
            f"{f_display:<{w_file}} | "
            f"{_format_ms(p.clang_parse_tu_s):>9} | "
            f"{_format_ms(p.clang_tokenize_s):>9} | "
            f"{_format_ms(p.token_processing_s):>9} | "
            f"{_format_ms(p.ast_extraction_s):>9} | "
            f"{_format_ms(p.cs_execute_s):>9} | "
            f"{_format_ms(p.te_db_commit_s):>9} | "
            f"{_format_ms(p_total):>9}"
        )
        lines.append(row)

    lines.append("=" * sep_len)

    grand_total = tot.total_elapsed_s or (
        tot.clang_parse_tu_s
        + tot.clang_tokenize_s
        + tot.token_processing_s
        + tot.ast_extraction_s
        + tot.cs_execute_s
        + tot.te_db_commit_s
    )

    tot_row = (
        f"{COLOR.cyan('TOTAL AGGREGATE'):<{w_file + 9}} | "
        f"{_format_ms(tot.clang_parse_tu_s):>9} | "
        f"{_format_ms(tot.clang_tokenize_s):>9} | "
        f"{_format_ms(tot.token_processing_s):>9} | "
        f"{_format_ms(tot.ast_extraction_s):>9} | "
        f"{_format_ms(tot.cs_execute_s):>9} | "
        f"{_format_ms(tot.te_db_commit_s):>9} | "
        f"{_format_ms(grand_total):>9}"
    )
    lines.append(tot_row)

    pct_row = (
        f"{COLOR.cyan('PERCENTAGE OF TOTAL'):<{w_file + 9}} | "
        f"{_format_pct(tot.clang_parse_tu_s, grand_total):>18} | "
        f"{_format_pct(tot.clang_tokenize_s, grand_total):>18} | "
        f"{_format_pct(tot.token_processing_s, grand_total):>18} | "
        f"{_format_pct(tot.ast_extraction_s, grand_total):>18} | "
        f"{_format_pct(tot.cs_execute_s, grand_total):>18} | "
        f"{_format_pct(tot.te_db_commit_s, grand_total):>18} | "
        f"{'100.0%':>9}"
    )
    lines.append(pct_row)
    lines.append(border + "\n")

    return "\n".join(lines)
