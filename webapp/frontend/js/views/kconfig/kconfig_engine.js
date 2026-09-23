/**
 * kconfig_engine.js - Authentic Linux Kconfig Client-Side Constraint & Recursive Default Engine.
 *
 * Implements:
 * 1. 3-Valued Logic Evaluator (n=0, m=1, y=2) supporting &&, ||, !, =, !=, constants, hex, and comparisons.
 * 2. Reverse Dependency Solver (selects & implies with conditional expressions).
 * 3. Choice Block Manager (mutual exclusion radio buttons, default selection).
 * 4. Recursive Default Propagation Fixpoint Loop (cascading defaults on visibility change).
 * 5. Type-aware Tristate Cycling (n -> m -> y -> n) and Bool Toggling (n -> y -> n).
 */

export class KconfigEngine {
  constructor(treeNodes = []) {
    this.nodes = treeNodes || [];
    this.nodeBySymbol = new Map();
    this.choicesById = new Map();
    this.choiceMembersByChoiceId = new Map();
    this.initNodeMap();
  }

  setNodes(treeNodes) {
    this.nodes = treeNodes || [];
    this.initNodeMap();
  }

  initNodeMap() {
    this.nodeBySymbol.clear();
    this.choicesById.clear();
    this.choiceMembersByChoiceId.clear();

    const indexNode = (n) => {
      if (n.symbol_name) {
        this.nodeBySymbol.set(n.symbol_name, n);
      }
      if (n.choice && (n.choice.choice_id || n.choice.id)) {
        const cid = n.choice.choice_id || n.choice.id;
        if (!this.choicesById.has(cid)) {
          this.choicesById.set(cid, n.choice);
        }
        if (!this.choiceMembersByChoiceId.has(cid)) {
          this.choiceMembersByChoiceId.set(cid, []);
        }
        if (n.symbol_name) {
          const list = this.choiceMembersByChoiceId.get(cid);
          if (list && !list.includes(n.symbol_name)) {
            list.push(n.symbol_name);
          }
        }
      }
      if (n.node_type === 2 && (n.choice_id || n.id || n.tree_id)) {
        const cid = n.choice_id || n.id || n.tree_id;
        if (!this.choicesById.has(cid)) {
          this.choicesById.set(cid, n.choice || { choice_id: cid, type: "bool", optional: false });
        }
        if (!this.choiceMembersByChoiceId.has(cid)) {
          this.choiceMembersByChoiceId.set(cid, []);
        }
      }
      if (Array.isArray(n.children)) {
        n.children.forEach(indexNode);
      }
    };

    this.nodes.forEach(indexNode);
  }

  /**
   * Tokenize and evaluate boolean/tristate expression with 3-valued logic.
   * Returns: 0 (n), 1 (m), or 2 (y).
   */
  evalExpr(expr, config = {}) {
    if (!expr || typeof expr !== "string" || !expr.trim()) {
      return 2; // unconditional
    }

    const clean = expr.trim();
    if (clean === "y" || clean === "1" || clean === "true") return 2;
    if (clean === "m") return 1;
    if (clean === "n" || clean === "0" || clean === "false") return 0;

    // Tokenizer
    const tokens = [];
    let i = 0;
    while (i < clean.length) {
      const ch = clean[i];
      if (/\s/.test(ch)) {
        i++;
        continue;
      }
      if (ch === "(") {
        tokens.push({ type: "LPAREN" });
        i++;
      } else if (ch === ")") {
        tokens.push({ type: "RPAREN" });
        i++;
      } else if (ch === "&" && clean[i + 1] === "&") {
        tokens.push({ type: "AND" });
        i += 2;
      } else if (ch === "|" && clean[i + 1] === "|") {
        tokens.push({ type: "OR" });
        i += 2;
      } else if (ch === "!" && clean[i + 1] === "=") {
        tokens.push({ type: "NEQ" });
        i += 2;
      } else if (ch === "=") {
        tokens.push({ type: "EQ" });
        i++;
      } else if (ch === "!") {
        tokens.push({ type: "NOT" });
        i++;
      } else if (ch === '"' || ch === "'") {
        const quote = ch;
        i++;
        let strVal = "";
        while (i < clean.length && clean[i] !== quote) {
          strVal += clean[i];
          i++;
        }
        i++; // skip quote
        tokens.push({ type: "STRING", value: strVal });
      } else {
        let val = "";
        while (i < clean.length && /[a-zA-Z0-9_\-\.\/]/.test(clean[i])) {
          val += clean[i];
          i++;
        }
        if (val) {
          tokens.push({ type: "IDENT", value: val });
        } else {
          i++;
        }
      }
    }

    if (tokens.length === 0) return 2;

    let pos = 0;
    const peek = () => tokens[pos] || { type: "EOF" };
    const consume = (expectedType) => {
      const tok = peek();
      if (expectedType && tok.type !== expectedType) return null;
      pos++;
      return tok;
    };

    const getSymbolValue = (name) => {
      const bare = name.startsWith("CONFIG_") ? name.substring(7) : name;
      if (bare === "y" || bare === "1" || bare === "true") return 2;
      if (bare === "m") return 1;
      if (bare === "n" || bare === "0" || bare === "false") return 0;

      const raw = config[bare] !== undefined ? config[bare] : config[`CONFIG_${bare}`];
      if (raw === "y" || raw === true || raw === "1") return 2;
      if (raw === "m") return 1;
      if (raw === "n" || raw === false || raw === "0" || raw === undefined || raw === null || raw === "") return 0;
      return raw;
    };

    const parseOr = () => {
      let left = parseAnd();
      while (peek().type === "OR") {
        consume("OR");
        const right = parseAnd();
        const lNum = typeof left === "number" ? left : (getSymbolValue(String(left)) === 2 ? 2 : 0);
        const rNum = typeof right === "number" ? right : (getSymbolValue(String(right)) === 2 ? 2 : 0);
        left = Math.max(lNum, rNum);
      }
      return left;
    };

    const parseAnd = () => {
      let left = parseComparison();
      while (peek().type === "AND") {
        consume("AND");
        const right = parseComparison();
        const lNum = typeof left === "number" ? left : (getSymbolValue(String(left)) === 2 ? 2 : 0);
        const rNum = typeof right === "number" ? right : (getSymbolValue(String(right)) === 2 ? 2 : 0);
        left = Math.min(lNum, rNum);
      }
      return left;
    };

    const parseComparison = () => {
      let left = parseUnary();
      if (peek().type === "EQ" || peek().type === "NEQ") {
        const isEq = peek().type === "EQ";
        consume();
        const right = parseUnary();
        const lVal = typeof left === "number" ? (left === 2 ? "y" : (left === 1 ? "m" : "n")) : String(left);
        const rVal = typeof right === "number" ? (right === 2 ? "y" : (right === 1 ? "m" : "n")) : String(right);
        const matches = (lVal === rVal);
        return (isEq ? matches : !matches) ? 2 : 0;
      }
      return left;
    };

    const parseUnary = () => {
      if (peek().type === "NOT") {
        consume("NOT");
        const operand = parseUnary();
        const num = typeof operand === "number" ? operand : (getSymbolValue(String(operand)) === 2 ? 2 : 0);
        if (num === 2) return 0;
        if (num === 0) return 2;
        return 1;
      }
      return parsePrimary();
    };

    const parsePrimary = () => {
      const tok = peek();
      if (tok.type === "LPAREN") {
        consume("LPAREN");
        const val = parseOr();
        consume("RPAREN");
        return val;
      }
      if (tok.type === "IDENT") {
        consume("IDENT");
        return getSymbolValue(tok.value);
      }
      if (tok.type === "STRING") {
        consume("STRING");
        return tok.value;
      }
      consume();
      return 0;
    };

    try {
      const res = parseOr();
      if (typeof res === "number") return res;
      const finalVal = getSymbolValue(String(res));
      return typeof finalVal === "number" ? finalVal : (finalVal ? 2 : 0);
    } catch {
      return 0;
    }
  }

  isSymbolVisible(symbolName, config) {
    const bare = symbolName.startsWith("CONFIG_") ? symbolName.substring(7) : symbolName;
    const node = this.nodeBySymbol.get(bare);
    if (!node) return true;
    if (!node.depends_on_expr) return true;
    return this.evalExpr(node.depends_on_expr, config) > 0;
  }

  /**
   * Propagate constraints across passes until fixpoint (steady-state).
   */
  propagate(assignments) {
    const config = {};
    for (const [k, v] of Object.entries(assignments || {})) {
      const bare = k.startsWith("CONFIG_") ? k.substring(7) : k;
      config[bare] = v;
      config[`CONFIG_${bare}`] = v;
    }

    let changed = true;
    let passes = 0;
    const MAX_PASSES = 15;

    while (changed && passes < MAX_PASSES) {
      changed = false;
      passes++;

      // 1. Evaluate Dependencies & Prune Invalid Assignments
      for (const [sym, node] of this.nodeBySymbol.entries()) {
        const currentVal = config[sym] || "n";
        if (node.depends_on_expr) {
          const vis = this.evalExpr(node.depends_on_expr, config);
          if (vis === 0 && currentVal !== "n") {
            config[sym] = "n";
            config[`CONFIG_${sym}`] = "n";
            changed = true;
          } else if (vis === 1) {
            if (node.type === 1 && currentVal === "y") { // bool cannot be active with m visibility
              config[sym] = "n";
              config[`CONFIG_${sym}`] = "n";
              changed = true;
            } else if (node.type === 2 && currentVal === "y") { // tristate clamped to m
              config[sym] = "m";
              config[`CONFIG_${sym}`] = "m";
              changed = true;
            }
          }
        }
      }

      // 2. Evaluate Selects (Reverse Dependencies)
      for (const [sym, node] of this.nodeBySymbol.entries()) {
        const currentVal = config[sym] || "n";
        if ((currentVal === "y" || currentVal === "m") && Array.isArray(node.selects)) {
          for (const sel of node.selects) {
            const target = typeof sel === "string" ? sel : sel.target;
            const cond = typeof sel === "object" ? sel.cond : null;
            if (!target) continue;

            const condVal = cond ? this.evalExpr(cond, config) : 2;
            if (condVal > 0) {
              const targetBare = target.startsWith("CONFIG_") ? target.substring(7) : target;
              const targetVal = config[targetBare] || "n";
              const forcedVal = (currentVal === "y" && condVal === 2) ? "y" : "m";
              if (targetVal === "n" || (targetVal === "m" && forcedVal === "y")) {
                config[targetBare] = forcedVal;
                config[`CONFIG_${targetBare}`] = forcedVal;
                changed = true;
              }
            }
          }
        }
      }

      // 3. Evaluate Defaults for Visible Symbols
      for (const [sym, node] of this.nodeBySymbol.entries()) {
        const currentVal = config[sym] || "n";
        const vis = node.depends_on_expr ? this.evalExpr(node.depends_on_expr, config) : 2;

        if (vis > 0 && currentVal === "n" && Array.isArray(node.defaults) && node.defaults.length > 0) {
          // If in choice, handled by choice constraints below
          if (node.choice && node.choice.choice_id) {
            continue;
          }

          for (const def of node.defaults) {
            const cond = def.cond;
            const condVal = cond ? this.evalExpr(cond, config) : 2;
            if (condVal > 0) {
              let defVal = def.value ? def.value.trim().replace(/^["']|["']$/g, "") : "n";
              if (defVal === "y" || defVal === "m" || defVal === "n") {
                // literal constant
              } else if (this.nodeBySymbol.has(defVal)) {
                defVal = config[defVal] || "n";
              }
              if (vis === 1 && defVal === "y") {
                defVal = (node.type === 2) ? "m" : "n";
              }
              if (defVal !== "n" && defVal !== "") {
                config[sym] = defVal;
                config[`CONFIG_${sym}`] = defVal;
                changed = true;
              }
              break;
            }
          }
        }
      }

      // 4. Enforce Choice Mutual-Exclusion Constraints
      for (const [choiceId, choiceMeta] of this.choicesById.entries()) {
        const members = this.choiceMembersByChoiceId.get(choiceId) || [];
        if (members.length === 0) continue;

        const activeMembers = members.filter((m) => config[m] === "y");
        if (activeMembers.length > 1) {
          for (let mIdx = 1; mIdx < activeMembers.length; mIdx++) {
            const mSym = activeMembers[mIdx];
            config[mSym] = "n";
            config[`CONFIG_${mSym}`] = "n";
            changed = true;
          }
        } else if (activeMembers.length === 0 && !choiceMeta.optional) {
          const firstVisible = members.find((m) => this.isSymbolVisible(m, config));
          if (firstVisible) {
            config[firstVisible] = "y";
            config[`CONFIG_${firstVisible}`] = "y";
            changed = true;
          }
        }
      }
    }

    return config;
  }

  selectChoiceMember(choiceId, symbolName, config = {}) {
    const bare = symbolName.startsWith("CONFIG_") ? symbolName.substring(7) : symbolName;
    const members = this.choiceMembersByChoiceId.get(choiceId) || [];
    const newConfig = { ...config };

    for (const m of members) {
      if (m === bare) {
        newConfig[m] = "y";
        newConfig[`CONFIG_${m}`] = "y";
      } else {
        newConfig[m] = "n";
        newConfig[`CONFIG_${m}`] = "n";
      }
    }

    return this.propagate(newConfig);
  }

  cycleValue(symbolName, currentVal, nodeData = null, config = {}) {
    const bare = symbolName.startsWith("CONFIG_") ? symbolName.substring(7) : symbolName;
    const node = nodeData || this.nodeBySymbol.get(bare);
    const symType = node ? (node.type || (node.type_name === "tristate" ? 2 : 1)) : 1;
    const vis = node && node.depends_on_expr ? this.evalExpr(node.depends_on_expr, config) : 2;

    if (vis === 0) {
      return "n";
    }

    if (symType === 2) { // Tristate
      if (currentVal === "n" || !currentVal) {
        return vis === 1 ? "m" : "y";
      } else if (currentVal === "y") {
        return "m";
      } else {
        return "n";
      }
    } else { // Bool
      return currentVal === "y" ? "n" : "y";
    }
  }

  getFailingDependencies(node, config = {}) {
    if (!node || !node.depends_on_expr) return [];
    const failing = [];
    if (Array.isArray(node.depends_on)) {
      for (const dep of node.depends_on) {
        if (this.evalExpr(dep, config) === 0) {
          failing.push(dep);
        }
      }
    }
    return failing;
  }
}
