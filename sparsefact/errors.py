"""Exception types for sparsefact."""


class MemoryBudgetExceeded(RuntimeError):
    """Raised when a factorization would exceed the configured memory budget.

    This is a *controlled* refusal: the process raises a catchable exception
    instead of being OOM-killed by the OS.  The message always states how
    much memory was estimated/required versus the configured limit, and what
    the caller can do about it (reorder, enable spilling, raise the budget).
    """

    def __init__(self, message, required_bytes=None, budget_bytes=None):
        super().__init__(message)
        self.required_bytes = required_bytes
        self.budget_bytes = budget_bytes
