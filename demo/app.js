/* THROWAWAY PROTOTYPE: Does a studio, guided creator, or task console best fit MuseFlow?
   Three structural variants on one route, ?variant=A|B|C. All state is in memory. */
const paths = {
  spark:'m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z',
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  clock:'M12 8v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  plus:'M12 5v14 M5 12h14', chevron:'m9 5 7 7-7 7', back:'m15 5-7 7 7 7',
  close:'m6 6 12 12 M6 18 18 6', upload:'M12 16V3 m-4 4 4-4 4 4 M4 14v6h16v-6',
  arrow:'M5 12h14 m-5-5 5 5-5 5', search:'M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14 m5-2 6 6',
  image:'M3 3h18v18H3z m0 13 5-5 5 5 3-3 5 5 M15 7h.01',
  check:'m5 12 4 4L19 6', info:'M12 11v6 M12 7h.01 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  retry:'M20 7v5h-5 M4 17v-5h5 M6 6a8 8 0 0 1 14 6 M4 12a8 8 0 0 0 14 6',
  download:'M12 3v12 m-4-4 4 4 4-4 M4 16v5h16v-5',
  sliders:'M4 6h16 M4 12h16 M4 18h16 M8 4v4 M15 10v4 M10 16v4',
  folder:'M3 6h7l2 3h9v12H3z', list:'M9 6h12 M9 12h12 M9 18h12 M3 6h.01 M3 12h.01 M3 18h.01',
  mark:'M3 19V5l9 10L21 5v14', alert:'M12 8v5 M12 17h.01 M12 3 2 21h20L12 3Z'
};
const icon = name => '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="'+paths[name]+'"/></svg>';
const esc = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
// Original vector illustrations: deliberately simulated output, no remote image dependencies.
function artwork(kind = 0) {
  const themes = [
    ['#d4e1dc','#b3c7b9','#e8bc8f','#ce875c','#e9eddf'],
    ['#eee1cf','#cfb398','#f5d6b2','#ae7958','#fcf6df'],
    ['#c1cfdb','#718baa','#e4b5a1','#b66f65','#e9ded6'],
    ['#9eb5b2','#527977','#e2cbb0','#ccaa7c','#e9debf'],
    ['#e7bfa7','#c99072','#eed9b4','#cdad78','#eee6ce'],
    ['#bdbdd7','#858aab','#ead4c5','#b28d92','#ded9e6']
  ];
  const c = themes[kind % 6];
  const defs = '<defs><linearGradient id="sky" x2=".1" y2="1"><stop stop-color="'+c[0]+'"/><stop offset="1" stop-color="'+c[4]+'"/></linearGradient><linearGradient id="wall" x2="1" y2=".3"><stop stop-color="'+c[2]+'"/><stop offset="1" stop-color="'+c[3]+'"/></linearGradient><linearGradient id="ball" x1=".1" y1=".1" x2=".9" y2="1"><stop stop-color="'+c[4]+'"/><stop offset=".55" stop-color="'+c[2]+'"/><stop offset="1" stop-color="'+c[3]+'"/></linearGradient><filter id="grain"><feTurbulence type="fractalNoise" baseFrequency=".62" numOctaves="3" stitchTiles="stitch"/><feColorMatrix type="saturate" values="0"/><feComponentTransfer><feFuncA type="linear" slope=".1"/></feComponentTransfer><feBlend in="SourceGraphic" mode="multiply"/></filter><filter id="shadow"><feGaussianBlur stdDeviation="10"/></filter><clipPath id="arch"><path d="M175 435V220a75 75 0 0 1 150 0v215Z"/></clipPath></defs>';
  let scene;
  if (kind % 3 === 0) {
    scene = '<path d="M0 370Q130 250 280 330T600 325v300H0Z" fill="'+c[1]+'"/><path d="M0 440Q130 330 340 430T650 420v200H0Z" fill="'+c[2]+'"/><path d="M135 458V203a115 115 0 0 1 230 0v255Z" fill="url(#wall)"/><path d="M175 435V220a75 75 0 0 1 150 0v215Z" fill="'+c[1]+'"/><g clip-path="url(#arch)"><rect x="175" y="125" width="150" height="310" fill="url(#sky)"/><path d="M155 350q60-80 200-10v180H155" fill="'+c[1]+'"/><path d="M155 385q90-30 200 20v150H155" fill="'+c[0]+'"/></g><path d="m135 458 175 80 55-80Z" fill="'+c[3]+'" opacity=".3"/><ellipse cx="382" cy="450" rx="53" ry="9" fill="#555" opacity=".12" filter="url(#shadow)"/><circle cx="385" cy="412" r="36" fill="url(#ball)"/><path d="M120 600 205 435h85l70 165" fill="'+c[4]+'" opacity=".62"/>';
  } else if (kind % 3 === 1) {
    scene = '<path d="M0 280q140-50 500 50v300H0Z" fill="'+c[2]+'"/><ellipse cx="273" cy="452" rx="138" ry="26" fill="'+c[3]+'" opacity=".28" filter="url(#shadow)"/><path d="M100 470v-73l282-17v80Z" fill="'+c[3]+'"/><path d="m100 397 79-51 244 16-41 18Z" fill="'+c[4]+'"/><path d="m382 380 41-18v63l-41 35Z" fill="'+c[1]+'"/><path d="M194 170h106v44c0 39 42 64 42 112 0 49-40 72-95 72s-95-23-95-72c0-48 42-73 42-112Z" fill="url(#wall)"/><ellipse cx="247" cy="171" rx="53" ry="13" fill="'+c[3]+'"/><ellipse cx="247" cy="171" rx="41" ry="8" fill="#544c45"/><path d="M265 171q-40-58-12-106" stroke="'+c[1]+'" fill="none" stroke-width="4"/><path d="M251 106q-80 3-58-40 47-2 58 40 M249 125q65-64 70-8-39 24-70 8" fill="'+c[1]+'"/><circle cx="356" cy="360" r="29" fill="url(#ball)"/>';
  } else {
    scene = '<rect y="323" width="500" height="277" fill="'+c[1]+'"/><circle cx="375" cy="170" r="60" fill="'+c[4]+'" opacity=".95"/><path d="M0 340 170 195l115 145Z" fill="'+c[2]+'"/><path d="m170 195 12 145h103Z" fill="'+c[3]+'"/><path d="m250 340 88-83 112 83Z" fill="'+c[0]+'"/><path d="M0 375q130-45 270 20t270-13 M-30 413q160-45 300 20t270-13 M-30 461q160-45 300 20t270-13 M-30 520q160-45 300 20t270-13" stroke="'+c[4]+'" stroke-width="2" fill="none" opacity=".48"/><path d="m70 418 105-28 47 51-105 28Z" fill="'+c[2]+'"/><path d="m117 469 105-28v24l-105 28Z" fill="'+c[3]+'"/>';
  }
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="500" height="600" viewBox="0 0 500 600">'+defs+'<g filter="url(#grain)"><rect width="500" height="600" fill="url(#sky)"/>'+scene+'</g></svg>');
}
const seeds = [
  ['静谧之门','一座奶油色拱门伫立在宁静的沙丘上，鼠尾草绿远山，柔和晨光，极简超现实主义建筑摄影。',0,'SUCCEEDED'],
  ['午后，陶与光','一只手工陶瓷花瓶与橄榄枝，暖米色背景，柔和侧光，质朴肌理，静物产品摄影。',1,'SUCCEEDED'],
  ['海面上的几何','粉色金字塔与平静海面，落日圆盘，雾蓝色与杏色，梦境般的极简几何构图。',2,'SUCCEEDED'],
  ['雾中绿洲','青绿色山谷间的石质拱门，清晨薄雾，宁静自然，柔和光影，建筑摄影。',3,'RUNNING'],
  ['暖色静物研究','暖杏色背景上的陶土器皿和植物，窗边光影，胶片颗粒，杂志静物摄影。',4,'FAILED'],
  ['暮色漫游','薰衣草色暮光下的海面和几何山峰，宁静水波，超现实主义风景插画。',5,'SUCCEEDED'],
  ['沙丘回响','暖调沙丘与孤独拱门，柔软的漫反射，极简艺术摄影。',0,'SUCCEEDED'],
  ['蓝调时刻','雾蓝色海岸，抽象几何结构与远山，宁静的日落光线。',2,'SUCCEEDED']
];
let sequence = 108;
const tasks = seeds.map(([title,prompt,art,status], i) => ({
  id:'MF-'+(108-i),title,prompt,art,status,ratio:'3:4',ref:null,retries:0,
  created:Date.now()-i*3600000, scenario:'success', originalId:null,
  error:status==='FAILED'?'模型服务暂时不可用，自动重试已达上限。你可以稍后重新提交。':'',
  events:status==='SUCCEEDED'?['任务已提交','开始生成','示例图片已就绪']:status==='FAILED'?['任务已提交','开始生成','服务不可用 · 已重试 2 次','生成失败']:['任务已提交','正在生成'],
}));
tasks.find(t=>t.status==='FAILED').retries=2;
const savedVariant = new URLSearchParams(location.search).get('variant');
const state = {variant:['A','B','C'].includes(savedVariant)?savedVariant:'A',page:'studio',filter:'ALL',query:'',sort:'new',pageNumber:1,mode:'text',prompt:seeds[0][1],ratio:'3:4',ref:null,refName:'',art:0,scenario:'success',activeId:null,modal:null,busy:false};
const timers=new Set();
const statusText={QUEUED:'排队中',RUNNING:'生成中',SUCCEEDED:'已完成',FAILED:'生成失败'};
const labels={A:'创作工作台',B:'引导式创建',C:'任务控制台'};
const app=document.querySelector('#app');
const overlay=document.querySelector('#overlay');
let previousFocus=null;
const badge = task => '<span class="badge '+task.status+'"><span class="dot"></span>'+statusText[task.status]+'</span>';
function notify(message){document.querySelector('#toast').className='toast';document.querySelector('#toast').textContent=message;clearTimeout(notify.timer);notify.timer=setTimeout(()=>{document.querySelector('#toast').className='';document.querySelector('#toast').textContent='';},3200);}
function later(fn,ms){const timer=setTimeout(()=>{timers.delete(timer);fn();},ms);timers.add(timer);}
function sidebar(){return '<aside class="sidebar"><a href="./" class="brand" aria-label="MuseFlow 首页"><div class="brand-mark">'+icon('mark')+'</div><span>MuseFlow</span><sup>BETA</sup></a><div class="side-nav"><div class="workspace-label">WORKSPACE</div><button class="nav-btn '+(state.page==='studio'?'active':'')+'" data-action="nav" data-page="studio">'+icon('spark')+'<span>创作工作台</span></button><button class="nav-btn '+(state.page==='history'?'active':'')+'" data-action="nav" data-page="history">'+icon('clock')+'<span>任务历史</span><span class="count">'+tasks.length+'</span></button></div><div class="side-bottom"><div class="demo-note"><strong>'+icon('spark')+' 让灵感，有迹可循。</strong>从一个念头，到一张作品。<br>你的每一次创作都值得被记录。</div><button class="nav-btn" data-action="help">'+icon('info')+'<span>使用指南</span></button><div class="row user"><div class="avatar">M</div><div>我的工作空间<span>个人演示空间</span></div></div></div></aside>';}
function header(){return '<header class="topbar"><div class="breadcrumb">工作空间 '+icon('chevron')+' <strong>'+(state.page==='studio'?labels[state.variant]:'任务历史')+'</strong></div><div class="row"><span class="demo-pill"><span class="dot"></span> INTERACTIVE DEMO</span><button class="text-btn" data-action="settings">'+icon('sliders')+' 演示设置</button></div></header><nav class="mobile-nav" aria-label="移动端导航"><button data-action="nav" data-page="studio" class="'+(state.page==='studio'?'active':'')+'">创作工作台</button><button data-action="nav" data-page="history" class="'+(state.page==='history'?'active':'')+'">任务历史</button><button data-action="help">使用指南</button></nav>';}
function switcher(){return '<div class="switcher" aria-label="原型布局切换"><span>布局探索</span><button class="arrow" data-action="cycle" data-direction="-1" aria-label="上一个布局">'+icon('back')+'</button>'+['A','B','C'].map(v=>'<button data-action="variant" data-variant="'+v+'" class="'+(state.variant===v?'active':'')+'" aria-pressed="'+(state.variant===v)+'">'+v+' · '+labels[v]+'</button>').join('')+'<button class="arrow" data-action="cycle" data-direction="1" aria-label="下一个布局">'+icon('chevron')+'</button></div>';}
function composer(){return '<section class="composer" aria-label="创建生成任务"><div class="composer-top"><div class="section-title">'+icon('spark')+' 开始新的创作</div><div class="tabs" aria-label="生成模式"><button data-action="mode" data-mode="text" class="'+(state.mode==='text'?'active':'')+'">文字生图</button><button data-action="mode" data-mode="image" class="'+(state.mode==='image'?'active':'')+'">参考图生成</button></div></div><form id="create-form"><div class="composer-body"><label class="field"><span class="field-head">画面描述<button type="button" class="text-btn" data-action="example">换个灵感 '+icon('retry')+'</button></span><div class="prompt-wrap"><textarea id="prompt" maxlength="1000" placeholder="描述你脑海中的画面，比如主体、场景、光线和风格…" required>'+esc(state.prompt)+'</textarea><span class="char-count">'+state.prompt.length+' / 1000</span></div><span class="prompt-examples">'+['极简建筑','静物摄影','梦境风景'].map((label,i)=>'<button type="button" class="chip" data-action="example" data-example="'+i+'">'+label+'</button>').join('')+'</span></label><div class="field"><div class="field-head">参考图片<span class="optional">'+(state.mode==='image'?'请添加一张参考图':'可选 · 让灵感更具体')+'</span></div>'+(state.ref?'<div class="ref-preview"><img alt="已选参考图" src="'+state.ref+'"><span>'+esc(state.refName)+'</span><button type="button" class="icon-btn" data-action="remove-ref" aria-label="移除参考图">'+icon('close')+'</button></div>':'<label class="upload" tabindex="0" role="button" aria-label="上传参考图片">'+icon('upload')+'<p>点击上传，或拖放图片至此</p><small>PNG / JPG / WEBP · 最大 10 MB</small><input type="file" id="reference" accept="image/png,image/jpeg,image/webp" hidden></label><button type="button" class="text-btn" style="margin-top:9px;font-size:10px" data-action="sample-ref">'+icon('image')+' 使用示例参考图</button>')+'</div><div class="field"><div class="field-head">画面比例<span class="optional">每次生成 1 张</span></div><div class="ratio-group">'+['1:1','3:4','16:9'].map(r=>'<button type="button" class="ratio '+(state.ratio===r?'active':'')+'" data-action="ratio" data-ratio="'+r+'" aria-pressed="'+(state.ratio===r)+'"><i></i><span>'+r+'</span></button>').join('')+'</div></div><div class="model-box"><div class="row"><div class="model-icon">'+icon('spark')+'</div><span>Muse Image <small> / 模拟模型</small></span></div><span class="dot" style="color:#8cac93"></span></div></div><div class="composer-footer"><button type="submit" class="primary full" '+(state.busy?'disabled':'')+'>'+icon('spark')+' '+(state.busy?'正在提交…':'生成图像')+icon('arrow')+'</button><p class="footer-caption">本地交互演示 · 不调用模型 · 不产生费用</p></div></form></section>';}
function taskVisual(task){if(task.status==='SUCCEEDED')return '<img src="'+artwork(task.art)+'" alt="'+esc(task.title)+' · 模拟生成的示例插画"><span class="card-badge">示例图</span>';return '<div class="task-visual '+(task.status==='FAILED'?'error':'running')+'"><div class="orb">'+icon(task.status==='FAILED'?'alert':'spark')+'</div><h3>'+(task.status==='FAILED'?'这次没能完成':task.status==='QUEUED'?'灵感正在排队':'正在描绘你的灵感')+'</h3><p>'+(task.status==='FAILED'?'服务暂时不可用，点开可重新尝试':task.retries?'刚遇到一次超时，正在自动重试':'可以继续创作，我们会保留任务状态')+'</p>'+(task.status==='FAILED'?'':'<div class="progress-rail"><span></span></div>')+'</div>';}
function filtered(){return tasks.filter(t=>(state.filter==='ALL'||(state.filter==='ACTIVE'?['RUNNING','QUEUED'].includes(t.status):state.filter===t.status))&&(!state.query||(t.title+' '+t.prompt+' '+t.id).toLowerCase().includes(state.query.toLowerCase()))).sort((a,b)=>state.sort==='old'?a.created-b.created:b.created-a.created);}
function controls(){return '<div class="results-head"><div class="results-title">'+(state.page==='history'?'所有任务':'最近创作')+'<small>'+tasks.length+' 个任务</small></div><label class="search">'+icon('search')+'<input id="search" aria-label="搜索任务" placeholder="搜索创作…" value="'+esc(state.query)+'"></label></div><div class="filters">'+[['ALL','全部'],['SUCCEEDED','已完成'],['ACTIVE','进行中'],['FAILED','失败']].map(([key,label])=>'<button class="filter '+(state.filter===key?'active':'')+'" data-action="filter" data-filter="'+key+'">'+label+'</button>').join('')+'<select id="sort" aria-label="排序"><option value="new" '+(state.sort==='new'?'selected':'')+'>最新优先</option><option value="old" '+(state.sort==='old'?'selected':'')+'>最早优先</option></select></div>';}
function pagination(total){const n=Math.max(1,Math.ceil(total/6));return '<div class="gallery-foot"><span>'+total+' 个任务 · 仅保存在当前演示会话</span><div class="pages">'+Array.from({length:n},(_,i)=>'<button class="'+(state.pageNumber===i+1?'active':'')+'" data-action="page" data-number="'+(i+1)+'" aria-label="第 '+(i+1)+' 页">'+(i+1)+'</button>').join('')+'</div></div>';}
function gallery(){const all=filtered();state.pageNumber=Math.min(state.pageNumber,Math.max(1,Math.ceil(all.length/6)));return '<div class="gallery">'+(all.slice((state.pageNumber-1)*6,state.pageNumber*6).map(task=>'<article class="card"><button class="card-main" data-action="detail" data-id="'+task.id+'" aria-label="查看 '+esc(task.title)+'"><div class="art-wrap">'+taskVisual(task)+'</div><div class="card-body"><div class="card-title">'+esc(task.title)+'</div><div class="card-meta"><span>'+task.id+' · '+task.ratio+'</span><span>'+(task.status==='SUCCEEDED'?'已完成':statusText[task.status])+'</span></div></div></button></article>').join('')||'<div class="empty">'+icon('search')+'<p>没有找到对应的创作</p><button class="secondary" data-action="clear-search">清除筛选</button></div>')+'</div>'+pagination(all.length);}
function results(){return '<section class="results">'+controls()+'<div id="result-items">'+gallery()+'</div><div class="inspiration"><div><strong>好作品，从一句好描述开始。</strong><p>试试「主体 + 场景 + 光线 + 风格」，让画面更接近想象。</p></div><button data-action="example">试试示例 ↗</button></div></section>';}
function variantA(){return '<div class="page-head"><div><div class="eyebrow">YOUR CREATIVE SPACE</div><h1>让想象，开始成像<span style="color:var(--blue)">。</span></h1><p>写下脑海中的画面，剩下的交给 MuseFlow。</p></div><div class="day-label"><b>一点灵感，无限可能</b>IMAGE GENERATION WORKSPACE</div></div><div class="studio-grid">'+composer()+results()+'</div>';}
function variantB(){return '<div class="focus-hero"><span class="eyebrow">FROM A THOUGHT TO AN IMAGE</span><h1>今天，想创造什么？</h1><p>从一段描述开始，让每一个灵感慢慢成形。</p></div><div class="focus-workbench">'+composer()+'<div class="focus-preview"><img src="'+artwork(state.art)+'" alt="当前灵感的示例插画"><div class="focus-preview-caption"><span class="eyebrow" style="color:#ffffffb9">INSPIRATION / 示例作品</span><h2>'+seeds[state.art][0]+'</h2><p>你的画面，你的节奏。一次专注于一张作品。</p></div></div></div><div class="focus-history">'+results()+'</div>';}
function taskTable(){const all=filtered();state.pageNumber=Math.min(state.pageNumber,Math.max(1,Math.ceil(all.length/6)));return '<div class="table-wrap"><table><thead><tr><th>创作任务</th><th>状态</th><th>画面比例</th><th>重试次数</th><th>操作</th></tr></thead><tbody>'+all.slice((state.pageNumber-1)*6,state.pageNumber*6).map(t=>'<tr><td><div class="table-task"><img src="'+artwork(t.art)+'" alt="示例缩略图"><button data-action="detail" data-id="'+t.id+'"><strong>'+esc(t.title)+'</strong><small>'+t.id+' · 模拟模型</small></button></div></td><td>'+badge(t)+'</td><td>'+t.ratio+'</td><td>'+t.retries+' 次</td><td><button class="text-btn" data-action="'+(t.status==='FAILED'?'retry':'detail')+'" data-id="'+t.id+'">'+(t.status==='FAILED'?'重新尝试':'查看详情')+' ↗</button></td></tr>').join('')+'</tbody></table>'+(all.length?'':'<div class="empty">没有找到任务 <button class="text-btn" data-action="clear-search">清除筛选</button></div>')+'</div>'+pagination(all.length);}
function variantC(){return '<div class="console-head"><div><h1>每一次创作，都有进展。</h1><p>统一查看生成任务、执行状态与图片结果。</p></div><button class="primary" data-action="new">'+icon('plus')+' 新建任务</button></div><div class="stats">'+[['全部任务',tasks.length,'folder'],['已完成',tasks.filter(t=>t.status==='SUCCEEDED').length,'check'],['进行中',tasks.filter(t=>['QUEUED','RUNNING'].includes(t.status)).length,'clock'],['需关注',tasks.filter(t=>t.status==='FAILED').length,'alert']].map(([label,n,i])=>'<div class="stat"><div class="row space"><span>'+label+'</span>'+icon(i)+'</div><b>'+n+'</b></div>').join('')+'</div><section class="results">'+controls()+'<div id="result-items">'+taskTable()+'</div></section>';}
function render(){app.className=state.variant==='B'?'focus-shell':state.variant==='C'?'console-shell':'';app.innerHTML=sidebar()+'<div class="shell">'+header()+'<main class="main">'+(state.page==='history'?'<div class="page-head"><div><div class="eyebrow">YOUR CREATIVE JOURNEY</div><h1>每一份灵感，都在这里。</h1><p>回看创作记录，找到下一次灵感的起点。</p></div><button class="primary" data-action="nav" data-page="studio">'+icon('plus')+' 开始创作</button></div><div class="history-wide">'+results()+'</div>':state.variant==='A'?variantA():state.variant==='B'?variantB():variantC())+'<footer class="app-foot"><span>MUSEFLOW © 2026 · MADE FOR YOUR IDEAS</span><span>所有图片、任务与耗时均为演示数据</span></footer></main></div>'+switcher();if(state.modal)renderModal();}
function refreshTasks(){const box=document.querySelector('#result-items');if(box)box.innerHTML=state.variant==='C'&&state.page==='studio'?taskTable():gallery();document.querySelectorAll('.results-title small').forEach(e=>e.textContent=tasks.length+' 个任务');const count=document.querySelector('.nav-btn .count');if(count)count.textContent=tasks.length;if(state.variant==='C'&&state.page==='studio'){const nums=[tasks.length,tasks.filter(t=>t.status==='SUCCEEDED').length,tasks.filter(t=>['QUEUED','RUNNING'].includes(t.status)).length,tasks.filter(t=>t.status==='FAILED').length];document.querySelectorAll('.stat b').forEach((e,i)=>e.textContent=nums[i]);}if(state.modal==='detail')renderModal();}
function modalOpen(kind,id=null){previousFocus=document.activeElement;state.modal=kind;state.activeId=id;renderModal();document.body.style.overflow='hidden';overlay.querySelector('[data-action="close"]').focus();}
function modalClose(){state.modal=null;state.activeId=null;overlay.innerHTML='';document.body.style.overflow='';if(previousFocus?.isConnected)previousFocus.focus();}
function renderModal(){
 let title='',sub='',body='';
 if(state.modal==='detail'){
  const t=tasks.find(t=>t.id===state.activeId);if(!t)return;
  title=t.title;sub=t.id+' · '+new Date(t.created).toLocaleString('zh-CN',{hour12:false});
  body='<div class="detail-grid"><div class="detail-art">'+(t.status==='SUCCEEDED'?'<img src="'+artwork(t.art)+'" alt="生成结果示例图">':taskVisual(t))+'</div><div class="detail-info">'+badge(t)+'<h3>画面描述</h3><p>'+esc(t.prompt)+'</p><div class="specs"><div><small>画面比例</small><b>'+t.ratio+'</b></div><div><small>生成模型</small><b>Muse Image · Mock</b></div><div><small>重试次数</small><b>'+t.retries+' 次</b></div><div><small>结果说明</small><b>预置示例插画</b></div></div>'+(t.ref?'<h3>参考图片</h3><img style="height:55px;border-radius:6px" alt="该任务参考图" src="'+t.ref+'">':'')+(t.originalId?'<h3>源任务</h3><button class="text-btn" data-action="detail" data-id="'+t.originalId+'">'+t.originalId+' ↗</button>':'')+'<h3>执行记录 · 模拟</h3><ol class="timeline">'+t.events.map(e=>'<li>'+esc(e)+'</li>').join('')+'</ol>'+(t.error?'<div class="error-message">'+esc(t.error)+'</div>':'')+'<div class="detail-actions">'+(t.status==='SUCCEEDED'?'<button class="primary" data-action="download" data-id="'+t.id+'">'+icon('download')+' 下载示例图</button>':t.status==='FAILED'&&t.scenario==='fail'?'<button class="primary" data-action="reuse" data-id="'+t.id+'">修改描述</button>':t.status==='FAILED'?'<button class="primary" data-action="retry" data-id="'+t.id+'">'+icon('retry')+' 重新尝试</button>':'')+'<button class="secondary" data-action="reuse" data-id="'+t.id+'">复用参数</button></div></div></div>';
 }else if(state.modal==='settings'){
  title='演示设置';sub='只影响下一次提交 · 当前页面刷新后重置';
  body='<div class="dialog-content"><label class="field"><span class="field-head">下一次生成的模拟结果</span><select id="scenario">'+[['success','正常生成成功（约 6 秒）'],['retry','两次超时后自动恢复（约 10 秒）'],['fail','永久错误，直接失败（约 4 秒）']].map(([key,label])=>'<option value="'+key+'" '+(state.scenario===key?'selected':'')+'>'+label+'</option>').join('')+'</select></label><div class="notice">任意提示词均返回预置插画，不进行真实生成。上传图片只在当前浏览器中预览，不发送到服务端。</div><h3 style="font-size:12px;margin-top:24px">当前原型状态</h3><pre>'+esc(JSON.stringify({variant:state.variant,page:state.page,nextScenario:state.scenario,tasks:tasks.map(t=>({id:t.id,status:t.status,retries:t.retries,originalId:t.originalId}))},null,2))+'</pre><button class="secondary" data-action="reset">'+icon('retry')+' 重置全部演示数据</button></div>';
 }else if(state.modal==='new'){
  title='新建生成任务';sub='填写描述，生成一张模拟图片';body='<div class="console-composer">'+composer()+'</div>';
 }else{
  title='欢迎来到 MuseFlow';sub='一个用于验证界面与操作流程的交互原型';
  body='<div class="dialog-content"><p>① 填写画面描述，或点击示例灵感快速开始。</p><p>② 可选择参考图和比例，点击「生成图像」。</p><p>③ 任务将从排队进入生成，点击卡片查看状态与记录。</p><p>④ 完成后下载示例图，或复用参数继续创作。</p><div class="notice">在「演示设置」里切换超时重试、永久失败场景。底部可切换三种布局，任务数据会保留。按 Esc 关闭弹窗。</div><p>所有结果均为预置 SVG 插画。Demo 不调用模型、不产生费用，刷新后恢复初始数据。</p></div>';
 }
 overlay.innerHTML='<div class="backdrop"><section role="dialog" aria-modal="true" aria-labelledby="modal-title" class="dialog '+(state.modal==='detail'?'':'small-dialog')+'"><div class="dialog-head"><div><h2 id="modal-title">'+esc(title)+'</h2><small>'+esc(sub)+'</small></div><button class="icon-btn" data-action="close" aria-label="关闭弹窗">'+icon('close')+'</button></div>'+body+'</section></div>';
}
function runTask(t){
 later(()=>{t.status='RUNNING';t.events.push('Worker 开始生成');refreshTasks();},1200);
 if(t.scenario==='retry'){
  later(()=>{t.retries=1;t.events.push('第 1 次超时 · 等待自动重试');refreshTasks();},3000);
  later(()=>{t.retries=2;t.events.push('第 2 次超时 · 等待自动重试');refreshTasks();},5600);
 }
 later(()=>{
  if(t.scenario==='fail'){t.status='FAILED';t.error='模拟内容安全拒绝。这是不可重试错误，未执行自动重试；你可以修改描述后重新提交。';t.events.push('内容安全拒绝 · 未自动重试');notify('任务未完成，可在详情中查看原因');}
  else{t.status='SUCCEEDED';t.events.push('图片已就绪 · 示例结果已保存');notify('「'+t.title+'」已完成');}
  refreshTasks();
 },t.scenario==='retry'?10000:t.scenario==='fail'?4000:6000);
}
function createTask(original=null){
 if(state.busy)return;
 if(!original&&!state.prompt.trim()){notify('先描述一下你想生成的画面');document.querySelector('#prompt')?.focus();return;}
 if(!original&&state.mode==='image'&&!state.ref){notify('请先添加参考图，也可以使用示例参考图');return;}
 state.busy=true;
 const t={id:'MF-'+(++sequence),title:original?original.title+' · 重试':state.prompt.trim().slice(0,18),prompt:original?original.prompt:state.prompt.trim(),ratio:original?original.ratio:state.ratio,ref:original?original.ref:state.ref,art:original?original.art:state.art,created:Date.now(),status:'QUEUED',retries:0,scenario:original?'success':state.scenario,originalId:original?.id??null,error:'',events:['任务已提交 · 等待执行']};
 tasks.unshift(t);state.filter='ALL';state.query='';state.pageNumber=1;
 modalClose();render();notify(original?'已新建重试任务 '+t.id+'，保留原失败记录':'任务已创建，可以继续创作');runTask(t);
 later(()=>{state.busy=false;document.querySelectorAll('#create-form button[type="submit"]').forEach(b=>{b.disabled=false;b.innerHTML=icon('spark')+' 生成图像 '+icon('arrow');});},500);
}
function applyRef(file){
 if(!file)return;
 if(!['image/png','image/jpeg','image/webp'].includes(file.type)){notify('请选择 PNG、JPG 或 WEBP 图片');return;}
 if(file.size>10*1024*1024){notify('参考图请控制在 10 MB 以内');return;}
 const reader=new FileReader();reader.onload=()=>{state.ref=reader.result;state.refName=file.name;state.mode='image';render();notify('参考图已添加，仅用于本地预览');};reader.readAsDataURL(file);
}
function setVariant(v){state.variant=v;const url=new URL(location.href);url.searchParams.set('variant',v);history.replaceState(null,'',url);modalClose();render();}
function action(e){
 const target=e.target.closest('[data-action]');if(!target)return;
 const a=target.dataset.action;const t=tasks.find(t=>t.id===target.dataset.id);
 if(a==='nav'){state.page=target.dataset.page;state.filter='ALL';state.query='';state.pageNumber=1;render();window.scrollTo(0,0);}
 if(a==='variant')setVariant(target.dataset.variant);
 if(a==='cycle'){const variants=['A','B','C'];setVariant(variants[(variants.indexOf(state.variant)+Number(target.dataset.direction)+3)%3]);}
 if(a==='mode'){state.mode=target.dataset.mode;render();}
 if(a==='ratio'){state.ratio=target.dataset.ratio;render();}
 if(a==='example'){const i=target.dataset.example!==undefined?Number(target.dataset.example):(state.art+1)%6;state.art=i;state.prompt=seeds[i][1];if(state.page!=='studio'){state.page='studio';}render();if(state.variant==='C'&&!state.modal)modalOpen('new');}
 if(a==='sample-ref'){state.ref=artwork(state.art);state.refName='灵感参考 · 示例插画.svg';state.mode='image';render();}
 if(a==='remove-ref'){state.ref=null;state.refName='';render();}
 if(a==='filter'){state.filter=target.dataset.filter;state.pageNumber=1;render();}
 if(a==='page'){state.pageNumber=Number(target.dataset.number);refreshTasks();}
 if(a==='clear-search'){state.query='';state.filter='ALL';state.pageNumber=1;render();}
 if(a==='detail')modalOpen('detail',target.dataset.id);
 if(a==='close')modalClose();
 if(a==='help')modalOpen('help');
 if(a==='settings')modalOpen('settings');
 if(a==='new')modalOpen('new');
 if(a==='retry'&&t?.status==='FAILED')createTask(t);
 if(a==='reuse'&&t){state.prompt=t.prompt;state.ratio=t.ratio;state.art=t.art;state.ref=t.ref;state.refName=t.ref?'已复用的参考图':'';state.mode=t.ref?'image':'text';state.page='studio';modalClose();render();if(state.variant==='C')modalOpen('new');notify('已带入原任务参数，可编辑后再次生成');window.scrollTo(0,0);}
 if(a==='download'&&t){const link=document.createElement('a');link.href=artwork(t.art);link.download=t.id+'-demo.svg';link.click();notify('正在下载示例插画（SVG）');}
 if(a==='reset'){location.reload();}
}
document.addEventListener('click',action);
document.addEventListener('submit',e=>{if(e.target.id==='create-form'){e.preventDefault();createTask();}});
document.addEventListener('input',e=>{
 if(e.target.id==='prompt'){state.prompt=e.target.value;const el=document.querySelector('.char-count');if(el)el.textContent=state.prompt.length+' / 1000';}
 if(e.target.id==='search'){state.query=e.target.value;state.pageNumber=1;refreshTasks();}
});
document.addEventListener('change',e=>{
 if(e.target.id==='reference')applyRef(e.target.files[0]);
 if(e.target.id==='sort'){state.sort=e.target.value;refreshTasks();}
 if(e.target.id==='scenario'){state.scenario=e.target.value;renderModal();}
});
document.addEventListener('dragover',e=>{if(e.target.closest('.upload'))e.preventDefault();});
document.addEventListener('drop',e=>{if(e.target.closest('.upload')){e.preventDefault();applyRef(e.dataTransfer.files[0]);}});
overlay.addEventListener('click',e=>{if(e.target.classList.contains('backdrop'))modalClose();});
document.addEventListener('keydown',e=>{
 if(e.key==='Escape'&&state.modal){modalClose();return;}
 if(e.key==='Tab'&&state.modal){const focusable=[...overlay.querySelectorAll('button:not(:disabled),input:not([hidden]),select,textarea,[href]')];const first=focusable[0],last=focusable.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}return;}
 if(e.target.closest('.upload')&&['Enter',' '].includes(e.key)){e.preventDefault();document.querySelector('#reference')?.click();return;}
 if(state.modal||e.target.closest('input,textarea,select,[contenteditable]'))return;
 if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const variants=['A','B','C'];setVariant(variants[(variants.indexOf(state.variant)+(e.key==='ArrowRight'?1:2))%3]);}
});
window.addEventListener('popstate',()=>{const v=new URLSearchParams(location.search).get('variant');state.variant=['A','B','C'].includes(v)?v:'A';render();});
render();
// A seeded running task finishes on its own, just like a newly submitted task.
const initial=tasks.find(t=>t.status==='RUNNING');later(()=>{initial.status='SUCCEEDED';initial.events.push('示例图片已就绪');refreshTasks();},14000);
