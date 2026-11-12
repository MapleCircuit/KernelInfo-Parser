"""Unit tests for `parser/man_ast` (roff man-page parser, page facts, repository path helpers)."""
import json
import unittest

from parser.man_ast import man_facts, man_parser, man_paths
from parser.man_ast.man_parser import parse, plain_text, so_target


def _sec(doc, sid):
    for s in doc["sections"]:
        if s["id"] == sid:
            return s
    raise AssertionError(f"no section {sid!r} in {[s['id'] for s in doc['sections']]!r}")


def _doc(body, th='.TH read 2 2024-05-02 "Linux man-pages 6.9"\n'):
    return parse(th + body)


READ_2 = r""".\" Copyright (c) the authors
.\" SPDX-License-Identifier: Linux-man-pages-copyleft
.TH read 2 2024-05-02 "Linux man-pages 6.9"
.SH NAME
read \- read from a file descriptor
.SH LIBRARY
Standard C library
.RI ( libc ,\~ \-lc )
.SH SYNOPSIS
.nf
.B #include <unistd.h>
.P
.BI "ssize_t read(int " fd ", void " buf [ count "], size_t " count );
.fi
.SH DESCRIPTION
.BR read ()
attempts to read up to
.I count
bytes; see
.BR open (2)
and \fBsignal\fP(7).
.SH ERRORS
.TP
.B EAGAIN
.TQ
.B EWOULDBLOCK
Would block.
.TP
.B EBADF
Bad.
.SH SEE ALSO
.BR close (2),
.BR fcntl (2),
.BR readv (3type)
"""


class TestPaths(unittest.TestCase):
    def test_page_of_path_layouts(self):
        self.assertEqual(man_paths.page_of_path("man/man2/read.2"), ("read", "2"))
        self.assertEqual(man_paths.page_of_path("man2/read.2"), ("read", "2"))
        self.assertEqual(man_paths.page_of_path("man/man2const/EPOLL_CTL_ADD.2const"), ("EPOLL_CTL_ADD", "2const"))
        self.assertEqual(man_paths.page_of_path("man/man3type/size_t.3type"), ("size_t", "3type"))
        self.assertEqual(man_paths.page_of_path("man3p/aio_cancel.3p"), ("aio_cancel", "3p"))
        self.assertEqual(man_paths.page_of_path("man8/ld.so.8"), ("ld.so", "8"))
        self.assertEqual(man_paths.page_of_path("man0p/aio.h.0p"), ("aio.h", "0p"))

    def test_page_of_path_rejects(self):
        for p in ("man2", "man/man2", "man2/read.3", "man/man2type/foo.2", "man1/README", "src/read.2",
                  "x/man2/read.2", "", "man/man2/.2"):
            self.assertIsNone(man_paths.page_of_path(p), p)

    def test_candidates_and_so(self):
        self.assertEqual(man_paths.candidate_paths("read", "2"), ["man/man2/read.2", "man2/read.2"])
        self.assertEqual(man_paths.resolve_so("man2/pread.2", "man/man2/pwrite.2"), "man/man2/pread.2")
        self.assertEqual(man_paths.resolve_so("man2/pread.2", "man2/pwrite.2"), "man2/pread.2")
        self.assertEqual(man_paths.resolve_so("man3type/x.3type", "man/man3/y.3"), "man/man3type/x.3type")
        self.assertEqual(man_paths.resolve_so("pread.2", "man/man2/pwrite.2"), "man/man2/pread.2")

    def test_section_order(self):
        order = man_paths.SECTION_ORDER
        self.assertEqual(order[:3], ["2", "2type", "2const"])
        self.assertLess(order.index("3"), order.index("3type"))
        self.assertLess(order.index("3attr"), order.index("1"))
        self.assertLess(man_paths.section_rank("2"), man_paths.section_rank("3"))
        self.assertLess(man_paths.section_rank("3"), man_paths.section_rank("3x"))
        self.assertLess(man_paths.section_rank("3x"), man_paths.section_rank("1"))
        self.assertLess(man_paths.section_rank("0p"), man_paths.section_rank("zz"))


class TestParserStructure(unittest.TestCase):
    def setUp(self):
        man_parser._RAISE = True

    def tearDown(self):
        man_parser._RAISE = False

    def test_read_page(self):
        doc = parse(READ_2)
        self.assertEqual(doc["format"], 1)
        self.assertIsNone(doc["so"])
        self.assertEqual(doc["th"], {"title": "read", "section": "2", "date": "2024-05-02",
                                     "source": "Linux man-pages 6.9", "manual": ""})
        self.assertEqual([s["id"] for s in doc["sections"]],
                         ["name", "library", "synopsis", "description", "errors", "see-also"])
        name = _sec(doc, "name")["blocks"]
        self.assertEqual(name, [{"t": "p", "c": [{"x": "read - read from a file descriptor"}]}])
        lib = _sec(doc, "library")["blocks"][0]["c"]
        self.assertEqual(plain_text(lib), "Standard C library (libc,\u00a0-lc)")
        self.assertIn({"x": "libc", "f": "I"}, lib)
        json.dumps(doc)

    def test_synopsis_pre(self):
        pre = _sec(parse(READ_2), "synopsis")["blocks"][0]
        self.assertEqual(pre["t"], "pre")
        self.assertFalse(pre["ex"])
        self.assertEqual(len(pre["lines"]), 3)
        self.assertEqual(pre["lines"][0], [{"x": "#include <unistd.h>", "f": "B"}])
        self.assertEqual(pre["lines"][1], [])
        self.assertEqual(plain_text(pre["lines"][2]), "ssize_t read(int fd, void buf[count], size_t count);")
        self.assertEqual(pre["lines"][2][1], {"x": "fd", "f": "I"})

    def test_cross_refs(self):
        p = _sec(parse(READ_2), "description")["blocks"][0]["c"]
        self.assertEqual(p[0], {"x": "read", "f": "B"})  # `()` is not a section
        self.assertIn({"x": "open", "f": "B", "ref": ["open", "2"]}, p)
        self.assertIn({"x": "signal", "f": "B", "ref": ["signal", "7"]}, p)
        k = p.index({"x": "open", "f": "B", "ref": ["open", "2"]})
        self.assertTrue(p[k + 1]["x"].startswith("(2)"))
        self.assertNotIn("f", p[k + 1])
        see = _sec(parse(READ_2), "see-also")["blocks"][0]["c"]
        self.assertIn({"x": "readv", "f": "B", "ref": ["readv", "3type"]}, see)

    def test_tp_tq(self):
        blk = _sec(parse(READ_2), "errors")["blocks"]
        self.assertEqual(len(blk), 2)
        tp = blk[0]
        self.assertEqual(tp["t"], "tp")
        self.assertEqual(tp["tags"], [[{"x": "EAGAIN", "f": "B"}], [{"x": "EWOULDBLOCK", "f": "B"}]])
        self.assertEqual(tp["body"], [{"t": "p", "c": [{"x": "Would block."}]}])
        self.assertEqual(blk[1]["tags"], [[{"x": "EBADF", "f": "B"}]])

    def test_tp_tag_continuation_and_ip(self):
        doc = _doc(".SH OPTIONS\n.TP\n.BR \\-C\\~\\c\n.I cache\nUse cache.\n.IP \\(bu 3\nbullet\n"
                   ".IP\nindented\n.IP \"\" 4\nalso indented\n")
        b = _sec(doc, "options")["blocks"]
        self.assertEqual([x["t"] for x in b], ["tp", "tp", "ip", "ip"])
        self.assertEqual(plain_text(b[0]["tags"][0]), "-C\u00a0cache")
        self.assertEqual(b[1]["tags"], [[{"x": "\u2022"}]])
        self.assertEqual(b[1]["body"], [{"t": "p", "c": [{"x": "bullet"}]}])
        self.assertEqual(b[2]["body"][0]["c"], [{"x": "indented"}])

    def test_rs_nesting(self):
        doc = _doc(".SH D\n.TP\n.B X\nouter\n.RS\n.TP\n.B Y\ninner\n.RS 4\ndeep\n.RE\n.RE\nafter\n.RS 0\nzero\n.RE\n")
        b = _sec(doc, "d")["blocks"]
        self.assertEqual([x["t"] for x in b], ["tp", "rs", "p", "p"])
        rs = b[1]
        self.assertEqual(rs["body"][0]["t"], "tp")
        self.assertEqual(rs["body"][1], {"t": "rs", "body": [{"t": "p", "c": [{"x": "deep"}]}]})
        self.assertEqual(b[2]["c"], [{"x": "after"}])
        self.assertEqual(b[3]["c"], [{"x": "zero"}])  # `.RS 0` adds no indent level

    def test_paragraphs_and_breaks(self):
        doc = _doc(".SH D\none\n.br\ntwo\n.P\nthree\n.sp\nfour\n\nfive\n  six\n.PP\n.LP\nseven\n")
        b = _sec(doc, "d")["blocks"]
        self.assertEqual([plain_text(x["c"]) for x in b], ["one\ntwo", "three", "four", "five\n  six", "seven"])
        self.assertEqual(b[0]["c"][1], {"br": 1})

    def test_example_block(self):
        doc = _doc(".SH EXAMPLES\n.EX\n#include <stdio.h>\n\\&\nint main(void) {\n    \\fBputs\\fP(\"a\\e\");\n}\n.EE\n")
        pre = _sec(doc, "examples")["blocks"][0]
        self.assertTrue(pre["ex"])
        self.assertEqual([plain_text(x) for x in pre["lines"]],
                         ["#include <stdio.h>", "", "int main(void) {", '    puts("a\\");', "}"])
        self.assertEqual(pre["lines"][3][1], {"x": "puts", "f": "B"})

    def test_subsections_and_ids(self):
        doc = _doc(".SH DESCRIPTION\n.SS Errors\ntext\n.SS \"C library/kernel differences\"\nx\n"
                   ".SH ERRORS\ny\n.SH ERRORS\nz\n")
        ids = [s["id"] for s in doc["sections"]]
        self.assertEqual(ids, ["description", "errors", "errors-2"])
        ss = [b for b in _sec(doc, "description")["blocks"] if b["t"] == "ss"]
        self.assertEqual(ss[0], {"t": "ss", "id": "description-errors", "title": "Errors"})
        self.assertEqual(ss[1]["id"], "c-library-kernel-differences")

    def test_leading_section(self):
        doc = parse("Some text before\n.SH NAME\nx \\- y\n")
        self.assertEqual(doc["sections"][0]["id"], "")
        self.assertEqual(plain_text(doc["sections"][0]["blocks"][0]["c"]), "Some text before")
        doc = parse(".\\\" only a comment\n.TH X 1\n.SH NAME\nx \\- y\n")
        self.assertEqual(doc["sections"][0]["id"], "name")

    def test_sh_title_next_line(self):
        doc = parse(".SH\nRETURN VALUE\nZero.\n")
        self.assertEqual(doc["sections"][0]["title"], "RETURN VALUE")
        self.assertEqual(doc["sections"][0]["id"], "return-value")

    def test_links(self):
        doc = _doc(".SH D\nsee\n.UR https://example.org/x\n.I the site\n.UE ,\nor\n.UR https://e.org\n.UE .\n"
                   ".MT a@b.c\nAuthor\n.ME\n")
        c = _sec(doc, "d")["blocks"][0]["c"]
        self.assertIn({"x": "the site", "f": "I", "url": "https://example.org/x"}, c)
        self.assertEqual(plain_text(c), "see the site, or https://e.org. Author")
        self.assertIn({"x": "https://e.org", "url": "https://e.org"}, c)
        self.assertIn({"x": "Author", "url": "mailto:a@b.c"}, c)
        self.assertEqual(c[0], {"x": "see "})  # the separator before a link is not part of it

    def test_synopsis_macros(self):
        doc = _doc(".SH SYNOPSIS\n.SY ldconfig\n.OP \\-f conf\n.RB [ \\-v ]\n.YS\n.SY ldconfig\n.B \\-p\n.YS\n"
                   ".SH OTHER\n.EX\n.SY #!\\f[I]interp\\f[]\n.RI [ arg ]\n.YS\n.EE\n")
        b = _sec(doc, "synopsis")["blocks"]
        self.assertEqual([plain_text(x["c"]) for x in b], ["ldconfig [-f conf] [-v]", "ldconfig -p"])
        self.assertEqual(b[0]["c"][0], {"x": "ldconfig", "f": "B"})
        pre = _sec(doc, "other")["blocks"][0]
        self.assertEqual([plain_text(x) for x in pre["lines"]], ["#!interp [arg]"])

    def test_mr_macro(self):
        c = _sec(_doc(".SH D\n.MR open 2 ,\nnext\n"), "d")["blocks"][0]["c"]
        self.assertEqual(c[0], {"x": "open", "f": "B", "ref": ["open", "2"]})
        self.assertEqual(plain_text(c), "open(2), next")


class TestInline(unittest.TestCase):
    def runs(self, text):
        doc = _doc(".SH D\n" + text + "\n")
        return _sec(doc, "D".lower())["blocks"][0]["c"]

    def test_escapes(self):
        txt = plain_text(self.runs(r"a\-b \e \(em\[en] \(lq\(rq\(aq \[u00E9]\(:a \*(lqq\*(rq \*R \*(Tm \(co"
                                   r" x\&y \%z w\~v \(<=\(>=\(!= \(mu\(di \(bu \[hy]\(dg\(sc \(*a"))
        self.assertEqual(txt, "a-b \\ \u2014\u2013 \u201c\u201d' \u00e9\u00e4 \u201cq\u201d \u00ae \u2122 \u00a9"
                              " xy z w\u00a0v \u2264\u2265\u2260 \u00d7\u00f7 \u2022 -\u2020\u00a7 \u03b1")

    def test_fonts(self):
        r = self.runs(r"\fBb\fIi\fPb2\fRr \f[B]x\f[] \f(CWcw\fR \f[CR]cr\f[R] \f[BI]bi\fP \f3three\f1")
        self.assertEqual(r, [{"x": "b", "f": "B"}, {"x": "i", "f": "I"}, {"x": "b2", "f": "B"}, {"x": "r "},
                             {"x": "x", "f": "B"}, {"x": " "}, {"x": "cw", "f": "CR"}, {"x": " "},
                             {"x": "cr", "f": "CR"}, {"x": " "}, {"x": "bi", "f": "BI"}, {"x": " "},
                             {"x": "three", "f": "B"}])

    def test_size_and_misc_stripped(self):
        self.assertEqual(plain_text(self.runs(r"\s-1SMALL\s0 \s+(12x\s[0] \m[red]c\m[] \h'1n'h \w'abc'w")),
                         "SMALL x c h w")

    def test_alternating_macros(self):
        doc = _doc(".SH D\n.BR read (2),\n.IR file .\n.RB [ \\-v ]\n.BI \"int \" fd ;\n.IB a b\n.RI x y\n")
        c = _sec(doc, "d")["blocks"][0]["c"]
        self.assertEqual(plain_text(c), "read(2), file. [-v] int fd; ab xy")
        self.assertEqual(c[0], {"x": "read", "f": "B", "ref": ["read", "2"]})

    def test_font_macro_next_line(self):
        c = _sec(_doc(".SH D\n.B\nbold line\nplain\n"), "d")["blocks"][0]["c"]
        self.assertEqual(c, [{"x": "bold line", "f": "B"}, {"x": " plain"}])

    def test_continuation(self):
        c = _sec(_doc(".SH D\n.B foo\\c\nbar\n.I x\\c\n.R y\n"), "d")["blocks"][0]["c"]
        self.assertEqual(plain_text(c), "foobar x")

    def test_line_continuation_and_comments(self):
        c = _sec(_doc('.SH D\nab\\\ncd \\" comment\n.\\" full comment\n\\" text comment\nef\n'), "d")["blocks"]
        self.assertEqual(len(c), 1)
        self.assertEqual(plain_text(c[0]["c"]), "abcd ef")


class TestRoffLayer(unittest.TestCase):
    def setUp(self):
        man_parser._RAISE = True

    def tearDown(self):
        man_parser._RAISE = False

    def test_conditions(self):
        doc = _doc(".SH D\n.ie n nroff\n.el troff\n.if t \\{\\\ntypeset\n.\\}\n.if n \\{\\\nterm\n.\\}\n"
                   ".if \\n(.g gnu\n.if !\\n(.g notgnu\n.ie '\\(lq'' x\n.el y\n.if (1+1)>1 math\n")
        self.assertEqual(plain_text(_sec(doc, "d")["blocks"][0]["c"]), "nroff term gnu y math")

    def test_strings_and_macros(self):
        doc = _doc('.ds Q " quoted\n.de q\n\\\\$3\\*(lq\\\\$1\\*(rq\\\\$2\n..\n.de INDENT\n. RS \\\\$1\n..\n'
                   '.de UNINDENT\n. RE\n..\n.SH D\nx\\*Q\n.q word , (\n.INDENT 3.5\ninside\n.UNINDENT\nafter\n')
        b = _sec(doc, "d")["blocks"]
        self.assertEqual(plain_text(b[0]["c"]), "x quoted (\u201cword\u201d,")
        self.assertEqual(b[1], {"t": "rs", "body": [{"t": "p", "c": [{"x": "inside"}]}]})
        self.assertEqual(b[2]["c"], [{"x": "after"}])

    def test_ignore_block_and_unknown_requests(self):
        doc = _doc(".SH D\n.ig\nhidden\n..\n.in +4n\n.ad l\n.nh\n.PD 0\n.zz what\nshown\n")
        self.assertEqual(_sec(doc, "d")["blocks"], [{"t": "p", "c": [{"x": "shown"}]}])

    def test_recursive_macro_is_bounded(self):
        doc = parse(".de x\n.x\n..\n.SH D\n.x\nend\n")
        self.assertEqual(plain_text(_sec(doc, "d")["blocks"][0]["c"]), "end")


class TestTbl(unittest.TestCase):
    def test_table(self):
        doc = _doc(".SH D\n.TS\nallbox tab(:);\nlB c r\nl c r.\nName:Value:Notes\n_\n\\fBA\\fP:1:T{\n"
                   "long text\n.br\nsecond\nT}\nB:2:see \\fBread\\fP(2)\n=\nC:\\_:\n.TE\nafter\n")
        b = _sec(doc, "d")["blocks"]
        t = b[0]
        self.assertEqual(t["t"], "table")
        self.assertTrue(t["box"])
        self.assertEqual(t["align"], ["l", "c", "r"])
        self.assertEqual(t["header"], 1)
        self.assertEqual(len(t["rows"]), 4)
        self.assertEqual(t["rows"][0][0], [{"x": "Name", "f": "B"}])
        self.assertEqual(t["rows"][1][0], [{"x": "A", "f": "B"}])
        self.assertEqual(t["rows"][1][2], [{"x": "long text"}, {"br": 1}, {"x": "second"}])
        self.assertIn({"x": "read", "f": "B", "ref": ["read", "2"]}, t["rows"][2][2])
        self.assertEqual(t["rows"][3], [[{"x": "C"}], [], []])
        self.assertEqual(b[1], {"t": "p", "c": [{"x": "after"}]})

    def test_table_defaults(self):
        t = _sec(_doc(".SH D\n.TS\nl l.\na\tb\nc\td\n.TE\n"), "d")["blocks"][0]
        self.assertEqual(t, {"t": "table", "align": ["l", "l"], "box": False, "header": 0,
                             "rows": [[[{"x": "a"}], [{"x": "b"}]], [[{"x": "c"}], [{"x": "d"}]]]})

    def test_spanned_title_row(self):
        t = _sec(_doc(".SH D\n.TS\nbox;\nc s\nl l.\nTitle\nx\ty\n.TE\n"), "d")["blocks"][0]
        self.assertEqual(t["header"], 1)
        self.assertTrue(t["box"])


class TestSoAndMdoc(unittest.TestCase):
    def test_so_alias(self):
        text = ".\\\" Copyright\n.so man2/pread.2\n"
        self.assertEqual(so_target(text), "man2/pread.2")
        doc = parse(text)
        self.assertEqual(doc["so"], "man2/pread.2")
        self.assertEqual(doc["sections"], [])
        self.assertIsNone(so_target(READ_2))
        self.assertIsNone(so_target(".so a.2\n.SH NAME\n"))

    def test_mdoc_subset(self):
        text = (".Dd March 6, 1999\n.Dt ERR 3\n.Os\n.Sh NAME\n.Nm err ,\n.Nm warn\n.Nd formatted messages\n"
                ".Sh SYNOPSIS\n.In err.h\n.Ft void\n.Fn err \"int eval\" \"const char *fmt\" \"...\"\n"
                ".Sh DESCRIPTION\nUse\n.Fl v\nand\n.Ar file .\n.Bl -bullet\n.It\none\n.El\n"
                ".Sh SEE ALSO\n.Xr printf 3 ,\n.Xr exit 3\n")
        doc = parse(text)
        self.assertEqual(doc["th"]["title"], "ERR")
        self.assertEqual(doc["th"]["section"], "3")
        self.assertEqual(plain_text(_sec(doc, "name")["blocks"][0]["c"]), "err, warn - formatted messages")
        syn = [plain_text(b["c"]) for b in _sec(doc, "synopsis")["blocks"]]
        self.assertEqual(syn, ["#include <err.h>", "void err(int eval, const char *fmt, ...);"])
        desc = _sec(doc, "description")["blocks"]
        self.assertEqual(plain_text(desc[0]["c"]), "Use -v and file.")
        self.assertEqual(desc[1]["tags"], [[{"x": "\u2022"}]])
        see = _sec(doc, "see-also")["blocks"][0]["c"]
        self.assertEqual(see[0], {"x": "printf", "f": "B", "ref": ["printf", "3"]})
        facts = man_facts.extract(text)
        self.assertEqual(facts["n"], ["err", "warn"])
        self.assertEqual(facts["fn"], ["err"])
        self.assertEqual(facts["hd"], ["err.h"])


class TestRobustness(unittest.TestCase):
    def test_never_raises(self):
        junk = ["", "\\", ".", ".TS\n", ".TS\nl.\nT{\n", ".if \\{", ".de", ".ie", ".el", ".RE\n.RE\n",
                "\\f", "\\(", "\\[", "\\*[", "\\s", "\\h'", ".TP", ".TP\n.TP\n.TQ\n", ".UE\n.ME\n",
                ".SY\n.YS\n.OP\n", ".BR\n.MR\n", "\\c\n\\c", ".if '\\(", "\x00\x01\x02", "\\N'abc'",
                ".nr x 1\n\\nx \\n[foo]", ".TH\n.SH\n.SS\n", ".Bl\n.It\n.El\n.Ed\n.Fn\n.Xr\n"]
        for t in junk:
            doc = parse(t)
            json.dumps(doc)
            man_facts.extract(t)
        self.assertEqual(parse(None)["sections"], [])  # type: ignore[arg-type]

    def test_internal_error_fallback(self):
        orig = man_parser._Parser.run

        def boom(self):
            raise RuntimeError("x")

        man_parser._Parser.run = boom
        try:
            doc = parse("line one\nline two\n")
        finally:
            man_parser._Parser.run = orig
        pre = doc["sections"][0]["blocks"][0]
        self.assertEqual(pre["t"], "pre")
        self.assertEqual(pre["lines"][0], [{"x": "line one"}])


class TestFacts(unittest.TestCase):
    def test_constants_and_applies(self):
        self.assertEqual(man_facts.FAMILY, 6)
        self.assertEqual(man_facts.FORMAT, 1)
        self.assertTrue(man_facts.applies("man/man2/read.2"))
        self.assertTrue(man_facts.applies("man3/printf.3"))
        self.assertFalse(man_facts.applies("README"))
        self.assertFalse(man_facts.applies("man2/read.2.orig"))

    def test_extract_read(self):
        f = man_facts.extract(READ_2)
        self.assertEqual(f["n"], ["read"])
        self.assertEqual(f["s"], "read from a file descriptor")
        self.assertEqual(f["th"], ["read", "2", "2024-05-02", "Linux man-pages 6.9", ""])
        self.assertEqual(f["so"], "")
        self.assertEqual(f["x"], [["close", "2"], ["fcntl", "2"], ["readv", "3type"], ["open", "2"],
                                  ["signal", "7"]])
        self.assertEqual(f["fn"], ["read"])
        self.assertEqual(f["hd"], ["unistd.h"])
        self.assertEqual(f["id"], ["ssize_t", "size_t"])

    def test_extract_multi_name_and_synopsis_forms(self):
        text = (".TH F_GETFD 2const\n.SH NAME\nF_GETFD,\nF_SETFD\n\\-\nget/set flags\n.SH SYNOPSIS\n.nf\n"
                ".B #include <fcntl.h>\n.B #include <sys/epoll.h>\n.P\n"
                '.BI "int fcntl(int " fd ", F_GETFD);"\n'
                '.BI "int epoll_ctl(int " epfd ", struct epoll_event *_Nullable " event );\n'
                '.B "typedef typeof(void (int))  *sighandler_t;"\n'
                '.BI "void (*signal(int " sig ", void (*" func ")(int)))(int);"\n'
                ".B struct timespec {\n.B \"    time_t tv_sec;\"\n.B };\n"
                ".B #define EPOLL_CTL_ADD 1  /* see foo(2) */\n"
                ".fi\n.P\nFeature Test Macro Requirements for glibc (see\n.BR feature_test_macros (7)):\n"
                ".P\n.BR fcntl ():\n_GNU_SOURCE\n")
        f = man_facts.extract(text)
        self.assertEqual(f["n"], ["F_GETFD", "F_SETFD"])
        self.assertEqual(f["s"], "get/set flags")
        self.assertEqual(f["hd"], ["fcntl.h", "sys/epoll.h"])
        self.assertEqual(f["fn"], ["fcntl", "epoll_ctl", "signal"])
        for ident in ("F_GETFD", "epoll_event", "sighandler_t", "timespec", "time_t", "EPOLL_CTL_ADD"):
            self.assertIn(ident, f["id"])
        for not_ident in ("int", "_Nullable", "typeof", "fd", "_GNU_SOURCE", "fcntl"):
            self.assertNotIn(not_ident, f["id"])
        self.assertIn(["feature_test_macros", "7"], f["x"])

    def test_extract_usage_and_prose(self):
        text = (".TH socket 7\n.SH NAME\nsocket \\- sockets\n.SH SYNOPSIS\n.nf\n"
                ".B #include <sys/socket.h>\n.P\n.IB sockfd \" = socket(int \" family \");\"\n.fi\n"
                ".P\n.IR Note :\nglibc provides no wrapper for gettid(), use\n.BR syscall (2).\n")
        f = man_facts.extract(text)
        self.assertEqual(f["fn"], ["socket"])
        self.assertNotIn("gettid", f["fn"])

    def test_command_pages_have_no_c_facts(self):
        f = man_facts.extract(".TH ls 1\n.SH NAME\nls \\- list\n.SH SYNOPSIS\n.B ls\n[\\fIFILE\\fP]\n")
        self.assertEqual((f["fn"], f["id"]), ([], []))
        self.assertEqual(f["n"], ["ls"])

    def test_extract_so_and_empty(self):
        f = man_facts.extract(".so man2/pread.2\n")
        self.assertEqual(f["so"], "man2/pread.2")
        self.assertEqual(f["n"], [])
        self.assertIsNone(man_facts.extract(""))
        self.assertIsNone(man_facts.extract("  \n"))

    def test_see_also_plain_refs(self):
        f = man_facts.extract(".TH x 3\n.SH NAME\nx \\- y\n.SH \"SEE ALSO\"\nfoo(3), bar(2)\n")
        self.assertEqual(f["x"], [["foo", "3"], ["bar", "2"]])

    def test_encode_decode(self):
        f = man_facts.extract(READ_2)
        blob = man_facts.encode(f)
        self.assertIsInstance(blob, bytes)
        self.assertEqual(man_facts.decode(blob), f)
        self.assertEqual(man_facts.decode(memoryview(blob)), f)
        self.assertEqual(man_facts.encode(None), b"")
        self.assertIsNone(man_facts.decode(None))
        self.assertIsNone(man_facts.decode(b""))
        self.assertIsNone(man_facts.decode(b"not zlib"))


if __name__ == "__main__":
    unittest.main()
