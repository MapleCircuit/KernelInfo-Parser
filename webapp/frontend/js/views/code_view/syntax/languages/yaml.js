/**
 * yaml.js - YAML syntax grammar for Devicetree schema bindings and kernel configs.
 */

export const YAML_CONSTANTS = new Set([
  "true", "false", "yes", "no", "null", "~"
]);

export const yamlGrammar = {
  id: "yaml",
  name: "YAML",
  ftypes: [],
  extensions: [".yaml", ".yml"],
  filenames: [],
  shebangs: [],
  keywords: new Set(["description", "properties", "required", "type", "allOf", "anyOf", "oneOf"]),
  types: YAML_CONSTANTS,
  hasPreprocessor: false,
  supportsInclude: false,
  isCommentLine(trimmed) {
    return trimmed.startsWith("#");
  },
  isType(val) {
    return YAML_CONSTANTS.has(val);
  }
};

