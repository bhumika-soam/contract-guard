I have a file called sample_diff_report.json in my agents folder. It describes a breaking API change:

- endpoint: /items/{id}
- change_type: field_renamed
- old_schema_fragment: { "full_name": "string" }
- new_schema_fragment: { "name": "string" }

Search the frontend codebase for every place that uses the field "full_name" from this schema — including the generated API client, any TypeScript types/interfaces, and any React components that read, render, or destructure this field.

For each file you find, tell me:
1. The file path
2. The exact line(s) where "full_name" is used
3. A one-sentence description of what that code is doing with the field

report what you find.