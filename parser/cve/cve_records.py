"""parser/cve/cve_records.py - CVE JSON 5 records about the Linux kernel (pure functions, no I/O).

Shared by the parser, which ingests a CVE list into `m_cve` / `m_cve_commit` (`parser/cve/cve_ingest.py`,
`core/API.md` section 9), and the webapp, which evaluates the stored records against releases
(`webapp/backend/services/cve_service.py`, `webapp/API.md` section 4.11). Three kinds of kernel record:

- `cna`: issued by the kernel CNA (2024 on, plus older ids it took over): git ranges (introducing commit,
  `lessThan` fixing commit, per branch) and version entries per affected product, each with its own
  `defaultStatus`;
- `commit`: another CNA's record whose references link a kernel commit (github.com/torvalds/linux,
  git.kernel.org trees): those commits are fixes, the introduction is unknown;
- `text`: neither, but about the kernel (product or description): versions from the product entries or from
  the description ("before 4.8.3", "through 6.0.6"), shown as *possibly affected* only.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Bumped whenever parse_record() changes what it keeps: the parser then ingests the whole list again
RECORD_FORMAT = 3
CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$")
KERNEL_CNA_ORG = "416baaa9-dc9f-4396-8d5f-8c081fb06d67"
# The initial import of Linux history (v2.6.12-rc2): "introduced by" it means "since the beginning"
ROOT_COMMIT = "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2"
KINDS = {"cna": 1, "commit": 2, "text": 3}
KIND_NAMES = {v: k for k, v in KINDS.items()}
ROLE_FIX = 1
ROLE_INTRODUCED = 2
# One `git grep -l` over the CVE list keeps the records that may concern the kernel (case-insensitive)
GREP_MARKERS = ("linux kernel", "torvalds/linux", "git.kernel.org", '"assignerShortName": "Linux"')
_FULL = re.compile(r"^[0-9a-f]{40}$")
HEXSHA = re.compile(r"^[0-9a-f]{12,40}$")
# A commit of a kernel tree: Linus' tree on GitHub, git.kernel.org trees under linux/kernel/git/ (cgit, gitweb),
# the git.kernel.org/linus/ and /stable/c/ shortcuts. Other git.kernel.org projects (e2fsprogs...) do not match.
KERNEL_COMMIT_URL = re.compile(
    r"^https?://(?:www\.)?(?:"
    r"github\.com/torvalds/linux/commit/"
    r"|git\.kernel\.org/(?:linus/|stable/c/"
    r"|(?:pub/scm/|cgit/)?linux/kernel/git/[^?#\s]+?\.git/(?:commit|patch)/?(?:\?[^#\s]*?\bid=)?"
    r"|\?p=linux/kernel/git/[^;\s]+?\.git;a=commit(?:diff)?;h=)"
    r"|kernel\.org/linus/)"
    r"([0-9a-fA-F]{12,40})(?![0-9a-fA-F])",
    re.IGNORECASE,
)
_KERNEL_PRODUCT = re.compile(r"^(?:the )?(?:linux(?:[ _-]?kernel)?|kernel)$", re.IGNORECASE)
_DESC_KERNEL = re.compile(r"\blinux\b[^.]{0,40}\bkernel|\bkernel\b[^.]{0,20}\blinux\b|\blinux kernel", re.IGNORECASE)
_DESC_BEFORE = re.compile(r"\b(before|prior to|through)\s+(?:version\s+|v)?(\d+\.\d+(?:\.\d+){0,2})\b", re.IGNORECASE)
_SEVERITY_KEYS = ("cvssV4_0", "cvssV3_1", "cvssV3_0", "cvssV2_0")


# ------------------------------------------------------------------------------------- parsing

def kernel_commits(urls: Iterable[str]) -> list[str]:
    """Kernel commit ids (12-40 hex digits, lower case, first seen first) linked by reference URLs."""
    out: list[str] = []
    for u in urls:
        m = KERNEL_COMMIT_URL.match(str(u or "").strip())
        if m:
            sha = m.group(1).lower()
            if sha not in out:
                out.append(sha)
    return out


def _date(value: Any) -> str:
    return str(value or "")[:10]


def _metrics(containers: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """(cvss, KEV date, SSVC exploitation) from the CNA container, then the ADP ones (CISA's vulnrichment)."""
    cvss = None
    kev = exploitation = None
    for c in containers:
        for m in c.get("metrics") or []:
            if not isinstance(m, dict):
                continue
            if cvss is None:
                for key in _SEVERITY_KEYS:
                    v = m.get(key)
                    if isinstance(v, dict) and v.get("baseScore") is not None:
                        try:
                            score = float(v.get("baseScore"))
                        except (TypeError, ValueError):
                            continue
                        cvss = {"score": score, "severity": str(v.get("baseSeverity") or "").upper()[:16],
                                "vector": str(v.get("vectorString") or "")[:200], "version": key[4:].replace("_", "."),
                                "source": str((c.get("providerMetadata") or {}).get("shortName") or "")[:40]}
                        break
            other = m.get("other") if isinstance(m.get("other"), dict) else None
            if other and other.get("type") == "kev":
                kev = _date((other.get("content") or {}).get("dateAdded")) or "listed"
            elif other and other.get("type") == "ssvc":
                for opt in (other.get("content") or {}).get("options") or []:
                    if isinstance(opt, dict) and "Exploitation" in opt:
                        exploitation = str(opt["Exploitation"])[:20]
    return cvss, kev, exploitation


_VERSION_TEXT = re.compile(r"v?\d+(?:\.\d+)*")
_BOUND_TEXT = re.compile(r"\*|v?\d+(?:\.\d+)*(?:\.\*)?")


def _version_entry(v: dict[str, Any]) -> dict[str, Any] | None:
    """A non-git version item when its bounds read as kernel versions (`0`, `2.6.32`, `6.1.43`, `6.1.*`, `*`)."""
    out: dict[str, Any] = {k: str(v[k]).strip()[:40] for k in ("version", "lessThan", "lessThanOrEqual", "status") if v.get(k) is not None}
    if out.get("status") not in ("affected", "unaffected") or not _VERSION_TEXT.fullmatch(out.get("version", "")):
        return None
    for bound in ("lessThan", "lessThanOrEqual"):
        if bound in out and not _BOUND_TEXT.fullmatch(out[bound]):
            return None
    # Pre-compute version tuples to avoid 200,000+ runtime regex evaluations in version_status()
    out["version_t"] = _ver_tuple(out["version"])
    if "lessThan" in out:
        out["lessThan_t"] = _ver_tuple(out["lessThan"])
    if "lessThanOrEqual" in out:
        pat = out["lessThanOrEqual"]
        if pat == "*":
            out["lessThanOrEqual_star"] = True
        elif pat.endswith(".*"):
            out["lessThanOrEqual_star_prefix"] = _ver_tuple(pat[:-2])
        else:
            out["lessThanOrEqual_t"] = _ver_tuple(pat)
    return out


def parse_record(data: dict[str, Any]) -> dict[str, Any] | None:
    """A CVE JSON 5 record -> the kernel record kept in the index, or None (not published, not about the kernel).

    {id, kind: cna|commit|text, title, description, published, updated, ranges: [{introduced, fixed, files}],
     entries: [{default, versions: [{version, lessThan?, lessThanOrEqual?, status}]}], files, references,
     cvss, kev, exploitation}"""
    meta = data.get("cveMetadata") or {}
    cve_id = str(meta.get("cveId") or "")
    if not CVE_ID.match(cve_id) or str(meta.get("state") or "PUBLISHED").upper() != "PUBLISHED":
        return None
    containers = data.get("containers") or {}
    cna = containers.get("cna") or {}
    adps = [a for a in (containers.get("adp") or []) if isinstance(a, dict)]
    is_cna = meta.get("assignerShortName") == "Linux" or meta.get("assignerOrgId") == KERNEL_CNA_ORG
    desc = ""
    for d in cna.get("descriptions") or []:
        if str(d.get("lang") or "en").startswith("en"):
            desc = str(d.get("value") or "")
            break
    urls = [str(r.get("url") or "") for c in [cna, *adps] for r in (c.get("references") or []) if isinstance(r, dict)]
    references = list(dict.fromkeys(u for u in urls if re.match(r"^https?://", u)))[:24]
    ranges: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    files: list[str] = []
    kernel_product = False
    for aff in cna.get("affected") or []:
        if not isinstance(aff, dict):
            continue
        product_ok = is_cna or bool(_KERNEL_PRODUCT.match(str(aff.get("product") or "").strip()))
        kernel_product = kernel_product or (product_ok and not is_cna)
        if not product_ok:
            continue
        aff_files = [str(f)[:200] for f in (aff.get("programFiles") or [])][:40]
        files.extend(f for f in aff_files if f not in files)
        versions = []
        for v in aff.get("versions") or []:
            if not isinstance(v, dict):
                continue
            if v.get("versionType") == "git":
                if not is_cna:
                    continue
                intro = str(v.get("version") or "").lower()
                fix = str(v.get("lessThan") or v.get("lessThanOrEqual") or "").lower()
                is_root = intro == ROOT_COMMIT or ROOT_COMMIT.startswith(intro) or intro.startswith("1da177e4c3f4")
                if v.get("status", "affected") == "affected" and (HEXSHA.match(fix) or HEXSHA.match(intro)):
                    ranges.append({"introduced": intro if HEXSHA.match(intro) and not is_root else None,
                                   "fixed": fix if HEXSHA.match(fix) else None, "files": aff_files[:20]})
                continue
            item = _version_entry(v)
            if item:
                versions.append(item)
        if versions or aff.get("defaultStatus") in ("affected", "unaffected"):
            entries.append({"default": aff.get("defaultStatus") if aff.get("defaultStatus") in ("affected", "unaffected") else None,
                            "versions": versions[:200]})
    if is_cna:
        kind = "cna"
    else:
        commits = kernel_commits(urls)
        if commits:
            kind = "commit"
            ranges = [{"introduced": None, "fixed": sha, "files": []} for sha in commits[:20]]
        elif kernel_product or _DESC_KERNEL.search(desc):
            kind = "text"
        else:
            return None
        if kind == "text" or not entries:
            entries = [e for e in entries if e["versions"]]
            bound = description_bound(desc)
            if bound and not entries:
                entries = [{"default": None, "source": "description", "versions": [bound]}]
    cvss, kev, exploitation = _metrics([cna, *adps])

    cwes: list[str] = []
    for c in [cna, *adps]:
        for pt in c.get("problemTypes") or []:
            if isinstance(pt, dict):
                for d in pt.get("descriptions") or []:
                    if isinstance(d, dict):
                        cwe_id = str(d.get("cweId") or "").strip().upper()
                        if cwe_id and cwe_id.startswith("CWE-") and cwe_id not in cwes:
                            cwes.append(cwe_id)
                        elif not cwe_id:
                            desc_text = str(d.get("description") or "").strip()
                            m_cwe = re.search(r"\b(CWE-\d+)\b", desc_text, re.IGNORECASE)
                            if m_cwe:
                                found = m_cwe.group(1).upper()
                                if found not in cwes:
                                    cwes.append(found)

    return {"id": cve_id, "kind": kind, "title": str(cna.get("title") or "")[:300], "description": desc[:3000],
            "published": _date(meta.get("datePublished")), "updated": _date(meta.get("dateUpdated")),
            "ranges": ranges, "entries": entries, "files": files[:40], "references": references,
            "cvss": cvss, "kev": kev, "exploitation": exploitation, "cwes": cwes[:10]}


def description_bound(desc: str) -> dict[str, str] | None:
    """The affected range a description states ("Linux kernel before 4.8.3", "through 6.0.6"): the highest bound."""
    if not _DESC_KERNEL.search(desc or ""):
        return None
    best: tuple[tuple[int, ...], str, str] | None = None
    for word, ver in _DESC_BEFORE.findall(desc):
        t = _ver_tuple(ver)
        if t is None or t[0] < 1 or t[0] > 20:
            continue
        key = "lessThanOrEqual" if word.lower() == "through" else "lessThan"
        if best is None or t > best[0]:
            best = (t, key, ver)
    if best is None:
        return None
    return {"version": "0", best[1]: best[2], "status": "affected"}


# ------------------------------------------------------------------------------------- versions

def _ver_tuple(v: str) -> tuple[int, ...] | None:
    m = re.match(r"^v?(\d+(?:\.\d+)*)", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _as_tuple(v: Any) -> tuple[int, ...] | None:
    if v is None:
        return None
    if isinstance(v, tuple):
        return v
    if isinstance(v, list):
        return tuple(v)
    return _ver_tuple(v)


def _matches(item: dict[str, Any], t: tuple[int, ...]) -> bool:
    lo = _as_tuple(item.get("version_t"))
    if lo is None:
        lo = _ver_tuple(item.get("version", ""))
        if lo is None:
            return False
    if "lessThan" in item:
        hi = _as_tuple(item.get("lessThan_t")) or _ver_tuple(item.get("lessThan", ""))
        return hi is not None and lo <= t < hi
    if "lessThanOrEqual" in item:
        pat = item.get("lessThanOrEqual", "")
        if item.get("lessThanOrEqual_star") or pat == "*":
            return lo <= t
        if "lessThanOrEqual_star_prefix" in item or pat.endswith(".*"):
            prefix = _as_tuple(item.get("lessThanOrEqual_star_prefix")) or _ver_tuple(pat[:-2])
            return prefix is not None and lo <= t and t[:len(prefix)] == prefix
        hi = _as_tuple(item.get("lessThanOrEqual_t")) or _ver_tuple(pat)
        return hi is not None and lo <= t <= hi
    return t == lo


def version_status(entries: list[dict[str, Any]], version: str) -> str | None:
    """"affected" | "fixed" | "unaffected" for a release version (`3.0`, `5.4.270`), or None when silent.

    Each affected entry answers on its own (an explicit range, else its defaultStatus); the version is affected
    when one entry says so. Unaffected becomes "fixed" when the bug reached this line before: an affected item
    starting at or below the version (in the same stable series for a stable item, e.g. 5.4.262 <= 5.4.270)."""
    t = _ver_tuple(version)
    if t is None:
        return None
    said: list[str] = []
    for e in entries:
        aff = unaff = False
        for item in e.get("versions") or []:
            if _matches(item, t):
                if item.get("status") == "affected":
                    aff = True
                elif item.get("status") == "unaffected":
                    unaff = True
        st = "affected" if aff else "unaffected" if unaff else e.get("default")
        if st:
            said.append(st)
    if "affected" in said:
        return "affected"
    if "unaffected" not in said:
        return None
    for e in entries:
        for item in e.get("versions") or []:
            if item.get("status") != "affected":
                continue
            lo = _as_tuple(item.get("version_t")) or _ver_tuple(item.get("version", ""))
            if not lo or lo == (0,) or lo > t:
                continue
            if len(lo) >= 3 and lo[:2] != (2, 6) and lo[:2] != t[:2]:
                continue  # another stable series
            if lo[:2] == (2, 6) and len(lo) >= 4 and lo[:3] != t[:3]:
                continue
            return "fixed"
    return "unaffected"


def may_be_kernel(body: bytes) -> bool:
    """The `git grep` markers, for records an incremental update reads without grep."""
    low = body.lower()
    return any(m.lower().encode() in low for m in GREP_MARKERS)


def record_commits(rec: dict[str, Any]) -> list[tuple[str, int]]:
    """(commit hex, ROLE_FIX | ROLE_INTRODUCED) of a parsed record, each once."""
    out: list[tuple[str, int]] = []
    for r in rec.get("ranges") or ():
        for key, role in (("fixed", ROLE_FIX), ("introduced", ROLE_INTRODUCED)):
            sha = str(r.get(key) or "").lower()
            if HEXSHA.match(sha) and (sha, role) not in out:
                out.append((sha, role))
    return out
