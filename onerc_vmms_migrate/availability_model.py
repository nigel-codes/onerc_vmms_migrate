"""Translate recorded HRMS shift windows without creating worked-hour claims."""
DAYS=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')


def _seconds(value):
    parts=str(value).split(':')
    if len(parts)!=3:raise ValueError('Unrecognised legacy shift time')
    hour,minute,second=int(parts[0]),int(parts[1]),float(parts[2])
    if not (0<=hour<=23 and 0<=minute<=59 and 0<=second<60):raise ValueError('Invalid legacy shift time')
    return hour*3600+minute*60+second


def windows(start,end):
    """Return (day offset,start,end) windows; None means hours were unspecified.

    Native Time cannot express 24:00, so an overnight evening segment closes at
    23:59:59. This conservatively omits at most the final second. Original exact
    times remain in the slot description and immutable source archive.
    """
    if not start or not end:return [(0,start or None,end or None)]
    opening,closing=_seconds(start),_seconds(end)
    if opening<closing:return [(0,str(start),str(end))]
    if opening==closing:return [(0,None,None)]  # Never infer a 24-hour commitment.
    result=[]
    if opening<86399:result.append((0,str(start),'23:59:59'))
    if closing>0:result.append((1,'00:00:00',str(end)))
    return result or [(0,None,None)]


def day(name,offset=0):
    return DAYS[(DAYS.index(name)+offset)%7]
