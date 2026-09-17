"""Carrying the in-progress step's Gemini turns across a checkpoint (docs/system-design.md
sections 8.6, 9.1).

Until Phase 5 the bounded ReAct loop in `execute_step` kept its turns in a local variable,
because nothing interrupted it. `approval_gate` does interrupt it, so the turns now have to
survive being written to a checkpoint and read back in a *different worker process* — and they
have to come back byte-identical, because Gemini requires previous model turns to be sent back
exactly as received, thought signatures included (section 9.1). A dropped or re-encoded
signature is not a cosmetic loss: the next request fails or silently degrades.

**Why dicts rather than `types.Content` in the state model.** LangGraph's msgpack serializer
will round-trip an arbitrary Pydantic model today, and it does preserve `thought_signature`
intact — but it emits "Deserializing unregistered type google.genai.types.Content from
checkpoint. This will be blocked in a future version." Pinning a run's resumability to a
deprecated serializer path is not a good trade, so the SDK's own `model_dump()` is used instead,
exactly as section 9.1 prescribes. `thought_signature` stays `bytes` through `model_dump()`
(this is a Python-mode dump, not `mode="json"`, which would base64 it), and msgpack handles
`bytes` natively, so the round trip is lossless.

`exclude_none=True` keeps the checkpoint small: `types.Content`/`types.Part` have many optional
fields, the scratchpad is re-serialized on every node transition within a step, and a dropped
`None` reconstructs to the same object anyway.
"""

from typing import Any

from google.genai import types


def dump_contents(contents: list[types.Content]) -> list[dict[str, Any]]:
    return [content.model_dump(exclude_none=True) for content in contents]


def load_contents(raw: list[dict[str, Any]]) -> list[types.Content]:
    return [types.Content.model_validate(content) for content in raw]
