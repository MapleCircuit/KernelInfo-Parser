import { test } from "node:test";
import assert from "node:assert/strict";
import { enclosingStruct } from "../js/views/code_view/ops_model.js";

test("enclosing ops struct of a line", () => {
  const lines = [
    "static int foo_read(void) { return 0; }",
    "const struct file_operations foo_fops = {",
    "\t.owner = THIS_MODULE, /* { */",
    "\t.read = foo_read,",
    "};",
    "int x;",
  ];
  assert.equal(enclosingStruct(lines, 4), "file_operations");
  assert.equal(enclosingStruct(lines, 6), "");
  assert.equal(enclosingStruct(lines, 1), "");
});

import { tokenRole } from "../js/views/code_view/ops_model.js";

test("token role on its line", () => {
  assert.equal(tokenRole("\t.aio_write = ext4_file_write,", "aio_write"), "field");
  assert.equal(tokenRole("\t.aio_write = ext4_file_write,", "ext4_file_write"), "function");
  assert.equal(tokenRole("\t.write = &foo_write,", "foo_write"), "function");
  assert.equal(tokenRole("\tret = foo(x);", "foo"), "");
});
