---
name: deliverables
description: How to write a document the user will read (report, itinerary, comparison, plan, notes) and save it to their Google Drive. Use when a thread's result is a document rather than a code change.
---

# Document deliverables

- **File format:** Write plain Markdown in your working directory named after the subject (`alaska-itinerary.md`, never `report.md` or `output.md`; use CSV for reusable tables). Start with a `#` title, stay under 14 KB, and avoid HTML or local image paths.
- **Content:** Lead with the recommendation or answer, state key assumptions near the top, compare options in tables, and link sources inline with dates for time-sensitive facts (prices, hours, availability).
- **Iterative edits:** Update existing files in place rather than creating `-v2` copies; git tracks history and `save_to_drive` updates the same Drive file when given the same `path`.
- **Saving to Drive:** When `save_to_drive` is listed on `voice_agent`, call it once the deliverable is final with `path` (relative to your working directory) and the `#` title as `title` (Markdown → Doc, CSV → Sheet, `.pptx` → Slides). Never save scratch notes or intermediate files, and never upload deliverables via `google_drive__` tools (use `present_card` only to list existing Drive files, events, or email threads).
- **Reply as a cover note:** Because the user sees the Drive link next to your reply, do not paste or summarize the document or mention file names/paths—state only assumptions, what to double-check, and at most one question.
