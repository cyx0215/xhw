
(function(){
  document.getElementById('theme').onclick=toggleTheme;
  var cards=[].slice.call(document.querySelectorAll('.card'));
  // 首屏内的卡片立即就位（错峰淡入仍保留），首屏以下的才等滚动触发——
  // 否则整页截图 / 打印 / Ctrl+F 跳转时下方卡片停在透明态
  var fold=innerHeight, below=[];
  cards.forEach(function(c,i){
    c.style.transitionDelay=(i*60)+'ms';
    if(c.getBoundingClientRect().top<fold) c.classList.add('in');
    else below.push(c);
  });
  reveal(below);
  // 入场延迟只用于首次，避免之后悬停也跟着延迟
  setTimeout(function(){ cards.forEach(function(c){ c.style.transitionDelay=''; }); },
    260+cards.length*60);

  // 卡片上的 EPUB / TXT 下载角标：截住外层 <a> 的跳转，改下对应文件
  document.addEventListener('click',function(e){
    var dl=e.target.closest('.dl');
    if(dl && dl.dataset.href){
      e.preventDefault();
      e.stopPropagation();
      location.href=dl.dataset.href;
    }
  });

  // 继续阅读：读阅读页存下的位置（xhw-pos-<书名>），换算成章名 + 全书进度
  cards.forEach(function(c){
    var st=null, toc=null;
    try{ st=JSON.parse(localStorage.getItem('xhw-pos-'+c.dataset.book)||'null'); }catch(e){}
    if(!st || st.ch==null) return;
    try{ toc=JSON.parse(c.dataset.toc||'[]'); }catch(e){ return; }
    var path=st.path||c.dataset.path;
    var chain=toc.filter(function(t){ return !t[3] || t[3]===path; });
    var ix=-1;
    for(var i=0;i<chain.length;i++) if(chain[i][0]===String(st.ch)){ ix=i; break; }
    if(ix<0) return;
    var ratio=Math.min(1,Math.max(0,+st.ratio||0));
    var pct=Math.round((ix+ratio)/chain.length*100);
    var box=c.querySelector('.resume');
    var e=chain[ix], br=e[4]?'（'+e[4]+'线）':'';
    box.querySelector('.resume-t').textContent=
      (pct>=100?'已读完':'读到第 '+e[1]+' 章'+br+' · '+e[2])+' · '+pct+'%';
    box.querySelector('b').style.width=pct+'%';
    box.hidden=false;
  });

  // 滚动后工具条显出分隔线
  var tb=document.getElementById('shelf-tools'), raf=0;
  addEventListener('scroll',function(){
    if(raf) return;
    raf=requestAnimationFrame(function(){
      raf=0; tb.classList.toggle('lifted',scrollY>10);
    });
  },{passive:true});

  // 码字日历：近 119 天。每日净增由构建时按书算好（新导入/删除的书不计入）
  var hist=document.getElementById('word-history'), cal=document.getElementById('cal');
  if(hist && cal){
    var payload={};
    try{ payload=JSON.parse(hist.textContent||'{}')||{}; }catch(e){}
    var days=payload.days||{}, net=payload.net||{};
    var keys=Object.keys(days).sort();
    if(!keys.length){
      // 公开构建不带进度数据：整块隐藏
      var wrap=cal.closest('.cal-wrap');
      if(wrap) wrap.style.display='none';
    }else{
      var cells=[], today=Date.now(), lvl;
      for(var i=118;i>=0;i--){
        var d=new Date(today-i*86400000);
        // 用本地日期取格子（进度.json 按本地日期记快照）；toISOString 是 UTC，
        // 凌晨 0-8 点构建会把今天的字数画到昨天的格子上
        var k=d.getFullYear()+'-'
            +String(d.getMonth()+1).padStart(2,'0')+'-'
            +String(d.getDate()).padStart(2,'0');
        var v=net[k]||0;
        lvl=0;
        if(v>=300) lvl=1;
        if(v>=1000) lvl=2;
        if(v>=2500) lvl=3;
        if(v>=5000) lvl=4;
        cells.push('<i class="l'+lvl+'" title="'+k+' · '
          +(v>0?('+'+v+' 字'):(v<0?(v+' 字'):'无记录'))+'"></i>');
      }
      cal.innerHTML=cells.join('');
      var last=days[keys[keys.length-1]];
      document.getElementById('cal-note').textContent=keys.length<2
        ? '从 '+keys[0]+' 开始记录 · 之后每天的净增会画在这里'
        : '最近一次构建累计约 '+Math.round(last/10000*10)/10+' 万字 · 深色＝当日净增更多';
    }
  }
})();
