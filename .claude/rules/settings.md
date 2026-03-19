# Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Master switch — stops workers, playback, pipeline when disabled |
| `debug` | `false` | Enables `DEBUG` log level (no restart needed) |
| `ocr_provider` | `"local"` | `"gcp"` or `"local"` |
| `ocr_language` | `"english"` | OCR recognition language: english, chinese, korean, latin, eslav, thai, greek |
| `tts_provider` | `"local"` | `"gcp"` or `"local"` |
| `voice_id` | `"en-US-Neural2-C"` | GCP Neural2 voice |
| `speech_rate` | `"medium"` | GCP speech rate |
| `local_voice_id` | `"en_US-amy-medium"` | Piper voice (auto-downloads on first use) |
| `local_speech_rate` | `"medium"` | Piper speech rate |
| `volume` | `100` | TTS volume 0-100 |
| `trigger_button` | `"L4"` | Hidraw button: None/L4/R4/L5/R5 |
| `hold_time_ms` | `500` | Button hold threshold |
| `touch_input_enabled` | `false` | Enable touchscreen gestures (swipe or two-tap) for OCR region selection |
| `touch_input_style` | `"two_tap"` | Touch gesture style: `"swipe"` (drag to select) or `"two_tap"` (tap two corners) |
| `mute_interface_sounds` | `false` | Disable/enable playback of UI feedback sounds |
| `hide_pipeline_toast` | `false` | Hide on-screen toast overlay with pipeline status |
| `show_text_overlay` | `false` | Show spoken text + region border overlay instead of word count pill |
| `fixed_region_x1` | `0` | Fixed region left X coordinate |
| `fixed_region_y1` | `0` | Fixed region top Y coordinate |
| `fixed_region_x2` | `1280` | Fixed region right X coordinate |
| `fixed_region_y2` | `800` | Fixed region bottom Y coordinate |
| `last_selection_x1` | `0` | Last selection left X (auto-saved) |
| `last_selection_y1` | `0` | Last selection top Y (auto-saved) |
| `last_selection_x2` | `1280` | Last selection right X (auto-saved) |
| `last_selection_y2` | `800` | Last selection bottom Y (auto-saved) |
| `ignored_words_always` | `""` | Comma-separated words to remove anywhere in text |
| `ignored_words_always_enabled` | `false` | Enable/disable the "always ignore" list |
| `ignored_words_beginning` | `""` | Comma-separated words to remove from start of text |
| `ignored_words_beginning_enabled` | `false` | Enable/disable the "ignore at beginning" list |
| `ignored_words_count` | `3` | How many leading words to check for "beginning" list |
| `translation_enabled` | `false` | Enable translation between OCR and TTS |
| `translation_target_language` | `"en"` | ISO 639-1 target language code for translation |
