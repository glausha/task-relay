from __future__ import annotations

try:
    from ..db import queries as q
except ImportError:  # pragma: no cover
    def _not_implemented(*args: object, **kwargs: object) -> None:
        raise NotImplementedError("Phase 1: db queries")

    enqueue = _not_implemented
    peek_head = _not_implemented
    update_status = _not_implemented
    remove = _not_implemented
    next_token = _not_implemented
else:
    enqueue = q.enqueue_waiter
    peek_head = q.peek_head_waiter
    update_status = q.update_waiter_status
    remove = q.remove_waiter
    next_token = q.next_branch_token
