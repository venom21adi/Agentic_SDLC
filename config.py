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

    # Agent configuration
    PLANNING_MODEL = "gpt-4-turbo-preview"
    CRITIQUE_MODEL = "gpt-4-turbo-preview"
    IMPLEMENTATION_MODEL = "gpt-4-turbo-preview"

    # Retry bounds
    MAX_RETRIES_PER_GATE = 3
    RETRY_BOUND_ESCALATION_THRESHOLD = 3

    # Stage timeouts (in seconds)
    STAGE_TIMEOUT = 3600  # 1 hour
