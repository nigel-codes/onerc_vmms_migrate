"""Use recorded invoice settlement; submission alone is not payment evidence."""
from decimal import Decimal


def state(booking, invoices):
    explicit=booking.get('payment_status')
    if explicit in ('Unpaid','Paid','Verification Pending'):
        return explicit
    amount=Decimal(str(booking.get('total_amount') or 0))
    if amount<=0:return 'Unpaid'
    active=[r for r in invoices if int(r.get('docstatus') or 0)==1]
    if not active or any(r.get('is_return') for r in active):return 'Verification Pending'
    if any(r.get('currency')!=booking.get('currency') or r.get('outstanding_amount') is None
           or Decimal(str(r['outstanding_amount']))!=0 for r in active):return 'Verification Pending'
    covered=sum((Decimal(str(r.get('grand_total') or 0)) for r in active),Decimal(0))
    return 'Paid' if covered>=amount else 'Verification Pending'
