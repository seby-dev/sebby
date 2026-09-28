// Parse-only TypeScript fact extractor for revgate.
//
// Usage: node ts_facts.cjs <path to the repository's typescript package>
//
// Protocol: one JSON object per line on stdin, {"id": n, "path": p, "source": s};
// one reply per request on stdout, {"id": n, "facts": {...}} or {"id": n, "error": "..."}.
// The first line written is a handshake, {"ready": true, "version": v}, or
// {"fatal": "..."} when the compiler can't be loaded. No type checker runs: every
// fact is syntactic, so it depends on the file's text alone.
"use strict";

const readline = require("readline");

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

let ts;
try {
  ts = require(process.argv[2]);
  if (typeof ts.createSourceFile !== "function") throw new Error("not a typescript package");
} catch (e) {
  send({ fatal: "cannot load typescript from " + process.argv[2] + ": " + String(e && e.message) });
  // Not process.exit: on macOS a pipe write is asynchronous and exit would drop it.
  process.exitCode = 1;
  return;
}

const IDENT_LIKE = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
const MAX_TEXT = 80;

function factsFor(path, source) {
  const kind = path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, kind);
  const lineOf = (pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;
  const qual = (name) => path + "::" + name;

  const out = {
    parse_error: null,
    imports: [],
    exports: [],
    functions: [],
    calls: [],
    refs: [],
  };
  const diags = sf.parseDiagnostics || [];
  if (diags.length) {
    const d = diags[0];
    const lc = sf.getLineAndCharacterOfPosition(d.start || 0);
    out.parse_error =
      lc.line + 1 + ":" + (lc.character + 1) + " " + ts.flattenDiagnosticMessageText(d.messageText, "\n");
  }

  const exportSeen = new Set();
  function addExport(name) {
    if (!exportSeen.has(name)) {
      exportSeen.add(name);
      out.exports.push(name);
    }
  }
  function isExported(node) {
    return !!(ts.getCombinedModifierFlags(node) & ts.ModifierFlags.Export);
  }
  function isDefault(node) {
    return !!(ts.getCombinedModifierFlags(node) & ts.ModifierFlags.Default);
  }

  // Each frame is one enclosing named function: its name and its try depth.
  const frames = [{ name: null, tries: 0 }];
  const top = () => frames[frames.length - 1];
  const caller = () => qual(top().name === null ? "<module>" : top().name);
  const inSymbol = () => (top().name === null ? null : qual(top().name));
  const classes = [];

  function addRef(node, name, kind, text) {
    out.refs.push({ line: lineOf(node.getStart(sf)), name, kind, in_symbol: inSymbol(), text: text || "" });
  }
  function addFunction(name, node, params, exported) {
    out.functions.push({
      name,
      line: lineOf(node.getStart(sf)),
      end: lineOf(node.end),
      params: params.map((p) => p.name.getText(sf)),
      exported,
    });
  }
  function withFrame(name, fn) {
    frames.push({ name, tries: 0 });
    try {
      fn();
    } finally {
      frames.pop();
    }
  }

  function visit(node) {
    if (ts.isImportDeclaration(node)) {
      if (node.importClause && ts.isStringLiteral(node.moduleSpecifier)) {
        const mod = node.moduleSpecifier.text;
        const ic = node.importClause;
        if (ic.name) {
          out.imports.push([mod, "default", ic.name.text]);
          addRef(ic.name, ic.name.text, "import");
        }
        const nb = ic.namedBindings;
        if (nb && ts.isNamespaceImport(nb)) {
          out.imports.push([mod, "*", nb.name.text]);
          addRef(nb.name, nb.name.text, "import");
        } else if (nb && ts.isNamedImports(nb)) {
          for (const el of nb.elements) {
            const imported = (el.propertyName || el.name).text;
            out.imports.push([mod, imported, el.name.text]);
            addRef(el, imported, "import");
          }
        }
      }
      return;
    }
    if (ts.isExportDeclaration(node)) {
      if (node.exportClause && ts.isNamedExports(node.exportClause)) {
        for (const el of node.exportClause.elements) addExport(el.name.text);
      } else if (node.exportClause && ts.isNamespaceExport(node.exportClause)) {
        addExport(node.exportClause.name.text);
      }
      ts.forEachChild(node, visit);
      return;
    }
    if (ts.isExportAssignment(node)) {
      addExport("default");
      ts.forEachChild(node, visit);
      return;
    }
    if (ts.isFunctionDeclaration(node) && node.name) {
      const name = node.name.text;
      const exported = isExported(node);
      addFunction(name, node, node.parameters, exported);
      if (exported) addExport(isDefault(node) ? "default" : name);
      withFrame(name, () => ts.forEachChild(node, visit));
      return;
    }
    if (ts.isVariableStatement(node)) {
      const exported = isExported(node);
      for (const d of node.declarationList.declarations) {
        if (exported && ts.isIdentifier(d.name)) addExport(d.name.text);
      }
      ts.forEachChild(node, visit);
      return;
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      const init = node.initializer;
      if (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) {
        const name = node.name.text;
        const stmt = node.parent && node.parent.parent;
        const exported = !!(stmt && ts.isVariableStatement(stmt) && isExported(stmt));
        addFunction(name, node, init.parameters, exported);
        addRef(node.name, name, "identifier");
        if (node.type) visit(node.type);
        withFrame(name, () => ts.forEachChild(init, visit));
        return;
      }
    }
    if ((ts.isClassDeclaration(node) || ts.isClassExpression(node)) && node.name) {
      if (ts.isClassDeclaration(node) && isExported(node)) {
        addExport(isDefault(node) ? "default" : node.name.text);
      }
      classes.push(node.name.text);
      try {
        ts.forEachChild(node, visit);
      } finally {
        classes.pop();
      }
      return;
    }
    if ((ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node) || ts.isEnumDeclaration(node)) && isExported(node)) {
      addExport(node.name.text);
    }
    if (ts.isMethodDeclaration(node) && node.name && ts.isIdentifier(node.name)) {
      const owner = classes.length ? classes[classes.length - 1] + "." : "";
      addRef(node.name, node.name.text, "identifier");
      withFrame(owner + node.name.text, () => {
        for (const p of node.parameters) visit(p);
        if (node.type) visit(node.type);
        if (node.body) visit(node.body);
      });
      return;
    }
    if (ts.isTryStatement(node)) {
      top().tries += 1;
      try {
        visit(node.tryBlock);
      } finally {
        top().tries -= 1;
      }
      if (node.catchClause) visit(node.catchClause);
      if (node.finallyBlock) visit(node.finallyBlock);
      return;
    }
    if (ts.isCallExpression(node)) {
      out.calls.push({
        caller: caller(),
        callee: node.expression.getText(sf).slice(0, MAX_TEXT),
        line: lineOf(node.getStart(sf)),
        in_try: top().tries > 0,
      });
    }
    if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.name)) {
      addRef(node.name, node.name.text, "attribute", node.getText(sf).slice(0, MAX_TEXT));
      visit(node.expression);
      return;
    }
    if (ts.isIdentifier(node)) {
      addRef(node, node.text, "identifier");
      return;
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (IDENT_LIKE.test(node.text)) addRef(node, node.text, "string");
      return;
    }
    ts.forEachChild(node, visit);
  }

  visit(sf);
  return out;
}

send({ ready: true, version: ts.version });

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let id = null;
  try {
    const req = JSON.parse(line);
    id = req.id;
    send({ id, facts: factsFor(req.path, req.source) });
  } catch (e) {
    send({ id, error: String((e && e.stack) || e).slice(0, 2000) });
  }
});
