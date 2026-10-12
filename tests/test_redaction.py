"""Tests for run-scoped secret redaction primitives.

Covers :class:`conductor.redaction.RunRedactor`, contextvars-based
run-scoped registration and resolution, and the emitter redaction sink.
"""

from __future__ import annotations

from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.redaction import (
    REDACTED_MARKER,
    RunRedactor,
    current,
    reset_current,
    set_current,
)


class TestRunRedactorLifecycle:
    """Registration, state transitions, and short-secret notifications."""

    def test_active_transitions_across_registration_and_clear(self) -> None:
        # Requirement: active property reflects registry state (False -> True -> False).
        redactor = RunRedactor()
        assert not redactor.active

        redactor.register(["secret-token"])
        assert redactor.active

        redactor.clear()
        assert not redactor.active

    def test_empty_string_values_are_skipped(self) -> None:
        # Requirement: empty strings are skipped and do not activate redactor or trigger warnings.
        short_lengths: list[int] = []
        redactor = RunRedactor(on_short=short_lengths.append)

        redactor.register(["", ""])
        assert not redactor.active
        assert short_lengths == []

    def test_re_registration_is_idempotent(self) -> None:
        # Requirement: re-registering a secret is idempotent and does not re-warn.
        short_lengths: list[int] = []
        redactor = RunRedactor(on_short=short_lengths.append)

        redactor.register(["short"])
        redactor.register(["short"])
        assert short_lengths == [5]
        assert repr(redactor) == "<RunRedactor active=True count=1>"

    def test_on_short_callback_called_for_secrets_under_threshold(self) -> None:
        # Requirement: on_short is invoked with length for values shorter than 8 chars.
        short_lengths: list[int] = []
        redactor = RunRedactor(on_short=short_lengths.append)

        redactor.register(["long-secret-key", "short", "1234567", "12345678"])
        # "short" (5) and "1234567" (7) are < 8; "long-secret-key" (15) and "12345678" (8) are >= 8.
        assert short_lengths == [5, 7]


class TestScrubbingSemantics:
    """Scrubbing behavior across data types, nesting, keys, and overlaps."""

    def test_longest_first_replacement_prevents_partial_tails(self) -> None:
        # Requirement: overlapping values replaced longest-first so abcd/abcdef leaves no 'ef' tail.
        redactor = RunRedactor()
        redactor.register(["abcd", "abcdef"])

        input_text = "token: abcdef and abcd"
        scrubbed = redactor.scrub(input_text)
        assert scrubbed == f"token: {REDACTED_MARKER} and {REDACTED_MARKER}"
        assert "ef" not in scrubbed

    def test_nested_dict_list_tuple_structures(self) -> None:
        # Requirement: nested dict, list, tuple structures recursively scrubbed preserving types.
        redactor = RunRedactor()
        redactor.register(["secret_password", "api_token"])

        payload = {
            "auth": ["bearer secret_password", 100],
            "details": ("api_token", True, None),
            "nested": {"key": "secret_password_here"},
        }
        scrubbed = redactor.scrub(payload)

        assert scrubbed == {
            "auth": [f"bearer {REDACTED_MARKER}", 100],
            "details": (REDACTED_MARKER, True, None),
            "nested": {"key": f"{REDACTED_MARKER}_here"},
        }
        assert isinstance(scrubbed["details"], tuple)
        assert isinstance(scrubbed["auth"], list)

    def test_secret_inside_dict_key(self) -> None:
        # Requirement: secret values appearing inside dictionary keys are scrubbed.
        redactor = RunRedactor()
        redactor.register(["secret_key"])

        data = {"secret_key_name": "value", "normal_key": "secret_key"}
        scrubbed = redactor.scrub(data)

        assert scrubbed == {
            f"{REDACTED_MARKER}_name": "value",
            "normal_key": REDACTED_MARKER,
        }

    def test_bytes_payload_scrubbing(self) -> None:
        # Requirement: bytes payloads are scrubbed by matching UTF-8 byte sequences longest-first.
        redactor = RunRedactor()
        redactor.register(["secret_val", "secret_val_extended"])

        raw_bytes = b"header: secret_val_extended and secret_val suffix"
        scrubbed = redactor.scrub(raw_bytes)

        expected = b"header: ***redacted*** and ***redacted*** suffix"
        assert scrubbed == expected
        assert isinstance(scrubbed, bytes)

    def test_unicode_and_emoji_secrets(self) -> None:
        # Requirement: multi-byte unicode chars, CJK strings, emojis are correctly scrubbed.
        redactor = RunRedactor()
        redactor.register(["🔑super_secret", "密码123"])

        text = "Login: 🔑super_secret with 密码123!"
        assert redactor.scrub(text) == f"Login: {REDACTED_MARKER} with {REDACTED_MARKER}!"

        raw_bytes = text.encode()
        scrubbed_bytes = redactor.scrub(raw_bytes)
        assert scrubbed_bytes == f"Login: {REDACTED_MARKER} with {REDACTED_MARKER}!".encode()

    def test_input_object_mutation_prevention(self) -> None:
        # Requirement: input objects are not mutated in place (identity + content preserved).
        redactor = RunRedactor()
        redactor.register(["my_secret"])

        original_inner_list = ["my_secret", "keep"]
        original_dict = {
            "key": "my_secret",
            "items": original_inner_list,
        }

        scrubbed = redactor.scrub(original_dict)

        # Output is sanitized
        assert scrubbed == {
            "key": REDACTED_MARKER,
            "items": [REDACTED_MARKER, "keep"],
        }
        # Original inputs retain identity and unmodified content
        assert original_dict["key"] == "my_secret"
        assert original_inner_list == ["my_secret", "keep"]
        assert scrubbed is not original_dict
        assert scrubbed["items"] is not original_inner_list

    def test_scrub_returns_original_when_inactive(self) -> None:
        # Requirement: inactive redactor returns input object as-is with zero modification cost.
        redactor = RunRedactor()
        data = {"key": "secret"}
        assert redactor.scrub(data) is data

    def test_scrub_event_data_wrapper(self) -> None:
        # Requirement: scrub_event_data returns scrubbed dict payload or unchanged if inactive.
        redactor = RunRedactor()
        event_payload = {"msg": "secret_data"}

        # Inactive returns unchanged identity
        assert redactor.scrub_event_data(event_payload) is event_payload

        redactor.register(["secret_data"])
        scrubbed = redactor.scrub_event_data(event_payload)
        assert scrubbed == {"msg": REDACTED_MARKER}
        assert scrubbed is not event_payload

    def test_unscrubbed_types_returned_as_is(self) -> None:
        # Requirement: unscrubbed types (int, float, bool, custom objects) are returned as-is.
        redactor = RunRedactor()
        redactor.register(["secret"])

        class CustomObj:
            pass

        obj = CustomObj()
        assert redactor.scrub(123) == 123
        assert redactor.scrub(3.14) == 3.14
        assert redactor.scrub(True) is True
        assert redactor.scrub(None) is None
        assert redactor.scrub(obj) is obj


class TestSecurityRepresentations:
    """Ensure safe representations without secret leakage."""

    def test_repr_and_str_contain_no_registered_secrets(self) -> None:
        # Requirement: repr and str do not leak registered secret values, showing counts only.
        redactor = RunRedactor()
        secret_val = "SUPER_SECRET_VALUE_DO_NOT_LEAK"
        redactor.register([secret_val])

        repr_str = repr(redactor)
        str_str = str(redactor)

        assert secret_val not in repr_str
        assert secret_val not in str_str
        assert "count=1" in repr_str
        assert "count=1" in str_str


class TestContextVarIntegration:
    """Run-scoped ContextVar access and token-based nesting."""

    def test_contextvar_set_current_and_reset_round_trip(self) -> None:
        # Requirement: set_current / current / reset_current round-trip via ContextVar Tokens.
        assert current() is None

        redactor_outer = RunRedactor()
        token_outer = set_current(redactor_outer)
        try:
            assert current() is redactor_outer

            redactor_inner = RunRedactor()
            token_inner = set_current(redactor_inner)
            try:
                assert current() is redactor_inner
            finally:
                reset_current(token_inner)

            assert current() is redactor_outer
        finally:
            reset_current(token_outer)

        assert current() is None

    def test_contextvar_set_none(self) -> None:
        # Requirement: set_current(None) can explicitly clear redactor and be reset via Token.
        redactor = RunRedactor()
        token1 = set_current(redactor)
        try:
            assert current() is redactor
            token2 = set_current(None)
            try:
                assert current() is None
            finally:
                reset_current(token2)
            assert current() is redactor
        finally:
            reset_current(token1)

        assert current() is None


class TestEmitterRedaction:
    """Requirement (secrets-contract sinks I): the event emitter scrubs payloads
    before the subscriber snapshot when an active redactor is attached, passes
    events through untouched (identity) otherwise, and never mutates the
    caller's original event object."""

    def _emit_and_capture(
        self, emitter: WorkflowEventEmitter, event: WorkflowEvent
    ) -> list[WorkflowEvent]:
        captured: list[WorkflowEvent] = []
        emitter.subscribe(captured.append)
        emitter.emit(event)
        assert len(captured) == 1
        return captured

    def test_active_redactor_subscriber_receives_sanitized_copy(self) -> None:
        # Requirement: with an active redactor, the subscriber receives a copy
        # with registered secrets replaced by the marker.
        secret = "emitter-secret-token"
        redactor = RunRedactor()
        redactor.register([secret])

        emitter = WorkflowEventEmitter()
        emitter.set_redactor(redactor)
        event = WorkflowEvent(
            type="agent_completed", timestamp=1.5, data={"output": f"answer uses {secret}"}
        )
        (received,) = self._emit_and_capture(emitter, event)

        assert received is not event
        assert received.type == "agent_completed"
        assert received.timestamp == 1.5
        assert received.data == {"output": f"answer uses {REDACTED_MARKER}"}

    def test_active_redactor_original_event_unmutated(self) -> None:
        # Requirement: the caller's original event object and its payload are
        # never mutated by the scrub.
        secret = "emitter-secret-token"
        redactor = RunRedactor()
        redactor.register([secret])
        original_payload = {"output": f"answer uses {secret}"}

        emitter = WorkflowEventEmitter()
        emitter.set_redactor(redactor)
        event = WorkflowEvent(type="agent_completed", timestamp=2.0, data=original_payload)
        self._emit_and_capture(emitter, event)

        assert event.data is original_payload
        assert event.data == {"output": f"answer uses {secret}"}

    def test_inactive_redactor_same_event_object_reaches_subscribers(self) -> None:
        # Requirement: an attached but inactive redactor performs zero work —
        # the exact same event object reaches subscribers (identity passthrough).
        redactor = RunRedactor()
        emitter = WorkflowEventEmitter()
        emitter.set_redactor(redactor)
        event = WorkflowEvent(type="agent_started", timestamp=3.0, data={"agent": "a"})

        (received,) = self._emit_and_capture(emitter, event)

        assert received is event

    def test_no_redactor_same_event_object_reaches_subscribers(self) -> None:
        # Requirement: without any redactor attached the event passes through
        # unchanged (identity) — pre-change behavior preserved.
        emitter = WorkflowEventEmitter()
        event = WorkflowEvent(type="agent_started", timestamp=4.0, data={"agent": "a"})

        (received,) = self._emit_and_capture(emitter, event)

        assert received is event

    def test_set_redactor_idempotent_and_clearable(self) -> None:
        # Requirement: set_redactor is idempotent and set_redactor(None) detaches,
        # restoring the identity passthrough.
        redactor = RunRedactor()
        redactor.register(["emitter-secret-token"])
        emitter = WorkflowEventEmitter()

        emitter.set_redactor(redactor)
        emitter.set_redactor(redactor)
        event = WorkflowEvent(
            type="agent_completed", timestamp=5.0, data={"out": "emitter-secret-token"}
        )
        (received,) = self._emit_and_capture(emitter, event)
        assert received.data == {"out": REDACTED_MARKER}

        emitter.set_redactor(None)
        passthrough = WorkflowEvent(
            type="agent_started", timestamp=6.0, data={"out": "emitter-secret-token"}
        )
        (received2,) = self._emit_and_capture(emitter, passthrough)
        assert received2 is passthrough
