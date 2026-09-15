"""Environment validation and lazily cached model clients."""
import os
from functools import lru_cache
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name(".env"))


def _require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. Configure it in .env (see .env.example).")
    return value


def validate_configuration():
    for name in ("OPENAI_API_KEY", "RESUME_PARSER_MODEL"):
        _require_env(name)


@lru_cache(maxsize=1)
def get_llm_client():
    from langchain_openai import ChatOpenAI
    options = dict(model=_require_env("RESUME_PARSER_MODEL"),
                   api_key=_require_env("OPENAI_API_KEY"), timeout=45, max_retries=1)
    if os.getenv("REASONING_EFFORT", "").strip():
        options["reasoning_effort"] = os.environ["REASONING_EFFORT"].strip()
    return ChatOpenAI(**options)
