# InkRef — Project Specification

## 1. Overview

**InkRef** is an iPad-first handwriting formatting tool that cleans and restructures digital handwritten notes while preserving the user's original handwriting as editable ink.

Instead of converting handwriting into typed text, InkRef manipulates the geometry of the original pen strokes.

The initial integration targets **Goodnotes** documents.

The core idea is:

> **Prettier for handwriting.**

A user writes notes normally in Goodnotes, sends a page or document to InkRef, applies automatic formatting, previews the result, and sends the cleaned document back to Goodnotes.

The output should remain native, editable handwriting rather than a PDF, image, or font recreation.

---

# 2. Problem

Digital handwriting apps preserve the freedom of paper, but they also inherit many of its formatting problems.

Handwritten notes commonly suffer from:

- uneven baselines
- inconsistent spacing
- drifting margins
- cramped sections
- inconsistent line height
- poorly aligned bullets
- headings that do not visually stand out
- diagrams that become messy
- arrows and connectors that do not line up
- insufficient room when adding information later

Users can manually move handwritten objects, but cleaning up an entire page is tedious.

Existing AI note tools usually solve a different problem:

**handwriting → OCR → typed text**

That destroys one of the main reasons users chose handwriting in the first place.

InkRef instead performs:

**handwriting → structured handwriting → cleaner handwriting**

---

# 3. Product Vision

InkRef should eventually become a general-purpose transformation engine for editable digital ink.

Goodnotes is the first supported format, not the entire product.

Long-term:

```text
Goodnotes ─────┐
Notability ────┤
Apple Notes ───┼──► InkRef Engine ───► Editable formatted ink
Other formats ─┘
```

The engine should conceptually understand handwritten content as structured objects:

```text
Document
 ├── Page
 │    ├── Text Block
 │    │    ├── Line
 │    │    │    ├── Word
 │    │    │    │    └── Strokes
 │    │
 │    ├── Heading
 │    ├── Bullet List
 │    ├── Equation
 │    ├── Diagram
 │    └── Freeform Drawing
```

The underlying pen strokes remain the source of truth.

---

# 4. Primary User

A student who:

- uses an iPad and Apple Pencil for school
- takes handwritten lecture notes
- prefers handwriting over typed notes
- cares about organization and aesthetics
- does not want AI replacing their handwriting
- wants notes to look cleaner after writing them

InkRef is primarily a **post-note-taking tool**.

It does not need to operate continuously while the user is writing.

---

# 5. Core User Experience

## Basic Flow

```text
Goodnotes
   ↓
Export / Share
   ↓
InkRef
   ↓
Analyze handwriting
   ↓
Preview proposed formatting
   ↓
Beautify
   ↓
Export
   ↓
Open / Import into Goodnotes
```

The process should feel closer to sending a photo through Lightroom than manually converting files.

---

# 6. Main Interaction

The initial InkRef screen should be extremely simple.

Example:

```text
┌─────────────────────────────┐
│        InkRef          │
│                             │
│      Lecture 07             │
│       12 pages              │
│                             │
│      [ Preview ]            │
│                             │
│ Formatting Strength         │
│                             │
│  ○ Light                    │
│  ● Balanced                 │
│  ○ Strong                   │
│                             │
│   [ Beautify Notes ]        │
└─────────────────────────────┘
```

After processing:

```text
┌─────────────────────────────┐
│        Before | After       │
│                             │
│      page preview           │
│                             │
│   ← swipe comparison →      │
│                             │
│ [Undo]     [Apply Changes]  │
│                             │
│    [Open in Goodnotes]      │
└─────────────────────────────┘
```

The user should never have to understand how the underlying file format works.

---

# 7. Core Principle

## Preserve Identity

InkRef should modify the **layout of handwriting**, not rewrite the handwriting itself.

For example, if the user wrote:

```text
Neural networks learn weights
```

InkRef should keep the same strokes that form those letters.

It may:

- translate the word
- scale it slightly
- rotate it slightly
- change spacing between words
- align it to a baseline

It should not normally regenerate the letters.

This guarantees that the output still looks like the user wrote it.

---

# 8. Hackathon MVP

The MVP should intentionally focus on a narrow set of transformations that are visually impressive and technically reliable.

## Required Features

### 8.1 Import Goodnotes Document

Accept a Goodnotes document exported by the user.

The application must:

1. receive the document
2. unpack/parse the relevant document structure
3. locate pages
4. locate editable pen strokes
5. recover stroke geometry
6. retain enough metadata to reconstruct the document

The original document must never be destructively modified.

---

### 8.2 Render Page Preview

Render handwriting inside InkRef.

The preview should approximately reproduce:

- stroke positions
- stroke size
- page dimensions
- ink colors
- page background if practical

Exact visual parity with Goodnotes is not required for the MVP.

Stroke geometry accuracy is required.

---

### 8.3 Detect Handwritten Lines

Group strokes spatially into approximate handwritten lines.

Example:

```text
raw strokes
   ↓
connected characters
   ↓
words
   ↓
lines
```

The first version does not require perfect handwriting recognition.

Useful signals include:

- stroke bounding boxes
- vertical overlap
- horizontal distance
- estimated writing height
- baseline proximity
- local stroke density

---

### 8.4 Baseline Alignment

Detect the approximate baseline of a handwritten line and move words vertically so the line appears cleaner.

Before:

```text
this is a handwritten
  line with uneven words
```

After:

```text
this is a handwritten
line with uneven words
```

Individual characters should not be altered unnecessarily.

Prefer moving a whole word or stroke group together.

---

### 8.5 Normalize Word Spacing

Detect unusually cramped or excessive gaps between words.

Reposition word groups horizontally to achieve more consistent spacing.

Do not modify the geometry of the letters themselves.

---

### 8.6 Normalize Line Spacing

Detect handwritten text lines and adjust their vertical position so consecutive lines use a more consistent spacing.

The algorithm must preserve ordering.

---

### 8.7 Margin Alignment

Estimate the dominant left margin of a text block.

Bring obvious outlier lines closer to that margin.

Do not force every line to begin at exactly the same X coordinate.

Natural handwriting variation should remain.

---

### 8.8 Formatting Strength

Provide three formatting strengths.

#### Light

Only fix obvious irregularities.

Examples:

- large baseline errors
- extreme gaps
- accidental line drift

#### Balanced

Default.

Improves:

- baselines
- word spacing
- line spacing
- margins

while preserving natural variation.

#### Strong

Produces a noticeably structured page.

Useful primarily for demonstrations and users who want particularly neat notes.

---

### 8.9 Before / After Comparison

The user must be able to compare the transformation.

Possible interface:

- toggle
- side-by-side
- draggable before/after slider

A transformation product needs a strong visual comparison because this is the core demonstration of value.

---

### 8.10 Export Back to Goodnotes

Reconstruct the Goodnotes document with transformed stroke coordinates.

The resulting document should:

- import successfully
- render correctly
- retain individual editable strokes
- remain erasable
- remain lasso-selectable where supported by the file format
- preserve pages not modified by InkRef

This is the most important technical requirement.

---

# 9. Hackathon Demo

The ideal demo should take less than two minutes.

## Demo Sequence

### Step 1 — Write Intentionally Messy Notes

On an iPad in Goodnotes:

```text
Machine Learning

Supervised Learning
 - classification
    - neural networks
 - regression

Unsupervised Learning
     clustering
```

Include:

- inconsistent alignment
- uneven spacing
- a cramped section
- a crooked heading
- perhaps a simple diagram

---

### Step 2 — Share to InkRef

Export/share the Goodnotes document.

Open it in InkRef.

---

### Step 3 — Show Analysis

Display detected structure.

For demo purposes, optionally visualize:

- line bounding boxes
- baselines
- text groups
- detected margins

This demonstrates that InkRef understands the spatial structure of the notes rather than merely applying an image filter.

---

### Step 4 — Beautify

Press:

**Beautify**

Animate stroke groups moving into position.

This should be one of the most visually impressive parts of the project.

---

### Step 5 — Before / After

Show the original and formatted notes.

The handwriting should visibly be the same handwriting.

---

### Step 6 — Return to Goodnotes

Export the modified file.

Open it in Goodnotes.

---

### Step 7 — Prove Native Ink

Use Goodnotes' native tools to:

- erase part of a letter
- lasso a word
- move a transformed object

This proves the output is not:

- a screenshot
- an image
- a PDF
- a font
- regenerated handwriting

The final demo message:

> InkRef doesn't replace your handwriting. It understands its structure and refactors it.

---

# 10. System Architecture

Recommended conceptual architecture:

```text
                    ┌──────────────────────┐
                    │      iPad App        │
                    │                      │
                    │ Import / Preview UI  │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │   Document Parser    │
                    │                      │
                    │ Goodnotes → strokes  │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │  Ink Representation  │
                    │                      │
                    │ Pages                │
                    │ Strokes              │
                    │ Bounding Boxes       │
                    │ Metadata             │
                    └──────────┬───────────┘
                               │
                               ▼
               ┌───────────────────────────────┐
               │       Structure Engine        │
               │                               │
               │ stroke grouping               │
               │ word grouping                 │
               │ line detection                │
               │ block detection               │
               │ semantic classification       │
               └──────────────┬────────────────┘
                              │
                              ▼
               ┌───────────────────────────────┐
               │       Layout Optimizer        │
               │                               │
               │ baseline alignment            │
               │ spacing                       │
               │ margins                       │
               │ transforms                    │
               └──────────────┬────────────────┘
                              │
                              ▼
                    ┌──────────────────────┐
                    │ Document Serializer  │
                    │                      │
                    │ strokes → Goodnotes  │
                    └──────────┬───────────┘
                               │
                               ▼
                          Goodnotes
```

---

# 11. Internal Ink Model

Do not allow the Goodnotes file format to leak throughout the application.

Convert imported data into an internal representation.

Example conceptual model:

```text
InkDocument
    pages[]

InkPage
    width
    height
    strokes[]
    elements[]

InkStroke
    id
    points[]
    color
    width
    metadata

InkPoint
    x
    y
    pressure?
    timestamp?

InkGroup
    strokeIds[]
    boundingBox
    type
    confidence

InkLine
    groups[]
    baseline
    boundingBox

InkBlock
    lines[]
    boundingBox
    type
```

All beautification algorithms operate on this model.

Goodnotes-specific parsing and serialization should live at the boundaries.

---

# 12. Stroke Transformations

Every transformation should eventually reduce to operations on stroke coordinates.

## Translation

```text
x' = x + dx
y' = y + dy
```

Used for:

- baseline alignment
- margin correction
- word spacing
- line spacing

---

## Scaling

Around the stroke group's center:

```text
x' = cx + sx(x - cx)
y' = cy + sy(y - cy)
```

Used sparingly for unusually large/small writing.

---

## Rotation

Around a group center:

```text
x' = cosθ(x-cx) - sinθ(y-cy) + cx
y' = sinθ(x-cx) + cosθ(y-cy) + cy
```

Potentially useful for obvious slant at the word/line level.

This should not be a major MVP feature.

---

# 13. Grouping Strategy

The initial implementation should avoid depending entirely on AI.

Geometry should perform most structural grouping.

Possible pipeline:

```text
strokes
   ↓
stroke bounding boxes
   ↓
spatial clustering
   ↓
candidate characters / words
   ↓
candidate lines
   ↓
text blocks
```

Useful heuristics:

### Same-line likelihood

Two stroke groups are likely on the same line when:

- their Y ranges overlap significantly
- their vertical centers are similar
- their horizontal distance is reasonable
- their approximate heights are similar

### Same-word likelihood

Groups are likely part of the same word if:

- horizontal gap is small relative to character height
- vertical position is similar

### New-word likelihood

A larger horizontal gap suggests a word boundary.

---

# 14. Role of AI

AI should assist the system rather than replace the geometry engine.

Potential uses:

### Structure Classification

Given rendered regions, classify them as:

- paragraph
- heading
- bullet list
- equation
- diagram
- annotation
- drawing

### OCR Assistance

OCR may help determine:

- where words begin/end
- semantic groupings
- headings
- list structure

OCR output does **not** replace the handwritten ink.

### Layout Suggestions

A model may suggest:

```text
Heading
↓
Paragraph
↓
Bullet List
↓
Diagram
```

The deterministic layout engine then performs actual stroke transformations.

---

# 15. Safety and Data Integrity

InkRef modifies personal notes, so destructive behaviour is unacceptable.

Requirements:

- preserve the original imported file
- transformations happen on a copy
- preserve unknown document fields whenever possible
- do not rewrite unrelated pages
- maintain stable stroke IDs where possible
- generate new IDs only when required
- validate reconstructed files before export
- expose Undo before final export

If parsing encounters unsupported document structures, prefer leaving those structures unchanged.

---

# 16. MVP Non-Goals

Do **not** attempt all of these during the hackathon:

- handwriting generation
- replacing handwriting with fonts
- perfect handwriting recognition
- live editing inside Goodnotes
- Goodnotes plugin integration
- real-time formatting while writing
- Notability support
- Apple Notes support
- collaborative editing
- cloud sync
- account system
- handwriting-to-LaTeX
- handwriting style transfer
- full semantic note rewriting
- automatic summarization
- study chatbot
- flashcard generation

Those features dilute the core idea.

The project should win because it manipulates editable handwriting unusually well.

---

# 17. Stretch Features

Only begin these if the complete Goodnotes round trip is stable.

## 17.1 Smart Headings

Detect headings and:

- center them
- increase spacing around them
- optionally scale them slightly

while retaining the original handwriting.

---

## 17.2 Bullet Alignment

Detect bullet lists and align:

```text
• item
• item
• item
```

including consistent indentation.

---

## 17.3 Diagram Cleanup

Recognize:

- boxes
- arrows
- nodes
- connectors

Then align and distribute them.

Example:

```text
messy:

 [A]
    \
      [B]
  /
[C]


clean:

[A] ───► [B]
          ▲
          │
         [C]
```

Original handwritten labels remain unchanged.

---

## 17.4 Smart Reflow

Allow the user to select an area and choose:

**Insert Space**

Everything below it moves downward automatically.

Example use:

```text
Original

Neural Networks
Backpropagation
Optimizers


Insert space after Backpropagation


Neural Networks
Backpropagation



Optimizers
```

The user can now add more handwritten content without manually moving dozens of objects.

This could become one of InkRef's most important long-term features.

---

## 17.5 Content-Aware Reflow

Move blocks intelligently rather than purely vertically.

Example:

```text
Text        Diagram
Text        Diagram
Text
```

Adding another paragraph should reposition surrounding objects while preserving the logical layout.

---

# 18. Suggested Repository Structure

```text
inkref/
│
├── app/
│   ├── ui/
│   ├── import/
│   ├── export/
│   └── preview/
│
├── core/
│   ├── ink/
│   │   ├── document
│   │   ├── page
│   │   ├── stroke
│   │   └── geometry
│   │
│   ├── grouping/
│   │   ├── words
│   │   ├── lines
│   │   └── blocks
│   │
│   ├── formatting/
│   │   ├── baselines
│   │   ├── word_spacing
│   │   ├── line_spacing
│   │   ├── margins
│   │   └── optimizer
│   │
│   └── analysis/
│
├── formats/
│   └── goodnotes/
│       ├── parser
│       ├── serializer
│       ├── compression
│       └── schema
│
├── ai/
│   ├── ocr
│   └── classification
│
└── tests/
    ├── fixtures/
    ├── roundtrip/
    ├── geometry/
    └── formatting/
```

Exact language-specific extensions are intentionally omitted.

The architecture matters more than the file naming.

---

# 19. Critical Technical Tests

The project should have explicit round-trip tests.

## Test 1 — No-op Round Trip

```text
Goodnotes file
→ parse
→ serialize without modification
→ Goodnotes
```

Result should visually match the original.

---

## Test 2 — Translate Stroke

Move one stroke by a fixed offset.

Verify it appears at the expected location.

---

## Test 3 — Translate Word Group

Move multiple strokes together.

Verify internal geometry remains unchanged.

---

## Test 4 — Full Page Formatting

Transform multiple groups.

Verify:

- page imports
- strokes render
- all strokes remain individually editable
- no corruption occurs

---

## Test 5 — Unsupported Objects

Import a page containing something InkRef does not understand.

The unknown object should survive the round trip unchanged.

---

# 20. Success Criteria

The hackathon MVP is successful if the following sequence works reliably:

```text
1. Write messy handwritten notes in Goodnotes.

2. Export them.

3. Open them in InkRef.

4. InkRef automatically identifies handwritten lines.

5. Press Beautify.

6. Baselines, spacing, and alignment visibly improve.

7. Preview the difference.

8. Export the transformed file.

9. Open it in Goodnotes.

10. The handwriting remains editable native ink.
```

Everything beyond this is secondary.

---

# 21. Product Differentiation

InkRef should never be marketed simply as:

> AI that makes notes prettier.

That description undersells the project and makes it sound generic.

The core differentiation is:

> **InkRef treats handwriting as structured, editable geometry.**

Traditional AI:

```text
handwriting
     ↓
    OCR
     ↓
typed text
```

InkRef:

```text
handwriting
     ↓
structural understanding
     ↓
layout optimization
     ↓
same handwriting
```

---

# 22. Hackathon Positioning

Best-fit tracks:

### Primary
**Apps**

InkRef is a polished user-facing iPad application.

### Secondary
**Design**

The central problem is fundamentally a user-interface and document-layout problem.

### Secondary
**Machine Learning / AI**

AI can assist in recognizing document structure while deterministic geometry preserves the original writing.

The project should avoid presenting itself as an AI wrapper.

The technical story is stronger:

> We built a system that understands handwritten notes as structured objects and modifies the underlying editable ink.

---

# 23. Suggested Pitch

### One sentence

**InkRef is Prettier for handwriting: it cleans the layout of handwritten digital notes while preserving every stroke as editable ink.**

### Short Pitch

People use an Apple Pencil because they want their notes to remain handwritten. But once those notes become messy, existing AI tools usually solve the problem by converting them into typed text.

InkRef takes a different approach.

It understands the spatial structure of a handwritten page—words, lines, margins, headings, and diagrams—and reorganizes the original pen strokes themselves.

Your handwriting stays yours.

It just gets refactored.

---

# 24. Product Philosophy

InkRef should follow one rule:

> **Improve the structure, preserve the expression.**

The imperfections inside someone's handwriting are part of its identity.

The mess around the handwriting is what InkRef fixes.