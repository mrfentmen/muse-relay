#!/usr/bin/env python3
"""Interop + wiring tests for the relay chat's tap-to-rename feature.

The chat (bus.html) got tap-to-rename: nicks in the message log are
tappable buttons, a rename sheet writes the name registry entry, the
history entry, and the audit-log entry directly. Renames are keyless —
no admin secret anywhere in the flow.

Part 1 (interop): extracts the pure builder block from bus.html
(delimited by /*__SIGNING_START__*/ / /*__SIGNING_END__*/), runs it in
Node with fixed vectors, and checks every output against Python's
json.dumps and admin.py's keyless verify contract.

Part 2 (wiring): static assertions that the tap-to-rename feature is
present in bus.html — tappable nicks, modal, admin-set check, the three
write paths — and that no admin key/secret UI exists in the flow.

No mocks: real Node JS vs real Python for the builders.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import admin  # noqa: E402 — the contract owner

HTML = os.path.join(REPO, "bus.html")

PYSTR_CASES = [
    "plain",
    'quote " and backslash \\',
    "line1\nline2\ttab\rcarriage",
    "caf\u00e9",
    "emoji \U0001F600",
    "del\x7f",
    "",
    "a" * 32,
]

NODE_DRIVER = """\
const out = {};
out.pystr = %s.map(pyStr);
out.nonce = randHexBytes(8);
out.log = renameLogEntry("Mrfentmen", "alice", "Alice Cooper", 1758390000, "0123456789abcdef");
out.hash = namesHashEntry("Mrfentmen", "Alice Cooper", 1758390000);
out.hist = namesHistoryEntry("Mrfentmen", "alice", "Alice Cooper", 1758390000);
out.qnick = [qNick("alice"), qNick("a b/c:d"), qNick("Mrfentmen")];
out.parsedObj = parseNames({
  "alice": JSON.stringify({display: "Alice Cooper", set_by: "Mrfentmen", set_at: 1}),
  "bob": "not-json",
  "carol": JSON.stringify({display: "", set_by: "Mrfentmen", set_at: 1})
});
out.parsedArr = parseNames(["alice", JSON.stringify({display: "Alice Cooper"}), "bob", "junk"]);
out.parsedNull = parseNames(null);
out.parsedUndef = parseNames(undefined);
out.label1 = labelFor({alice: "Alice Cooper"}, "alice");
out.label2 = labelFor({}, "alice");
out.isAdmin1 = isAdmin(["Mrfentmen", "del", "mute"], "mrfentmen");
out.isAdmin2 = isAdmin(["Mrfentmen", "del", "mute"], "MRFENTMEN");
out.isAdmin3 = isAdmin(["Mrfentmen", "del", "mute"], "Mrfentmen");
out.isAdmin4 = isAdmin(["Mrfentmen", "del", "mute"], "bob");
out.isAdmin5 = isAdmin([], "mrfentmen");
console.log(JSON.stringify(out));
"""


def _builder_block():
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"/\*__SIGNING_START__\*/(.*?)/\*__SIGNING_END__\*/",
                  html, re.S)
    assert m, "builder block markers missing from bus.html"
    return m.group(1)


def _feature_block():
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"// ---- FEATURE: tap-to-rename ----(.*?)"
                  r"// ================== END FEATURES",
                  html, re.S)
    assert m, "tap-to-rename feature block missing from bus.html"
    return m.group(1)


def _run_node(block):
    driver = block + "\n" + NODE_DRIVER % json.dumps(PYSTR_CASES)
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                     delete=False) as f:
        f.write(driver)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True,
                           timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


class BusRenameInterop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _run_node(_builder_block())

    def test_pystr_matches_json_dumps(self):
        for case, got in zip(PYSTR_CASES, self.js["pystr"]):
            self.assertEqual(got, json.dumps(case, ensure_ascii=True),
                             f"pyStr mismatch for {case!r}")

    def test_nonce_shape(self):
        self.assertRegex(self.js["nonce"], r"^[0-9a-f]{16}$")

    def test_log_entry_is_valid_json(self):
        env = json.loads(self.js["log"])
        self.assertEqual(env["v"], 1)
        self.assertEqual(env["cmd"], "rename")
        self.assertEqual(env["args"],
                         {"nick": "alice", "display": "Alice Cooper"})
        self.assertEqual(env["actor"], "Mrfentmen")

    def test_log_entry_is_keyless(self):
        env = json.loads(self.js["log"])
        self.assertIsNone(env.get("sig"))

    def test_keyless_entry_passes_admin_verify(self):
        env = json.loads(self.js["log"])
        # None = keyless entry, no signature to check (not a failure)
        self.assertIsNone(admin._verify_envelope(None, env))

    def test_names_hash_entry_shape(self):
        h = json.loads(self.js["hash"])
        self.assertEqual(h, {"display": "Alice Cooper",
                             "set_by": "Mrfentmen", "set_at": 1758390000})

    def test_history_entry_shape(self):
        h = json.loads(self.js["hist"])
        self.assertEqual(h, {"nick": "alice", "display": "Alice Cooper",
                             "set_by": "Mrfentmen", "set_at": 1758390000})

    def test_qnick_encoding(self):
        self.assertEqual(self.js["qnick"],
                         ["alice", "a%20b%2Fc%3Ad", "Mrfentmen"])

    def test_parse_names_object_form(self):
        # junk values and empty displays are dropped
        self.assertEqual(self.js["parsedObj"], {"alice": "Alice Cooper"})

    def test_parse_names_flat_array_form(self):
        self.assertEqual(self.js["parsedArr"], {"alice": "Alice Cooper"})

    def test_parse_names_nullish(self):
        self.assertEqual(self.js["parsedNull"], {})
        self.assertEqual(self.js["parsedUndef"], {})

    def test_label_for(self):
        self.assertEqual(self.js["label1"], "Alice Cooper")
        self.assertEqual(self.js["label2"], "alice")

    def test_is_admin_case_insensitive(self):
        # the exact failure the boss hit: "mrfentmen" vs admin "Mrfentmen"
        self.assertTrue(self.js["isAdmin1"])
        self.assertTrue(self.js["isAdmin2"])
        self.assertTrue(self.js["isAdmin3"])
        self.assertFalse(self.js["isAdmin4"])
        self.assertFalse(self.js["isAdmin5"])


class BusRenameWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feat = _feature_block()

    def test_nicks_become_tappable_buttons(self):
        self.assertIn("nickbtn", self.feat)
        self.assertIn("data-nick", self.feat)
        self.assertIn("tap to rename", self.feat)

    def test_rename_modal_present(self):
        for ident in ("busRenameModal", "bus-rn-nick", "bus-rn-current",
                      "bus-rn-display", "bus-rn-go", "bus-rn-cancel",
                      "bus-rn-msg"):
            self.assertIn(ident, self.feat, f"modal id missing: {ident}")

    def test_admin_set_check_keyless(self):
        self.assertIn(":admins", self.feat)
        self.assertIn("is not an admin", self.feat)

    def test_three_write_paths(self):
        self.assertIn('":names/"', self.feat)
        self.assertIn('":names:history"', self.feat)
        self.assertIn('":admin:log"', self.feat)

    def test_no_admin_key_anywhere_in_flow(self):
        low = self.feat.lower()
        self.assertNotIn("secret", low)
        self.assertNotIn("paste the key", low)
        self.assertNotIn("admin key", low)
        self.assertIn("no key needed", low)

    def test_display_name_validation(self):
        self.assertIn("max 32 chars", self.feat)
        self.assertIn("may not contain", self.feat)

    def test_feature_hooked_into_chat_lifecycle(self):
        self.assertIn("H.connect.push", self.feat)
        self.assertIn("H.poll.push", self.feat)
        self.assertIn("loadNames", self.feat)
        self.assertIn("wireNicks", self.feat)

def _feature_iife():
    """Extract the tap-to-rename IIFE source from bus.html."""
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    feat = re.search(r"// ---- FEATURE: tap-to-rename ----(.*?)"
                     r"// ================== END FEATURES",
                     html, re.S).group(1)
    start = feat.index("(function () {")
    return feat[start:feat.rindex("})();") + len("})();")]


# Node driver: stub DOM, load the real IIFE, wrap a stub renderPresence,
# then assert the online board shows display names and taps open rename.
# Built with string concatenation to dodge triple-quote escaping issues.
_PRESENCE_DOM_PARTS = [
    "var clickHandlers = [];",
    "var renderCalls = [];",
    "var els = {};",
    "function mkEl() {",
    "  return {",
    "    _t: '', _v: '', _h: true, _c: '', title: '', style: {},",
    "    get textContent() { return this._t; },",
    "    set textContent(v) { this._t = v; },",
    "    get value() { return this._v; },",
    "    set value(v) { this._v = v; },",
    "    get hidden() { return this._h; },",
    "    set hidden(v) { this._h = v; },",
    "    set className(v) { this._c = v; },",
    "    set innerHTML(v) { this._hml = v; },",
    "    appendChild: function (c) { this._kids = this._kids || []; this._kids.push(c); },",
    "    addEventListener: function (ev, fn) { this._fn = fn; },",
    "    focus: function () {},",
    "    querySelectorAll: function () { return []; }",
    "  };",
    "}",
    "function makeWho(nick) {",
    "  var w = mkEl();",
    "  w._nick = nick;",
    "  w._datanick = null;",
    "  w.getAttribute = function (k) { return this._datanick; };",
    "  w.setAttribute = function (k, v) { this._datanick = v; };",
    "  Object.defineProperty(w, 'textContent',",
    "    { get: function () { return '\\u25cf' + this._nick; } });",
    "  w.addEventListener = function (ev, fn) { clickHandlers.push({ nick: this._nick, fn: fn }); };",
    "  return w;",
    "}",
    "var whos = [makeWho('mute'), makeWho('del')];",
    "var presenceEl = mkEl();",
    "presenceEl.querySelectorAll = function (sel) { return sel === '.who' ? whos : []; };",
    "var window = {};",
    "var document = {",
    "  getElementById: function (id) {",
    "    if (id === 'bus-rename-css' || id === 'busRenameModal') return {};",
    "    if (id === 'presence') return presenceEl;",
    "    if (!els[id]) els[id] = mkEl();",
    "    return els[id];",
    "  },",
    "  createElement: function () { return mkEl(); },",
    "  createTextNode: function (tx) { return { _t: tx }; },",
    "  head: { appendChild: function () {} },",
    "  body: { appendChild: function () {} }",
    "};",
    "window.renderPresence = function (nicks) { renderCalls.push(nicks); };",
    "window.busUtil = {",
    "  el: function (id) { return document.getElementById(id); },",
    "  esc: function (s) { return s; },",
    "  api: function () { return Promise.resolve({}); },",
    "  myNick: function () { return 'mrfentmen'; }",
    "};",
    "window.busHooks = { message: [], connect: [], poll: [] };",
    "var crypto = { getRandomValues: function (b) { for (var i = 0; i < b.length; i++) b[i] = i; } };",
    "var setInterval = function () {};",
    "__IIFE__",
    "var out = {};",
    "out.wrapped = window.renderPresence._rnwrapped === true;",
    "window.__busTest.busRename._cache({ mute: 'hana', del: 'rowan' });",
    "window.renderPresence(['mute', 'del']);",
    "out.renderCalls = renderCalls.length;",
    "out.label0 = whos[0]._kids[1]._t;",
    "out.label1 = whos[1]._kids[1]._t;",
    "out.handlers = clickHandlers.length;",
    "clickHandlers[0].fn();",
    "out.openedNick = els['bus-rn-nick']._t;",
    "out.openedCurrent = els['bus-rn-current']._t;",
    "console.log(JSON.stringify(out));",
]


def _run_presence_dom():
    script = "\n".join(_PRESENCE_DOM_PARTS).replace("__IIFE__",
                                                   _feature_iife())
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                     delete=False) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True,
                           timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, "node failed: " + r.stderr
    return json.loads(r.stdout)


class BusRenamePresenceDom(unittest.TestCase):
    """The online board must show display names, and tapping one opens
    the rename sheet — verified with a stub DOM in Node."""

    @classmethod
    def setUpClass(cls):
        cls.dom = _run_presence_dom()

    def test_render_presence_is_wrapped(self):
        self.assertTrue(self.dom["wrapped"])

    def test_original_render_still_runs(self):
        self.assertEqual(self.dom["renderCalls"], 1)

    def test_presence_shows_display_names(self):
        self.assertEqual(self.dom["label0"], "hana")
        self.assertEqual(self.dom["label1"], "rowan")

    def test_presence_names_are_tappable(self):
        self.assertEqual(self.dom["handlers"], 2)

    def test_tapping_presence_name_opens_rename(self):
        self.assertEqual(self.dom["openedNick"], "mute")
        self.assertEqual(self.dom["openedCurrent"], "hana")

    def test_presence_wiring_present_in_feature(self):
        with open(HTML, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("applyPresenceNames", html)
        self.assertIn("renderPresence", html)




if __name__ == "__main__":
    unittest.main(verbosity=2)
