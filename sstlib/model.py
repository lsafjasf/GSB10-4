"""Data model: rich text, cells and sheets."""

from .errors import SstError


class RichText:
    """A shared string made of ordered runs.

    Each run is a tuple ``(text, props)`` where ``props`` is a dict of the
    run's formatting marks (bold, italic, color, ...) taken from ``<rPr>``.
    A run with no formatting has an empty props dict; no format mark is
    dropped. Concatenating ``text`` across all runs in order reproduces the
    plain-text value of the cell.
    """

    __slots__ = ("runs",)

    def __init__(self, runs):
        self.runs = [(text, dict(props)) for text, props in runs]

    @property
    def text(self):
        return "".join(text for text, _ in self.runs)

    def __repr__(self):
        return "RichText(%r)" % (self.runs,)

    def __eq__(self, other):
        if isinstance(other, RichText):
            return self.runs == other.runs
        return NotImplemented

    def __hash__(self):
        def freeze(obj):
            if isinstance(obj, dict):
                return tuple(sorted((k, freeze(v)) for k, v in obj.items()))
            return obj

        return hash(tuple((t, freeze(p)) for t, p in self.runs))


class Cell:
    """A restored cell.

    ``kind`` is one of:
      * ``"str"``    - shared string or inline string (``value`` is ``str``
                       or :class:`RichText`),
      * ``"number"`` - numeric cell, value is the raw numeric text,
      * ``"bool"``   - boolean cell,
      * ``"empty"``  - ``<c/>`` with no value.
    """

    __slots__ = ("kind", "value", "style", "formula")

    def __init__(self, kind, value=None, style=None, formula=None):
        self.kind = kind
        self.value = value
        self.style = style
        self.formula = formula

    def plain_text(self):
        """Plain text of a string cell (runs joined in original order)."""
        if isinstance(self.value, RichText):
            return self.value.text
        return self.value

    def __repr__(self):
        return "Cell(%r, %r, style=%r, formula=%r)" % (
            self.kind,
            self.value,
            self.style,
            self.formula,
        )

    def __eq__(self, other):
        if not isinstance(other, Cell):
            return NotImplemented
        return (
            self.kind == other.kind
            and self.value == other.value
            and self.style == other.style
            and self.formula == other.formula
        )


class Sheet:
    """One worksheet.

    ``cells`` is a sparse dict keyed by A1 coordinate - only coordinates that
    appear explicitly as ``<c>`` elements are stored; nothing is filled in
    just because the sheet's dimension says the grid is large.

    ``merges`` is the list of explicit ``<mergeCell>`` ranges as
    ``(top_left_coord, bottom_right_coord)`` tuples. Merged coverage is only
    expanded on demand via :meth:`merged_coords`; the covered cells are never
    synthesized into ``cells``.
    """

    def __init__(self, name, sheet_id, rel_id, cells, merges, path):
        self.name = name
        self.sheet_id = sheet_id
        self.rel_id = rel_id
        self.cells = cells
        self.merges = merges
        self.path = path

    def get(self, coord):
        return self.cells[coord]

    def merged_coords(self):
        """Yield ``(range_string, [coords...])`` for every explicit merge."""
        from .parser import iter_range

        for top_left, bottom_right in self.merges:
            yield "%s:%s" % (top_left, bottom_right), list(
                iter_range(top_left, bottom_right)
            )

    def to_plain_dict(self):
        """Coordinate -> plain text/number value for cross-checking."""
        out = {}
        for coord, cell in self.cells.items():
            if cell.kind == "empty":
                continue
            if cell.kind == "str":
                value = cell.plain_text()
            else:
                value = cell.value
            out[coord] = value
        return out

    def __repr__(self):
        return "Sheet(name=%r, cells=%d, merges=%d)" % (
            self.name,
            len(self.cells),
            len(self.merges),
        )
