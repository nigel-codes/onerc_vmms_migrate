"""Keep retained placeholder addresses off the native outbound transport."""

from email.utils import parseaddr

from onerc_vmms_migrate import placeholder_emails


class PlaceholderEmailGuard:
    def send(self, *args, **kwargs):
        # Check here as well as native queue status: force_send must not bypass
        # the restriction, including for a previously queued mixed message.
        candidates = {parseaddr(row.recipient)[1].strip().lower() for row in self.recipients}
        if any(placeholder_emails.PATTERN.fullmatch(value) for value in candidates):
            if candidates.intersection(placeholder_emails.blocked_addresses()):
                import frappe
                raise frappe.ValidationError("Email delivery blocked: a migration placeholder needs a verified real address.")
        return super().send(*args, **kwargs)
