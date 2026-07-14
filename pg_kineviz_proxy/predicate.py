"""WHERE predicate engine: parse a Cypher boolean expression into a tree, then
compile it to SQL or evaluate it in memory.

This replaces the earlier flat regex-based filter list. A real recursive-descent
parser with operator precedence handles AND / OR / NOT / parentheses and every
comparison uniformly, so nested and mixed boolean logic Just Works.

Grammar (low → high precedence):

    or   := and  ( OR  and  )*
    and  := not  ( AND not  )*
    not  := NOT not | pred
    pred := primary [ cmp | str-op | IN list | IS [NOT] NULL ]      (else bare bool)
    primary := '(' or ')' | operand
    operand := func '(' operand ')' | id '(' var ')' | literal | var.prop | var

Supported leaf predicates: = <> != < > <= >= , CONTAINS / STARTS WITH / ENDS
WITH, =~ (regex), IN [list], IS [NOT] NULL, id(n) IN [internal_id(...)], and a
bare boolean property (`WHERE n.isfraud`). toLower/toUpper wrappers are folded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


# ---------- AST ----------

@dataclass
class Prop:
    var: str
    prop: str


@dataclass
class Lit:
    value: Any


@dataclass
class And:
    parts: List[Any]


@dataclass
class Or:
    parts: List[Any]


@dataclass
class Not:
    expr: Any


@dataclass
class Cmp:
    lhs: Any            # Prop
    op: str            # = <> < > <= >=
    rhs: Any           # Prop (cross-var) or Lit


@dataclass
class StrOp:
    lhs: Prop
    op: str            # contains | startswith | endswith | regex
    value: Any


@dataclass
class InList:
    lhs: Prop
    values: List[Any]
    negated: bool = False


@dataclass
class IsNull:
    lhs: Prop
    negated: bool = False


@dataclass
class BoolProp:
    lhs: Prop


@dataclass
class IdIn:
    var: str
    refs: List[Tuple[int, int]]      # (table_id, offset)
    negated: bool = False


# transient operands used only during parsing
@dataclass
class _IdOperand:
    var: str


@dataclass
class _InternalId:
    t: int
    o: int


class ParseError(Exception):
    pass


# ---------- tokenizer ----------

_TOKEN = re.compile(
    r"""(?P<WS>\s+)
      | (?P<STRING>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
      | (?P<NUMBER>-?\d+\.\d+|-?\d+)
      | (?P<OP><=|>=|<>|!=|=~|=|<|>)
      | (?P<LP>\() | (?P<RP>\)) | (?P<LB>\[) | (?P<RB>\]) | (?P<COMMA>,)
      | (?P<NAME>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> List[Tuple[str, str]]:
    toks, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise ParseError("unexpected char at %d: %r" % (i, text[i:i + 12]))
        i = m.end()
        kind = m.lastgroup
        if kind == "WS":
            continue
        toks.append((kind, m.group()))
    return toks


_KEYWORDS = {"and", "or", "not", "in", "is", "null", "contains", "starts", "ends", "with", "true", "false"}
_FUNCS = {"tolower", "lower", "toupper", "upper"}


# ---------- parser ----------

class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def _peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else ("EOF", "")

    def _next(self):
        t = self._peek()
        self.i += 1
        return t

    def _kw(self, word) -> bool:
        k, v = self._peek()
        if k == "NAME" and v.lower() == word:
            self.i += 1
            return True
        return False

    def _expect(self, kind):
        k, v = self._next()
        if k != kind:
            raise ParseError("expected %s, got %s %r" % (kind, k, v))
        return v

    # -- grammar --

    def parse(self):
        e = self._or()
        if self._peek()[0] != "EOF":
            raise ParseError("trailing tokens: %r" % (self.toks[self.i:],))
        return e

    def _or(self):
        parts = [self._and()]
        while self._kw("or"):
            parts.append(self._and())
        return parts[0] if len(parts) == 1 else Or(parts)

    def _and(self):
        parts = [self._not()]
        while self._kw("and"):
            parts.append(self._not())
        return parts[0] if len(parts) == 1 else And(parts)

    def _not(self):
        # leading NOT that is not "NOT IN" (that is handled at predicate level)
        k, v = self._peek()
        if k == "NAME" and v.lower() == "not" and not (self._peek(1)[0] == "NAME" and self._peek(1)[1].lower() == "in"):
            self.i += 1
            return Not(self._not())
        return self._predicate()

    def _predicate(self):
        if self._peek()[0] == "LP":
            self.i += 1
            e = self._or()
            self._expect("RP")
            return e
        operand = self._operand()

        # IS [NOT] NULL
        if self._kw("is"):
            neg = self._kw("not")
            if not self._kw("null"):
                raise ParseError("expected NULL after IS")
            return IsNull(operand, neg)

        # string ops
        if self._kw("contains"):
            return StrOp(operand, "contains", _litval(self._operand()))
        if self._kw("starts"):
            self._kw("with")
            return StrOp(operand, "startswith", _litval(self._operand()))
        if self._kw("ends"):
            self._kw("with")
            return StrOp(operand, "endswith", _litval(self._operand()))

        # [NOT] IN [ ... ]
        neg = False
        if self._peek()[0] == "NAME" and self._peek()[1].lower() == "not" \
                and self._peek(1)[0] == "NAME" and self._peek(1)[1].lower() == "in":
            self.i += 1
            neg = True
        if self._kw("in"):
            items = self._bracket_list()
            if isinstance(operand, _IdOperand):
                refs = [(x.t, x.o) for x in items if isinstance(x, _InternalId)]
                return IdIn(operand.var, refs, neg)
            return InList(operand, [_litval(x) for x in items], neg)

        # comparison
        k, v = self._peek()
        if k == "OP":
            self.i += 1
            rhs = self._operand()
            if v == "=~":
                return StrOp(operand, "regex", _litval(rhs))
            op = "<>" if v == "!=" else v
            return Cmp(operand, op, rhs if isinstance(rhs, Prop) else Lit(_litval(rhs)))

        # bare boolean property
        if isinstance(operand, Prop):
            return BoolProp(operand)
        raise ParseError("unexpected operand without operator")

    def _bracket_list(self):
        # `IN UNNEST([ ... ])` — Kineviz emits this; it's equivalent to `IN [ ... ]`.
        if self._peek()[0] == "NAME" and self._peek()[1].lower() == "unnest":
            self.i += 1
            self._expect("LP")
            self._expect("LB")
            items = self._items_until("RB")
            self._expect("RB")
            self._expect("RP")
            return items
        # optional wrapping ( ... ) then [ ... ]
        if self._peek()[0] == "LP":
            self.i += 1
            self._expect("LB")
            items = self._items_until("RB")
            self._expect("RB")
            self._expect("RP")
            return items
        self._expect("LB")
        items = self._items_until("RB")
        self._expect("RB")
        return items

    def _items_until(self, end):
        items = []
        if self._peek()[0] == end:
            return items
        items.append(self._operand())
        while self._peek()[0] == "COMMA":
            self.i += 1
            items.append(self._operand())
        return items

    def _operand(self):
        # a parenthesized operand, e.g. the `('john')` in CONTAINS('john')
        if self._peek()[0] == "LP":
            self.i += 1
            inner = self._operand()
            self._expect("RP")
            return inner
        k, v = self._next()
        if k == "STRING":
            return Lit(_unquote(v))
        if k == "NUMBER":
            return Lit(int(v) if re.match(r"^-?\d+$", v) else float(v))
        if k != "NAME":
            raise ParseError("expected operand, got %s %r" % (k, v))
        low = v.lower()
        if low in _FUNCS and self._peek()[0] == "LP":
            self.i += 1
            inner = self._operand()
            self._expect("RP")
            return _fold(low, inner)
        if low == "id" and self._peek()[0] == "LP":
            self.i += 1
            var = self._expect("NAME")
            self._expect("RP")
            return _IdOperand(var)
        if low == "internal_id" and self._peek()[0] == "LP":
            self.i += 1
            a = int(self._expect("NUMBER"))
            self._expect("COMMA")
            b = int(self._expect("NUMBER"))
            self._expect("RP")
            return _InternalId(a, b)
        if low == "true":
            return Lit(True)
        if low == "false":
            return Lit(False)
        if low == "null":
            return Lit(None)
        if "." in v:
            var, prop = v.split(".", 1)
            return Prop(var, prop)
        return Prop(v, None)         # bare var (only meaningful for a bare-boolean prop; rare)


def _unquote(s):
    return s[1:-1].replace("\\'", "'").replace('\\"', '"')


def _litval(operand):
    return operand.value if isinstance(operand, Lit) else operand


def _fold(fn, inner):
    """Apply toLower/toUpper to a literal; unwrap around a property (ILIKE is case-insensitive)."""
    if isinstance(inner, Lit) and isinstance(inner.value, str):
        return Lit(inner.value.lower() if fn in ("tolower", "lower") else inner.value.upper())
    return inner


def parse(text: str):
    """Parse a WHERE clause body into an expression tree, or None if empty/unparseable."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return _Parser(_tokenize(text)).parse()
    except ParseError:
        return None


# ---------- label hints (for pattern-variable label inference) ----------

def id_hints(expr) -> List[Tuple[str, List[Tuple[int, int]]]]:
    """Positive top-level `id(var) IN [internal_id(...)]` constraints — the decoded
    element type is an authoritative label hint. Only AND/positive contexts count."""
    out = []

    def walk(e):
        if isinstance(e, IdIn) and not e.negated:
            out.append((e.var, e.refs))
        elif isinstance(e, And):
            for p in e.parts:
                walk(p)
    walk(expr)
    return out


# ---------- SQL compilation ----------

class SqlCtx:
    def __init__(self, gen, label_of, node_pk, registry, params):
        self.gen = gen                 # var -> generated pattern var (v0)
        self.label_of = label_of       # var -> label
        self.node_pk = node_pk         # label -> pk column
        self.registry = registry
        self.params = params           # list, appended to

    def param(self, value) -> str:
        self.params.append(value)
        return "%s"

    def col(self, prop: Prop) -> str:
        return "{}.{}".format(self.gen.get(prop.var, prop.var), prop.prop)


def to_sql(expr, ctx: SqlCtx) -> str:
    if isinstance(expr, And):
        return "(" + " AND ".join(to_sql(p, ctx) for p in expr.parts) + ")"
    if isinstance(expr, Or):
        return "(" + " OR ".join(to_sql(p, ctx) for p in expr.parts) + ")"
    if isinstance(expr, Not):
        return "NOT (" + to_sql(expr.expr, ctx) + ")"
    if isinstance(expr, Cmp):
        rhs = ctx.col(expr.rhs) if isinstance(expr.rhs, Prop) else ctx.param(expr.rhs.value)
        return "{} {} {}".format(ctx.col(expr.lhs), expr.op, rhs)
    if isinstance(expr, StrOp):
        col = ctx.col(expr.lhs)
        if expr.op == "regex":
            return "{}::text ~ {}".format(col, ctx.param(expr.value))
        pat = {"contains": "%{}%", "startswith": "{}%", "endswith": "%{}"}[expr.op].format(expr.value)
        return "{}::text ILIKE {}".format(col, ctx.param(pat))
    if isinstance(expr, InList):
        if not expr.values:
            return "false" if not expr.negated else "true"
        ph = ", ".join(ctx.param(v) for v in expr.values)
        s = "{} IN ({})".format(ctx.col(expr.lhs), ph)
        return "NOT ({})".format(s) if expr.negated else s
    if isinstance(expr, IsNull):
        return "{} IS {} NULL".format(ctx.col(expr.lhs), "NOT" if expr.negated else "").replace("IS  NULL", "IS NULL")
    if isinstance(expr, BoolProp):
        return "{} = true".format(ctx.col(expr.lhs))
    if isinstance(expr, IdIn):
        label = ctx.label_of.get(expr.var)
        keys = []
        for t, o in expr.refs:
            dec = ctx.registry.decode(t, o)
            if dec is not None:
                keys.append(dec[1][0])
        if not keys or not label:
            return "true" if expr.negated else "false"
        pk = ctx.node_pk.get(label, "id")
        ph = ", ".join(ctx.param(k) for k in keys)
        s = "{}.{} IN ({})".format(ctx.gen.get(expr.var, expr.var), pk, ph)
        return "NOT ({})".format(s) if expr.negated else s
    raise ValueError("cannot compile predicate node: %r" % (expr,))


# ---------- in-memory evaluation (mock backend) ----------

def evaluate(expr, binding, registry) -> bool:
    """Evaluate against `binding` = {var: {"label":.., "key":.., "props": {...}}}."""
    if expr is None:
        return True
    if isinstance(expr, And):
        return all(evaluate(p, binding, registry) for p in expr.parts)
    if isinstance(expr, Or):
        return any(evaluate(p, binding, registry) for p in expr.parts)
    if isinstance(expr, Not):
        return not evaluate(expr.expr, binding, registry)
    if isinstance(expr, Cmp):
        lv = _prop_val(expr.lhs, binding)
        rv = _prop_val(expr.rhs, binding) if isinstance(expr.rhs, Prop) else expr.rhs.value
        if lv is None or rv is None:
            return False
        return _cmp(lv, expr.op, rv)
    if isinstance(expr, StrOp):
        lv = _prop_val(expr.lhs, binding)
        if lv is None:
            return False
        s, v = str(lv), str(expr.value)
        if expr.op == "contains":
            return v.lower() in s.lower()
        if expr.op == "startswith":
            return s.lower().startswith(v.lower())
        if expr.op == "endswith":
            return s.lower().endswith(v.lower())
        if expr.op == "regex":
            return re.search(v, s) is not None
    if isinstance(expr, InList):
        lv = _prop_val(expr.lhs, binding)
        inside = lv in expr.values
        return (not inside) if expr.negated else inside
    if isinstance(expr, IsNull):
        lv = _prop_val(expr.lhs, binding)
        return (lv is not None) if expr.negated else (lv is None)
    if isinstance(expr, BoolProp):
        return _prop_val(expr.lhs, binding) is True
    if isinstance(expr, IdIn):
        node = binding.get(expr.var)
        if not node:
            return expr.negated
        want = (node.get("label"), (node.get("key"),))
        matched = any(registry.decode(t, o) == want for t, o in expr.refs)
        return (not matched) if expr.negated else matched
    return True


def _prop_val(p, binding):
    node = binding.get(p.var)
    return node.get("props", {}).get(p.prop) if node else None


def _cmp(a, op, b):
    if op == "=":
        return a == b
    if op == "<>":
        return a != b
    if op == "<":
        return a < b
    if op == ">":
        return a > b
    if op == "<=":
        return a <= b
    if op == ">=":
        return a >= b
    return False
