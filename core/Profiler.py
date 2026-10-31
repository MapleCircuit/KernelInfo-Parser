from dataclasses import dataclass, field
import time
from core.globalstuff import COLOR


@dataclass
class PipelineProfiler:
    """Tracks sub-millisecond durations for every core stage in the parsing pipeline."""

    file_path: str = ""
    clang_parse_tu_s: float = 0.0
    # extract_include_symbols(): Python walk over the TU's includes and imported declarations
    include_symbols_s: float = 0.0
    clang_tokenize_s: float = 0.0
    token_processing_s: float = 0.0
    ast_extraction_s: float = 0.0
    cs_execute_s: float = 0.0
    te_db_commit_s: float = 0.0
    total_elapsed_s: float = 0.0

    def component_total(self) -> float:
        """Sum of the measured components (`total_elapsed_s` when it was set explicitly)."""
        return self.total_elapsed_s or (
            self.clang_parse_tu_s
            + self.include_symbols_s
            + self.clang_tokenize_s
            + self.token_processing_s
            + self.ast_extraction_s
            + self.cs_execute_s
            + self.te_db_commit_s
        )

    def to_dict(self) -> dict[str, float | str]:
        """Convert profiler metrics to a serializable dictionary."""
        return {
            "file_path": self.file_path,
            "clang_parse_tu_s": self.clang_parse_tu_s,
            "include_symbols_s": self.include_symbols_s,
            "clang_tokenize_s": self.clang_tokenize_s,
            "token_processing_s": self.token_processing_s,
            "ast_extraction_s": self.ast_extraction_s,
            "cs_execute_s": self.cs_execute_s,
            "te_db_commit_s": self.te_db_commit_s,
            "total_elapsed_s": self.component_total(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, float | str]) -> "PipelineProfiler":
        """Recreate profiler instance from a serialized dictionary."""
        return cls(
            file_path=str(data.get("file_path", "")),
            clang_parse_tu_s=float(data.get("clang_parse_tu_s", 0.0)),
            include_symbols_s=float(data.get("include_symbols_s", 0.0)),
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
        self.include_symbols_s += other.include_symbols_s
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


def _format_pct(val_s: float, total_s: float, width: int = 0) -> str:
    """Format percentage of total, right-aligned to `width` visible characters, with threshold-based ANSI colour."""
    pct = (val_s / total_s) * 100.0 if total_s > 0.0 else 0.0
    formatted = f"{pct:5.1f}%".rjust(width)
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
    sep_len = w_file + 8 * 11 + 3
    border = "=" * sep_len

    lines.append("\n" + border)
    lines.append(COLOR.cyan(f"{title:^{sep_len}}"))
    lines.append(border)
    header = (
        f"{'Target File':<{w_file}} | "
        f"{'Clang TU':>9} | "
        f"{'Includes':>9} | "
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

        p_total = p.component_total()

        row = (
            f"{f_display:<{w_file}} | "
            f"{_format_ms(p.clang_parse_tu_s):>9} | "
            f"{_format_ms(p.include_symbols_s):>9} | "
            f"{_format_ms(p.clang_tokenize_s):>9} | "
            f"{_format_ms(p.token_processing_s):>9} | "
            f"{_format_ms(p.ast_extraction_s):>9} | "
            f"{_format_ms(p.cs_execute_s):>9} | "
            f"{_format_ms(p.te_db_commit_s):>9} | "
            f"{_format_ms(p_total):>9}"
        )
        lines.append(row)

    lines.append("=" * sep_len)

    grand_total = tot.component_total()

    tot_row = (
        f"{COLOR.cyan('TOTAL AGGREGATE'):<{w_file + 9}} | "
        f"{_format_ms(tot.clang_parse_tu_s):>9} | "
        f"{_format_ms(tot.include_symbols_s):>9} | "
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
        f"{_format_pct(tot.clang_parse_tu_s, grand_total, 9)} | "
        f"{_format_pct(tot.include_symbols_s, grand_total, 9)} | "
        f"{_format_pct(tot.clang_tokenize_s, grand_total, 9)} | "
        f"{_format_pct(tot.token_processing_s, grand_total, 9)} | "
        f"{_format_pct(tot.ast_extraction_s, grand_total, 9)} | "
        f"{_format_pct(tot.cs_execute_s, grand_total, 9)} | "
        f"{_format_pct(tot.te_db_commit_s, grand_total, 9)} | "
        f"{'100.0%':>9}"
    )
    lines.append(pct_row)
    lines.append(border + "\n")

    return "\n".join(lines)


def parse_duration(val: str | int | float | None) -> float | None:
    """Parse duration strings like '30m', '1800s', '1h', '0.5h', '1800' into seconds."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else None
    s = str(val).strip().lower()
    if not s:
        return None
    try:
        if s.endswith("s"):
            return float(s[:-1])
        elif s.endswith("m"):
            return float(s[:-1]) * 60.0
        elif s.endswith("h"):
            return float(s[:-1]) * 3600.0
        elif s.endswith("d"):
            return float(s[:-1]) * 86400.0
        return float(s)
    except ValueError:
        return None


@dataclass
class StageMetric:
    """Tracks duration, counts, and throughput metrics for a discrete pipeline stage."""

    name: str
    elapsed_s: float = 0.0
    start_time: float | None = None
    count: int = 0
    units: str = "items"
    call_count: int = 0


class PipelineStageProfiler:
    """System-level stage timing, worker CPU accumulation, and bottleneck profiler."""

    def __init__(self) -> None:
        self.stages: dict[str, StageMetric] = {}
        self.stage_order: list[str] = []
        self.worker_aggregate: PipelineProfiler = PipelineProfiler(file_path="ALL_WORKERS_AGGREGATE")
        self.worker_count: int = 0
        self.start_wall_time: float = time.perf_counter()
        self._active_stages: set[str] = set()

    def start_stage(self, name: str) -> None:
        """Begin measuring execution duration for a stage."""
        if name not in self.stages:
            self.stages[name] = StageMetric(name=name)
            self.stage_order.append(name)
        stage = self.stages[name]
        stage.start_time = time.perf_counter()
        stage.call_count += 1
        self._active_stages.add(name)

    def stop_stage(self, name: str, count: int = 0, units: str = "items") -> float:
        """Stop measuring execution duration for a stage and update metrics."""
        if name not in self.stages:
            return 0.0
        stage = self.stages[name]
        if stage.start_time is not None:
            delta = time.perf_counter() - stage.start_time
            stage.elapsed_s += delta
            stage.start_time = None
        if count > 0:
            stage.count += count
            stage.units = units
        self._active_stages.discard(name)
        return stage.elapsed_s

    import contextlib

    @contextlib.contextmanager
    def measure(self, name: str, count: int = 0, units: str = "items"):
        """Context manager to measure the execution duration of a stage block."""
        self.start_stage(name)
        try:
            yield
        finally:
            self.stop_stage(name, count=count, units=units)

    def record_worker_profiler(self, prof: PipelineProfiler | dict | None) -> None:
        """Accumulate micro-profiling metrics from an executed worker ChangeSet."""
        if not prof:
            return
        if isinstance(prof, dict):
            prof = PipelineProfiler.from_dict(prof)
        if prof.component_total() <= 0.0:
            return
        self.worker_aggregate.add(prof)
        self.worker_count += 1

    def format_stage_report(self, title: str = "PIPELINE STAGE EXECUTION & BOTTLENECK PROFILE") -> str:
        """Format an end-to-end report of all stages with identification of top bottlenecks."""
        now = time.perf_counter()
        total_wall_s = max(0.001, now - self.start_wall_time)

        # Finalize any in-flight stages for reporting
        stage_durations: dict[str, float] = {}
        for name, stage in self.stages.items():
            dur = stage.elapsed_s
            if stage.start_time is not None:
                dur += (now - stage.start_time)
            stage_durations[name] = dur

        total_stage_time = sum(stage_durations.values())
        effective_total = max(total_wall_s, total_stage_time, 0.001)

        lines = []
        w_name = 44
        sep_len = 104
        border = "=" * sep_len

        lines.append("\n" + border)
        lines.append(COLOR.cyan(f"{title:^{sep_len}}"))
        lines.append(border)
        header = (
            f"{'Pipeline Stage':<{w_name}} | "
            f"{'Elapsed':>9} | "
            f"{'% Total':>9} | "
            f"{'Items / Count':>16} | "
            f"{'Throughput':>14}"
        )
        lines.append(header)
        lines.append("-" * sep_len)

        for name in self.stage_order:
            stage = self.stages[name]
            dur = stage_durations.get(name, 0.0)
            pct_str = _format_pct(dur, effective_total, 9)
            items_str = f"{stage.count:,} {stage.units}" if stage.count > 0 else ""
            throughput_str = ""
            if stage.count > 0 and dur > 0.0001:
                rate = stage.count / dur
                if rate >= 1000:
                    throughput_str = f"{rate:,.0f} /s"
                else:
                    throughput_str = f"{rate:.1f} /s"

            lines.append(
                f"{name:<{w_name}} | "
                f"{_format_ms(dur):>9} | "
                f"{pct_str} | "
                f"{items_str:>16} | "
                f"{throughput_str:>14}"
            )

        lines.append("=" * sep_len)
        lines.append(
            f"{COLOR.cyan('TOTAL PIPELINE WALL-CLOCK TIME'):<{w_name + 9}} | "
            f"{_format_ms(total_wall_s):>9} | "
            f"{'100.0%':>9} | "
            f"{'':>16} | "
            f"{'':>14}"
        )
        lines.append(border)

        # Parallel worker CPU breakdown if collected
        if self.worker_count > 0:
            w_tot = self.worker_aggregate
            tot_cpu = w_tot.component_total()
            if tot_cpu > 0.001:
                lines.append(COLOR.cyan("\n--- PARALLEL WORKER CPU BREAKDOWN (Clang & AST Extraction) ---"))
                lines.append(f"Aggregate across {self.worker_count:,} parsed files:")
                for label, val in (
                    ("Clang TU Parse:", w_tot.clang_parse_tu_s),
                    ("Include Symbols:", w_tot.include_symbols_s),
                    ("Clang Tokenize:", w_tot.clang_tokenize_s),
                    ("Token Processing:", w_tot.token_processing_s),
                    ("AST Generation:", w_tot.ast_extraction_s),
                    ("CS Execution:", w_tot.cs_execute_s),
                    ("TE DB Staging:", w_tot.te_db_commit_s),
                ):
                    # Components no worker measured (e.g. ChangeSets now execute in the parent) are left out
                    if val > 0.0:
                        lines.append(f"  • {label:<19} {_format_ms(val):>9} ({_format_pct(val, tot_cpu)} of worker CPU)")
                lines.append(f"  • Total Worker CPU:   {_format_ms(tot_cpu):>9}")

        # Bottleneck analysis & recommendations
        sorted_stages = sorted(stage_durations.items(), key=lambda x: x[1], reverse=True)
        bottlenecks = [s for s in sorted_stages if s[1] > 0.01][:3]
        if bottlenecks:
            lines.append("\n" + border)
            lines.append(COLOR.yellow(f"{'TOP IDENTIFIED BOTTLENECKS & RECOMMENDATIONS':^{sep_len}}"))
            lines.append(border)
            for rank, (name, dur) in enumerate(bottlenecks, 1):
                pct = (dur / effective_total) * 100.0
                color_fn = COLOR.red if pct >= 30.0 else (COLOR.yellow if pct >= 15.0 else COLOR.green)
                lines.append(color_fn(f"[BOTTLENECK #{rank}] {name}: {_format_ms(dur)} ({pct:.1f}% of total runtime)"))
                low_name = name.lower()
                if "source" in low_name and "parsing" in low_name:
                    lines.append("  -> Worker translation unit parsing, parent ChangeSet execution and in-stage commits.")
                    lines.append("  -> See the per-stage 'time split' log lines for how parent, workers and commits share it.")
                elif "preload" in low_name:
                    lines.append("  -> TableEngine cache preload (carried hash tables are reused or rebuilt from memory).")
                    lines.append("  -> A long preload means a cold first load or a carried-table rebuild; see the [TECachedDB] lines.")
                elif "file references" in low_name:
                    lines.append("  -> Mostly the include/Kconfig reference INSERT...SELECT for new blobs; Kbuild and docs are a small share.")
                    lines.append("  -> Optimization: database-side (InnoDB buffer pool size, indexes on m_file_reference).")
                elif "header" in low_name and "wave" in low_name:
                    lines.append("  -> Topological sort and cycle-breaking deadlock resolution.")
                    lines.append("  -> Optimization: Increase wave chunking threshold or expand wave batch force-resolving.")
                elif "header" in low_name:
                    lines.append("  -> Stage 1 header AST pre-resolution.")
                    lines.append("  -> Optimization: Worker pre-resolution and compact shared memory table packing.")
                elif "git commit" in low_name:
                    lines.append("  -> Commit DAG traversal, hunk extraction, and tag bridging.")
                    lines.append("  -> Optimization: Preload existing commit hashes in bulk queries and batch insert commit tags.")
                elif "kbuild" in low_name or "make" in low_name:
                    lines.append("  -> Sequential Makefile/Kbuild reading and parsing.")
                    lines.append("  -> Optimization: Parallelize git cat-file reading or batch process directory trees.")
                elif "maintainer" in low_name:
                    lines.append("  -> Path matching against MAINTAINERS sections.")
                    lines.append("  -> Optimization: Use prefix/exact indexing for MAINTAINERS patterns.")
                elif "blob" in low_name or "dedup" in low_name:
                    lines.append("  -> Step 4.5 content-addressable blob hash matching.")
                    lines.append("  -> Optimization: Bulk prefetch known blob hashes into memory (already optimized).")
                elif "db" in low_name or "commit" in low_name:
                    lines.append("  -> TableEngine / MariaDB transaction commits and socket flushing.")
                    lines.append("  -> Optimization: Increase chunk commit interval or optimize multi-row SQL insert chunking.")
                else:
                    lines.append(f"  -> Processed {dur:.2f}s of pipeline runtime.")
            lines.append(border + "\n")

        return "\n".join(lines)

