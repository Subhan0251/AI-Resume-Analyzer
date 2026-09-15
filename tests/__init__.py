import os

# Offline tests must never export synthetic documents or traces to external services.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
