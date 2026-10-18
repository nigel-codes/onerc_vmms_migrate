"""Nobody hears about a migration by accident.

Loading a person runs vmmsx's own lifecycle hooks, and those hooks send people
email, SMS and in-app alerts ("your application was approved", "welcome"). On
a bulk load that would be thousands of real messages. Two layers stop it:

1. **`silenced()`** mutes Frappe's email and swaps vmmsx's three senders for
   no-ops *in this process only*. vmmsx is not changed. The senders are looked
   up on their module at call time (`lifecycle.notify(...)`), so replacing the
   module attribute is enough.
2. **`Guard`** is the check that does not trust layer 1. It counts the outbox
   tables before a person is loaded and again before the commit, and raises if
   any of them grew. A raise rolls the person back and stops the run. A sender
   added to vmmsx after this was written therefore stops the migration. It
   cannot message anyone.
"""

from contextlib import contextmanager

import frappe

# Tables a message leaves a row in on its way out.
OUTBOX = ("Email Queue", "Notification Log", "SMS Log", "Communication")


class SilenceBreached(Exception):
	pass


def _noop(returns):
	def replacement(*args, **kwargs):
		return returns

	return replacement


@contextmanager
def silenced():
	from vmmsx.notifications.services import direct, lifecycle, sms

	replaced = [
		(lifecycle, "notify", _noop(None)),
		(sms, "tell", _noop(False)),
		(direct, "tell", _noop([])),
	]
	originals = [(module, name, getattr(module, name)) for module, name, _ in replaced]
	previous_mute = frappe.flags.mute_emails

	try:
		frappe.flags.mute_emails = True
		for module, name, replacement in replaced:
			setattr(module, name, replacement)
		yield
	finally:
		for module, name, original in originals:
			setattr(module, name, original)
		frappe.flags.mute_emails = previous_mute


class Guard:
	def __init__(self):
		self.tables = [t for t in OUTBOX if frappe.db.table_exists(t)]
		self.before = self._counts()

	def _counts(self) -> dict[str, int]:
		return {t: frappe.db.count(t) for t in self.tables}

	def check(self) -> None:
		after = self._counts()
		grew = {t: after[t] - self.before[t] for t in self.tables if after[t] > self.before[t]}
		if grew:
			raise SilenceBreached(f"a message was about to leave: {grew}")
