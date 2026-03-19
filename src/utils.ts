// =============================================================================
// Utility functions extracted for testability
// =============================================================================

// Game screen dimensions (Steam Deck native resolution in landscape)
export const GAME_WIDTH = 1280;
export const GAME_HEIGHT = 800;

/**
 * Format a byte count as a human-readable string (B, KB, or MB).
 * Used in the file browser to display file sizes.
 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Calculate a font size that fits N characters of text inside an area of
 * width W and height H (in game pixels).
 *
 * The formula derives from:
 *   chars_per_line ~ W / (fontSize * avgCharWidth)
 *   num_lines ~ N / chars_per_line
 *   total_height ~ num_lines * fontSize * lineHeight
 *
 * Solving for fontSize:
 *   fontSize <= sqrt( W * H / (N * fitFactor) )
 *
 * @param textLength  Number of characters to fit
 * @param region      Optional crop region {x1, y1, x2, y2} in game pixels.
 *                    If null/undefined, assumes full-screen subtitle bar.
 * @returns Font size in pixels, clamped to min/max bounds.
 */
export function calculateFontSize(
  textLength: number,
  region?: { x1: number; y1: number; x2: number; y2: number } | null,
): number {
  const FIT_FACTOR = 2.0; // conservative: ensures text fits even with word-wrap waste
  const PADDING = 12; // total inset: 4px padding + 2px border, each side

  if (region) {
    // Cropped region: fit text inside the scanned area
    const availW = region.x2 - region.x1 - PADDING;
    const availH = region.y2 - region.y1 - PADDING;
    const fontSize = Math.floor(Math.sqrt((availW * availH) / (textLength * FIT_FACTOR)));
    return Math.max(7, Math.min(16, fontSize));
  } else {
    // Full-screen: fit text inside subtitle bar (94vw x 25vh minus padding)
    const availW = GAME_WIDTH * 0.94 - 32;
    const availH = GAME_HEIGHT * 0.25 - 24;
    const fontSize = Math.floor(Math.sqrt((availW * availH) / (textLength * FIT_FACTOR)));
    return Math.max(12, Math.min(20, fontSize));
  }
}
