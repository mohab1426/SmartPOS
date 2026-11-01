"""Explicit transaction boundary shared by all write services."""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def write_transaction(connection):
    """Run a block as one write transaction.

    * If the caller already opened a transaction (``connection.in_transaction``),
      the caller owns commit/rollback and this block simply participates.
    * Otherwise a ``BEGIN IMMEDIATE`` write lock is taken (so check-then-write
      sequences such as balance checks cannot race with another terminal), and
      the block is committed on success or rolled back on any exception.
    """
    if connection.in_transaction:
        yield connection
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
