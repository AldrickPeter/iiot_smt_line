"""Finite FIFO buffer (a short conveyor) between two stations."""

from collections import deque
from collections.abc import Callable

from smt_sim.board import Board
from smt_sim.defects import Disposition


class Buffer:
    """FIFO queue of boards with an optional capacity limit.

    `capacity=None` means unlimited; that is used for the end-of-line sink
    that collects finished boards.
    """

    def __init__(self, name: str, capacity: int | None) -> None:
        if capacity is not None and capacity < 1:
            raise ValueError(f"Buffer {name}: capacity must be >= 1 or None")
        self.name = name
        self.capacity = capacity
        self._boards: deque[Board] = deque()
        # Optional listener told about every board that arrives (used to track orders).
        self.on_arrival: Callable[[Board], None] | None = None

    def has_space(self) -> bool:
        return self.capacity is None or len(self._boards) < self.capacity

    def is_empty(self) -> bool:
        return len(self._boards) == 0

    def push(self, board: Board) -> None:
        if not self.has_space():
            raise RuntimeError(f"Buffer {self.name} is full")
        self._boards.append(board)
        if self.on_arrival is not None:
            self.on_arrival(board)

    def peek(self) -> Board:
        """The board that pop() would return, without removing it."""
        return self._boards[0]

    def pop(self) -> Board:
        return self._boards.popleft()

    def boards(self) -> list[Board]:
        """Snapshot of the boards waiting, oldest first."""
        return list(self._boards)

    def __len__(self) -> int:
        return len(self._boards)


class LineExit(Buffer):
    """End of the line: boards arriving here passed every inspection."""

    def __init__(self) -> None:
        super().__init__("line_exit", capacity=None)

    def push(self, board: Board) -> None:
        board.disposition = Disposition.GOOD
        super().push(board)
