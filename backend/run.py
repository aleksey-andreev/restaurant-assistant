import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import uvicorn


def main() -> None:
    backend_root = Path(__file__).resolve().parent
    project_root = backend_root.parent

    # Ensure backend/ is on PYTHONPATH so that `app` package is importable
    sys.path.insert(0, str(backend_root))

    # Load environment variables from .env in the project root, if present
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)

    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=True,
    )


if __name__ == "__main__":
    main()

