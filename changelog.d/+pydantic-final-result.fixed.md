**Pydantic AI structured-output agents explicitly require `final_result`** —
the generated output tool now tells models that they must call it before
finishing and that plain-text responses are not accepted. This improves
adherence for local models behind OpenAI- or Anthropic-compatible endpoints
without replacing tool-based output, weakening schema validation, or
changing authored system prompts.
