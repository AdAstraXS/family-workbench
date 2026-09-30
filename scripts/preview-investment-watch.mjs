// Explicit static allowlist. Never imports Django, reads .env, or accesses a database.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
const root = new URL('../docs/investment-watch/',import.meta.url);
const paths = new Map([['/','demo/index.html'],['/index.html','demo/index.html'],['/base.css','demo-base.css'],...['app.js','engine.mjs','data.json','styles.css'].map(f=>['/'+f,'demo/'+f])]);
const mime = {html:'text/html',css:'text/css',js:'text/javascript',mjs:'text/javascript',json:'application/json'};
export function createPreviewServer(){return http.createServer(async(req,res)=>{
  if(!['GET','HEAD'].includes(req.method)){res.writeHead(405);res.end();return;}
  const path=paths.get(new URL(req.url,'http://localhost').pathname);
  if(!path){res.writeHead(404);res.end('Not found');return;}
  try{const body=await readFile(new URL(path,root));res.writeHead(200,{'Content-Type':mime[path.split('.').pop()]+'; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"});res.end(req.method==='HEAD'?undefined:body);}catch{res.writeHead(500);res.end('Preview unavailable');}
});}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href){const port=Number(process.env.INVESTMENT_WATCH_DEMO_PORT??4318);createPreviewServer().listen(port,'127.0.0.1',()=>console.log(`Investment watch demo: http://127.0.0.1:${port}`));}
