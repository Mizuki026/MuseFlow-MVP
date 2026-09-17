// UI prototype only. Static files; no generation API or persistence.
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('.', import.meta.url));
const types = {'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8'};
http.createServer(async (req, res) => {
  const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  const filename = pathname === '/' ? 'index.html' : pathname.replace(/^\//, '');
  if (!['index.html', 'style.css', 'app.js'].includes(filename)) {
    res.writeHead(404); res.end('Not found'); return;
  }
  try {
    const data = await readFile(path.join(root, filename));
    res.writeHead(200, {'Content-Type': types[path.extname(filename)], 'Cache-Control': 'no-store'});
    res.end(data);
  } catch { res.writeHead(500); res.end('Unable to load prototype'); }
}).listen(4173, '127.0.0.1', () => console.log('MuseFlow Demo: http://127.0.0.1:4173'));
