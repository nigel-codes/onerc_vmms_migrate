"""Explicit source-data exceptions never waive runtime migration failures."""


def source_exclusions_accepted(people_excluded,people_total,accounts_excluded,accounts_total,acknowledged):
    if not people_excluded and not accounts_excluded:return True
    return bool(acknowledged and people_total>0 and accounts_total>0
        and people_excluded/people_total<=0.01 and accounts_excluded/accounts_total<=0.01)
