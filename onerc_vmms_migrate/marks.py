"""The history import keeps out of the way of the server's 4-minute marks.

On Frappe Cloud a memory guard stops the largest process on the server when
memory runs short (SIGTERM first, then SIGKILL). A shared server runs short
when every site's scheduled jobs start together: each 4-minute mark, Frappe's
"All" frequency (*/4 * * * *). Every stopped history job died one to three
seconds after such a mark, whatever the site's plan, because the import was
the largest process at that moment.

So a live run starts just after a mark, stops at a safe point (a commit)
before the next one, and a fresh job carries on after it. A SIGTERM between
safe points stops it at the next one instead of mid-page.
"""

import signal
import threading
import time
from contextlib import contextmanager

PERIOD = 240  # Frappe's default scheduler_interval: the "All" jobs, every 4 minutes.
SETTLE = 45  # Seconds after a mark before a run starts or carries on.
CLEAR = 10  # Seconds before a mark by which a run has stopped.
SPAN = PERIOD - SETTLE - CLEAR  # The longest stretch a run gets between two marks.


def next_mark(now):
	return now - now % PERIOD + PERIOD


def wait(need, report=None, clock=time.time, sleep=time.sleep):
	"""Sleep until a stretch with `need` seconds left before the next mark.

	Nothing is read before this, so the waiting job is no bigger than the
	worker it was forked from. Returns the seconds slept."""
	need = min(need, SPAN)
	since = clock() % PERIOD
	if since >= SETTLE and PERIOD - CLEAR - since >= need:
		return 0
	delay = SETTLE - since if since < SETTLE else PERIOD - since + SETTLE
	if report:
		report(delay)
	sleep(delay)
	return delay


class Window:
	"""The time a run has before the next mark, checked at each safe point."""

	MINIMUM = 15  # Never start a unit of work with less than this left.

	def __init__(self, clock=time.time):
		self.clock = clock
		self.ends = next_mark(clock()) - CLEAR
		self.since = clock()
		self.longest = 0.0
		self.stop_requested = False

	def allows(self, need=0):
		"""Whether the next unit, as long as `need` or as the longest one so
		far (with room to spare), ends before the mark."""
		now = self.clock()
		self.longest = max(self.longest, now - self.since)
		self.since = now
		return not self.stop_requested and self.ends - now >= max(need, 1.5 * self.longest, self.MINIMUM)


@contextmanager
def stop_on_term(window):
	"""While it holds, a SIGTERM asks the run to stop at its next safe point.

	Only around work that reaches a safe point every few seconds (the pages
	of the archive and the convert). Elsewhere the default stands: the
	process ends at once, as the guard asked. Only in a job's own process
	(RQ's forked work-horse, where SIGTERM has its default action): a worker
	that runs jobs in its own process keeps its shutdown handler."""
	if (
		window is None
		or threading.current_thread() is not threading.main_thread()
		or signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
	):
		yield
		return

	def request_stop(signum, frame):
		window.stop_requested = True

	previous = signal.signal(signal.SIGTERM, request_stop)
	try:
		yield
	finally:
		signal.signal(signal.SIGTERM, previous)
