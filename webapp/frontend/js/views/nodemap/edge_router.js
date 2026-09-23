/**
 * edge_router.js - Cubic Bezier & Orthogonal Edge Routing for NodeMap.
 */

export class EdgeRouter {
  /**
   * Calculate cubic bezier control points between two port anchor coordinates.
   */
  static computeBezierPath(x1, y1, x2, y2) {
    const dx = Math.abs(x2 - x1);
    const curvature = Math.max(40, dx * 0.45);

    const cp1x = x1 + curvature;
    const cp1y = y1;
    const cp2x = x2 - curvature;
    const cp2y = y2;

    return {
      svgPath: `M ${x1} ${y1} C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${x2} ${y2}`,
      points: [
        { x: x1, y: y1 },
        { x: cp1x, y: cp1y },
        { x: cp2x, y: cp2y },
        { x: x2, y: y2 }
      ]
    };
  }
}
