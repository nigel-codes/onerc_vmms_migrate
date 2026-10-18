"""Small pure functions that clean one value. No database, no Frappe state."""

import datetime
import re

# Match the receiver's native email atom/domain rules before any insert.
# A permissive '@' check accepts trailing dots and turns source exceptions into
# runtime failures. Never repair or redirect an address by guessing.
EMAIL = re.compile(
    r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
    re.IGNORECASE,
)


def text(value) -> str:
	if value is None:
		return ""
	return re.sub(r"\s+", " ", str(value)).strip()


def email(value) -> str | None:
	"""Lowercased and trimmed, as core's Red Profile stores it, or None."""
	cleaned = text(value).lower()
	return cleaned if EMAIL.fullmatch(cleaned) else None


def phone(value, country_code: str | None) -> str | None:
	"""A number in +<country><subscriber> form where it can be made one.

	Only the shapes a society actually types: `0712 345 678`, `712345678`,
	`254712345678`, `+254-712-345-678`. Anything else comes back digits-only with
	a leading + if it had one, and the society's own pattern decides whether it
	is acceptable.
	"""
	raw = text(value)

	if not raw:
		return None

	plus = raw.startswith("+") or raw.startswith("00")
	digits = re.sub(r"\D", "", raw)

	if raw.startswith("00"):
		digits = digits[2:]

	if not digits:
		return None

	if plus:
		return "+" + digits

	code = re.sub(r"\D", "", country_code or "")

	if code:
		if digits.startswith(code) and len(digits) > len(code) + 6:
			return "+" + digits
		if digits.startswith("0"):
			return "+" + code + digits[1:]
		if len(digits) == 9:
			return "+" + code + digits

	return digits


def names(first, middle, last) -> tuple[str, str, str] | None:
	"""(first, middle, last), splitting one full name when that is all there is.

	None when there is no way to produce both a first and a last name, which
	core requires. A single word is not split into two people's worth of name.
	"""
	first, middle, last = text(first), text(middle), text(last)

	if first and last:
		return first, middle, last

	whole = text(" ".join(p for p in (first, middle, last) if p))
	parts = whole.split(" ")

	if len(parts) < 2:
		return None

	return parts[0], " ".join(parts[1:-1]), parts[-1]


def date(value) -> datetime.date | None:
	if value in (None, "", "0000-00-00"):
		return None
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	try:
		return datetime.date.fromisoformat(str(value)[:10])
	except ValueError:
		return None


def plausible_birth_date(value, today: datetime.date) -> datetime.date | None:
	born = date(value)
	if not born or born > today or born.year < 1900:
		return None
	return born


def year(value) -> int | None:
	if value in (None, ""):
		return None
	if isinstance(value, datetime.date):
		return value.year
	match = re.search(r"(19|20)\d{2}", str(value))
	return int(match.group(0)) if match else None


def key(value) -> str:
	"""A crosswalk lookup key: trimmed, case-folded, single-spaced."""
	return text(value).casefold()
