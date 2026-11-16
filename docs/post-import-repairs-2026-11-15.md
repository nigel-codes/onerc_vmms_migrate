# Post-import repairs — 15 November 2026

Repairs to the people the production import wrote (seven agreed on 15 November, four added on 16 November for the history import and the end of the cutover), agreed after the
people audit. They live in `post_import_repairs.py` and run from
**VMMS Migration Settings → Post-import repairs**. Nothing runs on deploy.

| Repair | What it changes |
| --- | --- |
| System Manager | Removed from the `@navari.co.ke` accounts and `missingemail19@vmms.com`. The accounts stay enabled. |
| Desk access | Inbox User, Analytics, Dashboard Manager, VM Member, Gameplan Member and Gameplan Guest come off accounts whose only desk roles are those (and Raven User). Raven User stops granting the desk. Accounts left with no desk role become portal users. Staff are not touched. |
| Coordinator areas | Each County Coordinator gets a Geo Assignment for the companies their old User Permissions named, read from the selected capture. "Kenya Red Cross Society" and "KRCS - HQ" become the national node. Coordinators with no company are listed. |
| Waiting applications | In Review applications with no stage (old Hold and Replied) enter their county's first review step and the approver's queue. Old decisions stay. The assignment notice is off for the run. The step's deadline starts on the day of routing. |
| Approved without a volunteer | Job/opportunity applications are left alone. An application whose person is a volunteer is linked. Otherwise a volunteer is made through vmmsx's own acceptance, joining on the old application's last-modified date. |
| Identity numbers | 7–8 digit untyped numbers become National ID rows. A number on another profile, or shared between profiles, is skipped. Other patterns are counted, not converted. |
| Life memberships | The end date is cleared on Active life memberships. |
| Event bookings (16 Nov) | An imported booking with no account (`legacy_unclaimed_booking`) is linked to its person's account once it exists: the old booking's user, else its primary contact's email, through the people session's account record. Placeholder addresses are skipped. A plain write, so Buzz sends no ticket. |
| Approval routing (16 Nov) | Imported In Review applications and memberships whose queue differs from who may decide today are moved, with the assignment notice off. Otherwise vmmsx's resync (daily, and on every deploy) moves them and Frappe emails each new assignee. |
| Reviewer notes (16 Nov) | The old reviewers' comments on applications and memberships (type Comment) are added to the records they became, with the old name, text, author and date. Desk shows them on the record. Comments on applications that were left alone stay in the capture and are counted. |
| Held mail (16 Nov) | Email Queue rows still Not Sent or Partially Sent are set to Error with the reason, so Finish cutover does not send them. Nothing is deleted; Retry Sending in the Email Queue list sends one deliberately. |

## Operating

1. Deploy. The site migration changes no records.
2. **Preview repairs.** This writes `repairs-preview.json` and changes nothing.
   Read it, especially:
   - who stays a desk user, and why;
   - coordinators needing a look;
   - waiting applications held as "would not save";
   - the ID patterns.
3. **Apply repairs.** Tick the repairs, confirm the backup and type `REPAIR`.
   They run in the order above on the long worker and commit every 50 records.
   `repairs-applied.json` lists what was done and anything that failed.
4. Run Preview again. Every repair should show nothing left to do. After a later
   people capture, preview and apply again: only the newcomers are repaired.

Finish cutover refuses while mail is held in the queue, unless the operator
ticks "Send the mail held during the cutover". Muting holds mail rather than
dropping it, and the first scheduler run after the cutover sends all of it.
Every deploy runs vmmsx's approval resync outside the migration's isolation, so
a deploy during the cutover can queue assignment mail: preview Held mail after
each deploy.

No email, SMS or alert is sent. The run stops before committing if this worker
left a row in Email Queue, Notification Log, SMS Log or Communication.

Rehearsed on kenyaclone with prod-shaped cases, rolled back. Each repair applied,
nothing was sent, and a second apply found nothing to do.
