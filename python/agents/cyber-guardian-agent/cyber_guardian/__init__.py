import os

if not os.getenv("GOOGLE_CLOUD_PROJECT"):
    import google.auth

    _, project_id = google.auth.default()
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id or ""
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

from .agent import root_agent  # noqa: E402, F401
