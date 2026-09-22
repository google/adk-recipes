"""Live check: is the Vertex context cache actually forming across turns?

Reads what the model reports, not what we hope: usage_metadata's
cached_content_token_count plus ADK's own event.cache_metadata.
"""

import asyncio

from google.genai import types

from horizon.fast_api_app import build_runner

USER = "cache_probe_user"
TURNS = ["hi", "what is 2+2?", "and 3+3?"]


async def main() -> None:
    runner = build_runner()
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id=USER
    )

    for i, prompt in enumerate(TURNS):
        cached = prompt_toks = 0
        meta_seen = []
        async for event in runner.run_async(
            user_id=USER,
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=prompt)]
            ),
        ):
            um = getattr(event, "usage_metadata", None)
            if um:
                cached = max(cached, um.cached_content_token_count or 0)
                prompt_toks = max(prompt_toks, um.prompt_token_count or 0)
            cm = getattr(event, "cache_metadata", None)
            if cm is not None:
                meta_seen.append(cm)

        pct = (100.0 * cached / prompt_toks) if prompt_toks else 0.0
        print(
            f"turn {i} ({prompt!r:16}) prompt={prompt_toks:>6}  "
            f"cached={cached:>6}  ({pct:5.1f}% of prompt)"
        )
        for cm in meta_seen[:1]:
            fields = {
                k: getattr(cm, k, None)
                for k in (
                    "cache_name",
                    "invocations_used",
                    "cached_contents_count",
                    "expire_time",
                )
                if getattr(cm, k, None) is not None
            }
            print(f"         cache_metadata: {fields}")


if __name__ == "__main__":
    asyncio.run(main())
