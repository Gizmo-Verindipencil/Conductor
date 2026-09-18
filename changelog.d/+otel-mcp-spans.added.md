**OpenTelemetry spans for direct MCP workflow steps**: each `type: mcp`
execution is exported as an `execute_tool` span under its workflow, parallel
group, or for-each item. Spans include bounded server, tool, result-size, and
truncation metadata without recording arguments, result contents, or spill
paths, and preserve routed tool errors, execution failures, and interrupted
attempts as distinct outcomes.
