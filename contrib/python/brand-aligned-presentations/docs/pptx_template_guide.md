# PowerPoint Template Blueprint Guide

This guide explains how to create a PPTX template that allows the Presentation Expert Agent to generate flawless slides with less manual adjustment.

## The "Perfect Master" Blueprint

To ensure the agent's content maps 1:1 to your brand's design, you must create or rename layouts in your PowerPoint Slide Master to match the following specifications.

### 1. The Core 7 (Absolutely Mandatory)
These are the foundation. If these are missing, the agent will fall back to plain text rendering.

1. **Title Slide**
   - **Purpose:** The cover of the deck.
   - **Required Placeholders:** `Title`.

2. **Title and Content**
   - **Purpose:** The standard layout for bullet points and general text.
   - **Required Placeholders:** `Title`, `Content` (Text).

3. **Two Content** (or "Comparison" / "Side by Side")
   - **Purpose:** Critical for slides with two separate text contents.
   - **Required Placeholders:** `Title`, `Content` (Left), `Content` (Right).
   - **Note:** The agent uses the right-side placeholder for a second block of text.

4. **Section Header**
   - **Purpose:** Visual breaks between presentation chapters.
   - **Required Placeholders:** `Title`.

5. **Title and Image**
   - **Purpose:** To hold slides with a title, content and visual.
   - **Required Placeholders:** `Title`,`Content` (Left) ,`Picture` (Right).

6. **Title Only**
   - **Purpose:** A blank canvas for large diagrams or complex charts that need custom floating.
   - **Required Placeholders:** `Title`.

7. **Closing Slide**
   - **Purpose:** The "Thank You" or "Contact Us" slide.
   - **Required Placeholders:** `Title`, `Subtitle` (for contact info/URLs).

### 2. The Enhanced 4 (Highly Recommended)
Including these allows the agent to produce much more visually rich and professional output.

8. **Quote**
   - **Purpose:** Impactful testimonials or leadership statements.
   - **Required Placeholders:** `Title` (optional), `Content` (styled for large, impactful text).

9. **Comparison**
   - **Purpose:** Specifically designed to compare two lists of text, metrics, or images side-by-side with individual headers.
   - **Required Placeholders:** `Title`, `Text Placeholder` (Header 1), `Content Placeholder` (List 1), `Text Placeholder` (Header 2), `Content Placeholder` (List 2).

10. **Title and Chart**
    - **Purpose:** A clean layout for presenting data visualization.
    - **Required Placeholders:** `Title`, `Content` (or Chart) placeholder.

11. **Agenda**
    - **Purpose:** Used for the table of contents or roadmap at the beginning of the presentation.
    - **Required Placeholders:** `Title`, `Content` (Text).

---

## How to Build the Template in PowerPoint

Follow these steps to configure your template:

1. **Open Slide Master:** In PowerPoint, go to **View** > **Slide Master**.
2. **Rename Layouts:**
   - Hover over the layouts in the left-hand thumbnail pane to see their current names.
   - **Right-Click** a layout > **Rename Layout**.
   - Type the exact name from the list above (e.g., `Two Content`).
3. **Add Placeholders:**
   - On a selected layout, go to **Slide Master** > **Insert Placeholder**.
   - Add the specific types needed (e.g., `Text` for content, `Picture` for images).
4. **Style the Master:**
   - Apply your corporate fonts, colors, and logos to the top-most **Master Slide**. These styles will flow down to all layouts.
5. **Save the File:** Save as a `.pptx`.

## Why this matters
The agent uses an intelligent layout mapping algorithm. When it sees `layout_name: "Two Content"` in its internal plan, it searches your template for that exact string. If it matches, your custom branding is applied instantly. If it fails to find a match, it uses a fallback engine which may result in less precise visual alignment.
