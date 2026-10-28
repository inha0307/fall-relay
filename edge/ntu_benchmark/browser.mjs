// One real Chromium page uses the deployed AlertProvider, SSE and browser receipt path.
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const cfg=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const {chromium}=await import(pathToFileURL(cfg.playwright_module));
const log=x=>fs.appendFileSync(cfg.output,JSON.stringify({experiment_id:cfg.experiment_id,node_wall_ms:Date.now(),...x})+'\n');
const browser=await chromium.launch({headless:true,executablePath:cfg.chromium,args:['--no-sandbox','--disable-dev-shm-usage']});
try{
 const context=await browser.newContext({viewport:{width:1280,height:900}});
 await context.exposeBinding('benchLog',(_,x)=>log(x));
 await context.addInitScript(()=>{
   const old=EventSource.prototype.addEventListener;
   EventSource.prototype.addEventListener=function(type,fn,options){
    if(type==='alert'||type==='snapshot'){
     const wrapped=function(e){const at=Date.now(),mono=performance.now();let data;try{data=JSON.parse(e.data);}catch{}
      window.__benchFirstSSE=true;window.benchLog({kind:'browser_receive',event_type:type,browser_wall_ms:at,browser_perf_ms:mono,data});
      return fn.call(this,e);
     };
     return old.call(this,type,wrapped,options);
    }
    return old.call(this,type,fn,options);
   };
 });
 const login=await context.request.post(cfg.web+'/api/auth/login',{data:{userid:cfg.login,password:cfg.password}});
 if(login.status()!==200)throw Error('test login failed '+login.status());
 const page=await context.newPage();
 page.on('pageerror',e=>log({kind:'pageerror',error:e.message}));
 page.on('response',r=>{if(/\/received$/.test(r.url()))log({kind:'receipt_response',url:r.url(),status:r.status()});});
 await page.goto(cfg.web+'/alerts');
 await page.waitForFunction(()=>window.__benchFirstSSE===true,{},{timeout:30000});
 log({kind:'ready',browser:browser.version(),web:cfg.web,role:'admin',scope:'1,2',network:'same WSL host loopback to isolated Next proxy'});
 fs.writeFileSync(cfg.ready_file,JSON.stringify({ready_at:Date.now(),pid:process.pid}));
 await page.screenshot({path:cfg.screenshot});
 while(!fs.existsSync(cfg.stop_file)){
  await page.evaluate(async()=>{const start=Date.now(),m=performance.now();try{const r=await fetch('/api/alerts/status');await r.json();window.benchLog({kind:'browser_liveness',start_wall_ms:start,end_wall_ms:Date.now(),rtt_ms:performance.now()-m,status:r.status});}catch(e){window.benchLog({kind:'browser_liveness_failed',error:String(e)});}});
  await page.waitForTimeout(5000);
 }
 await page.screenshot({path:cfg.screenshot.replace('.png','-final.png')});
}finally{await browser.close();log({kind:'closed'});}
