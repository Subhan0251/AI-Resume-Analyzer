"""
config.py
=========
Single source of truth for environment configuration and for the shared
LLM/embedding client objects used across the whole pipeline.

Why this file exists (fixes point 3 and part of point 7 from the review):
  1. Every required environment variable is validated in exactly ONE place.
     If something is missing from .env, the process fails loudly at import
     time with a clear message - not silently, deep inside a request, as a
     confusing "model=None" style error.
  2. ChatOpenAI / OpenAIEmbeddings client objects are constructed ONCE, at
     module import time, and reused by every request across parser.py and
     matcher.py - instead of every single API request paying the (small but
     needless) cost of re-instantiating a fresh client object.

parser.py and matcher.py should import their config and clients from here
and nowhere else.
"""
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

load_dotenv()


def _require_env(name: str) -> str:
    """
    Read a required environment variable; raise immediately with a clear
    message if it's missing, rather than letting a blank/None value silently
    propagate into a client constructor somewhere downstream.
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to your .env file (see .env.example)."
        )
    return value


# ---- Required configuration -------------------------------------------------
OPENAI_API_KEY = _require_env("OPENAI_API_KEY")
RESUME_PARSER_MODEL = _require_env("RESUME_PARSER_MODEL")
EMBEDDING_MODEL = _require_env("EMBEDDING_MODEL")

# ---- Optional configuration ---------------------------------------------
# GPT-5-family models default to "medium" reasoning effort, which spends a
# meaningful number of hidden reasoning tokens before answering - this is a
# major, easy-to-miss source of latency for extraction-style tasks (parsing,
# classification, structured output) that don't need deep multi-step
# reasoning. "low" cuts that latency substantially with negligible quality
# loss for this kind of task. Override via .env if you want to tune it.
REASONING_EFFORT = os.getenv("REASONING_EFFORT")

# ---- Shared clients (built once, reused everywhere) --------------------
llm_client = ChatOpenAI(
    model=RESUME_PARSER_MODEL,
    api_key=OPENAI_API_KEY,
    temperature=0,
    reasoning_effort=REASONING_EFFORT,
)

embedding_client = OpenAIEmbeddings(
    model=EMBEDDING_MODEL,
    api_key=OPENAI_API_KEY,
)