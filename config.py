import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://sdlc:sdlc_password@localhost:5432/agentic_sdlc"
    )
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Git repo that implementation agents commit generated code into (one branch per ticket)
    WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "workspace")

    # Test execution. "docker" runs generated code in a locked-down container (default, recommended).
    # "local" runs it directly on this machine and also requires ALLOW_UNSAFE_LOCAL_RUNNER=1.
    TEST_RUNNER = os.getenv("TEST_RUNNER", "docker")
    SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "agentic-sdlc-sandbox:latest")
    TEST_TIMEOUT_SECONDS = int(os.getenv("TEST_TIMEOUT_SECONDS", "120"))

    # LLM provider. Defaults to deepseek when a DEEPSEEK_API_KEY is present, otherwise openai.
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    LLM_PROVIDER = os.getenv("LLM_PROVIDER") or ("deepseek" if os.getenv("DEEPSEEK_API_KEY") else "openai")
    _DEFAULT_MODEL = "deepseek-flash" if LLM_PROVIDER == "deepseek" else "gpt-4o"

    # Model per role (override with MODEL_PLANNING / MODEL_CRITIQUE / MODEL_IMPLEMENTATION)
    PLANNING_MODEL = os.getenv("MODEL_PLANNING", _DEFAULT_MODEL)
    CRITIQUE_MODEL = os.getenv("MODEL_CRITIQUE", _DEFAULT_MODEL)
    IMPLEMENTATION_MODEL = os.getenv("MODEL_IMPLEMENTATION", _DEFAULT_MODEL)

    # Call behaviour and spend control
    LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))          # transport-level: 429 / 5xx / timeouts
    LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "16000"))
    LLM_JSON_MODE = os.getenv("LLM_JSON_MODE", "1") == "1"            # ask the API for syntactically valid JSON
    RUN_TOKEN_CAP = int(os.getenv("RUN_TOKEN_CAP", "1000000"))        # a run stops once it has used this many tokens

    # Retry bounds
    MAX_RETRIES_PER_GATE = 3
    RETRY_BOUND_ESCALATION_THRESHOLD = 3

    # Stage timeouts (in seconds)
    STAGE_TIMEOUT = 3600  # 1 hour
