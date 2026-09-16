# Retention

`django-admin-errors` keeps storage bounded automatically — it does not grow forever. In plain
words, four limits apply:

1. **Individual occurrences (events) expire** after 30 days by default. The issue itself, and its
   total count, are unaffected — only the detailed record of each occurrence ages out.
2. **Daily trend numbers expire** after 90 days by default (the sparklines on the list page).
3. **Resolved issues are deleted** 14 days after they were last touched, and issues left **open**
   with no new occurrences are deleted after 90 days. Ignored issues are kept indefinitely unless
   your developer has configured otherwise.
4. **A hard cap on total issues** (5000 by default) — if you ever exceed it, the oldest,
   least-recently-seen issues are deleted first (ignored ones before resolved ones before open ones)
   to bring the count back down.

None of this needs your attention day to day — it runs automatically. If you want to see what it
would do, or run it yourself:

```sh
python manage.py errors_cleanup --dry-run
```

This prints a report of what each rule would delete, without deleting anything. Drop `--dry-run` to
actually run it. Your developer may also have this scheduled to run automatically (a cron job or a
Celery beat schedule), in which case you don't need to run it by hand at all.

For a rough sense of how much space this is using, ask your developer to run:

```sh
python manage.py errors_stats
```

This prints the number of issues/events/daily-count rows, the oldest and newest issue, a breakdown
by status, and an approximate on-disk size.
