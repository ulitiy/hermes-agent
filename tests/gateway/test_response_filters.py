from gateway.response_filters import (
    is_gateway_control_marker_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
    is_partial_gateway_control_marker,
    parse_reaction_only_response,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_blank_and_prose_mentions_are_not_silence():
    assert not is_intentional_silence_response("")
    assert not is_intentional_silence_response("Use NO_REPLY when no answer is needed.")
    assert not is_intentional_silence_response("The reply was [SILENT], intentionally.")


def test_failed_agent_result_never_counts_as_intentional_silence():
    assert is_intentional_silence_agent_result({"failed": False}, "NO_REPLY")
    assert not is_intentional_silence_agent_result({"failed": True}, "NO_REPLY")


def test_reaction_only_marker_parses_whole_response_only():
    assert parse_reaction_only_response("REACTION_ONLY: 👍") == "👍"
    assert parse_reaction_only_response(" reaction_only : 👀 \n") == "👀"
    assert parse_reaction_only_response("REACTION_ONLY: ✅") == "👍"
    assert parse_reaction_only_response("Use REACTION_ONLY: 👍 for acks") is None
    assert parse_reaction_only_response("REACTION_ONLY: nope") is None


def test_gateway_control_marker_includes_silence_and_reaction_only():
    assert is_gateway_control_marker_response("NO_REPLY")
    assert is_gateway_control_marker_response("REACTION_ONLY: 👍")
    assert not is_gateway_control_marker_response("REACTION_ONLY: 👍 and thanks")


def test_partial_gateway_control_marker_handles_reaction_prefixes():
    for text in ("R", "REACTION_", "REACTION_ONLY", "REACTION_ONLY:", "REACTION_ONLY: "):
        assert is_partial_gateway_control_marker(text)
    assert is_partial_gateway_control_marker("REACTION_ONLY: 👍")
    assert not is_partial_gateway_control_marker("REACTION_ONLY: nope")
    assert not is_partial_gateway_control_marker("Reaction only please")
