Use ONLY the "Diff Report" JSON block and the "Affected files" list provided
below in this prompt for all details of the breaking change — old/new field
mapping (if any), endpoint, method, and change type. Do NOT read
sample_diff_report.json or any other diff_report file from disk; the correct
data for this run is already embedded in this prompt.

Not every change type has an old/new field mapping — endpoint_removed, for
example, has none. Do not invent one if it isn't present in the Diff Report.

Modify ONLY the specific files listed in "Affected files" below, and within
each file, touch only the lines relevant to this specific breaking change.
Leave all surrounding code, comments, formatting, and unrelated logic
untouched.

Affected files and lines to repair:


The exact task for this change (rename, type change, or endpoint removal) is
specified after this section, along with the real Diff Report data. Follow
that task description precisely — do not assume it is a rename unless it
explicitly says so.