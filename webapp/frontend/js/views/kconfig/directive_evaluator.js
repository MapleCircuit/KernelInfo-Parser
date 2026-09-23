/**
 * directive_evaluator.js - C Preprocessor #ifdef Conditional Dimming Evaluator.
 * Computes active/inactive line ranges based on current KConfig symbol assignments.
 */

export class DirectiveEvaluator {
  constructor(assignments = {}) {
    this.assignments = assignments;
  }

  setAssignments(assignments) {
    this.assignments = assignments;
  }

  isSymbolActive(symbolName) {
    const s = symbolName.startsWith("CONFIG_") ? symbolName : `CONFIG_${symbolName}`;
    const val = this.assignments[s] || this.assignments[symbolName];
    return val === "y" || val === "m" || Boolean(val && val !== "n");
  }

  /**
   * Scan raw code lines and return a Set of 1-indexed line numbers that are inactive/dimmed.
   */
  evaluateDimmedLines(rawLines) {
    const dimmedLines = new Set();
    const branchStack = []; // Stack of { active: boolean, wasHandled: boolean }

    for (let idx = 0; idx < rawLines.length; idx++) {
      const lineNo = idx + 1;
      const trimmed = (rawLines[idx] || "").trim();

      if (trimmed.startsWith("#")) {
        const ifdefMatch = trimmed.match(/^#\s*ifdef\s+(\w+)/);
        const ifndefMatch = trimmed.match(/^#\s*ifndef\s+(\w+)/);
        const ifMatch = trimmed.match(/^#\s*if\s+(.+)/);
        const elifMatch = trimmed.match(/^#\s*elif\s+(.+)/);
        const elseMatch = trimmed.match(/^#\s*else/);
        const endifMatch = trimmed.match(/^#\s*endif/);

        if (ifdefMatch) {
          const parentActive = branchStack.length === 0 || branchStack[branchStack.length - 1].active;
          const condActive = this.isSymbolActive(ifdefMatch[1]);
          const currentActive = parentActive && condActive;
          branchStack.push({ active: currentActive, wasHandled: currentActive, parentActive });
          continue;
        }

        if (ifndefMatch) {
          const parentActive = branchStack.length === 0 || branchStack[branchStack.length - 1].active;
          const condActive = !this.isSymbolActive(ifndefMatch[1]);
          const currentActive = parentActive && condActive;
          branchStack.push({ active: currentActive, wasHandled: currentActive, parentActive });
          continue;
        }

        if (ifMatch) {
          const parentActive = branchStack.length === 0 || branchStack[branchStack.length - 1].active;
          const condActive = this.evalExpression(ifMatch[1]);
          const currentActive = parentActive && condActive;
          branchStack.push({ active: currentActive, wasHandled: currentActive, parentActive });
          continue;
        }

        if (elifMatch && branchStack.length > 0) {
          const top = branchStack[branchStack.length - 1];
          if (!top.wasHandled && top.parentActive) {
            const condActive = this.evalExpression(elifMatch[1]);
            top.active = condActive;
            if (condActive) top.wasHandled = true;
          } else {
            top.active = false;
          }
          continue;
        }

        if (elseMatch && branchStack.length > 0) {
          const top = branchStack[branchStack.length - 1];
          top.active = !top.wasHandled && top.parentActive;
          continue;
        }

        if (endifMatch && branchStack.length > 0) {
          branchStack.pop();
          continue;
        }
      }

      // If inside an inactive branch, mark line as dimmed
      if (branchStack.length > 0 && !branchStack[branchStack.length - 1].active) {
        dimmedLines.add(lineNo);
      }
    }

    return dimmedLines;
  }

  evalExpression(expr) {
    const definedMatch = expr.match(/defined\s*\(\s*(\w+)\s*\)/);
    if (definedMatch) {
      return this.isSymbolActive(definedMatch[1]);
    }
    const simpleWord = expr.trim();
    if (/^\w+$/.test(simpleWord)) {
      return this.isSymbolActive(simpleWord);
    }
    return true; // Default fallback
  }
}
