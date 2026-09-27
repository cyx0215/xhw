
(function(){
  // ---- 成人向内容门：确认一次，之后跨页保持
  if(document.body.dataset.adult==='1'){
    var gateOk=false;
    try{ gateOk=!!localStorage.getItem('xhw-adult-gate'); }catch(e){}
    var gate=document.getElementById('gate');
    if(gate && !gateOk){
      gate.classList.add('on');
      document.documentElement.style.overflow='hidden';
      document.getElementById('gate-ok').onclick=function(){
        try{ localStorage.setItem('xhw-adult-gate','1'); }catch(e){}
        gate.classList.remove('on');
        document.documentElement.style.overflow='';
      };
      document.getElementById('gate-no').onclick=function(){
        if(document.body.dataset.shelf==='1') location.href='index.html';
        else if(history.length>1) history.back();
        else location.href='about:blank';
      };
    }
  }

  // 关掉浏览器自带的滚动位置恢复，避免和下面按「章号+比例」算出的位置打架
  if('scrollRestoration' in history) history.scrollRestoration='manual';
  var PK='xhw-pos-'+document.body.dataset.book;
  var tools=document.getElementById('tools'), toc=document.getElementById('toc'),
      scrim=document.getElementById('scrim'), bar=document.getElementById('bar'),
      now=document.getElementById('now'), top=document.getElementById('top'),
      cover=document.getElementById('cover');
  var links=[].slice.call(toc.querySelectorAll('a[data-ch]'));
  var allChapters=[].slice.call(document.querySelectorAll('.chapter'));
  var hasFork=document.body.classList.contains('has-fork');
  // 双线（可选配置）：线 key 与展示名由构建时按本书实际分叉注入
  var BRANCHES=window.XHW_BRANCHES||['mei','dao'];
  var BLABELS=window.XHW_BRANCH_LABELS||{};
  var wide=function(){return window.innerWidth>=1080};
  var calm=matchMedia('(prefers-reduced-motion: reduce)').matches;

  function visibleChapters(){
    var path=document.body.dataset.path||BRANCHES[0];
    return allChapters.filter(function(c){
      var b=c.dataset.branch;
      return !b || b===path;
    });
  }

  // ---- 偏好（含双线 path）
  var st={scale:1,ch:null,ratio:0,path:BRANCHES[0]};
  try{ st=Object.assign(st,JSON.parse(localStorage.getItem(PK)||'{}')); }catch(e){}
  if(BRANCHES.indexOf(st.path)<0) st.path=BRANCHES[0];
  var sIdx=null;  // 全文搜索索引（懒构建；切线后失效）
  var save=function(){ try{localStorage.setItem(PK,JSON.stringify(st))}catch(e){} };
  var applyScale=function(){ document.documentElement.style.setProperty('--font-scale',st.scale); geo=null; };
  applyScale();

  function setPath(path, opts){
    opts=opts||{};
    if(BRANCHES.indexOf(path)<0) path=BRANCHES[0];
    var prev=document.body.dataset.path;
    document.body.dataset.path=path;
    st.path=path;
    sIdx=null;
    geo=null;
    var sw=document.getElementById('path-switch');
    if(sw) sw.textContent='线 · '+(BLABELS[path]||path);
    [].slice.call(document.querySelectorAll('.path-btn')).forEach(function(btn){
      btn.setAttribute('aria-pressed', btn.dataset.path===path ? 'true' : 'false');
    });
    // 若当前锚在另一条线的分叉章，跳到同章号本线
    if(opts.jumpSameNum && prev && prev!==path){
      var curEl=null;
      for(var i=0;i<allChapters.length;i++){
        if(allChapters[i].dataset.anchor===st.ch){ curEl=allChapters[i]; break; }
      }
      if(curEl && curEl.dataset.branch && curEl.dataset.branch!==path){
        var num=curEl.dataset.num;
        var alt=allChapters.filter(function(c){
          return c.dataset.num===num && c.dataset.branch===path;
        })[0];
        if(alt){
          st.ch=alt.dataset.anchor;
          if(opts.scroll!==false){
            var prevB=document.documentElement.style.scrollBehavior;
            document.documentElement.style.scrollBehavior='auto';
            alt.scrollIntoView();
            document.documentElement.style.scrollBehavior=prevB;
          }
        }
      }
    }
    if(opts.go){
      var t=document.getElementById(opts.go);
      if(t) t.scrollIntoView({behavior:calm?'auto':'smooth'});
    }
    save();
    if(typeof tick==='function') tick();
  }
  setPath(st.path||BRANCHES[0]);

  // 选线卡 / 浮动切换
  document.addEventListener('click',function(e){
    var btn=e.target.closest('.path-btn');
    if(btn && btn.dataset.path){
      setPath(btn.dataset.path, {go: btn.dataset.go, jumpSameNum:false});
      return;
    }
    if(e.target.closest('#path-switch')){
      var cur=document.body.dataset.path||BRANCHES[0];
      var next=(cur===BRANCHES[0])?(BRANCHES[1]||BRANCHES[0]):BRANCHES[0];
      setPath(next, {jumpSameNum:true, scroll:true});
    }
  });
  // 点目录里某线的章 → 自动切到该线
  toc.addEventListener('click',function(e){
    var a=e.target.closest('a[data-ch]');
    if(!a) return;
    if(a.dataset.branch) setPath(a.dataset.branch, {jumpSameNum:false});
  });

  // ---- 阅读设置面板（Aa）：字号按书存；行距 / 字体为全站偏好；背景走 theme.js
  var STEPS=[.88,1,1.14,1.3];
  var prefs={lh:'',font:''};
  try{ prefs=Object.assign(prefs,JSON.parse(localStorage.getItem('xhw-prefs')||'{}')); }catch(e){}
  var html=document.documentElement;
  var pbtn=document.getElementById('prefs-btn'), pbox=document.getElementById('prefs');
  function paintPrefs(){
    var i=STEPS.indexOf(st.scale); if(i<0) i=1;
    document.getElementById('font-val').textContent=(i+1)+' / '+STEPS.length;
    document.getElementById('font-dec').disabled=(i===0);
    document.getElementById('font-inc').disabled=(i===STEPS.length-1);
    if(prefs.lh) html.dataset.lh=prefs.lh; else delete html.dataset.lh;
    if(prefs.font) html.dataset.font=prefs.font; else delete html.dataset.font;
    [].forEach.call(pbox.querySelectorAll('[data-set-lh]'),function(b){
      b.setAttribute('aria-pressed',b.dataset.setLh===(prefs.lh||'')?'true':'false');
    });
    [].forEach.call(pbox.querySelectorAll('[data-set-font]'),function(b){
      b.setAttribute('aria-pressed',b.dataset.setFont===(prefs.font||'')?'true':'false');
    });
    paintTheme(html.dataset.theme);
  }
  function savePrefs(){ try{ localStorage.setItem('xhw-prefs',JSON.stringify(prefs)); }catch(e){} }
  function stepFont(d){
    var i=STEPS.indexOf(st.scale); if(i<0) i=1;
    i=Math.max(0,Math.min(STEPS.length-1,i+d));
    st.scale=STEPS[i]; applyScale(); save(); paintPrefs();
  }
  function setPrefs(open){
    pbox.hidden=!open;
    pbtn.setAttribute('aria-expanded',open?'true':'false');
    if(open){ var f=pbox.querySelector('button:not([disabled])'); if(f) f.focus(); }
  }
  paintPrefs();
  pbtn.onclick=function(e){ e.stopPropagation(); setPrefs(pbox.hidden); };
  pbox.addEventListener('click',function(e){
    e.stopPropagation();
    var b=e.target.closest('button'); if(!b) return;
    if(b.id==='font-dec') stepFont(-1);
    else if(b.id==='font-inc') stepFont(1);
    else if(b.dataset.setTheme!==undefined) setTheme(b.dataset.setTheme);
    else if(b.dataset.setLh!==undefined){ prefs.lh=b.dataset.setLh; savePrefs(); geo=null; paintPrefs(); }
    else if(b.dataset.setFont!==undefined){ prefs.font=b.dataset.setFont; savePrefs(); geo=null; paintPrefs(); }
  });
  document.addEventListener('click',function(){ if(!pbox.hidden) setPrefs(false); });
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape' && !pbox.hidden){ setPrefs(false); pbtn.focus(); }
  });

  // ---- 目录
  function setToc(open){
    toc.classList.toggle('open',open);
    if(wide()){
      toc.classList.toggle('hidden',!open);
      document.documentElement.dataset.toc=open?'on':'off';
      scrim.classList.remove('on');
    }else{
      scrim.classList.toggle('on',open);
      document.documentElement.dataset.toc='off';
    }
  }
  document.getElementById('menu').onclick=function(){ setToc(!toc.classList.contains('open')); };
  scrim.onclick=function(){ setToc(false); };
  toc.addEventListener('click',function(e){ if(e.target.closest('a')&&!wide()) setToc(false); });
  setToc(wide());
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'&&!wide()) setToc(false);
  });

  // ---- 入场动画
  reveal(document.querySelectorAll('.rise,.ch-head,.sec-mark'));

  // ---- 进度 / 当前章 / 封面视差
  var cur=-1, raf=0, vh=innerHeight;
  var lastY=window.scrollY, goingDown=false, pswitch=document.getElementById('path-switch');
  // 章节几何缓存：offsetTop 每帧读会强制同步布局（几百章的书会卡）。
  // 章节是纯文本、高度只在字号/视口/切线变化时变——按需重测一次即可。
  var geo=null;
  function measure(){
    var p=document.body.dataset.path||BRANCHES[0];
    var chs=visibleChapters();
    geo={path:p, tops:chs.map(function(c){return c.offsetTop;}),
         height:document.body.scrollHeight, chs:chs};
    return geo;
  }
  function geometry(){
    var p=document.body.dataset.path||BRANCHES[0];
    if(!geo || geo.path!==p) return measure();
    return geo;
  }
  function tick(){
    raf=0;
    var g=geometry(), chapters=g.chs, tops=g.tops;
    var y=window.scrollY, h=g.height-vh;
    bar.style.width=(h>0?Math.min(y/h*100,100):0)+'%';
    tools.classList.toggle('lifted',y>vh*.55);
    // 浮动按钮：往下读时让开正文，往上滑（想找东西）或到底时再出现。
    // 小于 6px 的抖动不改方向，避免惯性滚动末尾闪烁
    if(Math.abs(y-lastY)>6){ goingDown=y>lastY; lastY=y; }
    var atEnd=h>0 && y>=h-4;
    var showFloat=!goingDown || atEnd;
    top.classList.toggle('on',y>vh*1.2 && showFloat);
    if(pswitch) pswitch.classList.toggle('away',!showFloat && y>vh);

    if(cover && !calm){
      if(y<vh){
        var k=y/vh;
        cover.style.transform='translateY('+(y*.28)+'px)';
        cover.style.opacity=String(Math.max(0,1-k*1.35));
      }else if(cover.style.opacity!=='0'){
        cover.style.opacity='0';
      }
    }

    var probe=y+vh*.3, idx=-1;
    for(var i=0;i<tops.length;i++) if(tops[i]<=probe) idx=i;
    if(idx!==cur){
      cur=idx;
      var c=chapters[idx];
      var curAnchor=c?c.dataset.anchor:null;
      links.forEach(function(a){ a.classList.toggle('on',a.dataset.ch===curAnchor); });
      if(c){
        var vol=c.dataset.volume;
        var br=c.dataset.branch;
        var brTxt=br?(' · '+(BLABELS[br]||br)):'';
        now.textContent=(vol?vol+' · ':'')+'第 '+c.dataset.num+' 章'+brTxt+' · '+c.dataset.title;
      }else{ now.textContent=''; }
    }
    if(idx>=0){
      var chTop=tops[idx];
      var chBottom=(idx+1<tops.length)?tops[idx+1]:g.height;
      st.ch=chapters[idx].dataset.anchor;
      st.ratio=Math.min(1,Math.max(0,(y-chTop)/Math.max(1,chBottom-chTop)));
    }
  }
  var onScroll=function(){ if(!raf) raf=requestAnimationFrame(tick); };
  addEventListener('scroll',onScroll,{passive:true});
  addEventListener('resize',function(){
    vh=innerHeight;
    geo=null;
    setToc(wide()?!toc.classList.contains('hidden'):false);
    onScroll();
  });
  // 字体加载完成会改变文本高度，重测一次
  if(document.fonts && document.fonts.ready && document.fonts.ready.then){
    document.fonts.ready.then(function(){ geo=null; onScroll(); });
  }

  top.onclick=function(){ scrollTo({top:0,behavior:calm?'auto':'smooth'}); };

  // ---- 恢复进度
  if(st.ch!=null && !location.hash){
    // 若存档在分叉章，先对齐 path
    var restoreTarget=null;
    for(var i=0;i<allChapters.length;i++){
      if(allChapters[i].dataset.anchor===String(st.ch)){ restoreTarget=allChapters[i]; break; }
    }
    if(restoreTarget && restoreTarget.dataset.branch){
      setPath(restoreTarget.dataset.branch);
    }
    var target=null;
    var chapters0=visibleChapters();
    for(var j=0;j<chapters0.length;j++){
      if(chapters0[j].dataset.anchor===String(st.ch)){ target=chapters0[j]; break; }
    }
    if(!target && restoreTarget) target=restoreTarget;
    if(target){
      var restore=function(){
        var g=measure();
        var chapters=g.chs;
        var ix=chapters.indexOf(target);
        var chTop=(ix>=0)?g.tops[ix]:target.offsetTop;
        var chBottom=(ix>=0 && ix+1<chapters.length)
          ? g.tops[ix+1] : g.height;
        var prevBehavior=document.documentElement.style.scrollBehavior;
        document.documentElement.style.scrollBehavior='auto';
        scrollTo(0, chTop+st.ratio*(chBottom-chTop));
        document.documentElement.style.scrollBehavior=prevBehavior;
        tick();
      };
      addEventListener('load',restore);
      setTimeout(function(){ if(!window.scrollY) restore(); },60);
    }
  } else if(location.hash){
    var hashId=location.hash.slice(1);
    var hashEl=document.getElementById(hashId);
    if(hashEl && hashEl.dataset && hashEl.dataset.branch){
      setPath(hashEl.dataset.branch);
    }
  }
  tick();
  addEventListener('beforeunload',save);
  addEventListener('pagehide',save);
  document.addEventListener('visibilitychange',function(){
    if(document.visibilityState==='hidden') save();
  });
  setInterval(save,4000);

  // ---- 全文搜索（当前阅读线；懒建索引，切线自动重建）
  var sbox=document.getElementById('search-box'),
      sinput=document.getElementById('search-input'),
      sres=document.getElementById('search-results');
  function setSe(open){
    if(open){ sbox.hidden=false; sinput.focus(); sinput.select(); }
    else{ sbox.hidden=true; sres.innerHTML=''; }
  }
  function escHtml(s){
    return s.replace(/[&<>"]/g,function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
    });
  }
  function buildIndex(){
    sIdx=[];
    visibleChapters().forEach(function(c){
      var label=(c.dataset.volume?c.dataset.volume+' · ':'')
        +'第 '+c.dataset.num+' 章'
        +(c.dataset.branch&&BLABELS[c.dataset.branch]
          ?' · '+BLABELS[c.dataset.branch]:'')
        +' '+c.dataset.title;
      [].slice.call(c.querySelectorAll('p')).forEach(function(p){
        var t=(p.textContent||'').replace(/\s+/g,' ').trim();
        if(t) sIdx.push({el:p,label:label,t:t});
      });
    });
  }
  function runSearch(){
    var q=(sinput.value||'').trim();
    sres.innerHTML='';
    if(!q) return;
    if(!sIdx) buildIndex();
    var ql=q.toLowerCase(), hits=0;
    for(var i=0;i<sIdx.length && hits<30;i++){
      var item=sIdx[i], at=item.t.toLowerCase().indexOf(ql);
      if(at<0) continue;
      hits++;
      var st2=Math.max(0,at-16),
          en2=Math.min(item.t.length,at+q.length+24),
          snip=escHtml(item.t.slice(st2,at))+'<mark>'+escHtml(item.t.slice(at,at+q.length))
              +'</mark>'+escHtml(item.t.slice(at+q.length,en2));
      var d=document.createElement('div');
      d.className='sr';
      d.innerHTML='<div class="sr-ch">'+escHtml(item.label)+'</div>'
        +'<div class="sr-t">…'+snip+'…</div>';
      d.onclick=(function(it){
        return function(){
          setSe(false);
          it.el.scrollIntoView({behavior:calm?'auto':'smooth'});
          it.el.classList.remove('hit'); void it.el.offsetWidth;
          it.el.classList.add('hit');
          setTimeout(function(){ it.el.classList.remove('hit'); },2200);
        };
      })(item);
      sres.appendChild(d);
    }
    if(!hits) sres.innerHTML='<div class="sr none">当前线没有找到「'+escHtml(q)+'」</div>';
  }
  document.getElementById('search').onclick=function(){ sbox.hidden?setSe(true):setSe(false); };
  sinput.addEventListener('input',runSearch);
  sinput.addEventListener('keydown',function(e){
    if(e.key==='Escape'){ setSe(false); e.stopPropagation(); }
  });
  document.addEventListener('keydown',function(e){
    if((e.ctrlKey||e.metaKey)&&(e.key==='k'||e.key==='K')){
      e.preventDefault();
      sbox.hidden?setSe(true):setSe(false);
    }
  });

  // ---- 左右方向键翻章（只在当前线可见章之间）
  document.addEventListener('keydown',function(e){
    if(e.target.closest('input,textarea,button')) return;
    var d = (e.key==='ArrowRight'||e.key==='j') ? 1 : ((e.key==='ArrowLeft'||e.key==='k') ? -1 : 0);
    if(!d) return;
    var chapters=visibleChapters();
    var n=Math.max(0,Math.min(chapters.length-1,(cur<0?0:cur)+d));
    chapters[n].scrollIntoView({behavior:calm?'auto':'smooth'});
  });
})();
