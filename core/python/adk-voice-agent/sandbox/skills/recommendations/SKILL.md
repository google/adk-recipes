---
name: recommendations
description: How to suggest options that fit the user and don't repeat earlier ones (meals, restaurants, trips, activities, gifts, books). Use before any open-ended recommendation.
---

# Fresh recommendations

1. **Check prior context first:**
   - `<user_profile>` in your thread prompt for preferences and hard constraints (diet, allergies, budget, dates, accessibility, companions).
   - Past threads and files: `list_threads` and `read_thread` on `voice_agent`, plus files in the project folder.
   - Earlier conversations: `recall` with the topic.
2. **Select options:**
   - Treat all previously suggested options as an anti-repeat list unless the user asks to revisit one (say so if re-suggesting a past favorite on purpose).
   - Vary meaningful dimensions (cuisine, price, neighborhood, effort) rather than near-duplicates.
   - Verify live facts (hours, prices, availability) with `search_web_grounded` or `search_maps_grounded` and link sources.
3. **Record suggestions:** In a project folder, update `AGENTS.md` (e.g., "Suggested so far") so future threads don't repeat them.
