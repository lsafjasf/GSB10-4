"""Error types. Every error carries the sheet (table) name and the cell
coordinate so a corrupted shared-string index can be located precisely."""


class SstError(Exception):
    """Base error for shared-string-table related failures.

    Attributes:
        sheet: name of the worksheet (table) the failure belongs to.
        coord: A1-style coordinate of the offending cell, or None when the
            failure is not tied to a single cell.
    """

    def __init__(self, message, sheet=None, coord=None):
        self.sheet = sheet
        self.coord = coord
        location = []
        if sheet is not None:
            location.append("sheet=%r" % sheet)
        if coord is not None:
            location.append("cell=%s" % coord)
        if location:
            message = "%s (%s)" % (message, ", ".join(location))
        super().__init__(message)


class MissingSharedStringsError(SstError):
    """A cell references the shared string table but the package has none."""


class SharedStringIndexError(SstError):
    """A shared-string index is negative or out of range.

    Attributes:
        index: the offending index as stored in the cell.
        size:  number of entries in the shared string table.
    """

    def __init__(self, index, size, sheet=None, coord=None):
        self.index = index
        self.size = size
        message = (
            "shared string index %d out of range: table has %d entr%s"
            % (index, size, "y" if size == 1 else "ies")
        )
        super().__init__(message, sheet=sheet, coord=coord)
