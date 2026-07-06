from gateway.response_filters import (
    is_autonomous_silence_response,
    is_gateway_control_marker_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
    is_partial_gateway_control_marker,
    parse_reaction_only_response,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_autonomous_silence_accepts_marker_with_own_line_note():
    """The loose rule for cron/webhook lanes: marker + explanation suppresses."""
    assert is_autonomous_silence_response("[SILENT]")
    assert is_autonomous_silence_response("[SILENT]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[SILENT]")
    assert is_autonomous_silence_response("no_reply\nduplicate inbound, already handled")
    assert is_autonomous_silence_response("[SILENT] No changes detected")


def test_translated_sentinel_is_silence_in_every_form_the_english_one_is():
    """#110935: a lane that answers the cron instruction in its own language translates the
    sentinel; ``[静默]`` must suppress delivery exactly like ``[SILENT]`` (exact, own-line note,
    reordered lines, bracketless, edge punctuation)."""
    assert is_intentional_silence_response("[静默]")
    assert is_intentional_silence_response("**沉默**")
    assert is_autonomous_silence_response("[静默]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[沉默]")
    assert is_autonomous_silence_response("静默")


def test_prose_mentioning_the_translated_sentinel_is_delivered():
    assert not is_intentional_silence_response("status: 静默 means the lane is quiet")
    assert not is_autonomous_silence_response("the lane said 静默 mid-sentence and kept talking")


def test_autonomous_lane_agrees_with_interactive_lane_on_cjk_punctuation_variants():
    """A Chinese lane emits fullwidth brackets or a trailing ``。``; cron/webhook must suppress
    exactly what the interactive predicate suppresses, or the two lanes drift on the new tokens."""
    for variant in ("【静默】", "静默。", "【沉默】", "沉默。", "**[静默]**", "NO_REPLY."):
        assert is_intentional_silence_response(variant)
        assert is_autonomous_silence_response(variant) == is_intentional_silence_response(variant), variant
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
