"""mailsplit: streaming multi-message email archive splitter (stdlib only)."""

from .core import Message, SplitRule, parse_message, split_archive

__all__ = ["Message", "SplitRule", "parse_message", "split_archive"]
__version__ = "1.0.0"
