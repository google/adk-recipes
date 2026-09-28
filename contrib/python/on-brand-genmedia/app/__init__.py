import os

import google.auth
from dotenv import load_dotenv

# Load variables from .env if present. In production the environment is
# already populated by the platform (Cloud Run, GKE, etc.), so a missing
# .env is expected and not an error.
load_dotenv()

_, project_id = google.auth.default()
if project_id:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

from . import agent as agent  # noqa: E402
