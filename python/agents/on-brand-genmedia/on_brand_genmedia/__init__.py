import os

import google.auth

_, project_id = google.auth.default()
if project_id:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

from . import agent as agent  # noqa: E402
