"""Gemini requires previous model turns to be sent back exactly as received, thought signatures
included (docs/system-design.md section 9.1). Phase 5's approval gate parks a run mid-step, so
those turns now travel through a checkpoint and come back in another process — these tests pin
down that the encoding in `relay_core.agent.scratchpad` survives that trip byte-for-byte, and
that it goes through LangGraph's real serializer rather than a stand-in.
"""

import uuid
from typing import Any

from google.genai import types
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from relay_core.agent.scratchpad import dump_contents, load_contents
from relay_core.agent.state import AgentState

SIGNATURE = b"\x00\x01\x02opaque-thought-signature\xff\xfe"


def _state(**overrides: Any) -> AgentState:
    return AgentState(
        workspace_id=uuid.UUID(int=1),
        user_id=uuid.UUID(int=2),
        run_id=uuid.UUID(int=3),
        conversation_id=uuid.UUID(int=4),
        trigger_message_id=uuid.UUID(int=5),
        user_message="Which subscriptions end this month?",
        **overrides,
    )


def _model_turn_with_signature() -> types.Content:
    return types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    name="sales_db__run_sql", args={"sql": "SELECT 1"}
                ),
                thought_signature=SIGNATURE,
            )
        ],
    )


def _roundtrip(value: Any) -> Any:
    serde = JsonPlusSerializer()
    return serde.loads_typed(serde.dumps_typed(value))


def test_dump_then_load_reconstructs_an_identical_turn() -> None:
    original = [_model_turn_with_signature()]
    assert load_contents(dump_contents(original)) == original


def test_thought_signature_survives_the_checkpoint_serializer_as_raw_bytes() -> None:
    """The whole reason for the dict encoding: a Python-mode `model_dump()` leaves the signature
    as `bytes`, which msgpack carries natively. A `mode="json"` dump would base64 it into a
    `str` and the reconstructed turn would no longer match what Gemini issued."""
    dumped = dump_contents([_model_turn_with_signature()])
    assert isinstance(dumped[0]["parts"][0]["thought_signature"], bytes)

    restored = load_contents(_roundtrip(dumped))

    assert restored[0].parts[0].thought_signature == SIGNATURE


def test_mixed_scratchpad_survives_a_full_agent_state_roundtrip() -> None:
    """The realistic shape: a user turn, a model turn carrying a signed function call, and the
    function-response turn — checkpointed as part of `AgentState`, not in isolation."""
    turns = [
        types.Content(role="user", parts=[types.Part(text="Which subscriptions end this month?")]),
        _model_turn_with_signature(),
        types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="sales_db__run_sql", response={"result": "2 rows"}
                )
            ],
        ),
    ]
    state = _state(scratchpad=dump_contents(turns))

    restored = AgentState.model_validate(_roundtrip(state.model_dump()))

    assert load_contents(restored.scratchpad) == turns


def test_empty_scratchpad_is_the_default() -> None:
    state = _state()
    assert state.scratchpad == []
    assert state.pending_approval_id is None
