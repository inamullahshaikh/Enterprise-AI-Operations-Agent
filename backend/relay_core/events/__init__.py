"""Redis Streams event publisher and event-name constants (docs/system-design.md
section 16). SSE event *schemas* beyond a bare `type`/JSON `payload` aren't
formalized yet — the Phase 2 event types are plain dicts documented at their
call sites (`relay_core.events.types`), and approval/artifact/usage events are
added with the phases that produce them.
"""
