/**
 * text_coordinates.js - UTF-8 Byte Offset to JavaScript Character Index Mapping.
 *
 * Converts 1-based byte column offsets stored in database spatial coordinates
 * (e.g. m_bridge_tag, m_tag_map, m_blob_symbol_ref) to 0-based JavaScript character
 * offsets for DOM manipulation and syntax highlighting.
 */

/**
 * Builds a mapping from 1-based UTF-8 byte columns to 0-based JavaScript character indices.
 *
 * Handles multi-byte UTF-8 sequences (2-byte, 3-byte, and 4-byte surrogate pairs in UTF-16).
 *
 * @param {string} lineText - The line text to map.
 * @returns {number[]} Array where index `byteCol` (1-based) is the 0-based JS character index.
 */
export function buildByteToCharMap(lineText) {
  if (typeof lineText !== "string") {
    return [0, 0];
  }
  const len = lineText.length;
  // 0-th index is unused/padding so 1-based byte column indexing aligns directly.
  const map = [0];
  let charIdx = 0;

  while (charIdx < len) {
    const code = lineText.charCodeAt(charIdx);
    let byteLen = 1;
    let codeUnits = 1;

    if (code <= 0x7f) {
      byteLen = 1;
    } else if (code <= 0x7ff) {
      byteLen = 2;
    } else if (code >= 0xd800 && code <= 0xdbff && charIdx + 1 < len) {
      // 4-byte UTF-8 sequence represented as a surrogate pair (2 UTF-16 code units)
      byteLen = 4;
      codeUnits = 2;
    } else {
      byteLen = 3;
    }

    for (let b = 0; b < byteLen; b++) {
      map.push(charIdx);
    }
    charIdx += codeUnits;
  }

  // Push EOF position (charIdx = len) to handle past-the-end / trailing column coordinates cleanly
  map.push(len);
  return map;
}

/**
 * Resolves a 1-based database byte column to a 0-based JS character index using a prebuilt map.
 *
 * Clamps coordinates to valid boundaries:
 * - byteCol <= 1 resolves to character index 0.
 * - byteCol past end-of-line resolves to the clamped end character index.
 *
 * @param {number[]} byteToCharMap - Mapping array produced by buildByteToCharMap.
 * @param {number} byteCol - 1-based byte column offset.
 * @returns {number} 0-based character index in the string.
 */
export function byteColToCharIdx(byteToCharMap, byteCol) {
  if (!Array.isArray(byteToCharMap) || byteToCharMap.length <= 1) {
    return 0;
  }
  const col = Math.floor(Number(byteCol) || 0);
  if (col <= 1) {
    return byteToCharMap[1] ?? 0;
  }
  if (col >= byteToCharMap.length) {
    return byteToCharMap[byteToCharMap.length - 1];
  }
  return byteToCharMap[col];
}
