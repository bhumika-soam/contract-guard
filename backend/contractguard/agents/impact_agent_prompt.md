Use ONLY the "Diff Report" JSON block provided below in this prompt to
understand the breaking change — do NOT read sample_diff_report.json or any
other diff_report file from disk. The real data for this run is already
embedded in this prompt.

The Diff Report's `change_type` tells you what kind of break this is:
- `field_renamed`: a field's name changed. old_schema_fragment gives the old
  field name, new_schema_fragment gives the new one.
- `field_type_changed`: a field kept its name but changed type. Search for
  the field name (same in both old_schema_fragment and new_schema_fragment).
- `endpoint_removed`: an endpoint was deleted entirely. There is no old/new
  field mapping — instead, search for any code that calls this endpoint
  (the method + path below), including the generated API client function
  for it and any UI control (button, menu item, hook) that triggers that call.

Search the frontend codebase for every place affected by this specific
change — including the generated API client, any TypeScript types/interfaces,
and any React components that read, render, destructure, or call the
relevant field or endpoint.

For each file you find, report:
1. The file path
2. The exact line(s) where the relevant field/endpoint is used
3. A one-sentence description of what that code is doing with it

Report what you find.