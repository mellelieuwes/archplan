"""A small reader for Elixir and Ruby call arguments, enough for schema
macros: `field(:status, Ecto.Enum, values: [:a, :b], default: :a)` or
`t.string "name", null: false`.

Values are plain Python: an atom or symbol is ("atom", name), an alias is
("alias", "Foo.Bar"), a string is str, a number is int or float, a list is
list, a keyword list is dict, a tuple is tuple, a call is ("call", name,
args, kwargs), a module attribute is ("attr", name), anything else is
("expr", text)."""

from __future__ import annotations

import re

# A string, heredoc, sigil or quoted atom without its closing delimiter runs
# to the end of the text (the \Z alternatives). Without that, the regex
# would scan to the end again from every opening quote: quadratic on hostile
# input such as a long run of "\. A lone backslash at the very end closes
# too (the \\?\Z), for the same reason.

TOKEN = re.compile(r"""
    (?P<space>[ \t\r\n]+|\#[^\n]*)
  | (?P<sigil>~[a-zA-Z](?:\((?:[^)\\]|\\.)*(?:\)|\\?\Z)|\[(?:[^\]\\]|\\.)*(?:\]|\\?\Z)|\{(?:[^}\\]|\\.)*(?:\}|\\?\Z)|/(?:[^/\\]|\\.)*(?:/|\\?\Z)|\|(?:[^|\\]|\\.)*(?:\||\\?\Z)|"(?:[^"\\]|\\.)*(?:"|\\?\Z)|'(?:[^'\\]|\\.)*(?:'|\\?\Z)|<(?:[^>\\]|\\.)*(?:>|\\?\Z))[a-zA-Z]*)
  | (?P<heredoc>\"\"\"[\s\S]*?(?:\"\"\"|\\?\Z)|\'\'\'[\s\S]*?(?:\'\'\'|\\?\Z))
  | (?P<str>"(?:[^"\\]|\\.)*(?:"|\\?\Z)|'(?:[^'\\]|\\.)*(?:'|\\?\Z))
  | (?P<kwq>"(?:[^"\\]|\\.)*":(?=\s))
  | (?P<atom>:"(?:[^"\\]|\\.)*(?:"|\\?\Z)|:[A-Za-z_][\w]*[?!]?)
  | (?P<kw>[A-Za-z_][\w]*[?!]?:(?=[\s]))
  | (?P<alias>(?:[A-Z]\w*|__MODULE__)(?:\.[A-Z]\w*)*)
  | (?P<num>-?\d[\d_]*(?:\.\d+)?)
  | (?P<attr>@[a-z_]\w*)
  | (?P<ident>[a-z_][\w]*[?!]?)
  | (?P<op>=>|->|::|\|>|<>|\.\.|%\{|[()\[\]{},.=|%&*+\-/<>!\\^]|\S)
""", re.X | re.S)


def tokens(text: str) -> list:
    out = []
    for m in TOKEN.finditer(text):
        k = m.lastgroup
        if k != "space":
            out.append((k, m.group(0)))
    return out


def string_value(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s[1:-1])


def sigil_words(s: str) -> list | None:
    """The words of ~w(a b c) or ~w[a b]a, else None."""
    if len(s) < 4 or s[1] not in "wW":
        return None
    body = s[3:]
    end = {"(": ")", "[": "]", "{": "}", "<": ">"}.get(s[2], s[2])
    body = body[: body.rindex(end)]
    return body.split()


class Reader:
    def __init__(self, toks: list):
        self.t, self.i = toks, 0

    def peek(self, k: int = 0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def args(self, close: str | None = None) -> tuple:
        """Positional args and keyword args, up to close (or the end)."""
        pos, kw = [], {}
        while self.i < len(self.t):
            k, v = self.peek()
            if close and k == "op" and v == close:
                self.i += 1
                break
            if k == "kw" or k == "kwq":
                self.i += 1
                key = v[:-1] if k == "kw" else string_value(v[:-1])
                kw[key] = self.expr()
            else:
                if kw:  # a positional after keywords: tolerate
                    self.expr()
                else:
                    pos.append(self.expr())
            k, v = self.peek()
            if k == "op" and v == ",":
                self.i += 1
        return pos, kw

    def expr(self):
        start = self.i
        val = self.primary()
        # anything after the primary up to a separator makes it an expression
        depth = 0
        extra = False
        while self.i < len(self.t):
            k, v = self.peek()
            if depth == 0 and k == "op" and v in (",", ")", "]", "}"):
                break
            if depth == 0 and k in ("kw", "kwq"):
                break
            if k == "op" and v in ("(", "[", "{", "%{"):
                depth += 1
            elif k == "op" and v in (")", "]", "}"):
                depth -= 1
            extra = True
            self.i += 1
        if extra:
            return ("expr", " ".join(x[1] for x in self.t[start:self.i]))
        return val

    def primary(self):
        k, v = self.take()
        if k is None:
            return None
        if k == "atom":
            return ("atom", string_value(v[1:]) if v[1:2] == '"' else v[1:])
        if k == "str":
            return string_value(v)
        if k == "heredoc":
            return v[3:-3]
        if k == "num":
            v = v.replace("_", "")
            return float(v) if "." in v else int(v)
        if k == "sigil":
            w = sigil_words(v)
            if w is None:
                return ("expr", v)
            return [("atom", x) for x in w] if v.rstrip().endswith("a") and v[-1] == "a" else w
        if k == "attr":
            return ("attr", v[1:])
        if k == "op" and v == "[":
            pos, kw = self.args("]")
            return kw if kw and not pos else pos + ([kw] if kw else [])
        if k == "op" and v == "{":
            pos, kw = self.args("}")
            return tuple(pos + ([kw] if kw else []))
        if k == "op" and v == "%{":
            self.args("}")
            return ("expr", "%{}")
        if k == "op" and v == "(":
            pos, kw = self.args(")")
            return pos[0] if len(pos) == 1 and not kw else ("expr", "(...)")
        if k == "alias":
            name = v
            if self.peek() == ("op", ".") and self.peek(1)[0] == "ident":
                self.i += 1
                fn = self.take()[1]
                pos, kw = [], {}
                if self.peek() == ("op", "("):
                    self.i += 1
                    pos, kw = self.args(")")
                return ("call", f"{name}.{fn}", pos, kw)
            return ("alias", name)
        if k == "ident":
            if v in ("true", "false"):
                return v == "true"
            if v == "nil":
                return None
            if self.peek() == ("op", "("):
                self.i += 1
                pos, kw = self.args(")")
                return ("call", v, pos, kw)
            return ("expr", v)
        return ("expr", v)


def call(text: str) -> tuple | None:
    """(name, args, kwargs) for a statement like `field(:a, :b, c: 1)`,
    `field :a, :b` or `t.string "x", null: false`."""
    toks = tokens(text)
    if not toks:
        return None
    i = 0
    name = toks[0][1]
    if toks[0][0] not in ("ident", "alias"):
        return None
    i = 1
    while i + 1 < len(toks) and toks[i] == ("op", ".") and toks[i + 1][0] in ("ident", "alias"):
        name += "." + toks[i + 1][1]
        i += 2
    r = Reader(toks[i:])
    if r.peek() == ("op", "("):
        # call with parentheses: read up to the matching close
        r.i += 1
        pos, kw = r.args(")")
    else:
        pos, kw = r.args()
    return name, pos, kw


def atom(v) -> str | None:
    if isinstance(v, tuple) and v and v[0] == "atom":
        return v[1]
    if isinstance(v, str):
        return v
    return None


def atoms(v) -> list:
    """Names in a list of atoms or strings, or the keys of a keyword list."""
    if isinstance(v, dict):
        return list(v)
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out += list(x)
            elif atom(x) is not None:
                out.append(atom(x))
        return out
    return []


def balanced(text: str) -> bool:
    if text.count('"""') % 2 or text.count("'''") % 2:
        return False
    depth = 0
    for k, v in tokens(text):
        if k == "op" and v in ("(", "[", "{", "%{"):
            depth += 1
        elif k == "op" and v in (")", "]", "}"):
            depth -= 1
    return depth <= 0


OPEN, CLOSE = {"(", "[", "{", "%{"}, {")", "]", "}"}
MAX_STATEMENT = 1 << 16  # characters; a longer statement is hostile or generated


def _stop(lines: list, first: int, end: int) -> int | None:
    """The index of the line that ends the statement starting at lines[first],
    looking at lines[first:end] only; None when it does not end there.

    The statement ends at the first line where brackets are balanced, heredoc
    quotes paired (counted as text, like the old per-prefix check), and the
    line does not end with a comma or a backslash. Brackets count from the
    tokens that end on or before the line. One tokenizer pass per window."""
    text = "\n".join(lines[first:end])
    toks = list(TOKEN.finditer(text))
    final = end == len(lines)
    # A boundary at or after the start of the window's last token may change
    # when more lines are added; only the last window may stop there.
    # Whitespace and a comment at the end stay as they are.
    stable = len(text) if final or not toks or toks[-1].lastgroup == "space" else toks[-1].start()
    depth, ti, dq, sq, off, tail = 0, 0, 0, 0, 0, ""
    for j in range(first, end):
        line = lines[j]
        off += len(line)
        dq += line.count('"""')
        sq += line.count("'''")
        while ti < len(toks) and toks[ti].end() <= off:
            m = toks[ti]
            if m.lastgroup == "op":
                v = m.group(0)
                depth += v in OPEN
                depth -= v in CLOSE
            ti += 1
        if off > stable and not final:
            return None
        if line.strip():
            tail = line
        # an unclosed sigil such as ~w( counts as an open bracket
        open_sigil = ti < len(toks) and toks[ti].start() < off and toks[ti].lastgroup == "sigil"
        if depth <= 0 and not dq % 2 and not sq % 2 and not open_sigil and not tail.rstrip().endswith((",", "\\")):
            return j
        off += 1  # the newline
    return None


def statements(lines: list, start: int = 0):
    """(line number, text) for each statement from lines[start:], joining the
    lines of a statement that spans several. Each statement tokenizes a
    window of lines that doubles until the statement ends in it, so the work
    stays linear in the input. A window longer than MAX_STATEMENT characters
    is skipped."""
    i, n = start, len(lines)
    while i < n:
        first, w, size, sized = i, 2, 0, i
        while True:
            end = min(n, first + w)
            size += sum(len(x) + 1 for x in lines[sized:end])
            sized = end
            if size > MAX_STATEMENT:
                break  # no real statement is this long: skip the window
            stop = _stop(lines, first, end)
            if stop is not None:
                break
            if end == n:
                stop = n - 1
                break
            w *= 2
        if size > MAX_STATEMENT:
            i = end
            continue
        yield first + 1, "\n".join(lines[first:stop + 1])
        i = stop + 1
