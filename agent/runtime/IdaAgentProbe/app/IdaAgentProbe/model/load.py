from strands.models.bedrock import BedrockModel

# PROBE-ONLY MODEL CHOICE -- see docs/decisions/ADR-001-foundation-model.md
#
# Amazon Nova Micro, via the us. cross-region inference profile. Verified
# ACTIVE in us-east-1 and confirmed invokable (bedrock-runtime converse ->
# stopReason "end_turn"). There is no `global.` variant for Nova Micro; `us.`
# is the only published profile prefix.
#
# Why not the scaffold's default: `agentcore create` hardcodes
# global.anthropic.claude-sonnet-4-5-20250929-v1:0, which is (a) a generation
# behind current Bedrock Claude and (b) gated behind an Anthropic use-case
# access form in this account, which blocked the first invoke outright.
#
# This is Step 3 -- a toolchain probe. It proves the AgentCore loop
# (create -> deploy -> invoke), not model quality. It does no real
# tool-selection and reasons over no matter state, so the cheapest model that
# returns a coherent response is the correct choice here. Nova Micro is
# ~100x cheaper than Sonnet ($0.035/$0.14 vs $3/$15 per MTok) and measurably
# faster on the smoke test (696ms vs 1518ms).
#
# THE PRODUCTION MODEL IS DELIBERATELY UNDECIDED. Do not read this as the
# choice for the real Document-Chase Agent -- reliable tool-calling is that
# agent's entire job, and small models are typically weakest exactly there.
# The evaluation (correct-tool-selection rate, guardrail adherence, cost per
# decision) is specified in ADR-001 and runs in Step 5.
MODEL_ID = "us.amazon.nova-micro-v1:0"


def load_model() -> BedrockModel:
    """Get Bedrock model client using IAM credentials."""
    return BedrockModel(model_id=MODEL_ID)
