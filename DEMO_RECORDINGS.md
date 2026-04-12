# Demo Recordings Plan

Essential short MP4 recordings to showcase plugin functionality.

## Core Demos

1. **`01-basic-reading.mp4`** (~15s)
   Button press (L4) → toast "Reading..." → OCR → TTS audio plays → "N words read" toast. Full pipeline in a text-heavy game.

2. **`02-touch-two-tap.mp4`** (~15s)
   Two taps to select a region → region preview overlay flashes → OCR/TTS reads just that region.

3. **`03-touch-swipe.mp4`** (~15s)
   Swipe to select a text box → reads the selected area. Shows the alternative touch input style.

4. **`04-fixed-region.mp4`** (~15s)
   Region sliders adjusted in QAM → overlay preview shows the crop area → L4 reads only that region. Good for dialog boxes that stay in one spot.

5. **`05-stop-playback.mp4`** (~10s)
   TTS playing → tap screen or press button again → playback stops immediately with stop sound.

## Provider & Language

6. **`06-local-vs-gcp.mp4`** (~20s)
   Side-by-side comparison: switch provider in QAM, trigger same screen. Shows speed/quality difference.

7. **`07-ocr-language.mp4`** (~15s)
   Japanese game → select Japanese OCR language (shows download if first time) → reads Japanese text.

8. **`08-translation.mp4`** (~20s)
   Japanese game → OCR (Japanese) → translate JA→EN → TTS reads English translation. The killer feature for foreign-language games.

## Settings & Customization

9. **`09-qam-panel-walkthrough.mp4`** (~25s)
   Quick scroll through the full QAM panel: enabled toggle, provider selection, voice picker, capture settings, translation, text filtering, debug mode.

10. **`10-voice-selection.mp4`** (~15s)
    Switch between 2-3 local voices (different accents/genders), trigger same text. Shows on-demand voice download.

11. **`11-text-overlay.mp4`** (~15s)
    Enable "Show spoken text" → trigger pipeline → spoken text appears overlaid on screen with region border, stays during playback, auto-dismisses.

12. **`12-text-filtering.mp4`** (~15s)
    Add character name to "always ignore" list → trigger → name is stripped from TTS output (no more hearing "Narrator:" every line).

## Setup

13. **`13-install-and-first-run.mp4`** (~20s)
    Install from Decky store (or sideload zip) → open QAM → plugin ready with local provider → first button press triggers model download → reads screen.

14. **`14-gcp-credentials.mp4`** (~15s)
    Upload GCP service account JSON via file browser → validation → switch to GCP provider. (Only needed if users want GCP quality.)

---

**Total: 14 clips, ~3.5 minutes combined.** Recordings 1-5 are the must-haves for a README. 6-12 are for a "Features" section or wiki. 13-14 cover onboarding.
