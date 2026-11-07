/* Static check of the in-DOM template in index.html.
 *
 * Why not just compile it with the shipped Vue build: the production build is
 * runtime-compiler capable but does not expose compile() in a usable form, and
 * building a DOM shim good enough for its entity decoder is more machinery than
 * the check is worth. The two failures that actually ship are different anyway:
 *
 *   1. an unclosed tag - the browser silently reparents the rest of the page,
 *      which shows up as "the settings page is half missing";
 *   2. a block that vanished while editing - no error at all, just a gone card.
 *
 * So this checks tag balance over the whole #app template and asserts the
 * storage block is present and wired. Real rendering is verified on a device
 * with tools/console_check.py, which loads the page in a browser.
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PAGE = path.join(ROOT, "src", "fn-pvepanel", "app", "web", "index.html");
const VOID = new Set(["br", "hr", "img", "input", "meta", "link", "area", "base",
  "col", "embed", "source", "track", "wbr"]);

function templateOf(html) {
  const start = html.indexOf('<div id="app"');
  if (start < 0) return null;
  let i = html.indexOf(">", start) + 1;
  let depth = 1;
  const re = /<(\/?)div\b[^>]*>/gi;
  re.lastIndex = i;
  let m;
  while ((m = re.exec(html)) !== null) {
    if (m[1] === "/") { depth -= 1; if (depth === 0) return html.slice(i, m.index); }
    else depth += 1;
  }
  return null;
}

/* A nesting walk over every tag in the fragment. Vue's own parser is lenient in
 * places the HTML parser is not, so anything reported here is a real defect. */
function checkBalance(tpl) {
  const re = /<(\/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>/g;
  const stack = [];
  const problems = [];
  let m;
  while ((m = re.exec(tpl)) !== null) {
    const closing = m[1] === "/";
    const tag = m[2].toLowerCase();
    const selfClosing = m[4] === "/";
    if (VOID.has(tag) || selfClosing) continue;
    if (closing) {
      if (!stack.length) {
        problems.push("unexpected </" + tag + "> at offset " + m.index);
        continue;
      }
      const top = stack.pop();
      if (top.tag !== tag) {
        problems.push("</" + tag + "> closes <" + top.tag + "> opened at offset " + top.index);
      }
    } else {
      stack.push({ tag: tag, index: m.index });
    }
  }
  stack.forEach(function (s) {
    problems.push("<" + s.tag + "> at offset " + s.index + " is never closed");
  });
  return problems;
}

function main() {
  const html = fs.readFileSync(PAGE, "utf8");
  const tpl = templateOf(html);
  let ok = true;

  if (tpl === null) {
    console.log("  FAIL could not locate the #app template");
    return 1;
  }
  console.log("  ok   #app template found (" + tpl.length + " chars)");

  const problems = checkBalance(tpl);
  if (problems.length) {
    ok = false;
    problems.slice(0, 8).forEach(function (p) { console.log("  FAIL " + p); });
    if (problems.length > 8) console.log("       ... and " + (problems.length - 8) + " more");
  } else {
    console.log("  ok   every tag is balanced");
  }

  /* The tab label lives in app.js (setSections), so that one is checked there. */
  const appJs = fs.readFileSync(path.join(ROOT, "src", "fn-pvepanel", "app",
    "web", "js", "app.js"), "utf8");
  const jsProbes = [
    ["storage tab registered in setSections", /id: "storage", label: t\("storage\.tab"\)/],
    ["storage loads on tab switch", /if \(id === "storage"\) this\.loadStorage\(\)/],
    ["storage loads when settings opens", /this\.setTab === "storage"\) this\.loadStorage\(\)/],
    ["cleanup goes through the API", /API\.storageCleanup\(/],
    ["storage tab is deep-linkable", /"servers", "notify", "maint", "storage", "update", "ui"/],
  ];
  let jsOk = true;
  jsProbes.forEach(function (p) {
    if (p[1].test(appJs)) console.log("  ok   " + p[0]);
    else { jsOk = false; console.log("  FAIL " + p[0]); }
  });

  const probes = [
    ["storage card present", /setTab === 'storage'/],
    ["storage total block", /class="storage-total"/],
    ["storage usage table", /class="ttable tstorage"/],
    ["cleanup buttons wired", /askCleanup\('updates'\)[\s\S]*askCleanup\('logs'\)[\s\S]*askCleanup\('all'\)/],
    ["package list bound", /storageUpdates/],
    ["partial chip", /storage\.partial/],
    ["in-use chip", /storage\.inUse/],
    ["refresh handler", /loadStorage/],
    ["maintenance page untouched", /setTab === 'maint'/],
  ];
  probes.forEach(function (p) {
    if (p[1].test(tpl)) {
      console.log("  ok   " + p[0]);
    } else {
      ok = false;
      console.log("  FAIL " + p[0]);
    }
  });

  ok = ok && jsOk;
  console.log(ok ? "RESULT: PASS" : "RESULT: FAIL");
  return ok ? 0 : 1;
}

process.exit(main());
