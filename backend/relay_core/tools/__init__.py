"""Tool registry, executor, and JSON Schema sanitizer (docs/system-design.md sections 6.6-6.7,
8.7, 9.2). Tool-output post-processing (untrusted-content wrapping) lives in
`relay_core.agent.nodes.execute_step`, where the function-response content is actually built —
see that module's docstring for why."""
