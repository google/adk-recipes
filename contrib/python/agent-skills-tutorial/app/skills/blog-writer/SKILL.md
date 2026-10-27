---
name: blog-writer
description: Blog post writing skill with structure templates and style guidelines. Guides the agent through writing well-structured, engaging technical blog posts with proper formatting, section flow, and reader engagement techniques.
---

# Blog Writer Instructions

When asked to write a blog post, follow these steps:

## Step 1: Structure
Use `load_skill_resource` to read `references/style-guide.md` for the writing style rules.

## Step 2: Outline First
Before writing, create a brief outline with:
- **Hook**: Opening that grabs attention (question, bold claim, or relatable problem)
- **Context**: Why this topic matters now
- **Core sections**: 3-5 sections that build on each other
- **Takeaway**: What the reader walks away knowing

## Step 3: Write Each Section
For each section:
1. Start with a clear subheading (H2)
2. Lead with the key point, then support it
3. Include code examples where relevant (use fenced code blocks with language tags)
4. Keep paragraphs to 2-3 sentences
5. Use bullet lists for steps or comparisons

## Step 4: Polish
- Add transition sentences between sections
- Ensure consistent tone throughout
- Verify all code examples are complete and runnable
- End with a clear call-to-action or next steps
