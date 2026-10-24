/**
 * kconfig_parser.js - Linux .config reader and raw (engine-off) writer.
 *
 * With the engine on, .config text is produced by KconfigEngine.writeConfig() (exactly what
 * scripts/kconfig writes); serialize() only formats raw, unvalidated assignments.
 */
import { escapeConfigString } from "./kconfig_engine.js";

export class KconfigParser {
  /** Parse .config / defconfig text into {CONFIG_NAME: value} (strings unescaped like conf_read). */
  static parse(content) {
    const assignments = {};
    for (const line of String(content || "").split(/\r?\n/)) {
      const unset = /^# (CONFIG_\w+) is not set/.exec(line);
      if (unset) {
        assignments[unset[1]] = "n";
        continue;
      }
      if (!line.startsWith("CONFIG_")) continue;
      const eq = line.indexOf("=");
      if (eq < 0) continue;
      const key = line.slice(0, eq);
      let val = line.slice(eq + 1);
      if (val.startsWith('"')) {
        let out = "";
        for (let i = 1; i < val.length && val[i] !== '"'; i++) {
          out += val[i] === "\\" && i + 1 < val.length ? val[++i] : val[i];
        }
        val = out;
      }
      assignments[key] = val;
    }
    return assignments;
  }

  /**
   * Format raw assignments as .config lines (sorted, unvalidated). `types` maps bare names to
   * Kconfig types so strings are quoted and int/hex are not.
   */
  static serialize(assignments, version = "v3.0", arch = "x86_64", types = null) {
    const lines = [
      "#",
      `# Unvalidated configuration exported by KernelInfo for Linux ${version} (ARCH=${arch})`,
      "# The KConfig engine was off: run `make olddefconfig` (or oldconfig) before building.",
      "#",
    ];
    const seen = new Set();
    for (const key of Object.keys(assignments || {}).sort()) {
      const bare = key.startsWith("CONFIG_") ? key.slice(7) : key;
      if (seen.has(bare)) continue;
      seen.add(bare);
      const val = assignments[key];
      const type = types ? types(bare) : null;
      if (val === "n" || val === false) lines.push(`# CONFIG_${bare} is not set`);
      else if (val === "y" || val === "m" || val === true) lines.push(`CONFIG_${bare}=${val === true ? "y" : val}`);
      else if (type === "string") lines.push(`CONFIG_${bare}="${escapeConfigString(val)}"`);
      else if (type === "int" || type === "hex" || /^(-?\d+|0[xX][0-9a-fA-F]+)$/.test(String(val))) lines.push(`CONFIG_${bare}=${val}`);
      else lines.push(`CONFIG_${bare}="${escapeConfigString(val)}"`);
    }
    return `${lines.join("\n")}\n`;
  }
}
