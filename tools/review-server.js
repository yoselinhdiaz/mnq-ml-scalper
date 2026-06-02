#!/usr/bin/env node
// ============================================================
// DevAssistant — review-server.js
// Opción B: Servidor Express local que expone el código
//           del proyecto para que Copilot Studio lo lea
//           por nombre de archivo o por feature.
//
// INICIO:
//   node review-server.js
//   node review-server.js --root C:\Projects\my-app --port 3747
//
// ENDPOINTS:
//   GET /file?path=src/features/users/usersSagas.js
//   GET /feature?name=users
//   GET /feature?name=users&includeTests=true
//   GET /changed          — archivos modificados (git diff)
//   GET /health           — verificar que el servidor está corriendo
// ============================================================

const http    = require('http');
const fs      = require('fs');
const path    = require('path');
const url     = require('url');
const { execSync } = require('child_process');

// ── Config ────────────────────────────────────────────────────────────────────

const args = process.argv.slice(2);
const getArg = (flag) => {
  const i = args.indexOf(flag);
  return i !== -1 ? args[i + 1] : null;
};

const PORT         = parseInt(getArg('--port') || process.env.PORT || '3747', 10);
const PROJECT_ROOT = path.resolve(getArg('--root') || process.env.PROJECT_ROOT || process.cwd());
const ALLOWED_EXT  = ['.js', '.jsx', '.ts', '.tsx', '.json', '.css', '.html'];

console.log('\n DevAssistant Review Server');
console.log(' ─────────────────────────────────────────');
console.log(` Project root : ${PROJECT_ROOT}`);
console.log(` Listening on : http://localhost:${PORT}`);
console.log(' ─────────────────────────────────────────\n');

// ── Security helpers ─────────────────────────────────────────────────────────

const isInsideRoot = (filePath) =>
  path.resolve(filePath).startsWith(PROJECT_ROOT);

const isSafeExtension = (filePath) =>
  ALLOWED_EXT.includes(path.extname(filePath).toLowerCase());

// ── File helpers ──────────────────────────────────────────────────────────────

const walkDir = (dir, includeTests = false) => {
  const results = [];
  if (!fs.existsSync(dir)) return results;

  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);

    if (entry.isDirectory()) {
      if (['node_modules', 'dist', 'build', 'coverage', '.git'].includes(entry.name)) continue;
      results.push(...walkDir(fullPath, includeTests));
    } else if (entry.isFile()) {
      const ext = path.extname(entry.name);
      if (!['.js', '.jsx'].includes(ext)) continue;
      if (!includeTests && entry.name.match(/\.test\.(js|jsx)$/)) continue;
      results.push(fullPath);
    }
  }
  return results;
};

const readFile = (filePath) => ({
  path: path.relative(PROJECT_ROOT, filePath).replace(/\\/g, '/'),
  content: fs.readFileSync(filePath, 'utf8'),
  lines: fs.readFileSync(filePath, 'utf8').split('\n').length,
  size: fs.statSync(filePath).size,
  modified: fs.statSync(filePath).mtime.toISOString(),
});

// ── Response helpers ──────────────────────────────────────────────────────────

const send = (res, status, data) => {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
  });
  res.end(JSON.stringify(data, null, 2));
};

const err = (res, status, message) => send(res, status, { error: message });

// ── Request router ────────────────────────────────────────────────────────────

const server = http.createServer((req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*' });
    return res.end();
  }

  if (req.method !== 'GET') return err(res, 405, 'Method not allowed');

  const parsed   = url.parse(req.url, true);
  const pathname = parsed.pathname;
  const query    = parsed.query;

  console.log(`[${new Date().toISOString()}] GET ${req.url}`);

  // ── GET /health ──────────────────────────────────────────────────────────
  if (pathname === '/health') {
    return send(res, 200, {
      status: 'ok',
      projectRoot: PROJECT_ROOT,
      port: PORT,
      timestamp: new Date().toISOString(),
    });
  }

  // ── GET /file?path=... ───────────────────────────────────────────────────
  if (pathname === '/file') {
    if (!query.path) return err(res, 400, 'Missing query param: path');

    const filePath = path.resolve(PROJECT_ROOT, query.path);

    if (!isInsideRoot(filePath))  return err(res, 403, 'Access denied: path outside project root');
    if (!isSafeExtension(filePath)) return err(res, 403, 'Access denied: file type not allowed');
    if (!fs.existsSync(filePath)) return err(res, 404, `File not found: ${query.path}`);

    return send(res, 200, readFile(filePath));
  }

  // ── GET /feature?name=... ────────────────────────────────────────────────
  if (pathname === '/feature') {
    if (!query.name) return err(res, 400, 'Missing query param: name');

    const featureDir = path.resolve(PROJECT_ROOT, 'src', 'features', query.name);

    if (!isInsideRoot(featureDir)) return err(res, 403, 'Access denied');
    if (!fs.existsSync(featureDir)) return err(res, 404, `Feature not found: ${query.name}`);

    const includeTests = query.includeTests === 'true';
    const filePaths    = walkDir(featureDir, includeTests);

    if (filePaths.length === 0) return err(res, 404, `No JS/JSX files found in feature: ${query.name}`);

    const files = {};
    for (const fp of filePaths) {
      const rel = path.relative(PROJECT_ROOT, fp).replace(/\\/g, '/');
      files[rel] = readFile(fp);
    }

    return send(res, 200, {
      feature: query.name,
      fileCount: filePaths.length,
      totalLines: Object.values(files).reduce((sum, f) => sum + f.lines, 0),
      files,
    });
  }

  // ── GET /changed — archivos modificados según git diff ──────────────────
  if (pathname === '/changed') {
    try {
      const gitOut = execSync('git diff --name-only HEAD', { cwd: PROJECT_ROOT }).toString();
      const stagedOut = execSync('git diff --cached --name-only', { cwd: PROJECT_ROOT }).toString();

      const allChanged = [...new Set([
        ...gitOut.split('\n'),
        ...stagedOut.split('\n'),
      ])]
        .map(f => f.trim())
        .filter(f => f && /\.(js|jsx)$/.test(f) && !f.includes('.test.'));

      if (allChanged.length === 0) {
        return send(res, 200, { message: 'No changed JS/JSX files', files: {} });
      }

      const files = {};
      for (const rel of allChanged) {
        const abs = path.resolve(PROJECT_ROOT, rel);
        if (fs.existsSync(abs) && isInsideRoot(abs)) {
          files[rel] = readFile(abs);
        }
      }

      return send(res, 200, {
        changedCount: Object.keys(files).length,
        totalLines: Object.values(files).reduce((sum, f) => sum + f.lines, 0),
        files,
      });
    } catch (e) {
      return err(res, 500, `Git error: ${e.message}. Is this a git repository?`);
    }
  }

  // ── GET /list — listar todos los features disponibles ───────────────────
  if (pathname === '/list') {
    const featuresDir = path.resolve(PROJECT_ROOT, 'src', 'features');
    if (!fs.existsSync(featuresDir)) {
      return err(res, 404, 'src/features directory not found');
    }
    const features = fs.readdirSync(featuresDir, { withFileTypes: true })
      .filter(e => e.isDirectory())
      .map(e => e.name);

    return send(res, 200, { features, projectRoot: PROJECT_ROOT });
  }

  return err(res, 404, `Unknown endpoint: ${pathname}. Available: /health /file /feature /changed /list`);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(' Server ready. Endpoints:\n');
  console.log(`   GET http://localhost:${PORT}/health`);
  console.log(`   GET http://localhost:${PORT}/list`);
  console.log(`   GET http://localhost:${PORT}/file?path=src/features/users/usersSlice.js`);
  console.log(`   GET http://localhost:${PORT}/feature?name=users`);
  console.log(`   GET http://localhost:${PORT}/changed`);
  console.log('\n Press Ctrl+C to stop.\n');
});

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') {
    console.error(`\n Port ${PORT} already in use. Try: node review-server.js --port 3748\n`);
  } else {
    console.error('\n Server error:', e.message);
  }
  process.exit(1);
});
